#!/usr/bin/env bash
# 重新生成 requirements.lock.txt(锁定「实测过」的精确版本)。
#
# 用法:先在 .venv 里把依赖装好,然后
#     cd code && bash gen_lock.sh
#
# 为什么要脚本化而不是手敲 pip freeze:
# 1. 需要排除 pyflakes 这类纯开发工具;
# 2. 排序方式必须固定(sort -f),否则每次生成都有无意义的 diff;
# 3. **numpy 的 pin 必须带 Python 版本条件** —— 见下面的详细说明。
#    手敲很容易漏掉第 3 条,而漏掉的后果是「在新 Python 上装不上」。
set -euo pipefail
cd "$(dirname "$0")"

PY="${PY:-./.venv/bin/python}"
if [ ! -x "$PY" ]; then
  echo "找不到 $PY,请先创建 venv 并安装依赖" >&2
  exit 1
fi

TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

# 排除:lint 工具,以及 pip/setuptools/wheel 这类不该被锁的引导包
"$PY" -m pip freeze \
  | grep -vE '^(pyflakes|pip|setuptools|wheel)==' \
  | sort -f > "$TMP"

"$PY" - "$TMP" <<'PY'
"""把 numpy 的单条 pin 换成带 Python 版本条件的两条。

为什么必须这么做:numpy 每个大版本的 Python 支持窗口不一样 ——
  numpy 2.0.x → 3.9~3.12(没有 3.13 的轮子)
  numpy 2.1.x → 3.10~3.13
  numpy 2.3.x → 3.11~3.14
所以「一个精确版本」不可能同时覆盖 3.9 和 3.13。写死 2.0.2 的话,
在 3.13 上 pip 找不到轮子会转去源码编译,直接 metadata-generation-failed。
实测过:2.0.2 在 3.9 装成功、在 3.13 装失败。

numpy 本身是 streamlit → pandas 带进来的传递依赖,主界面(NiceGUI)不需要它,
但 legacy 界面要,所以不能从 lock 里删掉,只能分版本锁。
"""
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
lines = path.read_text(encoding="utf-8").splitlines()

PINS = [
    'numpy==2.0.2; python_version < "3.13"',
    'numpy==2.1.3; python_version >= "3.13"',
]

out = [ln for ln in lines if not ln.lower().startswith("numpy==")]
if len(out) == len(lines):
    print("注意:pip freeze 里没有 numpy,仍然补上分版本 pin", file=sys.stderr)
out.extend(PINS)

path.write_text("\n".join(sorted(out, key=str.lower)) + "\n", encoding="utf-8")
PY

mv "$TMP" requirements.lock.txt
trap - EXIT

echo "已重新生成 requirements.lock.txt:$(grep -c . requirements.lock.txt) 行"
echo "别忘了:改完 lock 跑一遍测试 → cd code && .venv/bin/python -m unittest discover -s tests"
