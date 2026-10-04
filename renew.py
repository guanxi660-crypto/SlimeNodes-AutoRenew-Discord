#!/usr/bin/env python3
"""SlimeNodes Auto-Renew — 只续期，不刷币（Discord Token 关联登录版）

改装自 guanxi660-crypto/SlimeNodes-AutoRenew（原版仅支持 Session Cookie 单登录）。
本版新增 **Discord Token 关联登录**：当 connect.sid 失效或未配置时，
用 Discord Token 走 OAuth 自动换取新的 connect.sid，并可回写 GitHub Secret，
实现真正的一次配置、长期免维护。

续期链路（与原版一致）：
  1. 查余额     (/dashboard)
  2. 查剩余时间 (/lastrenew?id=)
  3. 到期前 ≤RENEW_HOURS 且余额 ≥RENEW_THRESHOLD 时调 /renew 续期
  4. TG 通知（状态 + 过期时间 + 账号 + 登录方式 + 运行时间）

登录链路：
  快路径：SLIME_SESSION(connect.sid) 有效 → 直接复用，不碰 Discord
  备用路：SLIME_SESSION 失效/缺失 + 配置了 DISCORD_TOKEN → 纯 HTTP OAuth
            GET  {BASE}/login
                 → 302 到 discord.com/api/oauth2/authorize（SlimeNodes 不带 state）
            POST discord.com/api/v9/oauth2/authorize  (Authorization: <DISCORD_TOKEN>)
                 → 返回 location = {BASE}/callback?code=...
            GET  /callback?code=...      (带 cookie jar)
                 → 200 中间页，页面只有一段 JS：window.location.replace('/submitlogin?code=...')
                   （curl 不执行 JS，必须手动跟进，否则永远拿不到 session）
            GET  /submitlogin?code=...   (带 cookie jar)
                 → 302 /dashboard + Set-Cookie: connect.sid，登录完成
  回写路：拿到新 session 且配置了 GH_TOKEN → 用 gh CLI 回写 SLIME_SESSION Secret

纯 HTTP 方式，无需浏览器（无需 seleniumbase / chromedriver / Turnstile）。
依赖系统 curl。
"""
import os, sys, re, json, time, subprocess
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, parse_qs

# ---------------------------------------------------------------- 基础配置
BASE = os.environ.get("SLIME_BASE") or "https://dash.slimenodes.com"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
TGT = os.environ.get("TG_BOT_TOKEN") or ""
TGC = os.environ.get("TG_CHAT_ID") or ""
# 可选代理（vless/vmess 等需先转成 socks5/http 本地端口再填）
PX = os.environ.get("SOCKS_PROXY") or os.environ.get("HTTP_PROXY") or ""

SLIME_SESSION = os.environ.get("SLIME_SESSION") or ""
# 新增：Discord Token（备用登录）。兼容参考仓库的 "前缀,token" 写法，只取最后一段。
_raw_dc = os.environ.get("DISCORD_TOKEN") or ""
DISCORD_TOKEN = _raw_dc.split(",", 1)[-1].strip() if _raw_dc else ""
# 新增：GitHub PAT(classic)，用于自动回写 SLIME_SESSION Secret（可选）
GH_TOKEN = os.environ.get("GH_TOKEN") or ""

# 账号名默认从面板 dashboard 自动识别（Heliactyl 系面板的 navbar-profile-name）；
# 该环境变量仅在需要强制覆盖时使用。
ACCOUNT_LABEL = os.environ.get("ACCOUNT_LABEL") or ""
SERVER_ID = os.environ.get("SERVER_ID") or "10102"
# 续期最低余额（币）
RENEW_THRESHOLD = int(os.environ.get("RENEW_THRESHOLD") or "50")
# 剩余时间低于该小时数才续期
RENEW_HOURS = int(os.environ.get("RENEW_HOURS") or "24")

