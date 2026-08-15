"""一次性引导登录:python login.py [域名,默认 amazon.com]

打开可见浏览器窗口 → 人工登录专用买家小号 → 检测到 at-main / x-main
cookie 自动保存到 ~/.amreview/profile/<域名>/ → 关窗。之后 headless 静默跑。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

LOGIN_COOKIE_PREFIXES = ("at-", "x-")  # 各市场后缀不同:at-main/x-main、at-jp、at-cn 等
PROFILE_ROOT = Path.home() / ".amreview" / "profile"
TIMEOUT_SEC = 600  # 10 分钟内完成登录


def bootstrap_login(domain: str) -> bool:
    profile = PROFILE_ROOT / domain
    profile.mkdir(parents=True, exist_ok=True)
    pw = sync_playwright().start()
    kwargs = dict(user_data_dir=str(profile), headless=False, locale="en-US")
    try:
        ctx = pw.chromium.launch_persistent_context(channel="chrome", **kwargs)
    except Exception:
        ctx = pw.chromium.launch_persistent_context(**kwargs)
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.goto(f"https://www.{domain}/gp/sign-in.html")

    print(f"请在打开的浏览器窗口中登录 {domain} 专用买家小号(验证码随意过)…")
    deadline = time.time() + TIMEOUT_SEC
    ok = False
    while time.time() < deadline:
        names = {c["name"] for c in ctx.cookies()}
        if any(n.startswith(LOGIN_COOKIE_PREFIXES) for n in names):
            ok = True
            break
        if not ctx.pages:  # 用户手抖关了窗口
            break
        time.sleep(2)

    if ok:
        # 落盘 cookie(persistent context 退出时自动写档案,这里多留一份保险)
        ctx.storage_state(path=str(profile / "storage_state.json"))
        print(f"✅ 登录态已保存到 {profile},后续检测将静默复用")
    else:
        print("❌ 未检测到登录 cookie(超时或窗口被关闭),可重新运行本脚本")
    ctx.close()
    pw.stop()
    return ok


if __name__ == "__main__":
    domain = sys.argv[1] if len(sys.argv) > 1 else "amazon.com"
    sys.exit(0 if bootstrap_login(domain) else 1)
