"""「添加监控链接」的可见反馈回归测试。

现象:粘贴 6 条商品链接、点「添加」,界面没有任何变化,像没点过一样。
两个成因叠在一起,缺一个都还会复发:

1. ``_add()`` 成功入库后立刻调 ``on_done()`` → ``ui.navigate.to("/monitor")``,
   整页跳转当场销毁刚发出的 toast → 成功提示根本来不及看;
2. 监控页空态只看快照数(``count_snapshots``),而添加监控只写 ``profiles``、
   不产生快照 → 跳转回来照旧是「还没有监控数据」,连刚加的链接都看不到。

这里把 ``add_monitor_dialog()`` 真建在裸 Client 里,走线上真实链路点一次
「添加」,断言:链接真入库、提示真发出、没有立刻跳页、失败时不关窗。

不启服务、不启浏览器、不碰真 monitor.db(全程用临时库)。
"""

import asyncio
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


CODE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE_DIR))
sys.path.insert(0, str(CODE_DIR / "tests"))

from test_login_thread import _load_ui_module  # noqa: E402


# 用户报障时粘贴的那一批(含 .in / .com.mx / .com 三个站点、同 ASIN 跨站点)
LINKS = """https://www.amazon.in/dp/B0HCZCVK67
https://www.amazon.in/dp/B0HCZGQTK6
https://www.amazon.com.mx/dp/B0HCZGZWKL
https://www.amazon.in/dp/B0HCZS8W8F
https://www.amazon.com/dp/B0HCZS8W8F
https://www.amazon.com.mx/dp/B0HG91W26M"""

EXPECTED = {
    ("B0HCZCVK67", "amazon.in"),
    ("B0HCZGQTK6", "amazon.in"),
    ("B0HCZGZWKL", "amazon.com.mx"),
    ("B0HCZS8W8F", "amazon.in"),
    ("B0HCZS8W8F", "amazon.com"),
    ("B0HG91W26M", "amazon.com.mx"),
}

_LOOP = None


def _event_loop():
    """全模块共用一个事件循环。

    不能用 asyncio.run():它跑完会把当前循环清空,之后再建 Client 就
    "There is no current event loop in thread 'MainThread'"。
    """
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP


def _test_request():
    from starlette.requests import Request

    scope = {"type": "http", "method": "GET", "path": "/", "headers": [],
             "query_string": b"", "client": ("127.0.0.1", 1234),
             "server": ("127.0.0.1", 8765), "scheme": "http",
             "root_path": "", "http_version": "1.1"}
    return Request(scope)


class AddMonitorDialogTests(unittest.TestCase):
    def setUp(self):
        from nicegui.client import Client
        from nicegui.page import page as Page

        self.ns = _load_ui_module()
        self.loop = _event_loop()
        self.client = Client(page=Page('/__add_monitor_dialog__'),
                             request=_test_request())

        self.original_db = self.ns["MONITOR_DB"]
        self.db = Path(tempfile.mkdtemp()) / "monitor.db"
        self.ns["MONITOR_DB"] = self.db
        self.addCleanup(self.ns.__setitem__, "MONITOR_DB", self.original_db)

        # 真实环境里 ui.py 模块级已经 init_db 过,默认按真实情况建表;
        # 「表还没建」的场景由 test_fresh_db_... 单独覆盖
        from monitor import store as ms

        ms.init_db(self.db)

        self.notified, self.timers, self.navigated, self.done = [], [], [], []

        ui = self.ns["ui"]
        original = (ui.notify, ui.timer, ui.navigate.to)

        def restore():
            ui.notify, ui.timer, ui.navigate.to = original

        self.addCleanup(restore)

        ui.notify = lambda msg, **kw: self.notified.append((msg, kw.get("type")))
        # 真定时器要等 1.5s,这里只记下参数,由用例决定什么时候触发回调
        ui.timer = lambda delay, cb, **kw: self.timers.append((delay, cb, kw))
        ui.navigate.to = lambda target, new_tab=False: self.navigated.append(target)

    # ---- 操作辅助 ----

    def _open_dialog(self):
        with self.client:
            self.ns["add_monitor_dialog"](lambda: self.done.append(True))

    def _textarea(self):
        from nicegui.elements.textarea import Textarea

        return next(el for el in self.client.elements.values()
                    if isinstance(el, Textarea))

    def _button(self, text):
        from nicegui.elements.button import Button

        for el in self.client.elements.values():
            if isinstance(el, Button) and el.text == text:
                return el
        self.fail(f"弹窗里没找到按钮 {text!r}")

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

    def _label_texts(self):
        from nicegui.elements.label import Label

        return " ".join(el.text or "" for el in self.client.elements.values()
                        if isinstance(el, Label))

    # ---- 用例 ----

    def test_all_six_links_are_stored_and_reported(self):
        """六条链接都得入库,并且给出一句数得清的提示。"""
        self._open_dialog()
        self._textarea().value = LINKS
        self._click("添加")

        from monitor import store as ms

        rows = ms.list_profiles(self.db, only_enabled=False)
        self.assertEqual({(r["asin"], r["domain"]) for r in rows}, EXPECTED)
        self.assertEqual(self.notified, [("已添加 6 条监控", "positive")])

    def test_refresh_is_deferred_so_the_success_toast_survives(self):
        """回归:立刻 on_done() 是整页跳转,会把刚发出的 toast 一起冲掉。"""
        self._open_dialog()
        self._textarea().value = LINKS
        self._click("添加")

        self.assertTrue(self.notified, "得先给用户一条成功提示")
        self.assertEqual(self.done, [], "不能立刻跳页,否则提示还没看见就被冲掉")
        self.assertEqual([t[0] for t in self.timers],
                         [self.ns["ADD_MONITOR_REFRESH_DELAY"]])

        _, callback, kwargs = self.timers[0]
        self.assertTrue(kwargs.get("once"), "一次性定时器,别反复刷页")
        callback()
        self.assertEqual(self.done, [True], "延时到了还是得刷新,否则页面停在旧状态")

    def test_already_tracked_links_are_skipped_and_said_so(self):
        """重复添加不该报错,但要明说跳过了几条 —— 否则看着也像没反应。"""
        self._open_dialog()
        self._textarea().value = LINKS
        self._click("添加")
        self.notified.clear()
        self.done.clear()

        self._textarea().value = LINKS
        self._click("添加")

        from monitor import store as ms

        self.assertEqual(len(ms.list_profiles(self.db, only_enabled=False)), 6)
        self.assertEqual(self.notified, [("跳过已存在 6 条", "warning")])

    def test_unparsable_input_keeps_the_dialog_open_with_a_hint(self):
        self._open_dialog()
        self._textarea().value = "这不是链接"
        self._click("添加")

        self.assertEqual(self.notified, [])
        self.assertIn("未解析到有效商品链接", self._label_texts())
        self.assertEqual(self.done, [], "没入库就不该刷新页面")

    def test_storage_failure_is_shown_instead_of_silently_swallowed(self):
        """回归:入库炸了也要在弹窗里说一声,不能只落在服务端日志里。

        这正是一开始「点了没反应」的成因 —— NiceGUI 未捕获的界面异常默认
        只在服务端报错,浏览器上什么都不显示。
        """
        from monitor import store as ms

        original = ms.get_profile

        def broken(*_args, **_kwargs):
            raise sqlite3.OperationalError("database is locked")

        ms.get_profile = broken
        self.addCleanup(lambda: setattr(ms, "get_profile", original))

        self._open_dialog()
        self._textarea().value = LINKS
        self._click("添加")

        self.assertIn("添加失败", self._label_texts())
        self.assertIn("OperationalError", self._label_texts())
        self.assertEqual(self.notified, [], "一条都没进去就别报成功")
        self.assertEqual(self.done, [], "失败了得让用户看着错误,不能刷掉")

    def test_fresh_db_without_tables_still_accepts_links(self):
        """回归:get_profile 不建表,全新库第一次添加会 no such table: profiles。"""
        self.ns["MONITOR_DB"] = Path(tempfile.mkdtemp()) / "brand-new.db"
        self.db = self.ns["MONITOR_DB"]      # 故意不 init_db

        self._open_dialog()
        self._textarea().value = LINKS
        self._click("添加")

        self.assertEqual(self.notified, [("已添加 6 条监控", "positive")])


