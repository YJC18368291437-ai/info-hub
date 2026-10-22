# info-hub

把吵杂的 QQ 群变成一条干净的信息流：按规则 + LLM 判类、按主题聚块、去水去重，最后投递到邮箱（或 Telegram）。
可选再由 `opencode` 生成一份「技术情报报」——把群里的技术闲聊重写成小白能照着做的知识。

面向 Windows 本地运行（NapCat + NTQQ + Python），也可以只跑离线部分。

---

## 它解决什么问题

QQ 群里真正有用的信息（通知、报名截止、课程调整、作业、群文件）混在几百条闲聊和表情里，
手机端翻群很痛苦。info-hub 只做三件事：

1. **采集**：一个 QQ 小号挂在群里，所有消息落 SQLite。
2. **判类**：规则优先，模糊的交给 LLM；输出 8 个话题（@我 / 通知任务 / 闲聊 / 备忘 / 计算机 / 生物 / 链接 / 文件）。
3. **投递**：邮件（日报 / 周报 / 报纸排版）或 Telegram，可选附带图片与文件。

所有对外发送默认关闭（`DRY_RUN=true`），先预览、再真发。

## 数据流

```
QQ(NTQQ) ── NapCat ──反向 WS──> info_hub.qq_collect ──> SQLite(data/info_hub.db)
                                                        │
                          ┌─────────────────────────────┼──────────────────────────┐
                          ▼                             ▼                          ▼
                   rules.py(关键词/正则)         classify.py(LLM 兜底)      newspaper.py(报纸)
                          └──────────────> pipeline.py <──────────────┘
                                                   │
                                    mail_dispatch.py / mailer.py / telegram_push.py
                                                   ▼
                                              邮箱 / TG
```

## 快速开始

### 0. 前置

