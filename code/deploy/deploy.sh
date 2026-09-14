#!/bin/bash
# 服务器部署脚本(git 方式):拉取 → 装依赖 → 停旧实例 → 起新实例 → 健康检查
# 服务器路径: /www/wwwroot/amcheck_git/deploy.sh(由宝塔 WebHook 触发)
# 本文件是**仓库里的副本**,服务器上被 WebHook 触发的那份在仓库根级、git 不跟踪它。
# → 改完本文件后必须手动同步到服务器,否则「提交到仓库」不会自动生效。
#   先确认线上执行的到底是哪一份(只读,两条命令):
#     ls -l /www/wwwroot/amcheck_git/deploy.sh /www/wwwroot/amcheck_git/code/deploy/deploy.sh
#     git -C /www/wwwroot/amcheck_git ls-files | grep deploy.sh
#   若执行的是根级那份(未跟踪):直接改它**不会**被 git 冲掉,但也不会被 git 更新,
#   必须人工同步;若执行的是 code/deploy 那份:改服务器会被 reset --hard 冲掉,只能走 git。
#
# ── 定时采集(监控页每小时自动跑一轮)二选一 ──
# A. 宝塔计划任务 → Shell 脚本,每小时执行:
#      cd /www/wwwroot/amcheck_git/code && ./.venv/bin/python monitor_cli.py
# B. 常驻守护(部署后执行一次,开机自启可再挂 rc-local):
#      PYTHONUNBUFFERED=1 nohup ./.venv/bin/python monitor_cli.py --interval 1 \
#        > /www/wwwlogs/amcheck/cron.log 2>&1 &
# 通知机器人:环境变量 AMCHECK_NOTIFY_WEBHOOK(钉钉/企微),可选 AMCHECK_NOTIFY_SECRET
set -e
cd /www/wwwroot/amcheck_git/code
export GIT_SSH_COMMAND="ssh -i /root/.ssh/amcheck_deploy -o StrictHostKeyChecking=no"
git fetch --prune origin
git reset --hard origin/main
./.venv/bin/pip install -q -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt

# 端口跟应用保持一致:应用读 AMREVIEW_PORT(默认 8765),这里别再写死一个字面量
PORT="${AMREVIEW_PORT:-8765}"
APP_PATTERN="python ui.py"
LOG_DIR=/www/wwwlogs/amcheck
CONSOLE_LOG="$LOG_DIR/console.log"
mkdir -p "$LOG_DIR"

# 防呆:模式为空时 pkill 的行为不可预期(有的实现会匹配全部进程),宁可不杀
if [ -z "$APP_PATTERN" ]; then
  echo "!! APP_PATTERN 为空,拒绝执行停止流程" >&2
  exit 1
fi

# 端口是否还有人在听。ss → lsof → /dev/tcp 三档回退:服务器上不一定都装了。
port_busy() {
  if command -v ss >/dev/null 2>&1; then
    ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE "[:.]${PORT}$"
  elif command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN >/dev/null 2>&1
  else
    (exec 3<>"/dev/tcp/127.0.0.1/${PORT}") 2>/dev/null
  fi
}

# ── 停旧实例:TERM → 等 3 秒 → 仍在则 KILL → 确认端口真的释放 ──
# 2026-09-14 事故:进程僵死时 SIGTERM 完全无效,而原脚本只 `sleep 2` 就启动,
# 结果新实例抢不到端口、健康检查失败,旧僵尸还挂在后台。所以这里必须
# 「确认死了」才往下走 —— 端口没释放就启动,新实例一定起不来。
pkill -f "$APP_PATTERN" 2>/dev/null || true
for i in 1 2 3; do
  sleep 1
  if ! pgrep -f "$APP_PATTERN" >/dev/null 2>&1; then break; fi
done
if pgrep -f "$APP_PATTERN" >/dev/null 2>&1; then
  echo "!! SIGTERM 3 秒无效,改用 SIGKILL" >&2
  pkill -9 -f "$APP_PATTERN" 2>/dev/null || true
  sleep 1
fi
for i in 1 2 3 4 5; do
  if ! port_busy; then break; fi
  echo "端口 ${PORT} 仍被占用,等待释放…(${i}/5)" >&2
  sleep 1
done

# fd 上限:无头浏览器 + 长跑 socket 会开大量句柄,默认 1024 会中途 EMFILE
ulimit -n 65535 2>/dev/null || echo "!! fd 上限未抬到 65535(当前 $(ulimit -n))" >&2

# console.log 只兜「日志模块装好之前」的 stdout/stderr(导入期崩溃、uvicorn 启动报错)。
# 应用自己的结构化日志由 logsetup 写到同目录的 ui-YYYY-MM-DD.log,按天切、自动清理,
# 所以这里不再截断/滚动应用日志 —— 每次部署截断正是 9-14 事故丢日志的原因之一。
if [ -f "$CONSOLE_LOG" ]; then mv "$CONSOLE_LOG" "$CONSOLE_LOG.1"; fi
PYTHONUNBUFFERED=1 nohup ./.venv/bin/python ui.py > "$CONSOLE_LOG" 2>&1 &
NEW_PID=$!

for i in 1 2 3 4 5 6 7 8 9 10; do
  sleep 2
  if curl -fs -o /dev/null --max-time 2 "http://127.0.0.1:${PORT}/"; then
    echo "DEPLOY_OK pid=${NEW_PID} $(git log --oneline -1)"
    exit 0
  fi
done

# 健康检查失败:先留证据,再把半死不活的新实例收掉 ——
# 留着它只会让下一次部署继续撞端口,而且它已经证明自己起不来了。
echo "DEPLOY_HEALTH_FAIL" >&2
echo "--- 诊断:端口 / 进程 / 日志 ---" >&2
if port_busy; then
  echo "端口 ${PORT}: 有监听" >&2
else
  echo "端口 ${PORT}: 无监听" >&2
fi
pgrep -af "$APP_PATTERN" >&2 2>/dev/null || echo "(没有 ui.py 进程)" >&2
if [ -f "$CONSOLE_LOG" ]; then
  echo "--- console.log 末 50 行 ---" >&2
  tail -n 50 "$CONSOLE_LOG" >&2
fi
for f in "$LOG_DIR"/ui-*.log; do
  if [ -f "$f" ]; then
    echo "--- $(basename "$f") 末 50 行 ---" >&2
    tail -n 50 "$f" >&2
  fi
done
pkill -9 -f "$APP_PATTERN" 2>/dev/null || true
exit 1
