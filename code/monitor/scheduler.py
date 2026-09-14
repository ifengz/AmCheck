"""scheduler —— 应用内定时采集(后台守护线程,不依赖系统 cron)。

配置存在 settings 表,UI「定时与通知」弹窗改完即生效(线程每轮重读):
    schedule_enabled   "1"/"0"  开关
    schedule_interval_h 小时数,如 "6"
    schedule_last_run  上次完成时间(展示用)
    schedule_last_stat 上次结果摘要

设计:单线程循环,sleep 切成 30s 小段,保证改配置/停机响应及时。
演示 ASIN(B0TRACK*/B0DEL*)不采集——真实抓取会把它们打成脏数据。
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from . import store

log = logging.getLogger("monitor-scheduler")

_TICK = 30.0  # 轮询配置的粒度(秒)


def _demo_asins() -> set[str]:
    try:
        from .demo import TIMELINES
        return set(TIMELINES) | {"B0DEL00001"}
    except Exception:
        return {"B0DEL00001"}


def run_once(db_path) -> dict:
    """跑一轮真实采集(按站点并行)+ 推送新异常通知。返回统计。"""
    from .address import PlaywrightAdapter
    from .pipeline import run_round_parallel
    from .notify import notify_new_anomalies

    skip = _demo_asins()
    profs = [p for p in store.list_profiles(db_path)
             if p["asin"] not in skip]
    if not profs:
        return {"checked": 0, "anomalies": 0, "note": "无启用的真实监控链接"}
    r = run_round_parallel(db_path, lambda dom: PlaywrightAdapter(dom),
                           profiles=profs)
    pushed = notify_new_anomalies(db_path, r["anomalies"])
    return {"checked": r["checked"], "anomalies": r["anomalies"],
            "pushed": pushed}


class Scheduler:
    """后台线程:按 settings 里的间隔跑 run_once。start() 幂等。"""

    def __init__(self, db_path):
        self.db = db_path
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="amcheck-scheduler")
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:
                log.exception("定时采集轮次异常,线程继续存活")
                store.set_setting(self.db, "schedule_last_stat",
                                  f"失败 {e.__class__.__name__}: {e}")
            self._stop.wait(_TICK)

    def _tick(self) -> None:
        if store.get_setting(self.db, "schedule_enabled", "0") != "1":
            return
        try:
            interval_h = float(store.get_setting(self.db,
                                                 "schedule_interval_h", "6"))
        except ValueError:
            interval_h = 6.0
        interval_h = max(interval_h, 0.1)
        last = float(store.get_setting(self.db, "schedule_last_ts", "0") or 0)
        if time.time() - last < interval_h * 3600:
            return
        r = run_once(self.db)
        store.set_setting(self.db, "schedule_last_ts", str(time.time()))
        store.set_setting(self.db, "schedule_last_run",
                          datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        store.set_setting(self.db, "schedule_last_stat",
                          f"检查 {r['checked']} · 异常 {r['anomalies']}"
                          + (f" · 推送 {r['pushed']}" if r.get("pushed") else ""))
        log.info("定时采集完成:检查 %s 条 · 异常 %s 条%s", r["checked"],
                 r["anomalies"],
                 f" · 推送 {r['pushed']}" if r.get("pushed") else "")
