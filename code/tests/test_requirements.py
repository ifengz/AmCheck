"""依赖清单一致性的回归测试。

背景(真实踩过的坑):
`requirements.lock.txt` 曾长期停留在 Streamlit 时代 —— 只有 44 行,
整个 NiceGUI 运行栈(nicegui / fastapi / uvicorn / starlette / python-socketio…)
和 dingtalk-stream 都没写进去。而 `start.sh` 是**优先用 lock 文件**装依赖的:

    REQ=requirements.txt
    [ -f requirements.lock.txt ] && REQ=requirements.lock.txt

结果就是「本地跑得好好的,全新部署直接 ModuleNotFoundError: nicegui」。
这类问题本地很难察觉(venv 早就装好了),所以用测试钉住。

守五条:
1. requirements.txt 声明的包,必须都能在 lock 里找到(本文件的核心);
2. lock 必须是可解析的精确版本、且排序稳定(生成方式见 `code/gen_lock.sh`);
3. 核心运行栈必须显式出现在 lock 里(防止又一次整体漏掉);
4. 代码里 import 的第三方包,必须要么已声明,要么在下面登记过原因;
5. 同一个包出现多条 pin 时,环境标记必须齐全且互斥(numpy 的版本窗口)。

第 5 条是补上来的:lock 的失效不止「内容过期」一种,还有「Python 版本不匹配」——
numpy 2.0.x 只到 3.12,写死单条 pin 会让 3.13 上的安装转去源码编译并失败。
"""

import re
import sys
import unittest
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parents[1]
REQ_TXT = CODE_DIR / "requirements.txt"
REQ_LOCK = CODE_DIR / "requirements.lock.txt"

# 代码里出现的第三方顶层模块名 → 分发包名(pip 名)。
# 不在这张表里的第三方 import 会被 test_no_undeclared_third_party_imports 抓出来。
IMPORT_TO_DIST = {
    "PIL": "pillow",
    "dingtalk_stream": "dingtalk-stream",
    "nicegui": "nicegui",
    "openpyxl": "openpyxl",
    "packaging": "packaging",
    "playwright": "playwright",
    "pyotp": "pyotp",
    "qrcode": "qrcode",
    "streamlit": "streamlit",
    "xlrd": "xlrd",
    "xlwt": "xlwt",
    "starlette": "starlette",
}

# 允许「被 import 但不必写进 requirements.txt」的包,必须写清原因。
ALLOWED_UNDECLARED = {
    "starlette": "fastapi/nicegui 的传递依赖,只被测试用来构造请求,不必显式声明",
    "xlwt": "仅测试用(生成旧版 .xls 夹具),不随部署安装,见 requirements.txt 注释",
}

# 核心运行栈:缺任何一个应用都起不来,必须在 lock 里显式出现。
CORE_RUNTIME = [
    "nicegui", "fastapi", "uvicorn", "starlette", "python-socketio",
    "playwright", "openpyxl", "xlrd", "dingtalk-stream", "streamlit", "pillow",
]


def _norm(name: str) -> str:
    """按 PEP 503 归一化分发包名(大小写不敏感,下划线等价连字符)。"""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def _parse_requirements(path: Path) -> dict:
    """解析 requirements.txt → {归一化包名: 原始行}。

    只取直接依赖;跳过注释、空行和 `-r`/`-e` 之类的选项行。
    """
    out = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        # 切掉 extras([pil])、版本约束(>=1.2)、环境标记(;python_version…)
        name = re.split(r"[<>=!~\[; ]", line, 1)[0].strip()
        if name:
            out[_norm(name)] = line
    return out


def _lock_lines(path: Path) -> list:
    """lock 里所有有效行(去注释、去空行)。"""
    return [l.strip() for l in path.read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.strip().startswith("#")]


def _parse_lock(path: Path) -> dict:
    """{归一化包名: 版本}。

    环境标记会被剥掉 —— 同一个包按 Python 版本可能有多条 pin
    (numpy 就是这样),这里只关心「包在不在、锁的是哪个版本」。
    """
    out = {}
    for line in _lock_lines(path):
        spec = line.split(";", 1)[0].strip()
        name, sep, ver = spec.partition("==")
        if sep:
            out.setdefault(_norm(name), ver.strip())
    return out


