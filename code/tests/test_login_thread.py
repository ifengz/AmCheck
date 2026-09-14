"""登录弹窗的线程模型回归测试。

线上事故:点「开始登录」完全没反应。原因是登录回调是同步函数,被 NiceGUI 直接在
asyncio 事件循环里调用,而 Playwright 同步 API 在事件循环里会立刻抛
"It looks like you are using Playwright Sync API inside the asyncio loop",
异常又落在 try 之外,最终只在服务端日志里,浏览器上什么都不显示。

这里不启浏览器、不启服务,只验证修好后的两条约束:
1. 登录相关的同步调用必须跑在事件循环之外(专属线程);
2. 同一域名必须固定在同一条线程上(Playwright 同步 API 跨线程会
   "Cannot switch to a different thread")。
"""

import asyncio
import sys
import threading
import unittest
from pathlib import Path


CODE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE_DIR))

UI_PY = CODE_DIR / "ui.py"

_NS = None


def _load_ui_module():
    """载入 ui.py,但切掉文件末尾的 ui.run(...)——否则导入即启动服务。

    ui.py 是脚本式入口,没有 if __name__ == "__main__" 保护,所以只能截断源码。
    """
    global _NS
    if _NS is None:
        src = UI_PY.read_text(encoding="utf-8")
        src = src[:src.index("\nui.run(")]
        ns = {"__name__": "ui_under_test", "__file__": str(UI_PY)}
        exec(compile(src, str(UI_PY), "exec"), ns)
        _NS = ns
    return _NS


class LoginThreadTests(unittest.TestCase):
    def setUp(self):
        self.ns = _load_ui_module()
        self.dom = "amazon.com.mx"
        self.addCleanup(self._shutdown_pool)

    def _shutdown_pool(self):
        pool = self.ns["_LOGIN_POOLS"].pop(self.dom, None)
        if pool is not None:
            pool.shutdown(wait=True)

    def test_work_runs_off_event_loop_and_sticks_to_one_thread(self):
        ns = self.ns
        ns["weblogin"].get_session = lambda dom: "SESSION"
        seen = []

        def work():
            seen.append(threading.current_thread().name)
            return ns["weblogin"].get_session(self.dom)

        async def main():
            first = await ns["_login_bound"](self.dom, work)
            second = await ns["_login_bound"](self.dom, work)
            return first, second, threading.current_thread().name

        first, second, loop_thread = asyncio.run(main())

        self.assertEqual(first, "SESSION")
        self.assertEqual(second, "SESSION")
        self.assertEqual(len(seen), 2)
        self.assertNotEqual(seen[0], loop_thread, "登录操作不能跑在事件循环线程里")
        self.assertEqual(seen[0], seen[1], "同一域名必须固定在同一条线程上")

    def test_different_domains_do_not_share_a_thread(self):
        ns = self.ns
        ns["weblogin"].get_session = lambda dom: dom
        other = "amazon.in"

        def cleanup():
            pool = ns["_LOGIN_POOLS"].pop(other, None)
            if pool is not None:
                pool.shutdown(wait=True)

        self.addCleanup(cleanup)

        async def main():
            names = []

            def work(dom):
                names.append(threading.current_thread().name)
                return ns["weblogin"].get_session(dom)

            await ns["_login_bound"](self.dom, work, self.dom)
            await ns["_login_bound"](other, work, other)
            return names

        names = asyncio.run(main())
        self.assertNotEqual(names[0], names[1], "不同域名应各自一条线程,互不阻塞")

    def test_open_session_retries_while_profile_lock_is_released(self):
        ns = self.ns
        calls = []

        def flaky(dom):
            calls.append(dom)
            if len(calls) < 3:
                raise RuntimeError("profile 还在被占用")
            return "SESSION"

        ns["weblogin"].get_session = flaky
        # 缩短重试间隔,避免用例变慢
        original_sleep = ns["time"].sleep
        ns["time"].sleep = lambda *_: None
        try:
            self.assertEqual(ns["_open_session"](self.dom, tries=3), "SESSION")
        finally:
            ns["time"].sleep = original_sleep
        self.assertEqual(calls, [self.dom] * 3)


if __name__ == "__main__":
    unittest.main()