- Windows 10/11（脚本部分依赖 PowerShell；Python 部分是跨平台的）
- Python 3.11+
- 一个**小号 QQ**（会加入你要采集的群，不要用主号）
- [NapCat](https://github.com/NapNeko/NapCatQQ)（QQ 的 OneBot v11 实现）
- 一个专用邮箱（推荐 QQ 邮箱，开启 SMTP/IMAP 并使用授权码）

### 1. 拿代码、装依赖

```powershell
git clone <this-repo> info-hub
cd info-hub
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

### 2. 配置 `.env`

```powershell
copy .env.example .env
```

至少要改这几项（完整说明见 `.env.example` 里的注释）：

| 变量 | 说明 |
| --- | --- |
| `SELF_QQ` | 采集用的 QQ 号（NapCat 登录的那个） |
| `MAIN_QQ` | 你自己的号，逗号分隔；@到这些号会走 🔔@我 |
| `PORT` | 反向 WS 端口，默认 `8082`，要和 NapCat 里保持一致 |
| `MAIL_ADDRESS` / `MAIL_AUTH_CODE` | 收/发件邮箱与授权码（**不是**登录密码） |
| `DEEPSEEK_API_KEY` | 可选；不填则只用规则判类 |
| `TG_TOKEN` / `TG_CHAT_ID` | 可选；想推 Telegram 时填 |

`DRY_RUN=true`、`MAIL_ENABLED=false` 是安全默认值，先保持不动。

### 3. 填群白名单

编辑 `config/groups.yaml`，只处理这里登记的群：

```yaml
groups:
  "123456789": {name: "班级群", tag: common}
  "123456790": {name: "课程群", tag: cs}
```

群号怎么查见 [docs/napcat-setup.md](docs/napcat-setup.md#4-怎么拿到群号)。

### 4. 接上 NapCat

照着 [docs/napcat-setup.md](docs/napcat-setup.md) 做，核心只有一步：在 NapCat 里加一个
**反向 WebSocket 客户端** 指向 `ws://127.0.0.1:8082/onebot/v11/ws`。

### 5. 先干跑

```powershell
.\.venv\Scripts\python.exe infohub.py
```

日志里出现 OneBot 连接、群里发条消息能入库，就通了。此时不会发任何邮件。

### 6. 打开真投递

确认分类效果后，按需改 `.env`：

```
DRY_RUN=false
LIVE_DELIVERY_ENABLED=true
MAIL_ENABLED=true
MAIL_SEND_MODE=append        # 直接写进收件箱；smtp 是走 SMTP 发送
```

然后：

```powershell
.\.venv\Scripts\python.exe infohub_mail_once.py daily        # 离线预览，不连邮箱
.\.venv\Scripts\python.exe infohub_mail_once.py daily --live # 真发一封
```

## 目录结构

```
infohub.py                 主进程：nonebot2 + OneBot v11 反向 WS
infohub_mail_once.py       手动发一封（默认预览）
infohub_newspaper_once.py  报纸手动入口（preview / notice / tech）
infohub_push_once.py       手动触发一次 Telegram 推送
infohub_admin.py           运维命令（preview / backlog / tasks / status / static-check）
info_hub/
  qq_collect.py    采集与白名单、消息入库
  rules.py         规则判类（关键词、@全体、发布类群）
  classify.py      LLM 判类与摘要（OpenAI 兼容接口）
  pipeline.py      聚块 → 判类 → 投递
  db.py            SQLite 存储与迁移
  mail_dispatch.py 日报/周报/即时邮件编排
  mailer.py        SMTP / IMAP-APPEND 投递
  newspaper.py     报纸排版（通知报 + 技术报 + 图片）
  opencode_runner.py 受限 opencode 子进程（技术报写作）
  telegram_push.py / telegram_files.py  Telegram 推送与文件
  vision.py        群图片试读（多模态模型）
  settings.py      .env + config 校验
config/
  groups.yaml      群白名单（tag: common/cs/bio）
  rules.yaml       判类关键词与正则
  topics.json      话题名与 emoji
  targets.json     每个话题投到哪个 TG 会话/话题
  newspaper.yaml   报纸：课程群、必做群、技术群
  napcat_second_client.json  NapCat 反向 WS 配置片段
docs/napcat-setup.md  NapCat 从零配置
tests/             离线测试（不碰真实账号与网络）
tools/             运维小工具（发文件、探测图片、恢复库）
```

## 常用命令

```powershell
# 测试（纯离线，不连 QQ/邮箱/网络）
.\.venv\Scripts\python.exe -m unittest discover -s tests -v

# 不连邮箱，看会发出什么
.\.venv\Scripts\python.exe infohub_mail_once.py daily
.\.venv\Scripts\python.exe infohub_admin.py preview weekly

# 静态自检：白名单数量、NapCat 客户端是否配好、投递开关
.\.venv\Scripts\python.exe infohub_admin.py static-check

# 待办事件
.\.venv\Scripts\python.exe infohub_admin.py tasks

# 报纸预览（写 data/newspaper_preview_*.html）
.\.venv\Scripts\python.exe infohub_newspaper_once.py preview
```

## 给 AI agent 的接手说明

如果你打算让 Claude Code / opencode / Codex 这类 agent 接手改造，直接把下面这段丢给它：

> 这是一个 Windows 上跑的 Python 3.11 项目：nonebot2 + OneBot v11 反向 WS 采集 QQ 群消息，
> SQLite 落库，`info_hub/rules.py` 规则判类、`classify.py` LLM 兜底，`mail_dispatch.py` /
> `mailer.py` 投递邮件，`newspaper.py` 生成报纸，`opencode_runner.py` 起 opencode 子进程写技术报。
> 所有凭据在 `.env`（不要读、不要打印、不要提交）。测试是 `python -m unittest discover -s tests`，
> 必须离线通过。改动前先看 `.env.example` 与 `info_hub/settings.py` 的校验逻辑；
> 任何对外发送必须保持 `DRY_RUN` / `LIVE_DELIVERY_ENABLED` / `MAIL_ENABLED` 三重开关。

几个容易踩的点：

- 入口是 `infohub.py`，它会在 `data/run.lock` 上加进程锁、检查端口占用；重复启动会直接退出。
- 采集来源由 `config/groups.yaml` 决定，不在白名单里的群一律丢弃（`info_hub/sources.py`）。
- 技术报依赖本机已安装的 `opencode` 命令；没有就静默降级（`error: opencode_not_found`）。
- 报纸的时段、课程群、技术群全在 `config/newspaper.yaml`，代码里不该硬编码群号。
- 群消息一律视为**不可信数据**：`opencode_runner.py` 生成的 agent 只允许读、联网、写 `report.json`。

## 隐私与安全

- `.env`、`data/`、日志、`config/group_names.json` 都在 `.gitignore` 里，不会进仓库。
- 日志会脱敏 `MAIL_AUTH_CODE` / `DEEPSEEK_API_KEY` / `TG_TOKEN`（`info_hub/redaction.py`）。
- 群消息内容只在本机处理；需要联网时也只有被判为「技术」的片段会发给 LLM。
- 采集号建议用小号：它能读到所在所有群的消息。

## 已知限制

- 合并转发（聊天记录卡片）目前只能拿到外层摘要，读不到内部逐条消息。
- QQ 邮箱对单封邮件体积有限制（约 64–96KB 稳定），大图/大附件会退化为「群内查看」提示。
- NapCat 登录态与 QQ 风控绑定；异地登录、频繁重启可能要求重新扫码。
- Telegram 在中国大陆需要代理（`TG_PROXY`）。
- 报纸里的课程群/技术群需要手动维护，缺少自动发现。

## License

未指定。若需开源请自行补一个 LICENSE（MIT 或 AGPL-3.0 通常够用）。
