"""登录弹窗三个动作的返回值契约回归测试。

线上第二处「点了没反应」在「提交验证码」:``weblogin.submit_code()`` 返回的是截图
bytes(Streamlit 版 ``app.py`` 直接把它当图用),而 ``ui.py`` 里按
``m, img = sess.submit_code(...)`` 解包 → ``ValueError: too many values to unpack``。

修法:抽出 ``_login_step()``,由它统一把三个分支包成 ``(文案, 截图)`` 二元组。
这里用假会话跑,不启浏览器、不启服务。
"""

import asyncio
import base64
import io
import os
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
        self.alive = True
        self.calls = []

    def submit_code(self, kind, code):
        self.calls.append(("submit_code", kind, code))
        return b"PNG-BYTES"          # 真实返回就是截图 bytes,不是二元组

    def auto_login(self, acct, pwd, secret):
        self.calls.append(("auto_login", acct, pwd, secret))
        return "已提交,看截图确认下一步", b"PNG-BYTES"

    def logged_in(self):
        self.calls.append(("logged_in",))
        return self._logged_in

    def shot(self):
        return b"SHOT-BYTES"

    def finish(self):
        """真实实现会写 storage_state.json 并关掉浏览器。"""
        self.calls.append(("finish",))
        self.alive = False


class LoginActionTests(unittest.TestCase):
    def setUp(self):
        self.step = _load_ui_module()["_login_step"]

    def test_code_step_wraps_submit_code_bytes(self):
        """回归:submit_code 只返回 bytes,必须由 _login_step 包成二元组。"""
        sess = _FakeSession()
        m, img = self.step("code", "amazon.com.mx", sess, manual="123456")
        self.assertEqual(sess.calls, [("submit_code", "captcha", "123456"), ("logged_in",)])
        self.assertEqual(m, "已提交,看截图确认下一步")
        self.assertEqual(img, b"PNG-BYTES")

    def test_code_step_prefers_otp_when_page_has_otp_field(self):
        sess = _FakeSession(has_otp=True)
        self.step("code", "amazon.in", sess, manual="654321")
        self.assertIn(("submit_code", "otp", "654321"), sess.calls)

    def test_check_step_returns_no_screenshot_when_logged_in(self):
        m, img = self.step("check", "amazon.com", _FakeSession(logged_in=True))
        self.assertEqual(m, "✅ amazon.com 登录成功,登录态已保存")
        self.assertIsNone(img)

    def test_check_step_returns_screenshot_when_not_logged_in(self):
        m, img = self.step("check", "amazon.co.jp", _FakeSession(logged_in=False))
        self.assertIn("未登录", m)
        self.assertEqual(img, b"SHOT-BYTES")

    def test_login_step_passes_credentials_and_returns_pair(self):
        sess = _FakeSession()
        m, img = self.step("login", "amazon.com", sess, "a@b.com", "pw", "SECRET")
        self.assertEqual(sess.calls[0], ("auto_login", "a@b.com", "pw", "SECRET"))
        self.assertEqual((m, img), ("已提交,看截图确认下一步", b"PNG-BYTES"))

    def test_all_three_steps_return_a_two_tuple(self):
        """三个分支都必须返回二元组,否则调用处 `m, img = ...` 会解包失败。"""
        for kind in ("login", "code", "check"):
            with self.subTest(kind=kind):
                result = self.step(kind, "amazon.com", _FakeSession(),
                                   "a@b.com", "pw", "secret", "123456")
                self.assertIsInstance(result, tuple)
                self.assertEqual(len(result), 2)


