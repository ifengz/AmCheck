"""守住「不许把同步重活跑在事件循环里」(P0-3)。

NiceGUI 的 `async def` 回调直接在事件循环线程里执行,所以在里面做同步重活
(解 Excel、Playwright、子进程、同步网络)会把**整个站点**堵死 —— 表现是
「进程活着但所有请求无响应、监听队列堆积、SIGTERM 也退不掉」,正是
2026-09-14 僵死事故的形态。

判据很干脆:重活出现在 `async def` 体内就是错的。写进 `run.io_bound(...)` 的
同步 `def _work()` 是正确用法,所以遍历时要**跳过嵌套的同步函数定义**
(它在工作线程里跑);嵌套的 `async def` 不跳过,它自己的回调体也在循环里。

`WARN` 那几条是本地 sqlite 写、量被 `MAX_BATCH`(=50)封顶,毫秒级,先放行 ——
但**集合是钉死的**:新加一条就失败,逼你说明为什么它可以留在循环里。
"""

import ast
import sys
import unittest
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parents[1]
UI_PY = CODE_DIR / "ui.py"

# 出现在 async 体里就算阻塞(按被调函数名匹配,能覆盖属性调用如 x.parse_order_file())
BLOCKING = {
    "parse_order_file",          # openpyxl/xlrd 解 Excel:20MB 能卡几十秒到几分钟
    "check_batch", "check_one", "is_logged_in",
    "ReviewChecker",             # 构造函数会起 Playwright
    "get_session", "close_domains", "auto_login", "submit_code", "logged_in",
    "Popen", "run", "check_output", "call",
    "urlopen", "urlretrieve",
    "sleep",
    "execv", "system",
}

# 已放行的:本地 sqlite 写,条数被 MAX_BATCH 封顶,毫秒级
ALLOWED_WARN = {
    ("_confirm", "save_history"),
    ("_confirm", "update_track_state"),
    ("_check_now", "save_history"),
    ("_check_now", "update_track_state"),
    ("_run", "save_history"),
}
WARN = {"save_history", "upsert_review_meta", "update_track_state", "daily_slots"}


def _callee(node: ast.Call) -> str:
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return ""


def _iter_calls(fn: ast.AsyncFunctionDef):
    """遍历 async 函数体里的调用,跳过嵌套的**同步**函数(它们在线程池里跑)。"""
    stack = list(fn.body)
    while stack:
        node = stack.pop()
        if isinstance(node, ast.FunctionDef):
            continue
        if isinstance(node, ast.Call):
            yield node
        stack.extend(ast.iter_child_nodes(node))


def audit(source: str, filename: str = "ui.py"):
    """返回 (阻塞项, 留意项),每项是 (函数名, 被调名, 行号)。"""
    tree = ast.parse(source, filename=filename)
    bad, warn = [], []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for call in _iter_calls(node):
            name = _callee(call)
            if name in BLOCKING:
                bad.append((node.name, name, call.lineno))
            elif name in WARN:
                warn.append((node.name, name, call.lineno))
    return bad, warn


class NoBlockingOnLoopTests(unittest.TestCase):
    def test_ui_has_no_blocking_calls_on_the_loop(self):
        bad, _ = audit(UI_PY.read_text(encoding="utf-8"))
        self.assertEqual(
            bad, [],
            "这些同步重活跑在了事件循环里,会把整站堵死:\n  "
            + "\n  ".join(f"ui.py:{ln} {fn}() 调 {name}()" for fn, name, ln in bad))

    def test_warn_set_does_not_grow(self):
        _, warn = audit(UI_PY.read_text(encoding="utf-8"))
        extra = {(fn, name) for fn, name, _ in warn} - ALLOWED_WARN
        self.assertEqual(
            extra, set(),
            "新增了留在事件循环里的写库调用。确认它确实小且被封顶之后,"
            "把它加进 ALLOWED_WARN 并写明理由:\n  "
            + "\n  ".join(f"{fn}() 调 {name}()" for fn, name in sorted(extra)))

    def test_detector_actually_detects(self):
        """自检:检测器本身不能失灵,否则上面两条就是空断言。"""
        bad, _ = audit(
            "async def handler():\n"
            "    rows = parse_order_file(raw)\n"
            "    await run.io_bound(parse_order_file, raw)\n")
        self.assertEqual([(fn, name) for fn, name, _ in bad],
                         [("handler", "parse_order_file")],
                         "只该报第一处(直接调用),不该报丢进 io_bound 的那处")

    def test_nested_sync_helper_is_allowed(self):
        """`def _work()` 包起来再 run.io_bound 是正确写法,不能被误报。"""
        bad, _ = audit(
            "async def handler():\n"
            "    def _work():\n"
            "        return parse_order_file(raw)\n"
            "    await run.io_bound(_work)\n")
        self.assertEqual(bad, [], "嵌套同步函数里的调用不该报")


if __name__ == "__main__":
    unittest.main()
