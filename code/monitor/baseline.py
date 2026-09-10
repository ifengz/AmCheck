"""monitor.baseline —— 基线管理:首次自动设基点 / 用户"确认无误"后前移。

对应架构文档 doc/06 §4.2。判定核心逻辑是"当前快照 对比 基线"(不是对比上次),
这样卖家自己改价/改标题不会误报。

- 首拍:无基线 → 把第一次快照设为基线(之后动态类才有对比对象)。
- 用户确认一条异常无误 → 基线前移到当前快照,之后以此为基准。
"""

from __future__ import annotations

from . import store


def ensure_baseline(db_path, asin: str, domain: str,
                    snapshot_id: int, checked_at: str) -> bool:
    """若该 ASIN 尚无基线,则设首拍为基线。返回是否新建了基线。"""
    p = store.get_profile(db_path, asin, domain)
    if p and p.get("baseline_snapshot_id") is not None:
        return False
    store.set_baseline(db_path, asin, domain, snapshot_id)
    return True


def move_baseline(db_path, asin: str, domain: str, snapshot_id: int) -> None:
    """用户"确认无误"→ 基线前移到指定快照(通常是最新一条)。"""
    store.set_baseline(db_path, asin, domain, snapshot_id)


def current_baseline(db_path, asin: str, domain: str) -> dict | None:
    """取当前基线所在的快照;无基线返回 None。"""
    p = store.get_profile(db_path, asin, domain)
    if not p or not p.get("baseline_snapshot_id"):
        return None
    snap_id = p["baseline_snapshot_id"]
    snaps = store.snapshots_for(db_path, asin, domain)
    for s in snaps:
        if s["id"] == snap_id:
            return s
    return None
