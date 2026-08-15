#!/usr/bin/env bash
# 服务器一键启动:首次自动建 venv、装依赖、装浏览器,之后直接起 Streamlit
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "首次运行:创建虚拟环境并安装依赖…"
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt
  .venv/bin/playwright install chromium
fi

if [ ! -f accounts.json ]; then
  echo "提示:未找到 accounts.json(小号凭据)。复制 accounts.json.example 填写后重启,网页上即显示账号/二维码;不配也可手动登录。"
fi

exec .venv/bin/streamlit run app.py --server.port 8765 --server.headless true