def _stdlib_names() -> set:
    """标准库顶层模块名。3.9 没有 sys.stdlib_module_names,退化成扫目录。

    注意必须并上 sys.builtin_module_names:`sys` / `time` / `builtins` 这些
    是编译进解释器的,标准库目录里**没有对应的 .py 文件**,只扫目录会漏掉,
    于是它们会被误判成第三方包。
    """
    names = set(getattr(sys, "stdlib_module_names", ()))
    names |= set(sys.builtin_module_names)
    if not getattr(sys, "stdlib_module_names", None):
        import sysconfig
        stdlib = Path(sysconfig.get_paths()["stdlib"])
        names |= {p.stem for p in stdlib.glob("*.py")}
        names |= {p.parent.name for p in stdlib.glob("*/__init__.py")}
    return names


def _local_names() -> set:
    """本仓库自己的模块/包名(import 它们不算第三方)。"""
    names = set()
    for p in CODE_DIR.rglob("*.py"):
        if ".venv" in p.parts or "__pycache__" in p.parts:
            continue
        names.add(p.stem)
        if p.name == "__init__.py":
            names.add(p.parent.name)
    return names


def _third_party_imports() -> dict:
    """扫描全仓 .py,返回 {第三方顶层模块名: [出现它的文件…]}。"""
    import ast
    std = _stdlib_names()
    local = _local_names()
    found = {}
    for f in sorted(CODE_DIR.rglob("*.py")):
        if ".venv" in f.parts or "__pycache__" in f.parts:
            continue
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif (isinstance(node, ast.ImportFrom)
                  and node.level == 0 and node.module):
                mods = [node.module.split(".")[0]]
            else:
                continue
            for m in mods:
                if m in std or m in local:
                    continue
                found.setdefault(m, set()).add(f.name)
    return found


class RequirementsConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.declared = _parse_requirements(REQ_TXT)
        self.locked = _parse_lock(REQ_LOCK)

    def test_lock_file_exists(self):
        self.assertTrue(REQ_LOCK.is_file(), "start.sh 会优先用它,不能删")

    def test_every_declared_package_is_locked(self):
        """requirements.txt 里写的包必须都能在 lock 里找到。

        这就是「lock 文件过期」的直接检测:曾经 nicegui 等一整套都不在里面。
        """
        missing = sorted(d for d in self.declared if d not in self.locked)
        self.assertEqual(
            missing, [],
            f"这些包在 requirements.txt 里声明了,但 requirements.lock.txt 里没有:"
            f"{missing}\n→ 重新生成:cd code && bash gen_lock.sh")

    def test_core_runtime_stack_is_locked(self):
        """核心运行栈必须显式在 lock 里 —— 缺一个全新部署就起不来。"""
        missing = [d for d in CORE_RUNTIME if _norm(d) not in self.locked]
        self.assertEqual(missing, [],
                         f"核心运行依赖没进 lock 文件:{missing}")

    def test_lock_lines_are_exact_pins(self):
        """lock 每行都必须是可解析的精确 pin(`name==version`,可带环境标记)。

        用 packaging 真正解析,而不是正则 —— 标记里可能有引号、比较符、
        and/or 组合,正则很容易放行错的东西。
        """
        from packaging.requirements import InvalidRequirement, Requirement

        bad = []
        for line in _lock_lines(REQ_LOCK):
            try:
                req = Requirement(line)
            except InvalidRequirement as exc:
                bad.append(f"{line}   ← 解析失败: {exc}")
                continue
            spec = list(req.specifier)
            if len(spec) != 1 or spec[0].operator != "==":
                bad.append(f"{line}   ← 不是单一 == 精确版本")
        self.assertEqual(bad, [], "lock 里这些行不是合法精确 pin:\n" + "\n".join(bad))

    def test_lock_is_sorted(self):
        """生成方式是 `pip freeze | sort -f`,排序稳定才不会有假 diff。"""
        lines = _lock_lines(REQ_LOCK)
        self.assertEqual(lines, sorted(lines, key=str.lower),
                         "lock 文件没按包名排序,重新生成请用 `bash gen_lock.sh`")

    def test_duplicate_pins_carry_markers(self):
        """同一个包出现多条 pin 时,每条都必须带环境标记。

        现实案例:numpy 2.0.x 支持 3.9~3.12 但没有 3.13 的轮子,3.13 要换 2.1.x,
        所以必须写成两条带 `python_version` 标记的 pin。少了标记就变成
        「装哪个看 pip 心情」,而且是静默的。
        """
        from packaging.requirements import Requirement

        seen = {}
        for line in _lock_lines(REQ_LOCK):
            req = Requirement(line)
            seen.setdefault(_norm(req.name), []).append((line, req.marker))

        problems = []
        for name, entries in seen.items():
            if len(entries) < 2:
                continue
            for line, marker in entries:
                if marker is None:
                    problems.append(f"{name} 有多条 pin,但这条没有环境标记:{line}")
        self.assertEqual(problems, [], "\n".join(problems))

    def test_duplicate_pin_markers_do_not_overlap(self):
        """同一包的多条 pin,标记必须互斥 —— 否则某个 Python 上会同时命中两条。

        这条能挡住「标记写错方向」这类静默错误,比如把两条都写成
        `python_version >= "3.9"`。
        """
        from packaging.markers import Marker
        from packaging.requirements import Requirement

        versions = ["3.9", "3.10", "3.11", "3.12", "3.13", "3.14"]
        seen = {}
        for line in _lock_lines(REQ_LOCK):
            req = Requirement(line)
            seen.setdefault(_norm(req.name), []).append((line, req.marker))

        problems = []
        for name, entries in seen.items():
            if len(entries) < 2:
                continue
            for ver in versions:
                env = {"python_version": ver, "python_full_version": ver + ".0"}
                hits = [ln for ln, m in entries
                        if m is None or Marker(str(m)).evaluate(env)]
                if len(hits) > 1:
                    problems.append(
                        f"{name} 在 Python {ver} 上同时命中 {len(hits)} 条:{hits}")
        self.assertEqual(problems, [], "\n".join(problems))

    def test_numpy_pin_covers_both_windows(self):
        """numpy 的版本窗口是真实约束,不能退回单条 pin。

        numpy 2.0.x → 3.9~3.12;2.1.x → 3.10~3.13。
        一个精确版本盖不住 3.9 和 3.13 两端,写死单条会让某一端退化成源码编译。
        """
        lines = [l for l in _lock_lines(REQ_LOCK) if l.lower().startswith("numpy==")]
        self.assertGreaterEqual(len(lines), 2, f"numpy 应该有分版本的多条 pin,实际:{lines}")
        for line in lines:
            self.assertIn("python_version", line,
                          f"numpy 的 pin 必须带 python_version 标记:{line}")

    def test_no_undeclared_third_party_imports(self):
        """代码 import 的第三方包必须已声明,或在 ALLOWED_UNDECLARED 里登记过。

        防止「本地 venv 里恰好装了,所以没发现」的隐性依赖。
        """
        problems = []
        for mod, files in sorted(_third_party_imports().items()):
            dist = IMPORT_TO_DIST.get(mod)
            if dist is None:
                problems.append(
                    f"{mod}(被 {', '.join(sorted(files))} import)不在 IMPORT_TO_DIST 表里")
                continue
            if _norm(dist) in self.declared:
                continue
            if mod in ALLOWED_UNDECLARED:
                continue
            problems.append(f"{mod} → {dist} 既没写进 requirements.txt,也没登记豁免理由")
        self.assertEqual(problems, [], "\n".join(problems))


    def test_lock_matches_installed_environment(self):
        """lock 里的版本必须等于当前环境实际装的版本。

        lock 的意义就是「这是我实测过的那套版本」。如果本地升了包却没重新生成
        lock,这条会失败 —— 提醒你把实测结果同步回去,别让 lock 慢慢变成谎言。

        在没装依赖的解释器里跑测试时,对应的包会被跳过,不会误报。
        """
        from importlib.metadata import PackageNotFoundError, version as dist_version

        drift = []
        for dist in CORE_RUNTIME:
            try:
                installed = dist_version(dist)
            except PackageNotFoundError:
                continue  # 当前解释器没装,跳过(比如用系统 python 跑测试)
            locked = self.locked.get(_norm(dist))
            if locked and locked != installed:
                drift.append(f"{dist}: lock={locked} 实际={installed}")
        self.assertEqual(
            drift, [],
            "lock 与实测环境脱节,重新生成:cd code && bash gen_lock.sh\n"
            + "\n".join(drift))


if __name__ == "__main__":
    unittest.main()