class UntrackedProfileTests(unittest.TestCase):
    """「已添加、还没采集」的识别。

    有采集数据之后再加链接,新链接既进不了表格(表格数据源是快照)、
    也没有任何提示 —— 同样是「点了添加没反应」。这批链接必须能被单独认出来,
    页面上才给得出回执。
    """

    def setUp(self):
        from monitor import store as ms

        self.ms = ms
        self.db = Path(tempfile.mkdtemp()) / "monitor.db"
        ms.init_db(self.db)

    def _add(self, asin, domain="amazon.com", enabled=1):
        self.ms.upsert_profile(self.db, {
            "asin": asin, "domain": domain,
            "url": f"https://{domain}/dp/{asin}", "monitor_enabled": enabled})

    def _snapshot(self, asin, domain="amazon.com"):
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "INSERT INTO snapshots (asin, domain, checked_at, title) "
                "VALUES (?, ?, '2026-09-14 10:00', 't')", (asin, domain))

    def test_profiles_without_snapshots_are_reported(self):
        from monitor import board

        self._add("B0AAAAAAAA")
        self._add("B0BBBBBBBB", domain="amazon.in")
        self.assertEqual({p["asin"] for p in board.untracked_profiles(self.db)},
                         {"B0AAAAAAAA", "B0BBBBBBBB"})

    def test_profile_with_a_snapshot_is_not_reported(self):
        from monitor import board

        self._add("B0AAAAAAAA")
        self._add("B0BBBBBBBB")
        self._snapshot("B0AAAAAAAA")
        self.assertEqual([p["asin"] for p in board.untracked_profiles(self.db)],
                         ["B0BBBBBBBB"])

    def test_disabled_profile_with_a_snapshot_is_not_reported(self):
        """停用但有快照的链接不算「未采集」。

        别用 _latest_by_profile 反推 —— 它只遍历启用的 profile,会把这条误判成
        未采集,于是页面上永远挂着一句「另有 N 条已添加未采集」。
        """
        from monitor import board

        self._add("B0AAAAAAAA", enabled=0)
        self._snapshot("B0AAAAAAAA")
        self.assertEqual(board.untracked_profiles(self.db), [])

    def test_disabled_profile_without_a_snapshot_is_reported(self):
        """停用且没快照的仍要认出来(中间态里标「已停用」),别静默吞掉。"""
        from monitor import board

        self._add("B0AAAAAAAA", enabled=0)
        self.assertEqual([p["asin"] for p in board.untracked_profiles(self.db)],
                         ["B0AAAAAAAA"])


if __name__ == "__main__":
    unittest.main()
