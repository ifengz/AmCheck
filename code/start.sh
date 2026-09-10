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
  python3 -m venv .venv
  REQ=requirements.txt
  [ -f requirements.lock.txt ] && REQ=requirements.lock.txt
  .venv/bin/pip install -r "$REQ"
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
