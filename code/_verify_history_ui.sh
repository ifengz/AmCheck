#!/usr/bin/env bash
# 一次性验证脚本:起服务 → 浏览器实测历史页 → 收工。
# 之所以要在同一个进程组里做完,是因为本机沙箱会在 Bash 调用结束时回收
# 后台进程组,单独 run_in_background 起的服务活不过一次工具调用。
set -u
cd "$(dirname "$0")"
OUT=/Users/ifengz/CodingCase/AmCheck/shots_after
mkdir -p "$OUT"

# 先清场:残留的旧服务会占着 8765,新服务绑不上端口,于是测试跑在旧实例上,
# 结果难以复现;残留的 agent-browser 守护还会带着上一轮的页面状态(比如抽屉还开着)。
lsof -nP -iTCP:8765 -sTCP:LISTEN -t 2>/dev/null | xargs -r kill 2>/dev/null
agent-browser close --all >/dev/null 2>&1
sleep 1.5

# 所有 JS 都用单引号包住,避免与 shell 的 $() / " " 嵌套打架
JS_COLS='JSON.stringify([...document.querySelectorAll(".ag-header-cell")].map(function(e){var w=Math.round(e.getBoundingClientRect().width);var pin=e.closest(".ag-pinned-left-header")?"L":(e.closest(".ag-pinned-right-header")?"R":"");return e.textContent.trim().slice(0,6)+":"+w+(pin?"@"+pin:"");}))'
JS_ROWS='JSON.stringify({rows:document.querySelectorAll(".ag-row").length,refs:[...document.querySelectorAll(".ag-cell[col-id=order_ref]")].map(function(e){return e.textContent;})})'
JS_BTNS='JSON.stringify([...document.querySelectorAll(".q-drawer .q-btn__content")].map(function(b){return b.textContent.trim();}).filter(Boolean))'
JS_DRAWER='JSON.stringify((function(){var d=document.querySelector(".q-drawer");var g=document.querySelector(".q-drawer .ag-drawer-fill");if(!d||!g)return{err:"missing"};var dr=d.getBoundingClientRect(),gr=g.getBoundingClientRect();return{drawerH:Math.round(dr.height),gridH:Math.round(gr.height),gapBottom:Math.round(dr.bottom-gr.bottom)};})())'

env -u PYTHONPATH ./.venv/bin/python ui.py > /tmp/amreview_ui.log 2>&1 &
APP=$!
trap 'kill $APP 2>/dev/null; wait $APP 2>/dev/null' EXIT

for i in $(seq 1 40); do
  code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8765/history || true)
  [ "$code" = "200" ] && break
  sleep 0.5
done
echo "== app http: $code (pid $APP)"

ab() { agent-browser "$@"; }

ab open "http://127.0.0.1:8765/history" >/dev/null 2>&1
sleep 3

echo "== 列宽/固定状态(L=左固定 R=右固定 空=可滚动)"
ab eval "$JS_COLS" 2>&1 | tail -1
ab screenshot "$OUT/history_cols.png" >/dev/null 2>&1

echo "== 搜索(回车触发):逐关键词测"
probe() {
  ab fill "input[placeholder^='搜索']" "$1" >/dev/null 2>&1
  sleep 0.3
  ab press Enter >/dev/null 2>&1
  sleep 2.5
  printf '  %-22s -> %s\n' "$1" "$(ab eval "$JS_ROWS" 2>&1 | tail -1)"
}
probe "20321"
probe "403-6215176-3035513"
probe "KF-LY700"
probe "RYODA2Q3GE0DU"
ab screenshot "$OUT/history_search.png" >/dev/null 2>&1

echo "== 清空搜索"
ab fill "input[placeholder^='搜索']" "" >/dev/null 2>&1
ab press Enter >/dev/null 2>&1
sleep 2

echo "== 点第一行「产品型号」格 → 开抽屉"
ab click ".ag-center-cols-container .ag-row[row-index='0'] .ag-cell[col-id='model']" 2>&1 | tail -1
sleep 3

echo "== 抽屉按钮文案"
ab eval "$JS_BTNS" 2>&1 | tail -1

echo "== 抽屉内表格高度 vs 抽屉高度"
ab eval "$JS_DRAWER" 2>&1 | tail -1

ab screenshot "$OUT/history_drawer.png" >/dev/null 2>&1
echo "== shots done"
ab close >/dev/null 2>&1