class LoginFinishTests(unittest.TestCase):
    """回归:登录成功后必须 sess.finish(),否则「已保存」是句空话。

    finish() 写 storage_state.json —— login_status()(侧边栏 x/6 计数)读的就是它;
    不写的话按钮提示登录成功、侧边栏却永远显示未登录,而且浏览器一直占着档案目录,
    检测引擎随后起浏览器会撞 profile 锁。Streamlit 版 app.py 就是这么做的
    (第 868/876 行),迁移到 ui.py 时漏掉了。
    """

    def setUp(self):
        self.step = _load_ui_module()["_login_step"]

    def test_login_success_finishes_the_session(self):
        sess = _FakeSession(logged_in=True)
        m, img = self.step("login", "amazon.com", sess, "a@b.com", "pw")
        self.assertEqual(m, "✅ amazon.com 登录成功,登录态已保存")
        self.assertIsNone(img)                      # 登录成了就不用再推截图
        self.assertIn(("finish",), sess.calls)
        self.assertFalse(sess.alive)

    def test_login_failure_keeps_the_session_open(self):
        """没登上就绝不能关:用户还要接着提交验证码,会话得留着。"""
        sess = _FakeSession(logged_in=False)
        m, img = self.step("login", "amazon.com", sess, "a@b.com", "pw")
        self.assertNotIn(("finish",), sess.calls)
        self.assertTrue(sess.alive)
        self.assertEqual(img, b"PNG-BYTES")         # 失败时必须给截图,让人看停在哪一步

    def test_submitting_the_last_code_finishes_the_session(self):
        """填完验证码刚好登上,同样要收尾 —— 这是最常见的成功路径。"""
        sess = _FakeSession(logged_in=True)
        m, img = self.step("code", "amazon.com", sess, manual="123456")
        self.assertEqual(m, "✅ amazon.com 登录成功,登录态已保存")
        self.assertIsNone(img)
        self.assertIn(("finish",), sess.calls)

    def test_logged_in_is_checked_before_finish(self):
        """顺序不能反:finish() 会把 ctx 关掉,之后再问 logged_in() 就没意义了。"""
        sess = _FakeSession(logged_in=True)
        self.step("check", "amazon.com", sess)
        self.assertLess(sess.calls.index(("logged_in",)), sess.calls.index(("finish",)))


class LoginStatusCacheTests(unittest.TestCase):
    """回归:登录成功后要清 login_status() 的 30s 缓存,否则侧边栏数字不更新。"""

    def test_forget_login_status_drops_the_cache(self):
        ns = _load_ui_module()
        ns["_login_status_cache"] = (0.0, {"amazon.com": {"ok": True, "days": 0}})
        ns["forget_login_status"]()
        self.assertIsNone(ns["_login_status_cache"])


class LoginScreenshotTests(unittest.TestCase):
    """回归:ui.image() 不接受 bytes,截图必须先转 data URL。

    NiceGUI 3.6 的 ui.image 签名是 Union[str, Path, PIL_Image];直接喂
    page.screenshot() 的 bytes,元素能建出来,但发消息时 orjson 抛
    "Type is not JSON serializable: bytes" —— 截图永远显示不出来,
    还会连带丢掉同一批里的其他界面更新(整批 update 都没发出去)。
    """

    def setUp(self):
        self.convert = _load_ui_module()["_shot_data_url"]

    @staticmethod
    def _noise_png(width: int, height: int) -> bytes:
        """随机噪声 = 最不可压缩的极端输入,用来卡体积上界。"""
        from PIL import Image
        image = Image.frombytes("RGB", (width, height), os.urandom(width * height * 3))
        buffer = io.BytesIO()
        image.save(buffer, "PNG")
        return buffer.getvalue()

    def test_converts_to_a_decodable_jpeg_data_url(self):
        from PIL import Image
        url = self.convert(self._noise_png(320, 240))
        self.assertTrue(url.startswith("data:image/jpeg;base64,"))
        raw = base64.b64decode(url.split(",", 1)[1])
        self.assertTrue(raw.startswith(b"\xff\xd8"))       # JPEG 魔数
        self.assertEqual(Image.open(io.BytesIO(raw)).size, (320, 240))

    def test_downscales_oversized_screenshot(self):
        from PIL import Image
        url = self.convert(self._noise_png(3840, 2160))
        image = Image.open(io.BytesIO(base64.b64decode(url.split(",", 1)[1])))
        self.assertLessEqual(max(image.size), 1280)

    def test_output_is_smaller_than_the_raw_png(self):
        png = self._noise_png(1280, 900)
        url = self.convert(png)
        # 实测:1280x900 噪声 PNG 3380KB → 输出 907KB;真实登录页 PNG 49KB → 输出约 25KB
        self.assertLess(len(url), len(png))
        self.assertLess(len(url), 1_000_000)               # 别逼近 socket.io 的 1MB 量级

    def test_falls_back_to_png_when_the_image_is_broken(self):
        url = self.convert(b"not-an-image")
        self.assertTrue(url.startswith("data:image/png;base64,"))

    def test_raw_bytes_break_the_payload_but_data_url_does_not(self):
        from nicegui import json as ngjson

        with self.assertRaises(TypeError):
            ngjson.dumps({"src": b"\x89PNG"})              # 老写法:发不出去
        payload = ngjson.dumps({"src": self.convert(self._noise_png(64, 64))})
        self.assertIn("data:image/jpeg;base64,", payload)


