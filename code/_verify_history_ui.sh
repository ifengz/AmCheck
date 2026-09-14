#!/usr/bin/env bash
# 一次性验证脚本:起服务 → 浏览器实测历史页 → 收工。
# 之所以要在同一个进程组里做完,是因为本机沙箱会在 Bash 调用结束时回收
# 后台进程组,单独 run_in_background 起的服务活不过一次工具调用。
#
# 用 AMREVIEW_PORT 把服务起在**专用端口**上(ui.py 支持这个环境变量),
# 这样不会和任何人正在跑的 8765 实例抢端口,也就不需要「清场杀进程」——
# 之前那版会无差别 kill 占用 8765 的进程,在多人/多会话并行时很危险。
set -u
cd "$(dirname "$0")"
OUT=/Users/ifengz/CodingCase/AmCheck/shots_after
PORT="${AMREVIEW_PORT:-8799}"
mkdir -p "$OUT"

# 只清掉上一轮遗留的 agent-browser 页面状态(抽屉还开着会让点击落空)。
# 用 close 而不是 close --all:后者会连别人正在用的会话一起关掉。
agent-browser close >/dev/null 2>&1
sleep 1

# 所有 JS 都用单引号包住,避免与 shell 的 $() / " " 嵌套打架
JS_COLS='JSON.stringify([...document.querySelectorAll(".ag-header-cell")].map(function(e){var w=Math.round(e.getBoundingClientRect().width);var pin=e.closest(".ag-pinned-left-header")?"L":(e.closest(".ag-pinned-right-header")?"R":"");return e.textContent.trim().slice(0,6)+":"+w+(pin?"@"+pin:"");}))'
JS_ROWS='JSON.stringify({rows:document.querySelectorAll(".ag-row").length,refs:[...document.querySelectorAll(".ag-cell[col-id=order_ref]")].map(function(e){return e.textContent;})})'
JS_BTNS='JSON.stringify([...document.querySelectorAll(".q-drawer .q-btn__content")].map(function(b){return b.textContent.trim();}).filter(Boolean))'
JS_DRAWER='JSON.stringify((function(){var d=document.querySelector(".q-drawer");var g=document.querySelector(".q-drawer .ag-drawer-fill");if(!d||!g)return{err:"missing"};var dr=d.getBoundingClientRect(),gr=g.getBoundingClientRect();return{drawerH:Math.round(dr.height),gridH:Math.round(gr.height),gapBottom:Math.round(dr.bottom-gr.bottom)};})())'

AMREVIEW_PORT="$PORT" env -u PYTHONPATH ./.venv/bin/python ui.py > /tmp/amreview_ui.log 2>&1 &
APP=$!
trap 'kill $APP 2>/dev/null; wait $APP 2>/dev/null' EXIT

code=""
for i in $(seq 1 40); do
  code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/history" || true)
  [ "$code" = "200" ] && break
  sleep 0.5
done
if [ "$code" != "200" ]; then
  echo "!! 服务没起来(端口 $PORT,http=$code),看 /tmp/amreview_ui.log" >&2
  tail -20 /tmp/amreview_ui.log >&2
  exit 1
fi
echo "== app http: $code (端口 $PORT, pid $APP)"

ab() { agent-browser "$@"; }

ab open "http://127.0.0.1:$PORT/history" >/dev/null 2>&1
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
