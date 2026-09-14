"""登录弹窗三个动作的返回值契约回归测试。

线上第二处「点了没反应」在「提交验证码」:``weblogin.submit_code()`` 返回的是截图
bytes(Streamlit 版 ``app.py`` 直接把它当图用),而 ``ui.py`` 里按
``m, img = sess.submit_code(...)`` 解包 → ``ValueError: too many values to unpack``。

修法:抽出 ``_login_step()``,由它统一把三个分支包成 ``(文案, 截图)`` 二元组。
这里用假会话跑,不启浏览器、不启服务。
"""

import base64
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_login_thread import _load_ui_module


class _FakePage:
    """query_selector 恒返回 None(当前不是 OTP 页 → 走 captcha 分支)。"""

    def __init__(self, has_otp: bool = False):
        self.has_otp = has_otp

    def query_selector(self, _selector):
        return object() if self.has_otp else None


class _FakeSession:
    def __init__(self, has_otp: bool = False, logged_in: bool = False):
        self.page = _FakePage(has_otp)
        self.last_msg = "已提交,看截图确认下一步"
        self._logged_in = logged_in
        self.calls = []

    def submit_code(self, kind, code):
        self.calls.append(("submit_code", kind, code))
        return b"PNG-BYTES"          # 真实返回就是截图 bytes,不是二元组

    def auto_login(self, acct, pwd, secret):
        self.calls.append(("auto_login", acct, pwd, secret))
        return "已提交,看截图确认下一步", b"PNG-BYTES"

    def logged_in(self):
        return self._logged_in

    def shot(self):
        return b"SHOT-BYTES"


class LoginActionTests(unittest.TestCase):
    def setUp(self):
        self.step = _load_ui_module()["_login_step"]

    def test_code_step_wraps_submit_code_bytes(self):
        """回归:submit_code 只返回 bytes,必须由 _login_step 包成二元组。"""
        sess = _FakeSession()
        m, img = self.step("code", "amazon.com.mx", sess, manual="123456")
        self.assertEqual(sess.calls, [("submit_code", "captcha", "123456")])
        self.assertEqual(m, "已提交,看截图确认下一步")
        self.assertEqual(img, b"PNG-BYTES")

    def test_code_step_prefers_otp_when_page_has_otp_field(self):
        sess = _FakeSession(has_otp=True)
        self.step("code", "amazon.in", sess, manual="654321")
        self.assertEqual(sess.calls, [("submit_code", "otp", "654321")])

    def test_check_step_returns_no_screenshot_when_logged_in(self):
        m, img = self.step("check", "amazon.com", _FakeSession(logged_in=True))
        self.assertEqual(m, "✅ amazon.com 登录态已保存")
        self.assertIsNone(img)

    def test_check_step_returns_screenshot_when_not_logged_in(self):
        m, img = self.step("check", "amazon.co.jp", _FakeSession(logged_in=False))
        self.assertEqual(m, "未登录")
        self.assertEqual(img, b"SHOT-BYTES")

    def test_login_step_passes_credentials_and_returns_pair(self):
        sess = _FakeSession()
        m, img = self.step("login", "amazon.com", sess, "a@b.com", "pw", "SECRET")
        self.assertEqual(sess.calls, [("auto_login", "a@b.com", "pw", "SECRET")])
        self.assertEqual((m, img), ("已提交,看截图确认下一步", b"PNG-BYTES"))

    def test_all_three_steps_return_a_two_tuple(self):
        """三个分支都必须返回二元组,否则调用处 `m, img = ...` 会解包失败。"""
        for kind in ("login", "code", "check"):
            with self.subTest(kind=kind):
                result = self.step(kind, "amazon.com", _FakeSession(),
                                   "a@b.com", "pw", "secret", "123456")
                self.assertIsInstance(result, tuple)
                self.assertEqual(len(result), 2)


class LoginScreenshotTests(unittest.TestCase):
    """回归:ui.image() 不接受 bytes,截图必须先转 data URL。

    NiceGUI 3.6 的 ui.image 签名是 Union[str, Path, PIL_Image];直接喂
    page.screenshot() 的 bytes,元素能建出来,但发消息时 orjson 抛
    "Type is not JSON serializable: bytes" —— 截图永远显示不出来,
    还会连带丢掉同一批里的其他界面更新(整批 update 都没发出去)。
    """

    def setUp(self):
        self.ns = _load_ui_module()

    def test_png_data_url_round_trips(self):
        png = b"\x89PNG\r\n\x1a\n"
        url = self.ns["_png_data_url"](png)
        self.assertTrue(url.startswith("data:image/png;base64,"))
        self.assertEqual(base64.b64decode(url.split(",", 1)[1]), png)

    def test_raw_bytes_break_the_payload_but_data_url_does_not(self):
        from nicegui import json as ngjson

        with self.assertRaises(TypeError):
            ngjson.dumps({"src": b"\x89PNG"})          # 老写法:发不出去
        payload = ngjson.dumps({"src": self.ns["_png_data_url"](b"\x89PNG")})
        self.assertIn("data:image/png;base64,", payload)


if __name__ == "__main__":
    unittest.main()
