"""网页版引导登录:浏览器跑在服务器上,截图推给网页,人在网页上输验证码完成登录。

登录态存服务器档案 ~/.amreview/profile/<域名>/,之后所有检测静默复用。
迷你项目约定:同时在线 ≤3 人,不加锁——同一域名同时只开一个登录会话即可。
"""

from __future__ import annotations

import os
import random
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

PROFILE_ROOT = Path.home() / ".amreview" / "profile"


def _browser_args() -> list[str]:
    """服务器常以 root 运行浏览器,不带 --no-sandbox 会被 Chromium/Chrome 拒绝启动。"""
    return ["--no-sandbox"] if hasattr(os, "geteuid") and os.geteuid() == 0 else []
# 各市场登录 cookie 后缀不同(at-main/x-main 为主,日本/中国等为 at-jp/at-cn 等),按前缀判定
def _is_login_cookie(name: str) -> bool:
    return name.startswith("at-") or name.startswith("x-")

# Amazon 登录各步输入框(按 id/name 依次找可见的那个)
FILL_FIELDS = {
    "email": ["#ap_email", 'input[name="email"]'],
    "password": ["#ap_password", 'input[type="password"]'],
    "otp": ["#auth-mfa-otpcode", 'input[name="otpCode"]',
            'input[autocomplete="one-time-code"]'],  # 含印度站等变体
    "captcha": ["#auth-captcha-guess", 'input[name="guess"]'],
}
# 各步的提交按钮(点第一个可见的;隐藏的不点,避免在密码页误点 email 页的 continue)
SUBMIT_BUTTONS = ["#auth-signin-button", "#signInSubmit", "#continue",
                  'input[type="submit"]', 'button[type="submit"]']


def totp_code(secret: str) -> str:
    """用 TOTP 密钥算当前 6 位 OTP;临近过期窗口时等下一窗口,避免填完就过期。"""
    import pyotp
    remaining = 30 - (time.time() % 30)
    if remaining < 3:
        time.sleep(remaining + 0.5)
    return pyotp.TOTP(secret.strip().replace(" ", "")).now()


def _pause(a: float = 0.6, b: float = 1.8) -> None:
    """步骤间随机停顿,模拟人的反应时间。"""
    time.sleep(random.uniform(a, b))


def _human_type(el, text: str) -> None:
    """逐字符输入并发键盘事件(fill() 是瞬间设值且无按键事件,最易被识别)。"""
    for ch in text:
        el.type(ch, delay=random.randint(30, 90))
        if random.random() < 0.1:  # 偶尔停顿,像人打字
            time.sleep(random.uniform(0.2, 0.5))


class LoginSession:
    """一个域名的服务器端登录浏览器,截图推给 Streamlit 展示。"""

    def __init__(self, domain: str):
        self.domain = domain
        self.profile = PROFILE_ROOT / domain
        self.profile.mkdir(parents=True, exist_ok=True)
        self.pw = sync_playwright().start()
        kwargs = dict(user_data_dir=str(self.profile), headless=True,
                      locale="en-US", viewport={"width": 1280, "height": 900},
                      args=_browser_args())
        try:
            self.ctx = self.pw.chromium.launch_persistent_context(channel="chrome", **kwargs)
        except Exception:
            self.ctx = self.pw.chromium.launch_persistent_context(**kwargs)
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.alive = True
        self.last_msg = "登录页未打开"

    # ---- 基础动作(每个都返回最新截图 bytes,供网页展示) ----

    def open_login_page(self) -> bytes:
        self.page.goto(f"https://www.{self.domain}/gp/sign-in.html",
                       wait_until="domcontentloaded", timeout=30000)
        self.last_msg = "登录页已打开"
        return self.shot()

    def fill(self, kind: str, value: str) -> str:
        for sel in FILL_FIELDS.get(kind, []):
            el = self.page.query_selector(sel)
            if el and el.is_visible():
                el.click()
                _pause(0.2, 0.6)
                _human_type(el, value)
                return f"已填入{kind}"
        return f"页面上没看到{kind}输入框(截图确认当前步骤)"

    def _click_visible_submit(self) -> None:
        for sel in SUBMIT_BUTTONS:
            el = self.page.query_selector(sel)
            if el and el.is_visible():
                el.click()
                return

    def click_submit(self) -> bytes:
        self._click_visible_submit()
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass
        time.sleep(2)  # 等登录跳转
        self.last_msg = "已提交,看截图确认下一步"
        return self.shot()

    def auto_login(self, account: str, password: str, totp_secret: str = "") -> tuple[str, bytes]:
        """一键填登:账号 → 密码 → 提交;若停在 OTP 页且有 TOTP 密钥则自动填码续登。"""
        msgs = []
        self.open_login_page()
        _pause(0.8, 2.0)
        msgs.append(self.fill("email", account))
        _pause(0.5, 1.2)
        cont = self.page.query_selector("#continue")
        if cont and cont.is_visible():
            cont.click()
        self.page.wait_for_load_state("domcontentloaded", timeout=15000)
        _pause(0.8, 1.8)
        msgs.append(self.fill("password", password))
        rm = self.page.query_selector('input[name="rememberMe"]')
        if rm:
            try:
                rm.check()
            except Exception:
                pass
        _pause(0.6, 1.5)
        self._click_visible_submit()
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass
        _pause(1.5, 2.5)

        # OTP 页:有 TOTP 密钥则自动算码填入,无需人工中继
        if totp_secret and self.page.query_selector(
                "#auth-mfa-otpcode, input[name='otpCode']"):
            msgs.append(self.fill("otp", totp_code(totp_secret)) + "(TOTP 自动)")
            _pause(0.5, 1.2)
            self._click_visible_submit()
            try:
                self.page.wait_for_load_state("domcontentloaded", timeout=15000)
            except Exception:
                pass
            time.sleep(2)

        self.last_msg = "; ".join(msgs) + " · 若停在验证码页,需人工在网页上中继"
        return self.last_msg, self.shot()

    def submit_code(self, kind: str, code: str) -> bytes:
        self.fill(kind, code)
        return self.click_submit()

    # ---- 状态 ----

    def logged_in(self) -> bool:
        return any(_is_login_cookie(c["name"]) for c in self.ctx.cookies())

    def shot(self) -> bytes:
        try:
            return self.page.screenshot()
        except Exception:
            return b""

    def finish(self):
        """登录成功后调用:落盘保险副本并关闭,释放档案给检测引擎。"""
        try:
            self.ctx.storage_state(path=str(self.profile / "storage_state.json"))
        except Exception:
            pass
        self.close()

    def close(self):
        if not self.alive:
            return
        self.alive = False
        try:
            self.ctx.close()
        except Exception:
            pass
        try:
            self.pw.stop()
        except Exception:
            pass


# 模块级注册表:Streamlit 每次交互重跑脚本,但模块缓存不重载,会话得以存活
ACTIVE: dict[str, LoginSession] = {}


def get_session(domain: str) -> LoginSession:
    """取或建该域名的登录会话;同一域名同时只允许一个。"""
    sess = ACTIVE.get(domain)
    if sess and sess.alive:
        return sess
    sess = LoginSession(domain)
    ACTIVE[domain] = sess
    return sess


def close_domains(domains: set[str]) -> None:
    """关闭指定域名的登录会话,释放档案给检测引擎;不影响其他域名的会话。"""
    for d in domains:
        sess = ACTIVE.pop(d, None)
        if sess:
            sess.close()


def close_all():
    """关闭全部登录会话(等价于 close_domains 传所有域名)。"""
    close_domains(set(ACTIVE))
