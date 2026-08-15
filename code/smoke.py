"""手工回归冒烟:升级 Playwright / 改动引擎后跑一遍,全绿即无恙。

用法:
  .venv/bin/python smoke.py          # 快速:编译 + 解析单测(离线)
  .venv/bin/python smoke.py --live   # 完整:再加一次真实检测(假 ID 应判 🐕)
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

CODE = Path(__file__).parent
PY = sys.executable


def step(name: str, fn) -> bool:
    try:
        fn()
        print(f"✅ {name}")
        return True
    except Exception as e:
        print(f"❌ {name}: {e}")
        return False


def check_compile():
    for f in ("app.py", "engine.py", "weblogin.py", "login.py"):
        r = subprocess.run([PY, "-m", "py_compile", str(CODE / f)],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


def check_imports():
    sys.path.insert(0, str(CODE))
    import engine, weblogin  # noqa: F401


def check_parse():
    sys.path.insert(0, str(CODE))
    from engine import parse_links
    refs = parse_links(
        "https://www.amazon.com/gp/customer-reviews/R1ABC12345/\n"
        "https://www.amazon.in/review/RQWX9100MIJ7A\n"
        "https://www.amazon.com.mx/portal/customer-reviews/srp/-/R2XYZ98765\n"
        "https://www.amazon.evil.com/gp/customer-reviews/R9BAD00000/\n"  # 非法域名,应被拒
        "R3BARE0000X\n垃圾行\n")
    ids = [(r.review_id, r.domain) for r in refs]
    assert ids == [
        ("R1ABC12345", "amazon.com"),
        ("RQWX9100MIJ7A", "amazon.in"),
        ("R2XYZ98765", "amazon.com.mx"),
    ], f"解析结果不符: {ids}"


def check_totp():
    sys.path.insert(0, str(CODE))
    import pyotp
    from weblogin import totp_code
    s = pyotp.random_base32()
    assert totp_code(s) == pyotp.TOTP(s).now()


def check_live():
    sys.path.insert(0, str(CODE))
    from engine import ReviewChecker, parse_links
    ref = parse_links("https://www.amazon.com/gp/customer-reviews/AFAKEID12345/")[0]
    with ReviewChecker() as checker:
        r = checker.check_batch([ref])[0]
    assert r["status"] == "deleted", f"假 ID 应判已删,实际: {r['status']} ({r['note']})"


if __name__ == "__main__":
    live = "--live" in sys.argv
    ok = all([
        step("编译四个模块", check_compile),
        step("导入引擎/登录模块", check_imports),
        step("链接解析(六国 allowlist + 三格式 + 去重 + 拒非法域名)", check_parse),
        step("TOTP 算码", check_totp),
    ])
    if live:
        ok = step("实测:假 ID 应判 🐕 已删(走真实 Amazon)", check_live) and ok
    print("\n" + ("🎉 冒烟通过" if ok else "💥 有失败项,修复后再部署"))
    sys.exit(0 if ok else 1)
