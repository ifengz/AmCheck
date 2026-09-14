"""后台任务启动时机的回归测试。

背景:`ui.py` 是脚本式入口(末尾直接 `ui.run(...)`,没有 `__main__` 保护),
而测试要复用它的函数就得 exec 源码 —— 于是**导入期执行的每一行代码在测试里都会跑**。

所以定时采集(Scheduler)和评价链接每日跟踪(ReviewTracker)都改成
`app.on_startup` 里启动:只有 NiceGUI 真正开始服务时才拉起线程。
否则「跑一次单测」会顺手启动守护线程、立刻 tick,真的去请求 Amazon。

这里守住两条:
1. exec ui.py 之后,两个后台线程都不在运行;
2. 启动钩子确实注册到了 app 上(别改成永不触发)。
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
    global _NS
    if _NS is None:
        src = UI_PY.read_text(encoding="utf-8")
        src = src[:src.index("\nui.run(")]
        ns = {"__name__": "ui_under_test", "__file__": str(UI_PY)}
        exec(compile(src, str(UI_PY), "exec"), ns)
        _NS = ns
    return _NS


class BackgroundStartupTests(unittest.TestCase):
    def test_no_background_threads_started_on_import(self):
        ns = _load_ui_module()
        alive = {t.name for t in threading.enumerate() if t.is_alive()}
        self.assertNotIn("amcheck-scheduler", alive,
                         "定时采集不该在模块导入期启动")
        self.assertNotIn("amreview-review-tracker", alive,
                         "评价链接跟踪不该在模块导入期启动")
        # 对象要建好,只是不 start —— 启动钩子里直接用
        self.assertIsNotNone(ns["monitor_scheduler"])
        self.assertIsNotNone(ns["review_tracker"])

    def test_startup_hook_is_registered(self):
        ns = _load_ui_module()
        from nicegui import app
        hook = ns["_start_background_jobs"]
        self.assertIn(hook, app._startup_handlers,
                      "启动钩子必须注册到 app.on_startup,否则生产上后台任务不会跑")

    def test_startup_hook_starts_both(self):
        """钩子本身要能真的把两个线程拉起来(不依赖真实服务)。"""
        ns = _load_ui_module()
        started = []
        ns["monitor_scheduler"].start = lambda: started.append("scheduler")
        ns["review_tracker"].start = lambda: started.append("tracker")
        hook = ns["_start_background_jobs"]
        if asyncio.iscoroutinefunction(hook):
            asyncio.new_event_loop().run_until_complete(hook())
        else:
            hook()
        self.assertEqual(sorted(started), ["scheduler", "tracker"])


if __name__ == "__main__":
    unittest.main()
