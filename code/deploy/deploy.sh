#!/bin/bash
# 服务器部署脚本(git 方式):拉取 → 装依赖 → 重启 → 健康检查
# 服务器路径: /www/wwwroot/amcheck_git/deploy.sh(由宝塔 WebHook 触发)
# 改动本文件后需手动同步到服务器,或经初始化脚本首次写入
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
