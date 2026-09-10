"""monitor.pipeline —— 一轮采集→判定→落库的链路(被调度/按钮触发的入口)。

对应架构文档 doc/06 §0 一句话架构:采集 → 结构化快照(追加) → 规则判定
→ 通知+看板。这里把前三步串起来(通知 notify.py 在阶段 5 才做)。

阶段 4 换 Playwright 适配器后,上层的 run_round() 不用改——它只认
BaseAdapter.fetch 返回 SnapshotRecord。
"""

from __future__ import annotations

from .address import BaseAdapter, now_str
from .baseline import current_baseline, ensure_baseline
from .model import default_metric_config
from . import store
from .rules import detect_snapshot


def run_round(db_path, adapter: BaseAdapter, profiles=None) -> dict:
    """跑一轮:对每个 profile 采集一次、判定、写 snapshots 与 anomalies。

    返回 {"checked": n, "anomalies": n},供看板/调度展示。
    用 mock 适配器时,adapter.fetch 每次返回不同"拍",天然造历史。
    """
    store.init_db(db_path)
    profs = profiles if profiles is not None else store.list_profiles(db_path)
    checked = anomalies = 0
    for p in profs:
        if not p.get("monitor_enabled", 1):
            continue
        snap = adapter.fetch(p["asin"], p["domain"], p.get("url", ""))
        snap_dict = snap.to_dict()   # 注意:to_dict 里 checked_at 已是 now
        snap_id = store.insert_snapshot(db_path, snap)
        checked += 1
        store.update_last_checked(db_path, p["asin"], p["domain"], snap.checked_at)

        prev_snaps = store.snapshots_for(db_path, p["asin"], p["domain"])
        # 含刚写回的本条:period = 全部历史快照 = 该 ASIN 的观测期
        period_snaps = prev_snaps
        prev = prev_snaps[-2] if len(prev_snaps) >= 2 else None
        base = current_baseline(db_path, p["asin"], p["domain"])

        # 首拍自动设基线(动态类才有对比对象)
        if base is None:
            ensure_baseline(db_path, p["asin"], p["domain"], snap_id, snap.checked_at)
            base = snap_dict

        # 把整个观测期快照挂到 now 上,供断货全程规则使用
        snap_dict["period_snapshots"] = period_snaps

        det = detect_snapshot(p, base, prev, snap_dict)
        for a in det:
            store.insert_anomaly(db_path, a)
            anomalies += 1

    return {"checked": checked, "anomalies": anomalies}


def add_profile(db_path, *, asin: str, domain: str, url: str = "",
                title: str = "", parent_asin: str = "",
                metric_config: dict | None = None) -> int:
    """往 profiles 表加一条要监控的 ASIN,返回 id。"""
    store.init_db(db_path)
    return store.upsert_profile(db_path, {
        "asin": asin, "domain": domain, "url": url,
        "parent_asin": parent_asin, "title": title,
        "metric_config": metric_config or default_metric_config(),
    })


def confirm_and_move_baseline(db_path, asin: str, domain: str) -> None:
    """把某 ASIN 的基线前移到它最新一条快照,并清掉这条路线的未确认异常。

    看板"确认无误"按钮的落点:告诉规则层"这个值以后就是正常基准"。
    """
    store.init_db(db_path)
    latest = store.latest_snapshot(db_path, asin, domain)
    if latest:
        store.set_baseline(db_path, asin, domain, latest["id"])
