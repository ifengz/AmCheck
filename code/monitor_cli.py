"""monitor_cli —— 命令行跑一轮采集 + 推送异常通知。

两种定时方式二选一(推荐 A,进程管理更省心):

A. 守护模式(常驻,自己按小时跑):
    nohup ./.venv/bin/python monitor_cli.py --interval 1 &   # 每小时一轮

B. 外部 cron / 宝塔计划任务(每小时拉起一次,跑完即退):
    cd /www/wwwroot/amcheck_git/code
    ./.venv/bin/python monitor_cli.py            # 全站一轮(按站点并行)
    ./.venv/bin/python monitor_cli.py --domain amazon.in

站点间并行各开一个无头浏览器(headless=True,服务器无需显示器),
站点内仍逐条带随机间隔,防风控。

通知渠道:优先读 UI「定时与通知」写入的 settings 表;
兼容环境变量(未配 DB 时生效):
    AMCHECK_NOTIFY_WEBHOOK  钉钉/企微机器人 webhook 地址
    AMCHECK_NOTIFY_SECRET   钉钉加签 secret(可选,企微留空)

日志追赶到 /www/wwwroot/amcheck_git/deploy.log 同目录的 monitor.log。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from monitor import store as ms                     # noqa: E402
from monitor.address import PlaywrightAdapter       # noqa: E402
from monitor.pipeline import run_round, run_round_parallel  # noqa: E402
from monitor.notify import notify_new_anomalies     # noqa: E402

DB = Path(__file__).parent / "monitor.db"
LOG = Path(__file__).parent / "monitor.log"


def log(msg: str) -> None:
    line = f"[{datetime.now():%F %T}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ---------- 主流程 ----------

def run_scheduled(domain_filter: str | None = None, parallel: bool = True) -> int:
    from monitor.scheduler import demo_asins
    profs = [p for p in (ms.list_profiles(DB) if DB.exists() else [])
             if p["asin"] not in demo_asins()]   # 演示 ASIN 不真实抓取
    if domain_filter:
        profs = [p for p in profs if p["domain"] == domain_filter]
    if not profs:
        log("没有启用的监控链接,跳过本轮")
        return 0
    domains = sorted({p["domain"] for p in profs})
    log(f"定时采集开始:{len(profs)} 条 / {len(domains)} 站"
        + ("(并行)" if parallel and len(domains) > 1 else ""))
    checked = anomalies = 0
    if parallel and len(domains) > 1:
        # 按站点并行:每站点一个无头浏览器,站点内仍逐条
        def _factory(dom):
            return PlaywrightAdapter(dom)
        r = run_round_parallel(DB, _factory, profiles=profs)
        checked, anomalies = r["checked"], r["anomalies"]
        log(f"  并行完成:检查 {checked}, 异常 {anomalies}")
    else:
        for dom in domains:
            dom_profs = [p for p in profs if p["domain"] == dom]
            adapter = PlaywrightAdapter(dom)
            try:
                r = run_round(DB, adapter, profiles=dom_profs)
                checked += r["checked"]
                anomalies += r["anomalies"]
                log(f"  {dom}: {r['checked']} 条, 异常 {r['anomalies']}")
            finally:
                adapter.close()
    log(f"定时采集完成:检查 {checked}, 异常 {anomalies}")
    pushed = notify_new_anomalies(DB, anomalies)
    if pushed:
        log(f"已推送 {pushed} 条异常通知")
    return 0 if checked else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="AmCheck 监控定时采集 + 通知")
    ap.add_argument("--domain", help="只采集指定站点,如 amazon.in")
    ap.add_argument("--no-notify", action="store_true", help="本轮不推通知")
    ap.add_argument("--interval", type=float, default=0,
                    help="守护模式:每隔 N 小时自动跑一轮(如 --interval 1);"
                         "不填则跑单轮退出(适合外部 cron/宝塔计划任务拉起)")
    ap.add_argument("--serial", action="store_true",
                    help="禁用站点并行,退回逐站串行(调试用)")
    ap.add_argument("--chat", action="store_true",
                    help="守护模式下同时拉起钉钉聊天机器人(Stream 长连接,"
                         "可@机器人查监控/问问题)")
    args = ap.parse_args()
    if args.no_notify:
        os.environ["AMCHECK_NOTIFY_DISABLE"] = "1"
    if args.interval <= 0:
        sys.exit(run_scheduled(args.domain, parallel=not args.serial))

    # 守护模式:常驻进程,按 N 小时循环(误炸异常不退出,记日志继续)
    log(f"进入守护模式:每 {args.interval:g} 小时一轮,Ctrl-C 退出")
    if args.chat:
        from monitor.chatbot import start_in_thread
        start_in_thread(DB)
        log("钉钉聊天机器人已拉起(Stream 长连接)")
    while True:
        try:
            run_scheduled(args.domain, parallel=not args.serial)
        except KeyboardInterrupt:
            log("手动中断,退出")
            break
        except Exception as e:
            log(f"本轮失败,继续下一轮: {e.__class__.__name__}: {e}")
        try:
            time.sleep(args.interval * 3600)
        except KeyboardInterrupt:
            log("手动中断,退出")
            break
