"""链接监控页「刷新」按钮的回归测试。

需求:按钮区加一个「刷新」,点击后只重读库、重画明细
(表格 + 「已添加未采集」列表),不做整页跳转 —— 整页跳转会丢
滚动位置、闪白屏,用户要的是「明细里的记录变新」。

这里把 page_monitor 真建在裸 Client 里,走线上真实链路点「刷新」,断言:
1. 不触发 ui.navigate.to(整页跳转);
2. 刷新前刚入库的新链接,刷新后出现在页面上;
3. 空态页(有 profiles 没快照)刷新同样能长出新链接。

不启服务、不启浏览器、不碰真 monitor.db(全程用临时库)。
"""

import asyncio
import contextlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE_DIR))
sys.path.insert(0, str(CODE_DIR / "tests"))

from test_login_thread import _load_ui_module  # noqa: E402

_LOOP = None


def _event_loop():
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP


def _test_request():
    from starlette.requests import Request

    scope = {"type": "http", "method": "GET", "path": "/monitor", "headers": [],
             "query_string": b"", "client": ("127.0.0.1", 1234),
             "server": ("127.0.0.1", 8765), "scheme": "http",
             "root_path": "", "http_version": "1.1"}
    return Request(scope)


class MonitorRefreshButtonTests(unittest.TestCase):
    def setUp(self):
        from nicegui.client import Client
        from nicegui.page import page as Page

        self.ns = _load_ui_module()
        self.loop = _event_loop()
        self.client = Client(page=Page('/monitor'), request=_test_request())

        self.original_db = self.ns["MONITOR_DB"]
        self.db = Path(tempfile.mkdtemp()) / "monitor.db"
        self.ns["MONITOR_DB"] = self.db
        self.addCleanup(self.ns.__setitem__, "MONITOR_DB", self.original_db)
        from monitor import store as ms
        ms.init_db(self.db)

        # 页头的 app.storage.user 在裸 Client 里拿不到,换掉 shell 框架,
        # 被测主体(页面内容 + 刷新逻辑)保持原样
        self.original_shell = self.ns["build_shell"]

        @contextlib.contextmanager
        def fake_shell(_nav):
            with self.ns["ui"].column():
                yield
        self.ns["build_shell"] = fake_shell
        self.addCleanup(self.ns.__setitem__, "build_shell", self.original_shell)

        # 整页跳转的入口记下来:刷新按钮绝不该碰它
        self.navigated = []
        ui = self.ns["ui"]
        original_nav = ui.navigate.to
        ui.navigate.to = lambda target, new_tab=False: self.navigated.append(target)
        self.addCleanup(lambda: setattr(ui.navigate, "to", original_nav))

    # ---- 造数据 ----

    def _profile(self, asin, domain="amazon.com"):
        from monitor import store as ms
        ms.upsert_profile(self.db, {
            "asin": asin, "domain": domain,
            "url": f"https://{domain}/dp/{asin}", "monitor_enabled": 1})

    def _snapshot(self, asin, domain="amazon.com"):
        # 带 price_value 才是「可用快照」(rules.snapshot_usable);
        # 只有 title 的空壳拍会被当成风控页残缺拍,不算采集成功
        with sqlite3.connect(self.db) as conn:
            conn.execute(
                "INSERT INTO snapshots (asin, domain, checked_at, title, "
                "price, price_value) VALUES (?, ?, '2026-09-14 10:00', 't', "
                "'$9.99', 9.99)", (asin, domain))

    # ---- 页面操作 ----

    def _render(self):
        with self.client:
            self.ns["page_monitor"]()

    def _button(self, text):
        from nicegui.elements.button import Button
        for el in self.client.elements.values():
            if isinstance(el, Button) and el.text == text:
                return el
        self.fail(f"页面上没找到按钮 {text!r}")

    def _click_refresh(self):
        button = self._button("刷新")
        for listener in button._event_listeners.values():
            if getattr(listener, "type", None) != 'click':
                continue

            async def main():
                from nicegui import background_tasks, core
                core.loop = asyncio.get_running_loop()
                try:
                    ret = listener.handler(None)
                    if asyncio.iscoroutine(ret):
                        await ret
                    for _ in range(100):
                        pending = list(background_tasks.running_tasks)
                        if not pending:
                            break
                        await asyncio.gather(*pending, return_exceptions=True)
                finally:
                    core.loop = None

            self.loop.run_until_complete(main())
            return

    def _page_text(self):
        """整页可见文本(html + label),用来断言链接有没有被列出来。"""
        from nicegui.elements.html import Html
        from nicegui.elements.label import Label
        parts = []
        for el in self.client.elements.values():
            if isinstance(el, Html):
                parts.append(el.content or "")
            elif isinstance(el, Label):
                parts.append(el.text or "")
        return " ".join(parts)

    # ---- 用例 ----

    def test_refresh_button_exists_in_action_row(self):
        self._render()
        self._button("刷新")   # 找不到会 fail

    def test_click_does_not_navigate_the_whole_page(self):
        self._profile("B0AAAAAAAA")
        self._snapshot("B0AAAAAAAA")
        self._render()
        self._click_refresh()
        self.assertEqual(self.navigated, [],
                         "刷新只该重画明细,不该 ui.navigate.to 整页跳转")

    def test_new_pending_link_shows_up_after_refresh(self):
        """核心场景:已有看板数据的页面上加了新链接,点刷新就能看见它。"""
        self._profile("B0AAAAAAAA")
        self._snapshot("B0AAAAAAAA")
        self._render()
        self.assertNotIn("B0BBBBBBBB", self._page_text())

        self._profile("B0BBBBBBBB")
        self._click_refresh()
        self.assertIn("B0BBBBBBBB", self._page_text(),
                      "刷新后「已添加未采集」列表里应出现新链接")
        self.assertEqual(self.navigated, [])

    def test_pending_card_disappears_after_it_gets_a_snapshot(self):
        """跑完采集(有了快照)再刷新,新链接从中间态挪进表格区,不重复列。"""
        self._profile("B0AAAAAAAA")
        self._snapshot("B0AAAAAAAA")
        self._profile("B0BBBBBBBB")
        self._render()
        self.assertIn("B0BBBBBBBB", self._page_text())
        self.assertIn("已添加,还没有采集数据", self._page_text())

        self._snapshot("B0BBBBBBBB")
        self._click_refresh()
        self.assertNotIn("已添加,还没有采集数据", self._page_text(),
                         "有了快照就不该再挂在「未采集」列表里")

    def test_empty_state_page_refreshes_in_place(self):
        """空态(只有 profiles 没快照):刷新同样原地重画,新链接看得见。"""
        self._profile("B0AAAAAAAA")
        self._render()
        self._profile("B0BBBBBBBB")
        self._click_refresh()
        self.assertIn("B0BBBBBBBB", self._page_text())
        self.assertEqual(self.navigated, [],
                         "没发生数据形态翻转就别整页跳转")


if __name__ == "__main__":
    unittest.main()
