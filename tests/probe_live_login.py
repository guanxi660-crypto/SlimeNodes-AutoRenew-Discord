"""只读校验：确认线上面板 /login 的跳转能被正确解析（不访问 Discord）。"""
import importlib.util, sys
from urllib.parse import urlparse, parse_qs

spec = importlib.util.spec_from_file_location("r", "renew.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

url = m._discord_authorize_url()
print("authorize url:", url)
q = parse_qs(urlparse(url).query)
print("client_id   :", q.get("client_id"))
print("redirect_uri:", q.get("redirect_uri"))
print("scope       :", q.get("scope"))
print("state       :", q.get("state", ["(无 state)"]))
print("API 目标     :", f"{m.DISCORD_API_BASE}/oauth2/authorize?{urlparse(url).query}"[:110])
