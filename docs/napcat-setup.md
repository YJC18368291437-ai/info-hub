> ⚠️ 本文件在 `D:\info-hub\docs\napcat-setup.md` 维护，由 `infohub_sync_github.ps1` 复制到这里。

# NapCat 配置指南（给没配过 QQ 机器人的人）

info-hub 不直接连 QQ。它通过 [NapCat](https://github.com/NapNeko/NapCatQQ)（一个基于 NTQQ 的
OneBot v11 实现）接收群消息，NapCat 再通过**反向 WebSocket** 把事件推给 info-hub。

```
QQ 客户端(NTQQ)
   └─ NapCat 插件 ── 反向 WS ──> info-hub (nonebot2, 默认 127.0.0.1:8082/onebot/v11/ws)
```

所以你需要三样东西：一个 QQ 号、NapCat、一个能收邮件的邮箱。

---

## 1. 用哪个 QQ 号

- **强烈建议新注册或用小号**，不要用主号：NapCat 会接管这个 QQ 的登录（相当于在电脑上登录），
  而 info-hub 会读取它所在的所有群。
- 这个号必须**已经加入你要采集的群**。采集是按群号白名单的，号不在群里就什么都收不到。
- 同一个 QQ 号同一时间只能被一个 NapCat 实例接管。如果你已经有别的机器人用这个号登录，
  要么换号，要么给现有 NapCat 加一个额外的 WS 客户端（见第 3 步的多客户端写法）。

## 2. 安装并登录 NapCat

1. 从 [NapCatQQ Releases](https://github.com/NapNeko/NapCatQQ/releases) 下载 Windows 一键版
   （`NapCat.Shell.Windows.OneKey`）。
2. 解压到一个**路径不含中文和空格**的目录，例如 `D:\napcat\napcat`。
3. 双击 `launcher.bat`（或 `启动.bat`）→ 首次会拉取 QQ → 出现二维码 → 用目标 QQ 扫码登录。
4. 登录成功后 NapCat 会打印 WebUI 地址和 token，一般是：

   ```
   WebUI: http://127.0.0.1:6099/webui?token=xxxxxx
   ```

   记下 token。可以先改掉 token，别用默认值。

> 登录会触发 QQ 的安全验证（扫码 / 短信 / 设备锁）。这一步无法自动化，必须人工过一次。
> 过完之后 NapCat 目录里会留下登录态，之后启动通常免扫码。

## 3. 让 NapCat 把消息推给 info-hub

有两种改法，任选一种。

### 3a. 通过 WebUI（推荐）

1. 打开上面的 WebUI 地址，进入 **网络配置**。
2. 新建 **WebSocket 客户端**（注意是「客户端」，不是「服务端」）：
   - 名称：`info-hub`
   - URL：`ws://127.0.0.1:8082/onebot/v11/ws`
   - 消息格式：`array`（info-hub 用数组格式解析）
   - 重连间隔：`3000`
   - 启用：开
3. 保存并重启 NapCat。

### 3b. 直接改配置文件

NapCat 的配置在 `<NapCat 目录>\config\onebot11_<QQ号>.json`。在 `network.websocketClients`
数组里追加一个对象（数组里可以同时有多个客户端，互不影响）：

```json
{
  "name": "info-hub",
  "url": "ws://127.0.0.1:8082/onebot/v11/ws",
  "messagePostFormat": "array",
  "reportSelfMessage": false,
  "reconnectInterval": 3000,
  "enable": true
}
```

仓库里的 `config/napcat_second_client.json` 就是这个片段，可以直接复制粘贴。
改完必须**重启 NapCat**。

## 4. 怎么拿到群号

三个办法，任选：

1. **QQ 客户端里看**：进群 → 右上角设置/群信息 → 群号（一串 6~11 位数字）。
2. **NapCat WebUI 的群列表**：登录后 WebUI 上有群列表，直接能看到群号和群名。
3. **调 OneBot API**：

   ```bash
   curl -s -X POST http://127.0.0.1:6099/api/... # WebUI 接口，按你那版文档走
   ```

   或者更简单：在 info-hub 的日志里搜 `group_id`，把群拉进白名单后发一条消息就能看到入站事件。

> 群号和 QQ 号都是隐私。**不要把它们提交到 git**，也不要贴到 issue 里。

## 5. 验证链路是否打通

1. 先只开 NapCat，确认它自己在线（WebUI 里显示已连接）。
2. 启动 info-hub（`infohub_启动.bat` 或 `python infohub.py`）。
3. 看 info-hub 日志，应出现 OneBot 连接成功 / `Bot ... connected`。
4. 在群里（用别的账号）发一条消息，日志里应出现该群消息入库；`data/infohub.db` 里 `messages` 表会多一行。
5. 若开了 `DRY_RUN=true`，只打印分类结果、不发邮件 —— 建议先这样跑通再开真实投递。

## 6. 常见坑

| 现象 | 原因 / 处理 |
| --- | --- |
| info-hub 启动后没有任何连接 | WS 地址写成 `ws://127.0.0.1:8082/onebot/v11/ws`，末尾路径不能少；端口要和 `.env` 的 `PORT` 一致 |
| 连接反复断开 | NapCat 与 info-hub 之间被代理/VPN 拦截；或 info-hub 没启动。反向 WS 不需要开 NapCat 的 HTTP 服务 |
| 收不到群消息 | 号不在群里；群号没进 `config/groups.yaml` 白名单；NapCat 没订阅群消息事件 |
| 只有私聊没有群消息 | 用的是「WebSocket 服务端」而不是「客户端」，方向反了 |
| 登录二维码过期 | 重新启动 launcher 生成新的二维码；过期通常是几分钟 |
| 提示设备锁/安全验证 | 在手机 QQ 上确认登录，或关掉设备锁后重试 |
| 改完配置没生效 | NapCat 必须重启；WebUI 的改动有时需要点「重启」 |
| 多个机器人抢同一个号 | 一个 QQ 只有一个在线会话。要么换号，要么把多个 WS 客户端挂在同一个 NapCat 上 |

## 7. 开机自启（可选）

仓库里的 `infohub_开机自启.vbs` 会把 info-hub 挂到启动目录；NapCat 自己的自启用 NapCat 目录的
launcher 加进「启动」文件夹，或者在任务计划程序里加一个「登录时运行」。

注意顺序没有强依赖：info-hub 先起、NapCat 后起也能连上，因为反向 WS 由 NapCat 发起并会自动重连。