# Discord OAuth 参数。默认值取自 SlimeNodes 面板 /login 的真实跳转，
# 面板若变更可用环境变量覆盖（见 README）。
DISCORD_API_BASE = os.environ.get("DISCORD_API_BASE") or "https://discord.com/api/v9"
DISCORD_CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID") or ""
DISCORD_REDIRECT_URI = os.environ.get("DISCORD_REDIRECT_URI") or ""
DISCORD_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)

# 退出码
EXIT_OK = 0
EXIT_NO_SESSION = 1
EXIT_SESSION_EXPIRED = 2
EXIT_BALANCE_FAIL = 3
EXIT_DISCORD_LOGIN_FAIL = 4

NUL = os.devnull
JAR = os.path.join(os.environ.get("TEMP") or os.environ.get("TMPDIR") or "/tmp",
                   "slime_cookies.txt")

# 运行时 cookie 状态：优先 jar（Discord 登录后），否则用显式 cookie 头
# dashboard_html 由 get_balance() 顺手缓存，供账号识别复用（避免额外请求）
_STATE = {"session": "", "use_jar": False, "dashboard_html": ""}
# 本次实际使用的登录方式（用于通知）
_LOGIN_METHOD = "SESSION_TOKEN"


# ---------------------------------------------------------------- 小工具
def px():
    """返回 curl 代理参数列表（未配置代理时为空）"""
    return ["-x", PX] if PX else []


def log(m):
    """打印带 UTC 时间戳的日志行"""
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {m}", flush=True)


def ok(m):
    """成功日志（✅ 前缀）"""
    log(f"✅ {m}")


def er(m):
    """失败日志（❌ 前缀）"""
    log(f"❌ {m}")


def run_curl(args, timeout=25):
    """执行 curl 并返回 stdout；异常时返回 ERR: 前缀字符串"""
    cmd = ["curl", "-s", "--connect-timeout", "20", "--max-time", str(timeout)] + px() + args
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 15)
        return r.stdout
    except Exception as e:
        return f"ERR:{e}"


def cookie_args():
    """当前生效的 cookie 参数：Discord 登录后走 jar，否则走显式 Cookie 头"""
    if _STATE["use_jar"]:
        return ["-b", JAR, "-c", JAR]
    return ["-H", f"Cookie: connect.sid={_STATE['session']}"]


def send_tg(msg):
    """发送 Telegram 通知（未配置 token 则静默跳过）"""
    if not TGT or not TGC:
        return
    run_curl(["-X", "POST", f"https://api.telegram.org/bot{TGT}/sendMessage",
              "-H", "Content-Type: application/json",
              "-d", json.dumps({"chat_id": TGC, "text": msg})],
             timeout=15)


def mask_account(label):
    """账号脱敏：保留首尾各 1 字符，中间 ****（含 @ 时只脱敏 @ 前部分）"""
    local, _, domain = label.partition("@")
    if len(local) <= 2:
        return label
    return local[0] + "****" + local[-1] + ("@" + domain if domain else "")


def get_account_label():
    """识别当前登录账号。

    面板（Heliactyl 系）在 dashboard 导航栏渲染：
        <p class="... navbar-profile-name">用户名</p>
    该 HTML 由 get_balance() 顺手缓存，因此不额外发请求。
    识别不到时回退到 ACCOUNT_LABEL 环境变量；再没有则返回空串。
    """
    html = _STATE.get("dashboard_html") or ""
    m = re.search(r'navbar-profile-name[^>]*>([^<]+)<', html)
    if m and m.group(1).strip():
        return m.group(1).strip()
    return ACCOUNT_LABEL


