# SlimeNodes Auto Renew 🟢

**只续期、不刷币** 的 [SlimeNodes](https://dash.slimenodes.com) 服务器自动续期脚本，
支持 **Discord Token 关联登录**（Session 失效自动重新登录，免手动换 cookie）。

> **全程纯 HTTP（curl），不需要浏览器。**
> SlimeNodes 的 `/login` 直接 302 到 Discord 且不带 `state`、不预设 cookie，
> 因此整条 OAuth 链路可以用 curl 走完，无需 seleniumbase / chromedriver / Xvfb / Turnstile。

## 功能

- 🔄 **自动续期** — 服务器到期前 ≤ `RENEW_HOURS`（默认 24h）且余额 ≥ `RENEW_THRESHOLD`（默认 50 币）时自动调用 `/renew` 续期
- 🔑 **Discord Token 关联登录** — `SLIME_SESSION` 失效或未配置时，用 Discord Token 走 OAuth 自动换取新的 `connect.sid`
- ♻️ **Secret 自动回写** — 配了 `GH_TOKEN` 时，新 session 自动写回 `SLIME_SESSION` Secret，下次运行直接走快路径
- 📱 **TG 通知** — 每次运行后发送状态通知到 Telegram（可选）
- ⚠️ **余额不足提醒** — 余额不够续期时在通知中明确提示

## 登录机制

脚本按「快路径 → 备用路 → 回写路」三步走：

| 步骤 | 条件 | 动作 |
|------|------|------|
| ① 快路径 | `SLIME_SESSION` 有效 | 直接复用，**不向 Discord 发任何请求** |
| ② 备用路 | ① 失败 + 配了 `DISCORD_TOKEN` | 纯 HTTP 走 Discord OAuth 换新 `connect.sid` |
| ③ 回写路 | ② 成功 + 配了 `GH_TOKEN` | `gh secret set SLIME_SESSION` 回写，下次走快路径 |

备用路的三个 HTTP 步骤：

```
GET  https://dash.slimenodes.com/login
     → 302 https://discord.com/api/oauth2/authorize?client_id=...&redirect_uri=.../callback&scope=identify email guilds.join

POST https://discord.com/api/v9/oauth2/authorize?<同上 query>
     Header: authorization: <DISCORD_TOKEN>
     Body:   {"permissions":"0","authorize":true,"integration_type":0,"location_context":{...}}
     → 200 {"location": "https://dash.slimenodes.com/callback?code=..."}

GET  https://dash.slimenodes.com/callback?code=...
     (curl cookie jar)
     → Set-Cookie: connect.sid=... ; 302 /dashboard   ← 登录完成
```

> 💡 SlimeNodes 的 OAuth 是**无 state** 的，所以不需要像 bot-hosting 那样先用浏览器抓 state，
> 纯 curl 即可闭环。若面板日后改成带 state，需要补一步「先请求授权页、从 URL 里取 state」。

## 续期原理

纯 HTTP 方式，无需浏览器：

1. 请求 `/dashboard`，读取金币余额（顺带验证会话有效性）
2. 请求 `/lastrenew?id={SERVER_ID}` 获取服务器精确到期时间
3. 剩余时间 ≤ `RENEW_HOURS` 且余额 ≥ `RENEW_THRESHOLD` → 请求 `/renew?id={SERVER_ID}` 续期（约 115 币/次）
4. 发送 TG 通知

> 💡 **余额从哪来？** 本版不刷币，余额来自原版脚本积累或手动操作。余额不足时通知会提示，可手动去面板刷广告或充值。

## 定时任务

GitHub Actions 每天自动运行一次：

- ⏰ **12:30 UTC**（北京时间 20:30）

也可手动触发 `workflow_dispatch`（Actions 页面 → Run workflow）。

> 原版 README 写的「00:30 / 12:30 两次」与 workflow 实际内容不符（实际只有一条 cron）。
> 本版按实际内容保留单条 `30 12 * * *`，如需两次自行在 `on.schedule` 下再加一条。

## 运行记录自动清理

每次运行结束时会执行 `cleanup-runs` job：调用 GitHub API 把本工作流的**历史运行记录全部删除，只保留最新一份**（当前这次）。

- 只删除状态为 `completed` 的记录，正在进行中的运行不受影响
- 清理失败不影响续期结果（该 job 标记为 `continue-on-error`）
- 默认额外保留 `0` 条；如需再留几条历史，把 Variable `KEEP_RUNS` 改成对应数字
- 依赖 `permissions: actions: write`。若仓库把 `GITHUB_TOKEN` 设为只读，需到 Settings → Actions → General → Workflow permissions 改为 **Read and write**

## 环境变量

### Secrets（Settings → Secrets and variables → Actions → Secrets）

| 变量 | 说明 | 必填 |
|------|------|------|
| `SLIME_SESSION` | SlimeNodes session cookie (`connect.sid`) | ❌ 与 `DISCORD_TOKEN` 二选一 |
| `DISCORD_TOKEN` | Discord Token，`SLIME_SESSION` 失效时自动 OAuth 登录 | ❌ 与 `SLIME_SESSION` 二选一 |
| `GH_TOKEN` | GitHub **classic** PAT（`ghp_` 开头），用于回写 `SLIME_SESSION` | ❌ 不填则只能手动更新 |
| `TG_BOT_TOKEN` | Telegram Bot Token | ❌ |
| `TG_CHAT_ID` | Telegram Chat ID | ❌ |

> ⚠️ `SLIME_SESSION` 和 `DISCORD_TOKEN` 至少要有一个。两个都配是最佳实践：
> 平时走 session 快路径，Discord Token 兜底。

### Variables（Settings → Secrets and variables → Actions → Variables）

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `SERVER_ID` | LAST RENEW ID  | `10102` |
| `RENEW_HOURS` | 续期阈值小时数（剩余时间 ≤ 此值才续期） | `24` |
| `RENEW_THRESHOLD` | 续期最低余额（币） | `50` |
| `KEEP_RUNS` | 额外保留的历史运行记录条数 | `0` |
| `DISCORD_CLIENT_ID` | 面板 Discord 应用 ID（面板更换时才需要） | 自动从 `/login` 跳转解析 |
| `DISCORD_REDIRECT_URI` | 面板 OAuth 回调地址（面板更换时才需要） | 自动从 `/login` 跳转解析 |

不配置 Variables 时自动使用默认值，全部可选。

## Discord Token 获取

1. 浏览器登录 Discord 网页版
2. 按 `F12` 打开开发者工具 → **网络(Network)** → 随便点开一个频道
3. 在请求列表里找任意 `discord.com/api/v9/...` 请求，看 **请求标头** 里的 `authorization` 字段
4. 该值即为 Discord Token，整串填入 `DISCORD_TOKEN` Secret

> 也兼容 `前缀,token` 的写法（脚本只取逗号后最后一段）。

## GH_TOKEN 获取

1. GitHub 头像 → Settings → Developer settings → Personal access tokens → **Tokens (classic)**
2. Generate new token (classic) → 勾选 `repo` + `workflow`（不勾 workflow 时无法写 Secret）
3. 生成后立即复制，填入 `GH_TOKEN` Secret

## GitHub Secrets 示例

```json
{
  "SLIME_SESSION": "s%3A...",
  "DISCORD_TOKEN": "MTIz...",
  "GH_TOKEN": "ghp_...",
  "TG_BOT_TOKEN": "7935239797:AAH...",
  "TG_CHAT_ID": "644320820"
}
```

## TG 通知格式

```
🇺🇸 SlimeNodes 续期通知

✅ 续期成功
⏱️ 新过期时间: 6j 23h
👤 登录账户: b****b
🔐 登录方式: DISCORD_TOKEN
⏱️ 运行时间: 2026-09-06 05:00:28
```

过期时间用 `Xj Xh`（天+小时）原样展示；账号首尾各留 1 字符脱敏；
`🔐 登录方式` 仅在**非** session 直连时出现（即本次是 Discord 登录）。

状态行有五种：
- `✅ 续期成功`（同时显示续期后的新过期时间）
- `⏭️ 暂不需要续期`（剩余时间 > RENEW_HOURS）
- `⚠️ 余额不足 (需 50 币, 当前 30 币)`
- `❌ 续期失败`
- `❓ 无法获取到期时间`

登录失败时通知为 `❌ Session 已过期（未配置 Discord Token）` 或 `❌ Discord 关联登录失败`。

## 退出码（Actions 状态）

| 码 | 含义 |
|----|------|
| 0 | 正常（含"暂不需要续期"等正常跳过） |
| 1 | `SLIME_SESSION` 与 `DISCORD_TOKEN` 均未设置 |
| 2 | Session 已过期且未配置 `DISCORD_TOKEN` |
| 3 | 无法获取余额（网络/面板问题） |
| 4 | Discord 关联登录失败（Token 失效 / 授权被拒） |

## 本地测试

仓库自带一套 mock 面板，可在无真实账号的情况下端到端验证三条登录路径：

```bash
python tests/test_flow.py
```

会依次跑：SESSION 有效直连、SESSION 失效自动 Discord 登录、无效 Token、纯 Discord 登录。
测试会起两个 mock 服务（面板 + Discord），不联网、不需要真实账号。

另有一个只读诊断脚本，用于确认**线上面板**的 `/login` 跳转参数是否仍能被正确解析
（面板换 Discord 应用时用它排查，只访问 `dash.slimenodes.com`，不访问 Discord）：

```bash
python tests/probe_live_login.py
```
