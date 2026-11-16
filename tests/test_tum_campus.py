"""Offline tests for the tum-campus plugin script.

Run from the repository root:

    python3 -m unittest discover -s tests -v

No network: `http_get` is replaced with fixture files shaped like TUMonline's
`wbservicesbasic.*` XML (same fields the TUM Campus App's models read).
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import stat
import sys
import tempfile
import unittest
import urllib.parse
from datetime import date
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "plugins" / "tum-campus" / "skills" / "tum-campus" / "scripts" / "tum_campus.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures"

spec = importlib.util.spec_from_file_location("tum_campus", SCRIPT)
tum = importlib.util.module_from_spec(spec)
sys.modules["tum_campus"] = tum
spec.loader.exec_module(tum)

FAKE_TOKEN = "ABCDEF0123456789ABCDEF0123456789"


def error_xml(message: str) -> bytes:
    return f'<?xml version="1.0" encoding="utf-8"?><error><message>{message}</message></error>'.encode()


class FakeTumOnline:
    """Stands in for `http_get`; records the URLs it was asked for."""

    def __init__(self, overrides: dict[str, bytes] | None = None):
        self.overrides = overrides or {}
        self.urls: list[str] = []

    def __call__(self, url: str) -> bytes:
        self.urls.append(url)
        slug = urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]
        name = slug.split(".", 1)[-1]
        if name in self.overrides:
            return self.overrides[name]
        return (FIXTURES / f"{name}.xml").read_bytes()

    def query(self, index: int = -1) -> dict[str, str]:
        return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.urls[index]).query))


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = {k: v for k, v in os.environ.items() if k != tum.TOKEN_ENV}
        env[tum.CONFIG_DIR_ENV] = self.tmp.name
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cli(self, *argv, server: FakeTumOnline | None = None, token: bool = True):
        server = server or FakeTumOnline()
        out = io.StringIO()
        env = {tum.TOKEN_ENV: FAKE_TOKEN} if token else {}
        with mock.patch.object(tum, "http_get", server), mock.patch.dict(os.environ, env), \
                contextlib.redirect_stdout(out):
            code = tum.main(list(argv))
        return code, out.getvalue(), server


class BriefTests(CliTestCase):
    def brief(self, day="2026-11-16", **kwargs):
        code, out, server = self.run_cli("brief", "--date", day, **kwargs)
        return code, json.loads(out), server

    def test_today_lists_active_events_in_order(self):
        code, brief, server = self.brief()
        self.assertEqual(code, 0)
        today = brief["today"]
        self.assertEqual([e["start_time"] for e in today["events"]], ["08:15", "09:30"])
        self.assertEqual(today["first_start"], "08:15")
        self.assertEqual(today["last_end"], "11:00")
        self.assertEqual(server.query()["pToken"], FAKE_TOKEN)

    def test_overlapping_events_are_not_double_counted(self):
        _, brief, _ = self.brief()
        # 08:15-09:45 and 09:30-11:00 overlap by 15 minutes -> 165, not 180.
        self.assertEqual(brief["today"]["scheduled_minutes"], 165)

    def test_cancelled_event_is_reported_separately(self):
        _, brief, _ = self.brief()
        cancelled = brief["today"]["cancelled"]
        self.assertEqual(len(cancelled), 1)
        self.assertEqual(cancelled[0]["course"], "Digitaltechnik")
        self.assertTrue(cancelled[0]["cancelled"])

    def test_title_is_split_into_course_kind_group_module(self):
        _, brief, _ = self.brief()
        first, second = brief["today"]["events"]
        self.assertEqual(first["course"], "Analysis 1")
        self.assertEqual(first["kind"], "VO")
        self.assertEqual(first["kind_name"], "Vorlesung")
        self.assertEqual(first["module"], "MA9411")
        self.assertEqual(first["group"], "Standardgruppe")
        self.assertEqual(second["kind_name"], "Übung")
        self.assertEqual(second["group"], "Gruppe 3")
        self.assertEqual(first["title"], "Analysis 1 (MA9411) VO, Standardgruppe")

    def test_room_gets_a_navigatum_link(self):
        _, brief, _ = self.brief()
        first = brief["today"]["events"][0]
        self.assertEqual(first["room_id"], "0101.01.190")
        self.assertEqual(first["room_map_url"], "https://nav.tum.de/room/0101.01.190")
        self.assertEqual(first["start"], "2026-11-16T08:15+01:00")

    def test_links_outside_tumonline_are_dropped(self):
        _, brief, _ = self.brief()
        self.assertEqual(brief["today"]["cancelled"][0]["url"], "")
        self.assertTrue(brief["today"]["events"][0]["url"].startswith("https://campus.tum.de/"))

    def test_tomorrow_and_exam_window(self):
        _, brief, _ = self.brief()
        self.assertEqual([e["course"] for e in brief["tomorrow"]["events"]], ["Lineare Algebra"])
        self.assertEqual(brief["tomorrow"]["events"][0]["kind"], "TT")
        exams = brief["upcoming_exams"]
        self.assertEqual(len(exams), 1)  # the February exam is outside 21 days
        self.assertEqual(exams[0]["days_until"], 11)
        self.assertTrue(exams[0]["is_exam"])

    def test_exam_window_can_be_widened(self):
        code, out, _ = self.run_cli("brief", "--date", "2026-11-16", "--exam-days", "120")
        self.assertEqual(len(json.loads(out)["upcoming_exams"]), 2)

    def test_free_day_has_no_events(self):
        _, brief, _ = self.brief("2026-11-21")
        self.assertEqual(brief["today"]["events"], [])
        self.assertEqual(brief["today"]["scheduled_minutes"], 0)
        self.assertNotIn("first_start", brief["today"])

    def test_empty_calendar(self):
        server = FakeTumOnline({"kalender": b'<?xml version="1.0"?><events></events>'})
        code, brief, _ = self.brief(server=server)
        self.assertEqual(code, 0)
        self.assertEqual(brief["today"]["events"], [])
        self.assertEqual(brief["upcoming_exams"], [])

    def test_markup_in_titles_stays_plain_text(self):
        _, brief, _ = self.brief("2026-11-18")
        title = brief["today"]["events"][0]["title"]
        self.assertIn("<script>", title)  # passed through as data, never interpreted


class ErrorTests(CliTestCase):
    def check(self, message, kind, code):
        server = FakeTumOnline({"kalender": error_xml(message)})
        exit_code, out, _ = self.run_cli("brief", server=server)
        payload = json.loads(out)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], kind)
        self.assertEqual(exit_code, code)
        self.assertNotIn(FAKE_TOKEN, out)
        return payload

    def test_invalid_token(self):
        self.check("Token ist ungültig!", "token_invalid", 2)

    def test_unconfirmed_token(self):
        self.check("Token ist nicht bestätigt!", "token_not_confirmed", 2)

    def test_missing_permission(self):
        self.check("Keine Rechte für Funktion kalender", "no_permission", 2)

    def test_function_switched_off(self):
        self.check(
            "Kalenderabruf temporär nicht möglich! Bitte TUMonline nutzen! Techniker ist informiert!",
            "service_unavailable",
            1,
        )

    def test_unknown_message_is_passed_on_sanitised(self):
        payload = self.check("Irgendwas &lt;b&gt;Neues&lt;/b&gt;", "tumonline_error", 1)
        self.assertNotIn("<", payload["message"])

    def test_no_token_configured(self):
        code, out, server = self.run_cli("brief", token=False)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(out)["error"], "no_token")
        self.assertEqual(server.urls, [])

    def test_non_xml_answer(self):
        server = FakeTumOnline({"kalender": b"<html><body>Wartung"})
        code, out, _ = self.run_cli("brief", server=server)
        self.assertEqual(json.loads(out)["error"], "bad_response")

    def test_network_errors_never_leak_the_token(self):
        text = tum.redact(f"GET /x?pUsername=a&pToken={FAKE_TOKEN}&x=1 failed")
        self.assertNotIn(FAKE_TOKEN, text)
        self.assertIn("pToken=<redacted>", text)


class LoginTests(CliTestCase):
    TOKEN_XML = f'<?xml version="1.0" encoding="utf-8"?><token>{FAKE_TOKEN}</token>'.encode()

    def test_login_stores_token_privately_and_never_prints_it(self):
        server = FakeTumOnline({"requestToken": self.TOKEN_XML})
        code, out, server = self.run_cli("login", "GO42TUM", server=server, token=False)
        self.assertEqual(code, 0)
        self.assertNotIn(FAKE_TOKEN, out)
        query = server.query()
        self.assertEqual(query["pUsername"], "go42tum")
        self.assertEqual(query["pTokenName"], tum.DEFAULT_TOKEN_NAME)
        self.assertNotIn("pToken", query)
        path = Path(self.tmp.name) / "token.json"
        self.assertEqual(json.loads(path.read_text())["token"], FAKE_TOKEN)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_stored_token_is_used_for_later_calls(self):
        server = FakeTumOnline({"requestToken": self.TOKEN_XML})
        self.run_cli("login", "go42tum", server=server, token=False)
        code, out, server = self.run_cli("brief", "--date", "2026-11-16", token=False)
        self.assertEqual(code, 0)
        self.assertEqual(server.query()["pToken"], FAKE_TOKEN)

    def test_login_refuses_to_overwrite_without_force(self):
        server = FakeTumOnline({"requestToken": self.TOKEN_XML})
        self.run_cli("login", "go42tum", server=server, token=False)
        code, out, again = self.run_cli("login", "go42tum", token=False,
                                        server=FakeTumOnline({"requestToken": self.TOKEN_XML}))
        self.assertEqual(json.loads(out)["error"], "already_logged_in")
        self.assertEqual(again.urls, [])

    def test_export_writes_bare_token_file(self):
        target = Path(self.tmp.name) / "out" / "token.txt"
        server = FakeTumOnline({"requestToken": self.TOKEN_XML})
        code, out, _ = self.run_cli("login", "go42tum", "--export", str(target),
                                    server=server, token=False)
        self.assertEqual(target.read_text().strip(), FAKE_TOKEN)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        self.assertNotIn(FAKE_TOKEN, out)

    def test_rejects_things_that_are_not_a_tum_id(self):
        for bad in ("someone@tum.de", "03712345", "zc106", "go42tumx"):
            code, out, server = self.run_cli("login", bad, token=False)
            self.assertEqual(json.loads(out)["error"], "bad_argument", bad)
            self.assertEqual(server.urls, [])

    def test_unknown_user(self):
        server = FakeTumOnline({"requestToken": error_xml("Kein Benutzer angegeben (requestToken)")})
        code, out, _ = self.run_cli("login", "go42tum", server=server, token=False)
        self.assertEqual(json.loads(out)["error"], "user_not_found")

    def test_status_confirmed_and_unconfirmed(self):
        yes = FakeTumOnline({"isTokenConfirmed": b"<confirmed>true</confirmed>"})
        code, out, _ = self.run_cli("status", server=yes)
        self.assertEqual((code, json.loads(out)["confirmed"]), (0, True))
        no = FakeTumOnline({"isTokenConfirmed": b"<confirmed>false</confirmed>"})
        code, out, _ = self.run_cli("status", server=no)
        self.assertEqual((code, json.loads(out)["confirmed"]), (2, False))
        self.assertNotIn(FAKE_TOKEN, out)

    def test_logout_removes_the_file(self):
        self.run_cli("login", "go42tum", token=False,
                     server=FakeTumOnline({"requestToken": self.TOKEN_XML}))
        code, out, _ = self.run_cli("logout", token=False)
        self.assertTrue(json.loads(out)["removed_token_file"])
        self.assertFalse((Path(self.tmp.name) / "token.json").exists())


class ListingTests(CliTestCase):
    def test_calendar_range_json(self):
        code, out, _ = self.run_cli("calendar", "--from", "2026-11-16", "--days", "2", "--json")
        events = json.loads(out)["events"]
        self.assertEqual(len(events), 4)  # three on the 16th (one cancelled), one on the 17th

    def test_calendar_text(self):
        code, out, _ = self.run_cli("calendar", "--from", "2026-11-16", "--to", "2026-11-27")
        self.assertIn("Monday 2026-11-16", out)
        self.assertIn("[CANCELLED]", out)
        self.assertIn("[EXAM]", out)

    def test_calendar_rejects_reversed_range(self):
        code, out, _ = self.run_cli("calendar", "--from", "2026-11-17", "--to", "2026-11-16")
        self.assertEqual(json.loads(out)["error"], "bad_argument")

    def test_lectures_default_to_current_semester(self):
        code, out, _ = self.run_cli("lectures", "--json")
        data = json.loads(out)
        self.assertEqual(data["current_semester"], "26W")
        self.assertEqual({l["title"] for l in data["lectures"]}, {"Analysis 1", "Schaltungstheorie"})
        code, out, _ = self.run_cli("lectures", "--all", "--json")
        self.assertEqual(len(json.loads(out)["lectures"]), 3)

    def test_semester_ordering(self):
        ids = ["26W", "25W", "26S", "27S"]
        self.assertEqual(sorted(ids, key=tum.semester_sort_key), ["25W", "26S", "26W", "27S"])

    def test_grades_newest_first(self):
        code, out, _ = self.run_cli("grades", "--json")
        grades = json.loads(out)["grades"]
        self.assertEqual([g["date"] for g in grades], ["2026-09-28", "2026-07-20"])
        self.assertEqual(grades[1]["grade"], "1,7")
        code, out, _ = self.run_cli("grades", "--limit", "1", "--json")
        self.assertEqual(len(json.loads(out)["grades"]), 1)


class HelperTests(unittest.TestCase):
    def test_split_title_leaves_unknown_shapes_alone(self):
        parts = tum.split_title("Sprechstunde Studienberatung")
        self.assertEqual(parts["course"], "Sprechstunde Studienberatung")
        self.assertEqual(parts["kind"], "")

    def test_multi_day_event_shows_on_each_day(self):
        import xml.etree.ElementTree as ET
        event = tum.parse_event(ET.fromstring(
            "<event><nr>1</nr><status>FT</status><title>Exkursion</title>"
            "<dtstart>2026-11-16 09:00:00</dtstart><dtend>2026-11-17 17:00:00</dtend></event>"))
        self.assertEqual(len(tum.events_on([event], date(2026, 11, 17))), 1)
        self.assertEqual(tum.merged_minutes([event], date(2026, 11, 17)), 17 * 60)


if __name__ == "__main__":
    unittest.main()
