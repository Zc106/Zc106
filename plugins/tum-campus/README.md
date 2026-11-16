# tum-campus

一个 Claude 插件：用你自己的 TUMonline token 读取慕尼黑工业大学的个人数据（课程日历、教室、停课、考试日期、已选课程、成绩），并把今天的课程放进晨间简报。

接口和官方 [TUM Campus App](https://github.com/TUM-Dev/campus_flutter) 用的是同一套（`campus.tum.de/tumonline/wbservicesbasic.*`）。脚本只读，除了申请 token 本身，不会在 TUMonline 里创建、修改或删除任何东西。只依赖 Python 3.9+ 标准库。

## 安装

```bash
claude plugin marketplace add Zc106/Zc106
```

```bash
claude plugin install tum-campus@zc106
```

在这个仓库里开的 Claude Code 会话不用安装，`.claude/skills/tum-campus` 已经指向同一份 skill。

## 第一次使用：申请并确认 token

1. 申请 token（把 `go42tum` 换成你的 TUM-Kennung：两个字母、两位数字、三个字母）：

   ```bash
   python3 plugins/tum-campus/skills/tum-campus/scripts/tum_campus.py login go42tum
   ```

2. 登录 <https://campus.tum.de>，打开 **Token-Management**，激活名为 “Claude Morning Brief” 的 token，并勾选权限：至少勾日历（Kalender）；想用 `lectures` / `grades` 的话再勾课程和成绩。这一步只有你本人能做。

3. 检查：

   ```bash
   python3 plugins/tum-campus/skills/tum-campus/scripts/tum_campus.py status
   ```

token 确认后长期有效，直到你在 Token-Management 里删掉它。每个账号最多 10 个 token。

### token 存在哪

按顺序查找：

1. 环境变量 `TUM_ONLINE_TOKEN`
2. `~/.config/tum-campus/token.json`（权限 0600；可用 `TUM_CAMPUS_CONFIG_DIR` 改目录）

云端会话的主目录不会保留，所以定时运行的晨间简报要靠环境变量：在云端环境的设置里添加 `TUM_ONLINE_TOKEN`，并把 `campus.tum.de` 加进允许的域名。

token 是凭证：不要贴进聊天、不要提交进仓库。脚本自己不会打印它。

## 命令

| 命令 | 作用 |
| --- | --- |
| `brief [--date 日期] [--exam-days 21]` | 给晨间简报用的 JSON：今天、明天、近期考试（只发一次请求） |
| `calendar [--from 日期] [--to 日期 \| --days N] [--json]` | 某段时间的日历 |
| `lectures [--all] [--json]` | 已选课程（默认只看当前学期） |
| `grades [--limit N] [--json]` | 成绩，最新的在前 |
| `status` | token 是否已确认 |
| `login <tum-id> [--force] [--export 文件]` | 申请并保存新 token |
| `logout` | 删除本地保存的 token |

日期写 `YYYY-MM-DD`、`today` 或 `tomorrow`。所有时间都是慕尼黑当地时间。

## 接入晨间简报

skill 里的 “Morning brief” 一节告诉 Claude 怎么用 `brief` 的结果：

- 今天的课算作日历事件，参与当天“地形图”和三段时间的绘制；和 Google Calendar 里重复的同一节课只算一次。
- 被取消的课归入 Resolved。
- 明天的考试归入 Needs attention；更远的考试在课程板块里提一句还剩几天。
- `Sections:` 里有课程相关条目时，按时间列出今天的课、类型和教室（教室链接到 NavigaTUM）。

取不到数据时（没有 token、网络被拦、TUMonline 维护）简报照常生成，只是退回到其他日历来源。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试不联网，用的是按 TUM Campus App 数据模型字段编写的 XML 样例。
