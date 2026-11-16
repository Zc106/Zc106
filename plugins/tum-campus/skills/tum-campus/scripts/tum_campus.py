#!/usr/bin/env python3
"""TUMonline client for the tum-campus plugin.

Talks to the same `wbservicesbasic.*` web services the official TUM Campus App
(https://github.com/TUM-Dev/campus_flutter) uses: request a personal token once,
confirm it in TUMonline, then read your calendar, courses and grades with it.

Standard library only, read-only: it never creates, changes or deletes anything
in TUMonline apart from requesting the token itself during `login`.

The token travels as a URL query parameter (`pToken`), so this script never
prints, logs or echoes request URLs.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_URL = "https://campus.tum.de/tumonline/"
TOKEN_ENV = "TUM_ONLINE_TOKEN"
CONFIG_DIR_ENV = "TUM_CAMPUS_CONFIG_DIR"
DEFAULT_TOKEN_NAME = "Claude Morning Brief"
USER_AGENT = "tum-campus-plugin/1.0"
TIMEZONE = ZoneInfo("Europe/Berlin")  # TUMonline reports Munich wall-clock time
TIMEOUT_SECONDS = 25
MAX_RESPONSE_BYTES = 32 * 1024 * 1024

TUM_ID_PATTERN = re.compile(r"^[a-z]{2}[0-9]{2}[a-z]{3}$")
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9]{8,128}$")

# Course-type abbreviations TUMonline appends to calendar titles.
COURSE_KINDS = {
    "VO": "Vorlesung",
    "VI": "Vorlesung mit integrierten Übungen",
    "VU": "Vorlesung mit Übung",
    "UE": "Übung",
    "TT": "Tutorium",
    "PR": "Praktikum",
    "FP": "Forschungspraktikum",
    "SE": "Seminar",
    "PS": "Proseminar",
    "HS": "Hauptseminar",
    "KO": "Kolloquium",
    "RE": "Repetitorium",
    "EX": "Exkursion",
    "WS": "Workshop",
}
_KIND_SUFFIX = re.compile(
    r"\s+(?P<kind>" + "|".join(COURSE_KINDS) + r")(?:\s*,\s*(?P<group>.+))?\s*$"
)
_MODULE_CODE = re.compile(r"\s*[(\[](?P<code>[A-Z]{2,}[0-9]{2,}[A-Z0-9]*)[)\]]")
_ROOM_ID = re.compile(r"\((?P<id>[0-9]{4}\.[0-9A-Za-z]{1,4}\.[0-9A-Za-z]{1,8})\)\s*$")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NEEDS_SETUP = 2  # no token, token not confirmed/invalid, missing permission


class TumOnlineError(Exception):
    """A failure the caller can act on. `kind` is machine-readable."""

    def __init__(self, kind: str, message: str, hint: str = ""):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.hint = hint

    @property
    def needs_setup(self) -> bool:
        return self.kind in {
            "no_token",
            "token_not_confirmed",
            "token_invalid",
            "no_permission",
            "token_limit",
        }

    def as_dict(self) -> dict:
        out = {"ok": False, "error": self.kind, "message": self.message}
        if self.hint:
            out["hint"] = self.hint
        return out


# --------------------------------------------------------------------------- #
# Token storage
# --------------------------------------------------------------------------- #


def config_dir() -> Path:
    override = os.environ.get(CONFIG_DIR_ENV)
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "tum-campus"


def token_file() -> Path:
    return config_dir() / "token.json"


def clean_token(raw: str) -> str:
    token = (raw or "").strip()
    if not TOKEN_PATTERN.match(token):
        raise TumOnlineError(
            "token_invalid",
            "The stored TUMonline token does not look like a token.",
            f"Set {TOKEN_ENV} to the bare token value, or run `login` again.",
        )
    return token


def load_token() -> tuple[str, str]:
    """Return (token, source). Environment wins over the token file."""
    env_value = os.environ.get(TOKEN_ENV, "").strip()
    if env_value:
        return clean_token(env_value), f"environment variable {TOKEN_ENV}"
    path = token_file()
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise TumOnlineError(
                "token_invalid",
                f"Could not read the token file at {path}.",
                "Run `login <tum-id> --force` to request a new token.",
            ) from exc
        return clean_token(str(data.get("token", ""))), f"token file {path}"
    raise TumOnlineError(
        "no_token",
        "No TUMonline token is configured.",
        f"Run `login <tum-id>` once, or set the {TOKEN_ENV} environment variable.",
    )


def save_token(token: str, tum_id: str, name: str) -> Path:
    path = token_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, stat.S_IRWXU)
    except OSError:
        pass
    payload = {
        "token": token,
        "tum_id": tum_id,
        "token_name": name,
        "requested_at": datetime.now(TIMEZONE).isoformat(timespec="seconds"),
    }
    write_private(path, json.dumps(payload, indent=2) + "\n")
    return path


def write_private(path: Path, text: str) -> None:
    """Write a file only the current user can read (0600)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# HTTP + XML