class LoginScreenshotElementTests(unittest.TestCase):
    """在真实 NiceGUI 客户端上下文里建元素、走真实序列化器。

    只测 _shot_data_url 的返回值不够:得证明 ui.image() 真的收得下,
    并且元素真的能序列化成能发出去的 payload。这一步同时挡住两类问题:
    - 喂 bytes:NiceGUI 的 is_file() 只吞 OSError,Path(bytes) 抛的 TypeError
      会直接从 ui.image() 冒出来(老代码就是这样);
    - payload 过大:orjson 序列化后的体积要留在 socket.io 的 1MB 量级以内。
    """

    PNG = None

    @classmethod
    def setUpClass(cls):
        from nicegui.client import Client
        from nicegui.page import page as Page
        from starlette.requests import Request

        _event_loop()          # Client() 内部要 asyncio.Event(),先保证有当前循环
        scope = {"type": "http", "method": "GET", "path": "/", "headers": [],
                 "query_string": b"", "client": ("127.0.0.1", 1234),
                 "server": ("127.0.0.1", 8765), "scheme": "http",
                 "root_path": "", "http_version": "1.1"}
        cls.client = Client(page=Page('/__login_action_test__'),
                            request=Request(scope))
        cls.PNG = LoginScreenshotTests._noise_png(1280, 900)

    def test_data_url_survives_element_creation_and_serialization(self):
        from nicegui import json as ngjson
        from nicegui import ui

        convert = _load_ui_module()["_shot_data_url"]
        with self.client:
            element = ui.image(convert(self.PNG)).classes("w-full rounded-md")

        payload = ngjson.dumps(element._to_dict())
        self.assertIn("data:image/jpeg;base64,", payload)
        self.assertLess(len(payload), 1_000_000)      # 别逼近 1MB 量级

    def test_raw_bytes_are_rejected_at_the_call_site(self):
        from nicegui import ui

        with self.client:
            with self.assertRaises(TypeError):
                ui.image(b"\x89PNG\r\n\x1a\n")        # 老写法:当场抛


def _test_request():
    from starlette.requests import Request

    scope = {"type": "http", "method": "GET", "path": "/", "headers": [],
             "query_string": b"", "client": ("127.0.0.1", 1234),
             "server": ("127.0.0.1", 8765), "scheme": "http",
             "root_path": "", "http_version": "1.1"}
    return Request(scope)


_LOOP = None


def _event_loop():
    """全模块共用一个事件循环。

    两个原因不能用 asyncio.run:Client() 内部会建 asyncio.Event()(要求当前线程
    有一个事件循环),而 asyncio.run() 跑完会把当前循环清空,后面再建 Client 就
    "There is no current event loop in thread 'MainThread'"。
    """
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP


