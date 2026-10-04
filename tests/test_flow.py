#!/usr/bin/env python3
"""SlimeNodes 续期脚本本地端到端测试。

起两个模拟服务：
  * 面板服务（模拟 dash.slimenodes.com）—— /login /callback /submitlogin /dashboard /lastrenew /renew
    注意 /callback 忠实复现真实面板行为：返回 200 + JS 中间页（window.location.replace('/submitlogin?...')），
    真正下发 connect.sid 的是 /submitlogin。curl 不执行 JS，脚本必须自己跟进。
  * Discord 服务（模拟 discord.com）   —— /api/oauth2/authorize /api/v9/oauth2/authorize

之所以拆成两个端口，是为了忠实复现「会话失效时 curl -L 会被甩到异域 Discord 页」
这一真实行为，从而验证脚本的跨域失效判定。

验证四条路径：
  A. SLIME_SESSION 有效   → 直连，不应向 Discord 发请求
  B. SLIME_SESSION 失效   → 自动走 Discord OAuth 换新 session
  C. 无 SESSION + 无效 Token → 退出码 4
  D. 无 SESSION + 有效 Token → 纯 Discord 登录

不联网，不需要真实账号。用法：python tests/test_flow.py
"""
import os, sys, json, shutil, subprocess, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RENEW_PY = os.path.join(ROOT, "renew.py")

VALID_SID = "VALID_SESSION_ABC"
EXPIRED_SID = "EXPIRED_SESSION_XYZ"
MOCK_SID = "MOCK_NEW_SESSION_123"
GOOD_TOKEN = "MOCK_DISCORD_TOKEN_OK"
BAD_TOKEN = "MOCK_DISCORD_TOKEN_BAD"
BALANCE = 123
HOURS_LEFT = 10          # < RENEW_HOURS(24) → 应触发续期
RENEW_THRESHOLD = 50     # BALANCE(123) >= 50 → 余额够
CLIENT_ID = "1267847469501513799"
SCOPE = "identify email guilds.join"

STATE = {"panel": "", "discord": "", "discord_calls": 0, "renew_calls": 0,
         "balance_calls": 0}

MOCK_USERNAME = "MockUser"      # 面板 dashboard 里渲染的账号名（脱敏后应为 M****r）
DASHBOARD_HTML = f"""<html><body>
<p class="mb-0 d-none d-sm-block navbar-profile-name">{MOCK_USERNAME}</p>
<div id="balance"></div>
<script>balance.textContent = Math.floor({BALANCE} * 100);</script>
</body></html>"""