# --------------------------------------------------------------------------- #


def http_get(url: str) -> bytes:
    """GET a TUMonline URL. Never mentions the URL in errors (it holds the token)."""
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "text/xml, application/xml"}
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise TumOnlineError(
            "http_error",
            f"TUMonline answered HTTP {exc.code}.",
            "TUMonline may be down for maintenance; try again later.",
        ) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        text = redact(str(reason))
        hint = "Check the network connection."
        if "403" in text or "Tunnel" in text:
            hint = (
                "The network policy of this environment blocks campus.tum.de; "
                "allow that host in the environment's network settings."
            )
        raise TumOnlineError(
            "network", f"Could not reach campus.tum.de: {text}", hint
        ) from None
    if len(body) > MAX_RESPONSE_BYTES:
        raise TumOnlineError("too_large", "The TUMonline response was unexpectedly large.")
    return body


def redact(text: str) -> str:
    return re.sub(r"(pToken=)[^&\s'\"]+", r"\1<redacted>", text)


def classify_error(message: str) -> TumOnlineError:
    """Map TUMonline's German error strings (same ones the campus app handles)."""
    text = " ".join(message.split())
    lowered = text.lower()
    if "keine rechte" in lowered or "nicht erlaubt" in lowered:
        return TumOnlineError(
            "no_permission",
            "The token is not allowed to use this function.",
            "In TUMonline open Token-Management and tick the permission for this "
            "function (calendar, courses, grades) on the token.",
        )
    if "nicht bestätigt" in lowered:
        return TumOnlineError(
            "token_not_confirmed",
            "The token has not been confirmed yet.",
            "Log in to TUMonline, open Token-Management, activate the token and "
            "enable its permissions.",
        )
    if "ungültig" in lowered and "token" in lowered:
        return TumOnlineError(
            "token_invalid",
            "TUMonline rejected the token as invalid.",
            "Request a new one with `login <tum-id> --force`.",
        )
    if "limit" in lowered and "token" in lowered:
        return TumOnlineError(
            "token_limit",
            "This account already has the maximum number of tokens (10).",
            "Delete an unused token in TUMonline's Token-Management, then retry.",
        )
    if "temporär" in lowered or "techniker" in lowered:
        return TumOnlineError(
            "service_unavailable",
            "TUMonline has switched this function off for the moment.",
            "This is on TUMonline's side; the other commands may still work. Try again later.",
        )
    if "rate" in lowered or "zu viele" in lowered:
        return TumOnlineError(
            "rate_limited", "TUMonline is rate-limiting requests.", "Try again later."
        )
    if "benutzer" in lowered and ("kein" in lowered or "nicht" in lowered):
        return TumOnlineError(
            "user_not_found",
            "TUMonline does not know this TUM ID.",
            "Check the ID: two letters, two digits, three letters (e.g. go42tum).",
        )
    safe = re.sub(r"[<>\x00-\x1f]", " ", text)[:300]
    return TumOnlineError("tumonline_error", f"TUMonline reported: {safe}")


