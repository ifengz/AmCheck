"""架构护栏(2026-09-14 解耦审计的产物):三条分层规则,静态扫描,不连库。

1. ui.py / mockdata.py 的依赖闭包里不许出现 streamlit —— 上次就是
   ui.py → monitor.board 把整个 Streamlit 栈拖进 NiceGUI 进程,
   逼着 requirements 永远钉着 streamlit。
2. monitor/ 下除 store.py 外不许出现 sqlite3 / store._connect ——
   SQL 唯一入口是 store 公开函数(notify/chatbot/ai 曾各自穿墙手写)。
3. 跨模块 import 不许以下划线开头 —— _is_blank/_demo_asins 这类
   "私有函数被外面当接口用"就是抽象漏了;同包内自测的 tests/ 除外。

三条都是纯文本/AST 扫描,秒级跑完;谁改回去测试立刻红。
"""

from __future__ import annotations

import ast
import re
import sys
import unittest
from pathlib import Path

CODE = Path(__file__).resolve().parent.parent
EXCLUDE_DIRS = {".venv", "graphify-out", "logs", "screenshots", "shots_ng",
                "ui_screenshots_polabel2_style", "deploy", "tests"}


def project_pyfiles():
    for p in sorted(CODE.rglob("*.py")):
        rel = p.relative_to(CODE)
        if EXCLUDE_DIRS & set(rel.parts):
            continue
        yield p


# ---------- 规则 1:streamlit 不进 ui/mockdata 的传递闭包 ----------

def _module_to_file(mod: str, importer_pkg: str):
    """把 import 名解析成 CODE 下的项目文件;第三方包返回 None。"""
    if mod.startswith("."):
        n = len(mod) - len(mod.lstrip("."))
        parts = importer_pkg.split("/")
        base = parts[:len(parts) - n + 1] if n <= len(parts) else parts
        mod = "/".join(base + mod.lstrip(".").split(".")) if mod.lstrip(".") \
            else "/".join(base[:-1]) if len(base) > 1 else ""
        if not mod:
            return None
    else:
        mod = mod.replace(".", "/")
    f = CODE / (mod + ".py")
    if f.exists():
        return f
    d = CODE / mod / "__init__.py"
    return d if d.exists() else None


def _raw_imports(path: Path):
    """AST 抠出一文件的全部 import 名(含函数体内的延迟 import)。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out.extend(a.name for a in n.names)
        elif isinstance(n, ast.ImportFrom):
            mod = ("." * (n.level or 0)) + (n.module or "")
            out.append(mod)
            # from a import b:b 也可能是子模块(view 用法一致)
            for a in n.names:
                if mod.lstrip("."):
                    out.append(mod.rstrip(".") + "." + a.name
                               if not mod.endswith(".") else mod + a.name)
    return [m for m in out if m]


def _closure(entry: Path, _seen=None):
    """entry.py 出发、只走项目内文件的传递 import 闭包。"""
    seen = _seen if _seen is not None else set()
    if entry in seen or entry is None:
        return seen
    seen.add(entry)
    pkg = str(entry.parent.relative_to(CODE))
    for imp in _raw_imports(entry):
        if "streamlit" in imp.split("."):
            seen.add(("STREAMLIT", str(entry), imp))
            continue
        f = _module_to_file(imp, "" if pkg == "." else pkg)
        if f is not None:
            _closure(f, seen)
    return seen


class StreamlitIntrusionTests(unittest.TestCase):
    def test_ui_closure_has_no_streamlit(self):
        # 直接扫 ui.py 可达的项目文件里,谁写了 import streamlit
        hit = _closure(CODE / "ui.py")
        offenders = [h for h in hit if isinstance(h, tuple)]
        # 闭包内文件级检查:被 import 到的项目文件自己不许 import streamlit
        for f in list(hit):
            if isinstance(f, tuple):
                continue
            src = f.read_text(encoding="utf-8")
            if re.search(r"^\s*(import streamlit|from streamlit)\b", src, re.M):
                offenders.append(str(f.relative_to(CODE)))
        self.assertEqual(
            offenders, [],
            "ui.py 的依赖闭包拖进了 streamlit —— NiceGUI 进程会被整个 "
            "Streamlit 栈污染(解耦审计 P0,详见 monitor/view.py 顶部)")

    def test_mockdata_has_no_framework(self):
        # 用 AST 看真实 import(文档字符串里"不 import streamlit"这类说明文字不算)
        for target in ("mockdata.py", "monitor/view.py"):
            imports = _raw_imports(CODE / target)
            bad = [m for m in imports
                   if m.split(".")[0].lstrip(".") in ("streamlit", "nicegui")]
            self.assertEqual(bad, [], f"{target} 不许依赖 Streamlit/NiceGUI")


# ---------- 规则 2:SQL 唯一入口 ----------

class StoreSqlSoleEntryTests(unittest.TestCase):
    def test_monitor_package_no_direct_sql_outside_store(self):
        offenders = []
        for p in (CODE / "monitor").glob("*.py"):
            if p.name == "store.py":
                continue
            src = p.read_text(encoding="utf-8")
            if re.search(r"^\s*(import sqlite3|from sqlite3)\b", src, re.M):
                offenders.append(f"{p.name}: 裸 sqlite3")
            if re.search(r"store\._connect\b", src):
                offenders.append(f"{p.name}: 穿墙 store._connect")
        self.assertEqual(
            offenders, [],
            "monitor 包内 SQL 一律走 store 公开函数;两张推送状态表的建表"
            "也在 store.init_db(解耦审计第 3 项)")


# ---------- 规则 3:跨模块不 import 下划线名 ----------

# 允许的例外:(文件, 被 import 的私有名) —— 目前没有
PRIVATE_IMPORT_ALLOWLIST = set()


class NoCrossModulePrivateImportTests(unittest.TestCase):
    def test_no_underscore_imports(self):
        offenders = []
        for p in project_pyfiles():
            try:
                tree = ast.parse(p.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for n in ast.walk(tree):
                if not isinstance(n, ast.ImportFrom):
                    continue
                # 同包内私有互用也拦:monitor 包内模块之间同样不许
                for a in n.names:
                    if not a.name.startswith("_") or a.name == "__future__":
                        continue
                    if (str(p.relative_to(CODE)), a.name) in PRIVATE_IMPORT_ALLOWLIST:
                        continue
                    offenders.append(
                        f"{p.relative_to(CODE)}:{n.lineno} "
                        f"from {n.module or '(pkg)'} import {a.name}")
        self.assertEqual(
            offenders, [],
            "下划线函数被跨模块 import = 抽象漏了,改内部实现会连带炸 UI; "
            "要么升公开(_is_blank→is_blank 即先例),要么在本模块内复制")


if __name__ == "__main__":
    unittest.main()