class Base(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, body=b"", headers=None, ctype="text/html; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or []):
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _redirect(self, location, cookies=None):
        self._send(302, b"", [("Location", location)] + (cookies or []))

    def _cookie(self):
        out = {}
        for part in self.headers.get("Cookie", "").split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                out[k] = v
        return out


class PanelHandler(Base):
    def _authed(self):
        return self._cookie().get("connect.sid") in (VALID_SID, MOCK_SID)

    def do_GET(self):
        u = urlparse(self.path)
        p, q = u.path, parse_qs(u.query)

        if p == "/login":
            # SlimeNodes 真实行为：302 到 Discord 授权页，不带 state、不预设 cookie
            url = (f"{STATE['discord']}/api/oauth2/authorize?client_id={CLIENT_ID}"
                   f"&redirect_uri={STATE['panel']}/callback&response_type=code"
                   f"&scope={SCOPE.replace(' ', '%20')}")
            return self._redirect(url)

        if p == "/callback":
            # 真实面板行为：只返回一个靠 JS 跳转的中间页，
            # 真正处理登录的是 /submitlogin（curl 不执行 JS，脚本必须自己跟进）
            code = q.get("code", [""])[0]
            html = ("<html><body><script>"
                    f"window.location.replace('/submitlogin?code={code}')"
                    "</script></body></html>")
            return self._send(200, html)

        if p == "/submitlogin":
            if q.get("code", [""])[0] == "MOCKCODE":
                # HttpOnly 用于验证 jar 解析能剥掉 #HttpOnly_ 前缀
                return self._redirect("/dashboard", [
                    ("Set-Cookie", f"connect.sid={MOCK_SID}; Path=/; HttpOnly")
                ])
            return self._redirect("/login")

        if p == "/dashboard":
            STATE["balance_calls"] += 1
            if not self._authed():
                return self._redirect("/login?redirect=dashboard")
            return self._send(200, DASHBOARD_HTML)

        if p == "/lastrenew":
            end_ms = int((time.time() + HOURS_LEFT * 3600) * 1000)
            return self._send(200, json.dumps({"lastrenew": end_ms}),
                              ctype="application/json; charset=utf-8")

        if p == "/renew":
            STATE["renew_calls"] += 1
            if not self._authed():
                return self._redirect("/login")
            return self._redirect("/dashboard?success=RENEWED")

        self._send(404, "panel: not found")


class DiscordHandler(Base):
    def do_GET(self):
        # 真实 Discord 对未登录的 authorize 请求会给出登录页，这里用 200 页面模拟
        if urlparse(self.path).path == "/api/oauth2/authorize":
            return self._send(200, "<html><title>Discord</title>请登录 Discord</html>")
        self._send(404, "discord: not found")

    def do_POST(self):
        if urlparse(self.path).path == "/api/v9/oauth2/authorize":
            STATE["discord_calls"] += 1
            token = self.headers.get("authorization", "")
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            if token != GOOD_TOKEN:
                return self._send(401, json.dumps({"message": "401: Unauthorized", "code": 0}),
                                  ctype="application/json; charset=utf-8")
            return self._send(200, json.dumps({
                "location": f"{STATE['panel']}/callback?code=MOCKCODE"
            }), ctype="application/json; charset=utf-8")
        self._send(404, "discord: not found")


def start_server(handler, key):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    STATE[key] = f"http://127.0.0.1:{srv.server_address[1]}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def run_case(name, env_extra, expect_exit, expect_discord_calls=None, expect_renew=True,
             expect_log=()):
    tmp = tempfile.mkdtemp(prefix="slimetest_")
    env = os.environ.copy()
    env.update({
        "SLIME_BASE": STATE["panel"],
        "DISCORD_API_BASE": f"{STATE['discord']}/api/v9",
        "TEMP": tmp, "TMPDIR": tmp,
        "TG_BOT_TOKEN": "", "TG_CHAT_ID": "", "GH_TOKEN": "",
        "SERVER_ID": "10102", "RENEW_HOURS": "24",
        "RENEW_THRESHOLD": str(RENEW_THRESHOLD), "ACCOUNT_LABEL": "btpphlmb",
    })
    for k in ("SLIME_SESSION", "DISCORD_TOKEN"):
        env.pop(k, None)
    env.update(env_extra)

    before_dc, before_rn = STATE["discord_calls"], STATE["renew_calls"]
    p = subprocess.run([sys.executable, RENEW_PY], capture_output=True, text=True,
                       env=env, timeout=120)
    out = p.stdout + p.stderr
    dc = STATE["discord_calls"] - before_dc
    rn = STATE["renew_calls"] - before_rn

    checks = [("退出码", p.returncode, expect_exit)]
    if expect_discord_calls is not None:
        checks.append(("Discord 授权请求次数", dc, expect_discord_calls))
    if expect_renew:
        checks.append(("续期请求次数", rn, 1))
    for frag in expect_log:
        checks.append((f"日志含 {frag!r}", frag in out, True))

    failed = [(n, a, e) for n, a, e in checks if a != e]
    print(f"\n{'='*64}\n[{'PASS' if not failed else 'FAIL'}] {name}\n{'='*64}")
    for n, a, e in checks:
        print(f"  {'✓' if a == e else '✗'} {n}: 实际={a!r} 期望={e!r}")
    if failed:
        print("  ---- 脚本输出 ----")
        print("\n".join("  | " + l for l in out.strip().splitlines()[-45:]))
    else:
        for l in out.strip().splitlines():
            if any(k in l for k in ("✅", "🔑", "🔄", "❌", "登录", "续期", "余额", "剩余")):
                print(f"  | {l}")
    shutil.rmtree(tmp, ignore_errors=True)
    return not failed


def main():
    srv_panel = start_server(PanelHandler, "panel")
    srv_discord = start_server(DiscordHandler, "discord")
    print(f"mock 面板:   {STATE['panel']}")
    print(f"mock Discord: {STATE['discord']}")

    results = []

    results.append(run_case(
        "A. SLIME_SESSION 有效（快路径，不应访问 Discord）",
        {"SLIME_SESSION": VALID_SID, "DISCORD_TOKEN": GOOD_TOKEN},
        expect_exit=0, expect_discord_calls=0, expect_renew=True,
        expect_log=("SESSION_TOKEN 有效", "Server renewed!", "M****r"),
    ))

    results.append(run_case(
        "B. SLIME_SESSION 失效 → 自动 Discord 关联登录",
        {"SLIME_SESSION": EXPIRED_SID, "DISCORD_TOKEN": GOOD_TOKEN},
        expect_exit=0, expect_discord_calls=1, expect_renew=True,
        expect_log=("SLIME_SESSION 已失效", "Discord 登录成功", "Server renewed!",
                    "🔐 登录方式: DISCORD_TOKEN"),
    ))

    results.append(run_case(
        "C. 无 SLIME_SESSION + 无效 Discord Token → 退出码 4",
        {"DISCORD_TOKEN": BAD_TOKEN},
        expect_exit=4, expect_discord_calls=1, expect_renew=False,
        expect_log=("Discord 授权失败", "Discord 关联登录失败"),
    ))

    results.append(run_case(
        "D. 无 SLIME_SESSION + 有效 Discord Token → 纯 Discord 登录",
        {"DISCORD_TOKEN": GOOD_TOKEN},
        expect_exit=0, expect_discord_calls=1, expect_renew=True,
        expect_log=("Discord 登录成功", "Server renewed!", "M****r"),
    ))

    srv_panel.shutdown()
    srv_discord.shutdown()
    passed = sum(results)
    print(f"\n{'='*64}\n结果: {passed}/{len(results)} 通过\n{'='*64}")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
