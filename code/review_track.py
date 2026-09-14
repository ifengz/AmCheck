"""review_track —— 评价链接的每日分散定时跟踪。

需求:全部评价链接都要定时跟踪,但**每天一次就够,且不能所有链接挤在同一时刻**
(同一时间集中请求容易触发 Amazon 风控)。做法是把链接均匀铺满 24 小时:

1. ``review_db.daily_slots`` 按 review_id 哈希稳定排序后等分到 0~86399 秒,
   再加 ±7 分钟确定性抖动 —— N 条链接彼此间隔约 24h/N,集合不变时每天次序一致;
2. 后台线程每 2 分钟醒一次,只把"当日轮到且今天还没查过"的链接取出来;
3. 每轮最多处理 ``review_track_batch`` 条(默认 3,小批量),站点内由引擎
   自带 3~5 秒随机间隔;
4. 连续 ``STOP_STREAK``(5)次判定为「已删/变狗」的链接自动停止跟踪,不再浪费请求。

配置沿用 monitor.db 的 settings 表(与「定时与通知」弹窗同一处):
    review_track_enabled  "1"/"0"   总开关
    review_track_batch    每轮最多几条(默认 3)
    review_track_last_run 上次运行时间(展示用)
    review_track_last_stat 上次结果摘要
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

import review_db as rdb
from engine import ReviewChecker, ReviewRef

log = logging.getLogger("review-track")

TICK = 120.0          # 线程轮询粒度(秒)
_BATCH_MIN, _BATCH_MAX = 1, 20

_run_lock = threading.Lock()   # 定时跟踪与手动"立即跟踪"互斥,不并发开浏览器


def _settings():
    """settings 存取(monitor.db),延迟导入避免循环依赖。"""
    from monitor import store as ms
    return ms


def get_cfg(settings_db) -> dict:
    ms = _settings()
    def g(key, default):
        return ms.get_setting(settings_db, key, default)
    try:
        batch = int(float(g("review_track_batch", "3") or 3))
    except ValueError:
        batch = 3
    return {
        "enabled": g("review_track_enabled", "1") == "1",
        "batch": max(_BATCH_MIN, min(_BATCH_MAX, batch)),
        "last_run": g("review_track_last_run", ""),
        "last_stat": g("review_track_last_stat", ""),
    }


def due_links(db_path, settings_db, now: datetime | None = None) -> list[dict]:
    """当日已到点、且今天还没查过的链接,按当日排期先后排序。"""
    links = rdb.list_tracked()
    if not links:
        return []
    now = now or datetime.now()
    sec = now.hour * 3600 + now.minute * 60 + now.second
    today = now.strftime("%Y-%m-%d")
    slots = rdb.daily_slots([l["review_id"] for l in links])
    due = [l for l in links
           if slots.get(l["review_id"], 86399) <= sec
           and (l.get("last_tracked_at") or "")[:10] < today]
    due.sort(key=lambda l: slots.get(l["review_id"], 86399))
    return due


def _run_batch(db_path, settings_db, picks: list[dict]) -> list[dict]:
    """对一批链接跑一次检测:写检测历史 + 刷新台账(含自动停止判断)。"""
    refs = [ReviewRef(raw=m.get("url") or "", review_id=m["review_id"],
                      domain=m["domain"], url=m.get("url") or "")
            for m in picks if m.get("domain") and m.get("url")]
    if not refs:
        return []
    t0 = time.time()
    log.info("跟踪批次开始 共 %d 条:%s", len(refs),
             ", ".join(r.review_id for r in refs))
    checker = ReviewChecker()
    try:
        results = checker.check_batch(refs)
    finally:
        checker.close()
    rdb.save_history(results)
    stopped = []
    for r in results:
        st = rdb.update_track_state(r["review_id"], r["status"], r["checked_at"])
        if st["stopped"]:
            stopped.append(r["review_id"])
    for r in results:
        r["_stopped"] = r["review_id"] in stopped
    log.info("跟踪批次结束 共 %d 条 耗时=%.1fs", len(results), time.time() - t0)
    return results


def run_once(db_path, settings_db, limit: int | None = None,
             force: bool = False) -> dict:
    """跑一轮(供后台线程与"立即跟踪一轮"按钮共用)。

    force=True 时忽略"当日排期是否到点",按排期顺序取前 N 条(手动触发用)。
    """
    if not _run_lock.acquire(blocking=False):
        log.info("已有跟踪轮次在进行中,本次跳过")
        return {"checked": 0, "note": "已有一轮跟踪在进行中"}
    try:
        cfg = get_cfg(settings_db)
        n = limit or cfg["batch"]
        if force:
            links = rdb.list_tracked()
            slots = rdb.daily_slots([l["review_id"] for l in links])
            links.sort(key=lambda l: slots.get(l["review_id"], 86399))
            picks = links[:n]
        else:
            picks = due_links(db_path, settings_db)[:n]
        if not picks:
            return {"checked": 0, "note": "本轮没有到点的链接"}
        log.info("跟踪轮次开始 force=%s 取 %d 条", force, len(picks))
        results = _run_batch(db_path, settings_db, picks)
        tally: dict[str, int] = {}
        for r in results:
            tally[r["status"]] = tally.get(r["status"], 0) + 1
        stopped = [r["review_id"] for r in results if r.get("_stopped")]
        ms = _settings()
        ms.set_setting(settings_db, "review_track_last_run",
                       datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        stat = (f"检查 {len(results)} 条 · "
                + " · ".join(f"{k} {v}" for k, v in sorted(tally.items())))
        if stopped:
            stat += f" · 停止跟踪 {len(stopped)} 条(连续已删)"
        ms.set_setting(settings_db, "review_track_last_stat", stat)
        log.info(stat)
        return {"checked": len(results), "tally": tally, "stopped": stopped,
                "results": results}
    finally:
        _run_lock.release()


class ReviewTracker:
    """后台守护线程:每 TICK 秒看一次"该轮到哪些链接了"。start() 幂等。"""

    def __init__(self, db_path, settings_db):
        self.db = db_path
        self.settings_db = settings_db
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="amreview-review-tracker")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:            # 单轮出错不拖垮线程
                log.exception("跟踪轮次异常,线程继续存活")
                try:
                    _settings().set_setting(
                        self.settings_db, "review_track_last_stat",
                        f"失败 {e.__class__.__name__}: {e}")
                except Exception:
                    pass
            self._stop.wait(TICK)

    def _tick(self) -> None:
        if not get_cfg(self.settings_db)["enabled"]:
            return
        run_once(self.db, self.settings_db)