def call(slug: str, params: dict[str, str] | None = None, *, token: str | None = None) -> ET.Element:
    """Call one `wbservicesbasic.*` function and return the parsed XML root."""
    query = dict(params or {})
    if token is not None:
        query["pToken"] = token
    url = BASE_URL + slug + "?" + urllib.parse.urlencode(query)
    body = http_get(url)
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        raise TumOnlineError(
            "bad_response",
            "TUMonline did not answer with XML.",
            "TUMonline may be showing a maintenance page; try again later.",
        ) from None
    if root.tag == "error":
        raise classify_error(root.findtext("message") or "unknown error")
    return root


def text_of(element: ET.Element, tag: str) -> str:
    return " ".join((element.findtext(tag) or "").split())


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #


def parse_datetime(raw: str) -> datetime | None:
    raw = (raw or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=TIMEZONE)
        except ValueError:
            continue
    return None


def split_title(title: str) -> dict:
    """Split 'Analysis 1 (MA9411) VO, Standardgruppe' into its parts (best effort)."""
    out = {"course": title, "kind": "", "kind_name": "", "group": "", "module": ""}
    rest = title
    match = _KIND_SUFFIX.search(rest)
    if match:
        out["kind"] = match.group("kind")
        out["kind_name"] = COURSE_KINDS[match.group("kind")]
        out["group"] = (match.group("group") or "").strip()
        rest = rest[: match.start()]
    module = _MODULE_CODE.search(rest)
    if module:
        out["module"] = module.group("code")
        rest = (rest[: module.start()] + rest[module.end():])
    rest = " ".join(rest.split()).strip(" ,;-")
    if rest:
        out["course"] = rest
    return out


def safe_campus_url(raw: str) -> str:
    """Keep only https links that really point at TUMonline."""
    raw = (raw or "").strip()
    try:
        parsed = urllib.parse.urlsplit(raw)
    except ValueError:
        return ""
    if parsed.scheme == "https" and parsed.hostname == "campus.tum.de":
        return raw
    return ""


def split_description(description: str) -> tuple[str, str]:
    """Split 'fix; Abhaltung; Start um 08:00' into (state, note).

    TUMonline writes '<state>; <kind of date>; <free remark>'. The state is
    'fix' for a session that takes place and e.g. 'verschoben' (moved) or
    'abgelehnt' (declined) on entries it has replaced or dropped.
    """
    parts = [part.strip() for part in description.split(";") if part.strip()]
    if len(parts) >= 2 and re.fullmatch(r"[a-zäöüß]+", parts[0]):
        return parts[0], "; ".join(parts[2:])
    return "", description


def room_of(location: str) -> dict:
    room = {"location": location}
    match = _ROOM_ID.search(location)
    if match:
        room["room_id"] = match.group("id")
        room["room_map_url"] = "https://nav.tum.de/room/" + match.group("id")
    return room


