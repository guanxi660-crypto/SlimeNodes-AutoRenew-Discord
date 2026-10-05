"""校验 workflow YAML：语法合法性 + 关键字段（不联网）。

用 pip 装过 PyYAML 才能跑；环境里没有就退化为最小解析检查。
"""
import sys

path = ".github/workflows/renew.yml"
raw = open(path, "rb").read()

print(f"文件: {path}")
print(f"大小: {len(raw)} bytes")
print(f"行尾: CRLF={raw.count(b'\r\n')} LF={raw.count(b'\n')}")
if raw.count(b"\r\n"):
    print("❌ 存在 CRLF，.gitattributes 要求 eol=lf")
    sys.exit(1)

try:
    import yaml
except ImportError:
    print("⚠️ 未安装 PyYAML，跳过结构化解析")
    sys.exit(0)

d = yaml.safe_load(raw)
print("YAML 解析: OK")
print(f"name: {d['name']}")

# YAML 1.1 会把裸键 on 解析成布尔 True，这里两种写法都取一遍
on = d.get("on") or d.get(True)
if on is None:
    print("❌ 找不到 on 触发器")
    sys.exit(1)
print(f"cron: {on['schedule']}")
print(f"workflow_dispatch: {'有' if 'workflow_dispatch' in on else '无'}")
print(f"permissions: {d['permissions']}")

for job, cfg in d["jobs"].items():
    print(f"\njob {job}:")
    print(f"  runs-on: {cfg['runs-on']}")
    print(f"  if: {cfg.get('if', '(无)')}")
    print(f"  needs: {cfg.get('needs', '(无)')}")
    print(f"  continue-on-error: {cfg.get('continue-on-error', False)}")

steps = d["jobs"]["renew-server"]["steps"]
print(f"\nrenew-server steps ({len(steps)}):")
for s in steps:
    label = s.get("uses") or s.get("name")
    print(f"  - {label}")

run_script = steps[1]["run"]
print(f"\nrun 脚本末尾 3 行:")
for line in run_script.rstrip().splitlines()[-3:]:
    print(f"  | {line}")

# 关键断言
assert d["jobs"]["renew-server"]["runs-on"] == "ubuntu-24.04", "runs-on 不是固定标签"
assert steps[0]["uses"] == "actions/checkout@v6", "checkout 版本不对"
assert run_script.rstrip().endswith("exit $code"), "退出码未透传"
assert "always()" not in str(d["jobs"]["cleanup-runs"].get("if", "")), "cleanup 仍是 always()"
print("\n✅ 所有关键断言通过")
