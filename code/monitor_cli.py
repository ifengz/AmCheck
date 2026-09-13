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

通知渠道走环境变量(不配则只落库、不打扰):
    AMCHECK_NOTIFY_WEBHOOK  钉钉/企微机器人 webhook 地址
    AMCHECK_NOTIFY_SECRET   钉钉加签 secret(可选,企微留空)
    AMCHECK_NOTIFY_UPTIME_H 同一异常的静默小时数(默认 12,防刷屏)

日志追赶到 /www/wwwroot/amcheck_git/deploy.log 同目录的 monitor.log。
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import base64
import json
import os
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus

sys.path.insert(0, str(Path(__file__).parent))

from monitor import store as ms                     # noqa: E402
from monitor.address import PlaywrightAdapter       # noqa: E402
from monitor.pipeline import run_round, run_round_parallel  # noqa: E402
from monitor.rules import METRIC_LABELS             # noqa: E402

DB = Path(__file__).parent / "monitor.db"
LOG = Path(__file__).parent / "monitor.log"


def log(msg: str) -> None:
    line = f"[{datetime.now():%F %T}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# ---------- 通知 ----------

def _dingtalk_sign(secret: str, ts_ms: int) -> str:
    string = f"{ts_ms}\n{secret}"
    digest = hmac.new(secret.encode(), string.encode(), hashlib.sha256).digest()
    return quote_plus(base64.b64encode(digest).decode())


def push_notify(text: str) -> bool:
    """推到钉钉/企微机器人;未配 webhook 返回 False(静默跳过)。"""
    url = os.environ.get("AMCHECK_NOTIFY_WEBHOOK", "").strip()
    if not url:
        return False
    secret = os.environ.get("AMCHECK_NOTIFY_SECRET", "").strip()
    if secret and "dingtalk" in url:
        ts = str(round(time.time() * 1000))
        url += f"&timestamp={ts}&sign={_dingtalk_sign(secret, int(ts))}"
    body = json.dumps({"msgtype": "text", "text": {"content": text}}).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status == 200
    except Exception as e:
        log(f"通知推送失败: {e.__class__.__name__}: {e}")
        return False


def _anomaly_fingerprint(asin: str, domain: str, metric: str,
                         hours_mute: int) -> bool:
    """同一 (asin, metric) 异常 hours_mute 小时内只推一次(查 anomalies 表)。"""
    with ms._connect(DB) as conn:
        row = conn.execute(
            """SELECT MAX(checked_at) FROM anomalies
               WHERE asin=? AND domain=? AND metric=? AND checked_at >=
                     datetime('now', ?)""",
            (asin, domain, metric, f"-{hours_mute} hours")).fetchone()
    return bool(row and row[0])


def _recent(db: Path, hours: float) -> list[dict]:
    """最近 hours 小时内新产生、未确认的异常(通知只说新话,不翻旧账)。"""
    with ms._connect(db) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT * FROM anomalies
               WHERE confirmed = 0 AND checked_at >= datetime('now', ?)
               ORDER BY
                 CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1
                  ELSE 2 END, id DESC""",
            (f"-{hours} hours",)).fetchall()
    return [dict(r) for r in rows]


def notify_new_anomalies(round_anomaly_count: int, hours: float = 2.0) -> int:
    """把本轮新异常格式化后推送;返回实际推送条数。"""
    if not round_anomaly_count:
        return 0
    fresh = _recent(DB, hours)
    if not fresh:
        return 0
    mute = float(os.environ.get("AMCHECK_NOTIFY_UPTIME_H", "12") or 12)
    sent = 0
    lines = []
    for a in fresh:
        metric = METRIC_LABELS.get(a["metric"], a["metric"])
        if _anomaly_fingerprint(a["asin"], a["domain"], a["metric"], mute):
            continue  # 静默期内,不重复打扰
        detail = " → ".join(x for x in (a.get("old_value"), a.get("new_value"))
                            if x) or a.get("detail", "")
        lines.append(f"{'🚨' if a['severity'] == 'critical' else '⚠'} "
                     f"[{metric}] {a['asin']}({a['domain']}) {detail}")
        sent += 1
    if lines:
        head = (f"AmCheck 监控告警:新增 {sent} 条异常\n"
                f"时间 {datetime.now():%F %T}\n" + "\n".join(lines[:10]) +
                ("\n…" if len(lines) > 10 else ""))
        push_notify(head)
    return sent


# ---------- 主流程 ----------

def run_scheduled(domain_filter: str | None = None, parallel: bool = True) -> int:
    profs = ms.list_profiles(DB) if DB.exists() else []
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
    pushed = notify_new_anomalies(anomalies)
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
    args = ap.parse_args()
    if args.no_notify:
        os.environ["AMCHECK_NOTIFY_WEBHOOK"] = ""
    if args.interval <= 0:
        sys.exit(run_scheduled(args.domain, parallel=not args.serial))

    # 守护模式:常驻进程,按 N 小时循环(误炸异常不退出,记日志继续)
    log(f"进入守护模式:每 {args.interval:g} 小时一轮,Ctrl-C 退出")
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
