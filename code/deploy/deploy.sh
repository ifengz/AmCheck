#!/bin/bash
# 服务器部署脚本(git 方式):拉取 → 装依赖 → 重启 → 健康检查
# 服务器路径: /www/wwwroot/amcheck_git/deploy.sh(由宝塔 WebHook 触发)
# 改动本文件后需手动同步到服务器,或经初始化脚本首次写入
#
# ── 定时采集(监控页每小时自动跑一轮)二选一 ──
# A. 宝塔计划任务 → Shell 脚本,每小时执行:
#      cd /www/wwwroot/amcheck_git/code && ./.venv/bin/python monitor_cli.py
# B. 常驻守护(部署后执行一次,开机自启可再挂 rc-local):
#      nohup ./.venv/bin/python monitor_cli.py --interval 1 > /tmp/amcheck_cron.log 2>&1 &
# 通知机器人:环境变量 AMCHECK_NOTIFY_WEBHOOK(钉钉/企微),可选 AMCHECK_NOTIFY_SECRET
set -e
cd /www/wwwroot/amcheck_git/code
export GIT_SSH_COMMAND="ssh -i /root/.ssh/amcheck_deploy -o StrictHostKeyChecking=no"
git fetch --prune origin
git reset --hard origin/main
./.venv/bin/pip install -q -i https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt
pkill -f "python ui.py" || true
sleep 2
nohup ./.venv/bin/python ui.py > /tmp/amcheck_ui.log 2>&1 &
for i in 1 2 3 4 5 6 7 8 9 10; do
  sleep 2
  if curl -fs -o /dev/null --max-time 2 http://127.0.0.1:8765/; then
    echo "DEPLOY_OK $(git log --oneline -1)"
    exit 0
  fi
done
echo "DEPLOY_HEALTH_FAIL" >&2
exit 1
