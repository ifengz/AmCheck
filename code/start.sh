#!/usr/bin/env bash
# 服务器一键启动:首次自动建 venv、装依赖、装浏览器,之后起 NiceGUI 版界面
# 用法: ./start.sh [ui|legacy]
#   ui     NiceGUI 新版界面(ui.py,端口 8765)——默认
#   legacy Streamlit 旧版界面(app.py,端口 8766)
set -e
cd "$(dirname "$0")"

MODE="${1:-ui}"

if [ ! -d .venv ]; then
  echo "首次运行:创建虚拟环境并安装依赖…"

  # 先卡 Python 版本:锁定的依赖需要 3.9+(宝塔 Python 项目管理器建议 3.10+)
  python3 - <<'PY'
import sys
if sys.version_info < (3, 9):
    raise SystemExit(f"需要 Python 3.9+,当前 {sys.version.split()[0]}")
print(f"Python {sys.version.split()[0]} OK")
PY

  python3 -m venv .venv

  # 优先装 requirements.lock.txt(实测过的精确版本,可复现),但它是在
  # 「某个 Python 版本 + 某个平台」下 freeze 出来的,换环境可能装不上,
  # 所以装失败就退回 requirements.txt(版本浮动但装得动),别让首次初始化卡死。
  # 历史教训:lock 曾长期停留在 Streamlit 时代、连 nicegui 都没有,而这里又硬信
  # lock → 新机器装完一堆 Streamlit 依赖后才在启动时 ModuleNotFoundError。
  # (另:原来写成 `[ -f X ] && REQ=X`,在 set -e 下文件不存在会直接退出脚本。)
  INSTALLED=""
  if [ -f requirements.lock.txt ]; then
    echo "→ 尝试锁定版本:requirements.lock.txt"
    if .venv/bin/pip install -r requirements.lock.txt; then
      INSTALLED=lock
    else
      echo "!! 锁定版本安装失败,退回 requirements.txt(依赖版本将浮动)"
    fi
  fi
  if [ -z "$INSTALLED" ]; then
    echo "→ 安装 requirements.txt"
    .venv/bin/pip install -r requirements.txt
  fi

  # 装完立刻自检,别等服务起不来才发现装漏了
  MISSING=""
  for mod in nicegui playwright openpyxl PIL; do
    .venv/bin/python -c "import $mod" 2>/dev/null || MISSING="$MISSING $mod"
  done
  if [ -n "$MISSING" ]; then
    echo "!! 依赖自检失败,这些核心模块导入不了:$MISSING" >&2
    echo "   请看上面的 pip 输出;也可以 rm -rf .venv 后重跑本脚本" >&2
    exit 1
  fi
  for mod in xlrd streamlit; do
    .venv/bin/python -c "import $mod" 2>/dev/null \
      || echo "!! 警告:$mod 未安装,相关功能不可用(旧版 .xls 导入 / legacy 界面)"
  done
  echo "依赖自检通过"

  .venv/bin/playwright install chromium
fi

if [ ! -f accounts.json ]; then
  echo "提示:未找到 accounts.json(小号凭据)。复制 accounts.json.example 填写后重启,网页上即显示账号/二维码;不配也可手动登录。"
fi

case "$MODE" in
  ui)
    exec .venv/bin/python ui.py
    ;;
  legacy)
    exec .venv/bin/streamlit run app.py --server.port 8766 --server.headless true
    ;;
  *)
    echo "未知模式: $MODE(可选 ui | legacy)" >&2
    exit 1
    ;;
esac
