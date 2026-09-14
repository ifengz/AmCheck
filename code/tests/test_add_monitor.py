"""「添加监控链接」的可见反馈回归测试。

现象:粘贴 6 条商品链接、点「添加」,界面没有任何变化,像没点过一样。
现在的契约:入库 → 提示 → 立即把刚加这批交给采集回调(「入库即采集」,
不再「入库即返回」干等手动跑一轮)。失败时不关窗、错误写在弹窗里。

这里把 ``add_monitor_dialog()`` 真建在裸 Client 里,走线上真实链路点一次
「添加」,断言:链接真入库、提示真发出、采集回调拿到刚加的那批、
失败时不关窗也不开采。

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

        self.notified, self.timers, self.navigated, self.collected = \
            [], [], [], []

        ui = self.ns["ui"]
        original = (ui.notify, ui.timer, ui.navigate.to)

        def restore():
            ui.notify, ui.timer, ui.navigate.to = original

        self.addCleanup(restore)

        ui.notify = lambda msg, **kw: self.notified.append((msg, kw.get("type")))
        ui.timer = lambda delay, cb, **kw: self.timers.append((delay, cb, kw))
        ui.navigate.to = lambda target, new_tab=False: self.navigated.append(target)

    # ---- 操作辅助 ----

    def _open_dialog(self):
        with self.client:
            # on_collect 收到的就是「添加监控」移交过来的刚入库那批 profiles
            self.ns["add_monitor_dialog"](
                lambda added: self.collected.append(added))

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
        self.assertEqual(len(self.notified), 1)
        msg, typ = self.notified[0]
        self.assertEqual(typ, "positive")
        self.assertIn("已添加 6 条监控", msg)
        self.assertIn("立即开始采集", msg)

    def test_storage_immediately_hands_new_links_to_collection(self):
        """「入库即采集」:刚加的这批必须立刻交给采集回调(不再干等手动跑)。

        回调拿到的是 profiles 里的完整行(含 metric_config 等采集要用的字段),
        且只含本次新加的,不含库里原有的其它监控。
        """
        self._profile_first("B0OLDOLDOL")   # 库里原有一条,不属于这批

        self._open_dialog()
        self._textarea().value = LINKS
        self._click("添加")

        self.assertEqual(len(self.collected), 1, "恰好一次采集,不多不少")
        batch = self.collected[0]
        self.assertEqual({(p["asin"], p["domain"]) for p in batch}, EXPECTED)
        self.assertTrue(all("metric_config" in p for p in batch),
                        "得是回读的完整 profile 行,采集要读 metric_config")
        self.assertNotIn("B0OLDOLDOL", {p["asin"] for p in batch},
                         "只抓刚加这批,别把旧链接也捎上")

    def _profile_first(self, asin):
        from monitor import store as ms
        ms.upsert_profile(self.db, {
            "asin": asin, "domain": "amazon.com",
            "url": f"https://amazon.com/dp/{asin}", "monitor_enabled": 1})

    def test_already_tracked_links_are_skipped_and_said_so(self):
        """重复添加不该报错,但要明说跳过了几条 —— 否则看着也像没反应。"""
        self._open_dialog()
        self._textarea().value = LINKS
        self._click("添加")
        self.notified.clear()
        self.collected.clear()

        self._textarea().value = LINKS
        self._click("添加")

        from monitor import store as ms

        self.assertEqual(len(ms.list_profiles(self.db, only_enabled=False)), 6)
        self.assertEqual(self.notified, [("跳过已存在 6 条", "warning")])
        self.assertEqual(self.collected, [], "全是跳过,就不该开浏览器")

    def test_unparsable_input_keeps_the_dialog_open_with_a_hint(self):
        self._open_dialog()
        self._textarea().value = "这不是链接"
        self._click("添加")

        self.assertEqual(self.notified, [])
        self.assertIn("未解析到有效商品链接", self._label_texts())
        self.assertEqual(self.collected, [], "没入库就不该采集")

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
        self.assertEqual(self.collected, [], "失败了就别开浏览器")

    def test_fresh_db_without_tables_still_accepts_links(self):
        """回归:get_profile 不建表,全新库第一次添加会 no such table: profiles。"""
        self.ns["MONITOR_DB"] = Path(tempfile.mkdtemp()) / "brand-new.db"
        self.db = self.ns["MONITOR_DB"]      # 故意不 init_db

        self._open_dialog()
        self._textarea().value = LINKS
        self._click("添加")

        self.assertEqual(len(self.notified), 1)
        self.assertIn("已添加 6 条监控", self.notified[0][0])
        self.assertEqual(self.notified[0][1], "positive")


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
        # 带 price_value 才是「可用快照」(rules.snapshot_usable):
        # 只有 title 的空壳拍会被当成风控页残缺拍,不算采集成功
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "INSERT INTO snapshots (asin, domain, checked_at, title, "
                "price, price_value) VALUES (?, ?, '2026-09-14 10:00', 't', "
                "'$9.99', 9.99)", (asin, domain))

    def test_profiles_without_snapshots_are_reported(self):
        from monitor import view

        self._add("B0AAAAAAAA")
        self._add("B0BBBBBBBB", domain="amazon.in")
        self.assertEqual({p["asin"] for p in view.untracked_profiles(self.db)},
                         {"B0AAAAAAAA", "B0BBBBBBBB"})

    def test_profile_with_a_snapshot_is_not_reported(self):
        from monitor import view

        self._add("B0AAAAAAAA")
        self._add("B0BBBBBBBB")
        self._snapshot("B0AAAAAAAA")
        self.assertEqual([p["asin"] for p in view.untracked_profiles(self.db)],
                         ["B0BBBBBBBB"])

    def test_disabled_profile_with_a_snapshot_is_not_reported(self):
        """停用但有快照的链接不算「未采集」。

        别用 latest_by_profile 反推 —— 它只遍历启用的 profile,会把这条误判成
        未采集,于是页面上永远挂着一句「另有 N 条已添加未采集」。
        """
        from monitor import view

        self._add("B0AAAAAAAA", enabled=0)
        self._snapshot("B0AAAAAAAA")
        self.assertEqual(view.untracked_profiles(self.db), [])

    def test_disabled_profile_without_a_snapshot_is_reported(self):
        """停用且没快照的仍要认出来(中间态里标「已停用」),别静默吞掉。"""
        from monitor import view

        self._add("B0AAAAAAAA", enabled=0)
        self.assertEqual([p["asin"] for p in view.untracked_profiles(self.db)],
                         ["B0AAAAAAAA"])


if __name__ == "__main__":
    unittest.main()
