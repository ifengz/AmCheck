"""ui.py 启动端口的回归测试。

`ui.py` 末尾的 `ui.run(port=...)` 读环境变量 `AMREVIEW_PORT`(默认 8765),
验证脚本 `_verify_history_ui.sh` 靠它把服务起在专用端口(8799)上,
从而**不需要**去 kill 占用 8765 的进程。

如果哪天有人把这个改回硬编码,验证脚本就会重新开始抢端口、
在多人/多会话并行时误杀别人正在跑的实例 —— 这条测试拦住这种回退。
"""

import os
import sys
import unittest
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE_DIR))

UI_PY = CODE_DIR / "ui.py"


def _exec_ui_source():
    """在干净命名空间里 exec ui.py(截到 ui.run 之前)。

    与 test_background_startup.py / test_history_ui.py 同一套做法:
    ui.py 是脚本式入口,要拿到里面的模块级变量只能 exec 源码。
    """
    src = UI_PY.read_text(encoding="utf-8")
    src = src[:src.index("\nui.run(")]
    ns = {"__name__": "ui_port_probe", "__file__": str(UI_PY)}
    exec(compile(src, str(UI_PY), "exec"), ns)
    return ns


class UiPortTests(unittest.TestCase):
    def test_env_var_overrides_port(self):
        """AMREVIEW_PORT 必须能覆盖端口 —— 验证脚本靠这个避让 8765。"""
        old = os.environ.get("AMREVIEW_PORT")
        try:
            os.environ["AMREVIEW_PORT"] = "8799"
            self.assertEqual(_exec_ui_source()["PORT"], 8799)
        finally:
            if old is None:
                os.environ.pop("AMREVIEW_PORT", None)
            else:
                os.environ["AMREVIEW_PORT"] = old

    def test_default_port_is_8765(self):
        """没设环境变量时仍是 8765 —— 生产/start.sh 的默认行为不能变。"""
        src = UI_PY.read_text(encoding="utf-8")
        self.assertIn('os.environ.get("AMREVIEW_PORT", "8765")', src,
                      "默认端口必须还是 8765")

    def test_ui_run_uses_the_variable(self):
        """ui.run 必须用 PORT 变量,别写回字面量。"""
        src = UI_PY.read_text(encoding="utf-8")
        self.assertIn("port=PORT", src, "ui.run 应写成 port=PORT")
        self.assertNotIn("port=8765", src, "别把端口写回硬编码")


class RestartEntrypointTests(unittest.TestCase):
    """界面上的「重启服务」必须重启**自己**,不能 exec 一条跑不起来的命令。

    原实现是 `os.execv(python, [python, "-m", "nicegui", "run", ui.py])`,
    而 `python -m nicegui` 直接报 "No module named nicegui.__main__" ——
    os.execv 在报错前就把旧进程映像换掉了,等于「点一下自杀且起不来」。
    """

    def test_restart_reexecs_the_same_script(self):
        src = UI_PY.read_text(encoding="utf-8")
        self.assertNotIn('"-m", "nicegui"', src,
                         "别用 `-m nicegui` 重启:该包没有 __main__,exec 完服务就没了")
        self.assertIn("os.execv(sys.executable, [sys.executable, str(Path(__file__))])",
                      src, "重启要 exec 回 ui.py 本身")

    def test_restart_is_logged(self):
        """重启这类"把进程换掉"的操作必须留痕,否则事后只看到日志断了一截。"""
        src = UI_PY.read_text(encoding="utf-8")
        idx = src.index("os.execv(sys.executable, [sys.executable, str(Path(__file__))])")
        self.assertIn("log.warning", src[:idx], "重启之前要先写一行日志")


if __name__ == "__main__":
    unittest.main()