class LoginDialogFlowTests(unittest.TestCase):
    """把 login_dialog() 真建在客户端里,再真点一次「检测登录态」。

    前面的用例只卡 _login_step 的返回值契约;这里跑的是三个按钮共用的整条回调链:
    禁用+loading → 丢专属线程 → 回填文案 → 建截图元素 → 刷新下拉框和侧边栏 →
    finally 复位按钮。这条链上任何一环写错(元素删了还去改、props 语法不对、
    refresh 抛异常)都会让用户看到"点了没反应",而单测 _login_step 是发现不了的。

    假会话 + 假 _open_session,不启浏览器、不启服务、不碰真实档案。
    """

    DOMAIN = "amazon.com"

    def setUp(self):
        from nicegui.client import Client
        from nicegui.page import page as Page

        self.ns = _load_ui_module()
        self.loop = _event_loop()
        self.client = Client(page=Page('/__login_dialog_flow__'), request=_test_request())

        self.patched = {}

        def patch(name, value):
            self.patched.setdefault(name, self.ns[name])
            self.ns[name] = value

        self.addCleanup(lambda: [self.ns.__setitem__(k, v)
                                 for k, v in self.patched.items()])

        self.opened, self.forgotten, self.refreshed = [], [], []
        patch("forget_login_status", lambda: self.forgotten.append(True))

        # 侧边栏换成真的 @ui.refreshable 假件:不渲染真 sidebar_nav()
        # (它要 app.storage.user,裸 Client 里没有请求上下文),
        # 但走的仍是 refresh() → AwaitableResponse → background_tasks 那条真路径,
        # 所以"core.loop 没设 / refresh 抛异常"这类问题还是会被抓出来。
        @self.ns["ui"].refreshable
        def fake_sidebar():
            self.refreshed.append(True)
            self.ns["ui"].label("fake-sidebar")

        patch("sidebar_nav", fake_sidebar)
        self.fake_sidebar = fake_sidebar

        self.sess = None

        def open_session(dom):
            self.opened.append(dom)
            return self.sess

        patch("_open_session", open_session)

    # ---- 操作辅助 ----

    def _build(self, logged_in: bool):
        self.sess = _FakeSession(logged_in=logged_in)
        with self.client:
            self.fake_sidebar()      # 先渲染一次,refresh() 才有目标
            self.ns["login_dialog"]()
        self._select(self.DOMAIN)

    def _select(self, domain):
        from nicegui.elements.select import Select

        for el in self.client.elements.values():
            if isinstance(el, Select):
                el.value = domain
                return el
        self.fail("登录弹窗里没找到站点下拉框")

    def _button(self, text):
        from nicegui.elements.button import Button

        for el in self.client.elements.values():
            if isinstance(el, Button) and el.text == text:
                return el
        self.fail(f"登录弹窗里没找到按钮 {text!r}")

    def _click(self, text):
        """走线上那条真实链路:Button.on_click → handle_event → 后台任务。

        唯一的手脚是把 core.loop 指到临时循环上,好把异步回调 await 完 ——
        真跑起来时它就是服务自己的事件循环。
        """
        from nicegui import background_tasks, core

        button = self._button(text)
        for listener in button._event_listeners.values():
            if getattr(listener, "type", None) != 'click':
                continue

            async def main():
                core.loop = asyncio.get_running_loop()
                try:
                    listener.handler(None)
                    for _ in range(100):
                        pending = list(background_tasks.running_tasks)
                        if not pending:
                            break
                        await asyncio.gather(*pending, return_exceptions=True)
                finally:
                    core.loop = None

            self.loop.run_until_complete(main())
            return button
        self.fail(f"按钮 {text!r} 没挂 click 回调")
    def _texts(self):
        return [t for t in (getattr(el, "text", None)
                            for el in self.client.elements.values()) if t]

    def _input(self, label):
        for el in self.client.elements.values():
            if el._props.get("label") == label:
                return el
        self.fail(f"登录弹窗里没找到输入框 {label!r}")

    def _images(self):
        from nicegui.elements.image import Image

        return [el for el in self.client.elements.values() if isinstance(el, Image)]

    # ---- 用例 ----

    def test_check_button_round_trip_when_already_logged_in(self):
        self._build(logged_in=True)
        button = self._click("检测登录态")

        self.assertEqual(self.opened, [self.DOMAIN], "必须走该域名的专属会话")
        self.assertIn("✅", " ".join(self._texts()))
        self.assertIn(("finish",), self.sess.calls, "登上就得落盘,否则侧边栏永远显示未登录")
        self.assertTrue(self.forgotten, "得清 login_status 的 30s 缓存")
        self.assertGreaterEqual(len(self.refreshed), 2,
                                "得真的把侧边栏重渲染一遍(1 次初次渲染 + 1 次刷新)")
        self.assertEqual(self._images(), [], "已登录就不用再推截图")
        self.assertNotIn("disable", button._props, "跑完必须把按钮恢复可点")
        self.assertNotIn("loading", button._props)

    def test_check_button_shows_screenshot_when_not_logged_in(self):
        self._build(logged_in=False)
        self._click("检测登录态")

        self.assertIn("未登录", " ".join(self._texts()))
        self.assertNotIn(("finish",), self.sess.calls, "没登上不能关会话")
        images = self._images()
        self.assertEqual(len(images), 1, "失败时必须给截图,让人看停在哪一步")
        self.assertTrue(images[0]._props["src"].startswith("data:image/"),
                        "截图必须以 data URL 进元素,喂 bytes 会当场抛 TypeError")

    def test_all_three_buttons_survive_a_round_trip(self):
        """三个按钮都得能点完并复位 —— 这正是一开始报的「点了没反应」。"""
        self._build(logged_in=False)
        for text in ("开始登录", "提交验证码", "检测登录态"):
            with self.subTest(button=text):
                button = self._click(text)
                self.assertNotIn("disable", button._props)
                self.assertNotIn("loading", button._props)

    def test_credentials_are_forwarded_to_auto_login(self):
        self._build(logged_in=False)
        for label, value in (("Amazon 账号", "a@b.com"),
                             ("账号密码", "pw"),
                             ("TOTP 密钥(可选)", "SECRET")):
            self._input(label).value = value
        self._click("开始登录")
        self.assertEqual(self.sess.calls[0], ("auto_login", "a@b.com", "pw", "SECRET"))


if __name__ == "__main__":
    unittest.main()