def fmt_remaining(hours):
    """剩余小时数 → '4j 9h' 格式（j=天 h=小时），不足 1 天只显示小时，不做小数天换算"""
    if hours is None or hours < 0:
        hours = 0
    d, h = int(hours // 24), int(hours % 24)
    return f"{d}j {h}h" if d > 0 else f"{h}h"


def now_local():
    """北京时间字符串 (UTC+8)，格式 2026-09-06 05:00:28"""
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def notify(status, extra="", expiry_hours=None, renewed=False):
    """统一构造并发送 TG 通知（同时打印进 Actions 日志）"""
    lines = ["🇺🇸 SlimeNodes 续期通知", "", status]
    if expiry_hours is not None:
        lines.append(f"⏱️ {'新过期时间' if renewed else '过期时间'}: {fmt_remaining(expiry_hours)}")
    if extra:
        lines.append(extra)
    acct = get_account_label()
    lines.append(f"👤 登录账户: {mask_account(acct) if acct else '未识别'}")
    if _LOGIN_METHOD != "SESSION_TOKEN":
        lines.append(f"🔐 登录方式: {_LOGIN_METHOD}")
    lines.append(f"⏱️ 运行时间: {now_local()}")
    msg = "\n".join(lines)
    log("\n" + msg)  # 未配 TG 也能在 Actions 日志看到完整状态
    send_tg(msg)


def same_host(url, base=BASE):
    """判断 url 是否与面板同域（netloc 比较）。

    未登录时 curl -L 会一路跟到 Discord 授权页，最终 URL 里根本没有 "/login"，
    所以不能只看路径，必须看是否被甩出了面板域名。
    """
    try:
        return urlparse(url).netloc == urlparse(base).netloc
    except Exception:
        return True


def looks_logged_out(final_url, body=""):
    """根据最终落点判断是否被踢回登录"""
    if not final_url or final_url.startswith("ERR:") or body.startswith("ERR:"):
        return False  # 网络层问题，不能当会话失效
    if "/login" in final_url or "oauth2/authorize" in final_url:
        return True
    return not same_host(final_url)


# ---------------------------------------------------------------- 面板请求
def get_balance():
    """获取金币余额。

    返回 (balance:int|None, expired:bool)：
      - balance 为余额数值；获取失败为 None
      - expired 为 True 表示会话已失效（被重定向到 /login 或甩到 Discord）
    """
    body = run_curl(["-L", "-w", "\n%{url_effective}",
                     "-H", f"User-Agent: {UA}"] + cookie_args() + [f"{BASE}/dashboard"])
    lines = body.strip().split("\n")
    final_url = lines[-1] if lines else ""
    m = re.search(r'balance\.textContent\s*=\s*Math\.floor\((\d+)\s*\*\s*100\)', body)
    if m:
        _STATE["dashboard_html"] = body   # 供账号识别复用，不额外发请求
        return int(m.group(1)), False
    if looks_logged_out(final_url, body):
        return None, True
    return None, False


def get_hours_left():
    """通过 /lastrenew API 获取服务器剩余小时数；失败返回 None"""
    body = run_curl(["-H", f"User-Agent: {UA}"] + cookie_args() +
                    [f"{BASE}/lastrenew?id={SERVER_ID}"])
    try:
        end_ms = json.loads(body).get("lastrenew", 0)
        if not end_ms:
            return None
        return (end_ms - time.time() * 1000) / (1000 * 60 * 60)
    except Exception:
        return None


def renew(server_id):
    """续期服务器。成功返回 True。"""
    log(f"Renewing server {server_id}...")
    # -w 捕获重定向后的最终 URL，续期成功会落在 success=RENEWED
    cmd = ["-L", "-w", "\n%{url_effective}",
           "-H", f"User-Agent: {UA}"] + cookie_args() + [f"{BASE}/renew?id={server_id}"]
    output = run_curl(cmd, timeout=25)
    lines = output.strip().split("\n")
    final_url = lines[-1] if lines else ""
    log(f"Renew URL: {final_url[:80]}")
    if "RENEWED" in final_url:
        ok("Server renewed!")
        return True
    if looks_logged_out(final_url):
        er("Session expired during renew")
        return False
    er(f"Renew failed: {final_url[:80]}")
    return False


# ---------------------------------------------------------------- Discord 关联登录
def _read_jar_session():
    """从 cookie jar 里取出 connect.sid（缺失时退回第一个非空 cookie）"""
    if not os.path.exists(JAR):
        return ""
    fallback = ""
    try:
        with open(JAR, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                # curl 把 HttpOnly cookie 写成 "#HttpOnly_<domain>\t..."，必须剥前缀而不是当注释丢弃
                if line.startswith("#HttpOnly_"):
                    line = line[len("#HttpOnly_"):]
                elif line.startswith("#"):
                    continue
                parts = line.split("\t")
                if len(parts) < 7:
                    continue
                name, value = parts[5], parts[6]
                if name == "connect.sid" and value:
                    return value
                if value and not fallback:
                    fallback = value
    except Exception:
        pass
    return fallback


def _discord_authorize_url():
    """GET {BASE}/login，返回 302 的目标（Discord 授权页 URL）"""
    url = run_curl(["-o", NUL, "-w", "%{redirect_url}", "-H", f"User-Agent: {UA}",
                    f"{BASE}/login"], timeout=20).strip()
    if "/oauth2/authorize" not in url:
        er(f"/login 未跳转到 OAuth 授权页，实际: {url[:120] or '(空)'}")
        return ""
    if "discord.com" not in url and DISCORD_API_BASE == "https://discord.com/api/v9":
        log(f"⚠️ 授权页不在 discord.com 域下: {url[:100]}")
    return url


def _discord_exchange(authorize_url):
    """用 DISCORD_TOKEN 完成 Discord 侧授权，返回回调 location（含 code）"""
    parsed = urlparse(authorize_url)
    params = parse_qs(parsed.query)
    client_id = DISCORD_CLIENT_ID or (params.get("client_id") or [""])[0]
    redirect_uri = DISCORD_REDIRECT_URI or (params.get("redirect_uri") or [""])[0]
    if not client_id or not redirect_uri:
        er("无法解析 client_id / redirect_uri")
        return ""

    # 直接复用面板给出的 query（自动跟随面板变更），并指向 v9 授权接口
    api_url = f"{DISCORD_API_BASE}/oauth2/authorize?{parsed.query}"
    referer = f"https://discord.com/oauth2/authorize?{parsed.query}"
    headers = [
        "-H", "accept: */*",
        "-H", f"authorization: {DISCORD_TOKEN}",
        "-H", "content-type: application/json",
        "-H", "origin: https://discord.com",
        "-H", f"referer: {referer}",
        "-H", f"user-agent: {DISCORD_UA}",
        "-H", "x-discord-locale: zh-CN",
    ]
    body = json.dumps({
        "permissions": "0",
        "authorize": True,
        "integration_type": 0,
        "location_context": {"guild_id": "10000", "channel_id": "10000", "channel_type": 10000},
    })
    out = run_curl(["-X", "POST", "-o", "-", "-w", "\n%{http_code}", api_url] + headers +
                   ["-d", body], timeout=25)
    payload, _, code = out.rpartition("\n")
    code = code.strip()
    if code != "200":
        er(f"Discord 授权失败: HTTP {code or '?'} - {payload[:200]}")
        return ""
    try:
        location = json.loads(payload).get("location", "")
    except Exception:
        er(f"Discord 授权响应非 JSON: {payload[:200]}")
        return ""
    if not location:
        er(f"授权响应中无 location 字段: {payload[:200]}")
        return ""
    log("Discord 授权通过，已拿到回调链接: " + re.sub(r"code=[^&]+", "code=***", location)[:120])
    return location


def _finish_panel_login(location):
    """走完面板回调：/callback 只是 JS 中间页，真正登录在 /submitlogin。

    面板 /callback?code=... 返回 200，正文只有一段脚本：
        window.location.replace('/submitlogin?code=...')
    浏览器会自动跳转，curl 不会执行 JS，所以必须手动提取并跟进 /submitlogin，
    由它下发 connect.sid。返回跟随后的最终 URL（失败返回空串）。
    """
    # 1) 先取中间页 HTML，从中提取 /submitlogin 链接
    body = run_curl(["-H", f"User-Agent: {UA}", "-c", JAR, "-b", JAR, location],
                    timeout=25)
    m = re.search(r"""['"](/submitlogin\?[^'"]*)['"]""", body)
    if m:
        submit_url = BASE.rstrip("/") + m.group(1)
    else:
        # 2) 兜底：面板若换了中间页写法，按 /callback → /submitlogin 路径替换
        p = urlparse(location)
        if p.netloc != urlparse(BASE).netloc or not p.path.rstrip("/").endswith("/callback"):
            er(f"无法从回调页解析登录地址: {location[:120]}")
            return ""
        submit_url = f"{BASE.rstrip('/')}/submitlogin?{p.query}"
        log(f"⚠️ 未从中间页提取到 /submitlogin，按路径替换兜底: {submit_url[:120]}")
    log(f"面板登录端点: {submit_url[:120]}")

    # 3) 请求 /submitlogin，跟随 302；Set-Cookie 会被 curl 写进 jar
    final_url = run_curl(["-L", "-o", NUL, "-w", "%{url_effective}",
                          "-H", f"User-Agent: {UA}",
                          "-c", JAR, "-b", JAR, submit_url], timeout=25).strip()
    log(f"回调落地: {final_url[:120]}")
    return final_url


def discord_login():
    """纯 HTTP 走完 Discord OAuth，把新 connect.sid 收进 cookie jar。成功返回 session 值。"""
    global _LOGIN_METHOD
    log("🔑 使用 Discord Token 关联登录...")

    authorize_url = _discord_authorize_url()
    if not authorize_url:
        return ""
    location = _discord_exchange(authorize_url)
    if not location:
        return ""

    final_url = _finish_panel_login(location)
    if not final_url:
        return ""
    if "error=" in final_url:
        er(f"回调返回错误: {final_url[:160]}")
        return ""
    if looks_logged_out(final_url):
        er("回调后仍未登录成功（被退回登录页）")
        return ""

    session = _read_jar_session()
    if not session:
        er("回调成功但未拿到 connect.sid cookie")
        return ""
    ok(f"Discord 登录成功，已获取新 session ({session[:6]}...)")
    _LOGIN_METHOD = "DISCORD_TOKEN"
    return session


def update_github_secret(secret_name, value):
    """用 gh CLI 回写 GitHub Secret（需 GH_TOKEN，classic PAT 带 repo 权限）"""
    if not GH_TOKEN:
        log(f"ℹ️ 未配置 GH_TOKEN，跳过回写 {secret_name}")
        return False
    masked = value[:4] + "..." + value[-4:] if len(value) > 8 else "***"
    log(f"🔄 回写 Secret: {secret_name} ({masked})")
    env = os.environ.copy()
    env["GH_TOKEN"] = GH_TOKEN
    try:
        p = subprocess.run(["gh", "secret", "set", secret_name, "--body", value],
                           capture_output=True, text=True, timeout=30, env=env)
        if p.returncode == 0:
            ok(f"{secret_name} 回写成功")
            return True
        er(f"{secret_name} 回写失败: {p.stderr.strip()[:200]}")
        return False
    except Exception as e:
        er(f"{secret_name} 回写异常: {e}")
        return False


# ---------------------------------------------------------------- 主流程
def main():
    log(f"\n{'='*44}\n  SlimeNodes 自动续期 (Discord 关联登录)\n{'='*44}")

    if not SLIME_SESSION and not DISCORD_TOKEN:
        er("SLIME_SESSION 与 DISCORD_TOKEN 均未设置，无法登录")
        sys.exit(EXIT_NO_SESSION)

    # ---- 1. 建立会话：快路径优先，失效则 Discord 关联登录
    balance = None
    session_expired = False   # 区分「会话失效」与「网络故障」，用于退出码/通知措辞
    if SLIME_SESSION:
        _STATE.update(session=SLIME_SESSION, use_jar=False)
        balance, expired = get_balance()
        if expired:
            session_expired = True
            er("SLIME_SESSION 已失效，转入 Discord 关联登录")
            balance = None
        elif balance is None:
            er("无法获取余额（网络/面板问题），先尝试 Discord 关联登录兜底")
        else:
            ok(f"SESSION_TOKEN 有效，余额: {balance}币")

    if balance is None:
        if not DISCORD_TOKEN:
            if session_expired:
                er("会话已失效且未配置 DISCORD_TOKEN，无法继续")
                notify("❌ Session 已过期（未配置 Discord Token）")
                sys.exit(EXIT_SESSION_EXPIRED)
            er("无法获取余额且未配置 DISCORD_TOKEN，无法继续")
            notify("❌ 无法获取余额（网络/面板问题）")
            sys.exit(EXIT_BALANCE_FAIL)

        # 清掉可能的旧 jar，避免串号
        try:
            if os.path.exists(JAR):
                os.remove(JAR)
        except Exception:
            pass

        new_session = discord_login()
        if not new_session:
            er("Discord 关联登录失败")
            notify("❌ Discord 关联登录失败", extra="请检查 DISCORD_TOKEN 是否有效")
            sys.exit(EXIT_DISCORD_LOGIN_FAIL)

        _STATE.update(session=new_session, use_jar=True)
        balance, expired = get_balance()
        if expired or balance is None:
            er("Discord 登录后仍无法读取余额")
            notify("❌ Discord 登录后无法读取余额")
            sys.exit(EXIT_BALANCE_FAIL)
        ok(f"Discord 登录后余额: {balance}币")

        # 回写 Secret，下次直接走快路径
        update_github_secret("SLIME_SESSION", new_session)

    # 会话已建立，此时可识别真实账号
    log(f"账号: {get_account_label() or '(未识别)'}")

    # ---- 2. 查剩余时间
    hl = get_hours_left()
    if hl is None:
        er("无法获取到期时间")
    else:
        log(f"服务器剩余: {hl:.0f}小时 ({fmt_remaining(hl)})")

    # ---- 3. 续期判断：到期前 ≤RENEW_HOURS 且余额够 → 续期
    renewed = False
    if hl is not None and hl <= RENEW_HOURS:
        log(f"进入续期窗口 (≤{RENEW_HOURS}h)")
        if balance >= RENEW_THRESHOLD:
            renewed = renew(SERVER_ID)
            if renewed:
                b2, _ = get_balance()
                log(f"续期后余额: {b2}")
        else:
            er(f"余额不足续期 (需要 {RENEW_THRESHOLD} 币, 当前 {balance} 币)")
    elif hl is not None:
        log(f"离到期还有 {hl:.0f}h，暂不续期 (>{RENEW_HOURS}h)")

    # 续期成功后重新获取新到期时间
    if renewed:
        new_hl = get_hours_left()
        if new_hl is not None:
            hl = new_hl

    # ---- 4. TG 通知
    if renewed:
        notify("✅ 续期成功", expiry_hours=hl, renewed=True)
    elif hl is None:
        notify("❓ 无法获取到期时间")
    elif hl > RENEW_HOURS:
        notify("⏭️ 暂不需要续期", expiry_hours=hl)
    elif balance < RENEW_THRESHOLD:
        notify(f"⚠️ 余额不足 (需 {RENEW_THRESHOLD} 币, 当前 {balance} 币)", expiry_hours=hl)
    else:
        notify("❌ 续期失败", expiry_hours=hl)

    ok("完成")
    sys.exit(EXIT_OK)


if __name__ == "__main__":
    main()