def parse_event(element: ET.Element) -> dict | None:
    start = parse_datetime(element.findtext("dtstart") or "")
    end = parse_datetime(element.findtext("dtend") or "")
    if start is None:
        return None
    if end is None or end < start:
        end = start
    title = text_of(element, "title")
    description = text_of(element, "description")
    status = text_of(element, "status")
    state, note = split_description(description)
    locations = [
        " ".join((loc.text or "").split())
        for loc in element.findall("location")
        if (loc.text or "").strip()
    ]
    event = {
        "id": text_of(element, "nr"),
        "title": title,
        **split_title(title),
        "start": start.isoformat(timespec="minutes"),
        "end": end.isoformat(timespec="minutes"),
        "date": start.date().isoformat(),
        "start_time": start.strftime("%H:%M"),
        "end_time": end.strftime("%H:%M"),
        "minutes": int((end - start).total_seconds() // 60),
        "locations": locations,
        "status": status,
        "cancelled": status.upper() == "CANCEL",
        "is_exam": "prüfungstermin" in description.lower(),
        "state": state,
        "note": note,
        "description": description,
        "url": safe_campus_url(element.findtext("url") or ""),
    }
    if locations:
        room = room_of(locations[0])
        room.pop("location")
        event.update(room)
    event["_start"] = start
    event["_end"] = end
    return event


def public(event: dict) -> dict:
    return {key: value for key, value in event.items() if not key.startswith("_")}


def fetch_events(token: str) -> list[dict]:
    root = call("wbservicesbasic.kalender", token=token)
    events = [parse_event(element) for element in root.iter("event")]
    return sorted((e for e in events if e), key=lambda e: (e["_start"], e["_end"], e["title"]))


def events_on(events: list[dict], day: date) -> list[dict]:
    return [e for e in events if e["_start"].date() <= day <= e["_end"].date()]


def semester_sort_key(semester_id: str) -> tuple:
    """'25W' < '26S' < '26W' (winter term 26W starts after summer term 26S)."""
    match = re.match(r"^(\d{2})([SW])$", semester_id.strip().upper())
    if not match:
        return (-1, 0, semester_id)
    return (int(match.group(1)), 0 if match.group(2) == "S" else 1, "")


def fetch_lectures(token: str) -> list[dict]:
    root = call("wbservicesbasic.veranstaltungenEigene", token=token)
    lectures = []
    for row in root.iter("row"):
        lectures.append(
            {
                "title": text_of(row, "stp_sp_titel"),
                "type": text_of(row, "stp_lv_art_name"),
                "type_short": text_of(row, "stp_lv_art_kurz"),
                "sws": text_of(row, "dauer_info"),
                "semester": text_of(row, "semester_name"),
                "semester_id": text_of(row, "semester_id"),
                "organisation": text_of(row, "org_name_betreut"),
                "lecturers": text_of(row, "vortragende_mitwirkende"),
                "lv_number": text_of(row, "stp_lv_nr"),
                "id": text_of(row, "stp_sp_nr"),
            }
        )
    lectures.sort(key=lambda l: (semester_sort_key(l["semester_id"]), l["title"]), reverse=False)
    return lectures


def current_semester(lectures: list[dict]) -> str:
    ids = {l["semester_id"] for l in lectures if l["semester_id"]}
    return max(ids, key=semester_sort_key) if ids else ""


def fetch_grades(token: str) -> list[dict]:
    root = call("wbservicesbasic.noten", token=token)
    grades = []
    for row in root.iter("row"):
        when = parse_datetime(row.findtext("datum") or "")
        grades.append(
            {
                "date": when.date().isoformat() if when else "",
                "title": text_of(row, "lv_titel"),
                "grade": text_of(row, "uninotenamekurz"),
                "semester": text_of(row, "lv_semester"),
                "lv_number": text_of(row, "lv_nummer"),
                "examiner": text_of(row, "pruefer_nachname"),
                "exam_type": text_of(row, "exam_typ_name"),
                "mode": text_of(row, "modus"),
                "study": text_of(row, "studienbezeichnung"),
            }
        )
    grades.sort(key=lambda g: g["date"], reverse=True)
    return grades


# --------------------------------------------------------------------------- #
# Brief
# --------------------------------------------------------------------------- #


def merged_minutes(events: list[dict], day: date) -> int:
    """Minutes of the day covered by events, overlaps counted once."""
    day_start = datetime.combine(day, datetime.min.time(), tzinfo=TIMEZONE)
    day_end = day_start + timedelta(days=1)
    spans = sorted(
        (max(e["_start"], day_start), min(e["_end"], day_end)) for e in events
    )
    total = timedelta()
    cursor = None
    for start, end in spans:
        if end <= start:
            continue
        if cursor is None or start > cursor:
            total += end - start
            cursor = end
        elif end > cursor:
            total += end - cursor
            cursor = end
    return int(total.total_seconds() // 60)


def merge_parallel(events: list[dict]) -> list[dict]:
    """Fold entries for one session held in several rooms into one event.

    TUMonline lists a lecture once per room, e.g. the lecture hall plus a
    second hall with a video feed. The extra rooms end up in `also_in`.
    """
    groups: dict[tuple, list[dict]] = {}
    for event in events:
        groups.setdefault((event["title"], event["_start"], event["_end"]), []).append(event)
    merged = []
    for group in groups.values():
        # The room people are actually taught in comes first.
        group.sort(key=lambda e: "übertragung" in e["note"].lower())
        main = dict(group[0])
        extras = []
        for other in group[1:]:
            for location in other["locations"]:
                if location in main["locations"]:
                    continue
                extra = room_of(location)
                if other["note"]:
                    extra["note"] = other["note"]
                extras.append(extra)
        if extras:
            main["also_in"] = extras
        merged.append(main)
    merged.sort(key=lambda e: (e["_start"], e["_end"], e["title"]))
    return attach_overflow_rooms(merged)


def attach_overflow_rooms(events: list[dict]) -> list[dict]:
    """A video-feed room booked across several sessions joins the first of them."""
    kept = []
    for event in events:
        if "übertragung" in event["note"].lower():
            hosts = [
                other for other in events
                if other is not event
                and other["title"] == event["title"]
                and "übertragung" not in other["note"].lower()
                and other["_start"] < event["_end"] and event["_start"] < other["_end"]
            ]
            if hosts:
                host = hosts[0]
                known = set(host["locations"]) | {r["location"] for r in host.get("also_in", [])}
                for location in event["locations"]:
                    if location not in known:
                        host.setdefault("also_in", []).append({**room_of(location), "note": event["note"]})
                continue
        kept.append(event)
    return kept


def apply_replacements(active: list[dict], replaced: list[dict]) -> list[dict]:
    """Work out what each CANCEL entry means; return the ones truly called off.

    TUMonline keeps the old entry with status CANCEL when a session moves to
    another room or time, next to the new entry. Only a CANCEL entry with no
    replacement on the same day is a session that does not take place.
    """
    called_off = []
    for old in replaced:
        same_day = [e for e in active if e["title"] == old["title"]]
        same_slot = [e for e in same_day if e["_start"] == old["_start"]]
        if same_slot:
            new = same_slot[0]
            known = set(new["locations"]) | {r["location"] for r in new.get("also_in", [])}
            previous = [loc for loc in old["locations"] if loc not in known]
            if previous and "change" not in new:
                new["change"] = {"type": "room_changed", "previous_locations": previous}
        elif same_day:
            new = min(same_day, key=lambda e: abs((e["_start"] - old["_start"]).total_seconds()))
            if "change" not in new:
                new["change"] = {
                    "type": "time_changed",
                    "previous_start_time": old["start_time"],
                    "previous_end_time": old["end_time"],
                }
        else:
            gone = dict(old)
            gone["reason"] = "moved_to_another_day" if old["state"] == "verschoben" else "cancelled"
            called_off.append(gone)
    return called_off


def day_summary(events: list[dict], day: date) -> dict:
    on_day = events_on(events, day)
    active = merge_parallel([e for e in on_day if not e["cancelled"]])
    called_off = apply_replacements(active, merge_parallel([e for e in on_day if e["cancelled"]]))
    summary = {
        "date": day.isoformat(),
        "weekday": day.strftime("%A"),
        "events": [public(e) for e in active],
        "cancelled": [public(e) for e in called_off],
        "scheduled_minutes": merged_minutes(active, day),
    }
    if active:
        summary["first_start"] = min(e["_start"] for e in active).strftime("%H:%M")
        summary["last_end"] = max(e["_end"] for e in active).strftime("%H:%M")
    return summary


def build_brief(events: list[dict], today: date, exam_days: int) -> dict:
    horizon = today + timedelta(days=exam_days)
    exams = []
    for event in events:
        day = event["_start"].date()
        if event["is_exam"] and not event["cancelled"] and today <= day <= horizon:
            exams.append({**public(event), "days_until": (day - today).days})
    return {
        "ok": True,
        "source": "TUMonline (campus.tum.de)",
        "timezone": "Europe/Berlin",
        "generated_at": datetime.now(TIMEZONE).isoformat(timespec="seconds"),
        "today": day_summary(events, today),
        "tomorrow": day_summary(events, today + timedelta(days=1)),
        "upcoming_exams": exams,
        "exam_window_days": exam_days,
    }


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def emit(data) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def parse_day(raw: str | None) -> date:
    if not raw or raw == "today":
        return datetime.now(TIMEZONE).date()
    if raw == "tomorrow":
        return datetime.now(TIMEZONE).date() + timedelta(days=1)
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise TumOnlineError("bad_argument", f"Not a date (YYYY-MM-DD): {raw}") from None


def format_event_line(event: dict) -> str:
    line = f"{event['start_time']}-{event['end_time']}  {event['title']}"
    if event["locations"]:
        line += f" @ {event['locations'][0]}"
    for extra in event.get("also_in", []):
        line += f" + {extra['location']}"
    if event["is_exam"]:
        line += " [EXAM]"
    change = event.get("change", {})
    if change.get("type") == "room_changed":
        line += f" [ROOM CHANGED, was {'; '.join(change['previous_locations'])}]"
    elif change.get("type") == "time_changed":
        line += f" [TIME CHANGED, was {change['previous_start_time']}-{change['previous_end_time']}]"
    if event.get("reason") == "moved_to_another_day":
        line += " [NOT TODAY: MOVED]"
    elif event.get("reason"):
        line += " [CANCELLED]"
    if event["note"]:
        line += f"  ({event['note']})"
    return line


def cmd_login(args) -> int:
    tum_id = args.tum_id.strip().lower()
    if not TUM_ID_PATTERN.match(tum_id):
        raise TumOnlineError(
            "bad_argument",
            "That is not a TUM ID.",
            "A TUM ID has two letters, two digits and three letters, e.g. go42tum "
            "(not your e-mail address or matriculation number).",
        )
    path = token_file()
    if path.exists() and not args.force:
        raise TumOnlineError(
            "already_logged_in",
            f"A token is already stored at {path}.",
            "Run `status` to check it, or pass --force to request a new one "
            "(TUMonline allows at most 10 tokens per account).",
        )
    root = call(
        "wbservicesbasic.requestToken",
        {"pUsername": tum_id, "pTokenName": args.name},
    )
    token = (root.text or "").strip() if root.tag == "token" else (root.findtext("token") or "").strip()
    if not TOKEN_PATTERN.match(token):
        raise TumOnlineError("bad_response", "TUMonline did not return a token.")
    saved = save_token(token, tum_id, args.name)
    result = {
        "ok": True,
        "token_name": args.name,
        "token_file": str(saved),
        "confirmed": False,
        "next_steps": [
            "Log in at https://campus.tum.de",
            "Open 'Token-Management' (search for it on the TUMonline start page)",
            f"Activate the token named '{args.name}' and tick its permissions: "
            "calendar (Kalender) at least; courses and grades if you want those too",
            "Run `status` to check that the token is confirmed",
        ],
    }
    if args.export:
        export_path = Path(args.export).expanduser()
        export_path.parent.mkdir(parents=True, exist_ok=True)
        write_private(export_path, token + "\n")
        result["token_exported_to"] = str(export_path)
    emit(result)
    return EXIT_OK


def cmd_status(args) -> int:
    token, source = load_token()
    root = call("wbservicesbasic.isTokenConfirmed", token=token)
    raw = (root.text or "").strip() if root.tag == "confirmed" else (root.findtext("confirmed") or "")
    confirmed = raw.strip().lower() == "true"
    result = {"ok": True, "confirmed": confirmed, "token_source": source}
    if not confirmed:
        result["hint"] = (
            "Log in to TUMonline, open Token-Management, activate the token and "
            "enable its permissions."
        )
    emit(result)
    return EXIT_OK if confirmed else EXIT_NEEDS_SETUP


def cmd_calendar(args) -> int:
    token, _ = load_token()
    start = parse_day(args.start)
    end = parse_day(args.end) if args.end else start + timedelta(days=max(args.days, 1) - 1)
    if end < start:
        raise TumOnlineError("bad_argument", "--to is before --from.")
    if (end - start).days > 400:
        raise TumOnlineError("bad_argument", "The range is longer than 400 days.")
    events = fetch_events(token)
    days = []
    day = start
    while day <= end:
        summary = day_summary(events, day)
        if summary["events"] or summary["cancelled"]:
            days.append(summary)
        day += timedelta(days=1)
    if args.json:
        emit(
            {
                "ok": True,
                "from": start.isoformat(),
                "to": end.isoformat(),
                "timezone": "Europe/Berlin",
                "days": days,
            }
        )
        return EXIT_OK
    if not days:
        print(f"No TUMonline events between {start} and {end}.")
        return EXIT_OK
    for summary in days:
        print(f"\n{summary['weekday']} {summary['date']}")
        entries = summary["events"] + summary["cancelled"]
        for event in sorted(entries, key=lambda e: (e["start"], e["end"])):
            print("  " + format_event_line(event))
    return EXIT_OK


def cmd_brief(args) -> int:
    token, _ = load_token()
    emit(build_brief(fetch_events(token), parse_day(args.date), args.exam_days))
    return EXIT_OK


def cmd_lectures(args) -> int:
    token, _ = load_token()
    lectures = fetch_lectures(token)
    semester = current_semester(lectures)
    if not args.all and semester:
        lectures = [l for l in lectures if l["semester_id"] == semester]
    if args.json:
        emit({"ok": True, "current_semester": semester, "lectures": lectures})
        return EXIT_OK
    if not lectures:
        print("No courses found in TUMonline.")
        return EXIT_OK
    for lecture in lectures:
        sws = f", {lecture['sws']} SWS" if lecture["sws"] else ""
        print(f"[{lecture['semester_id']}] {lecture['title']} ({lecture['type']}{sws})")
    return EXIT_OK


def cmd_grades(args) -> int:
    token, _ = load_token()
    grades = fetch_grades(token)
    if args.limit:
        grades = grades[: args.limit]
    if args.json:
        emit({"ok": True, "grades": grades})
        return EXIT_OK
    if not grades:
        print("No grades found in TUMonline.")
        return EXIT_OK
    for grade in grades:
        print(f"{grade['date'] or '????-??-??'}  {grade['grade'] or '-':>4}  {grade['title']} [{grade['semester']}]")
    return EXIT_OK


def cmd_logout(args) -> int:
    path = token_file()
    removed = False
    if path.exists():
        path.unlink()
        removed = True
    emit(
        {
            "ok": True,
            "removed_token_file": removed,
            "note": "The token still exists in TUMonline; delete it there under "
            "Token-Management to revoke it.",
        }
    )
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tum_campus.py",
        description="Read your TUMonline calendar, courses and grades with a personal token.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    login = sub.add_parser("login", help="request a new token for a TUM ID and store it")
    login.add_argument("tum_id", help="TUM ID (TUM-Kennung), e.g. go42tum")
    login.add_argument("--name", default=DEFAULT_TOKEN_NAME, help="label shown in TUMonline's Token-Management")
    login.add_argument("--force", action="store_true", help="replace an already stored token")
    login.add_argument("--export", metavar="FILE", help="also write the bare token to FILE (0600), e.g. to copy it into an environment variable")
    login.set_defaults(func=cmd_login)

    status = sub.add_parser("status", help="check whether the token is confirmed")
    status.set_defaults(func=cmd_status)

    calendar = sub.add_parser("calendar", help="list calendar events")
    calendar.add_argument("--from", dest="start", metavar="DATE", help="first day (YYYY-MM-DD, today, tomorrow); default today")
    calendar.add_argument("--to", dest="end", metavar="DATE", help="last day, inclusive")
    calendar.add_argument("--days", type=int, default=1, help="number of days when --to is not given (default 1)")
    calendar.add_argument("--json", action="store_true")
    calendar.set_defaults(func=cmd_calendar)

    brief = sub.add_parser("brief", help="JSON digest for a morning brief: today, tomorrow, upcoming exams")
    brief.add_argument("--date", metavar="DATE", help="the day to brief (default today, Europe/Berlin)")
    brief.add_argument("--exam-days", type=int, default=21, help="look this many days ahead for exams (default 21)")
    brief.set_defaults(func=cmd_brief)

    lectures = sub.add_parser("lectures", help="list your registered courses")
    lectures.add_argument("--all", action="store_true", help="all semesters instead of only the current one")
    lectures.add_argument("--json", action="store_true")
    lectures.set_defaults(func=cmd_lectures)

    grades = sub.add_parser("grades", help="list your grades")
    grades.add_argument("--limit", type=int, default=0, help="only the newest N")
    grades.add_argument("--json", action="store_true")
    grades.set_defaults(func=cmd_grades)

    logout = sub.add_parser("logout", help="delete the locally stored token")
    logout.set_defaults(func=cmd_logout)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except TumOnlineError as error:
        emit(error.as_dict())
        return EXIT_NEEDS_SETUP if error.needs_setup else EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
