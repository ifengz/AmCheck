"""monitor.demo —— 演示数据种子:用 mock 适配器造出"有历史、有异常"的监控数据。

对应架构文档 §9 决策"先路线 X":阶段 1-3 不碰真实抓取,用 mock 造历史。
每 ASIN 一条完整时间线(多拍快照),动态值在时间线上波动,让规则引擎能
判出异常、看板能"点开看变化史"。

刻意让几条链路覆盖不同异常类型(价格降/丢BuyBox/新增差评/上下架删除),
另几条保持正常(在售),以演示"异常优先 + 正常折叠"。
"""

from __future__ import annotations

import time

from .address import snapshot_for_step
from . import store
from .pipeline import add_profile

# 每个 ASIN 的完整时间线步进(含 step 语义):见 address.snapshot_for_step
# 长度 = 造几拍快照;step 序列 = 该 ASIN 每次检查落在哪个状态
TIMELINES = {
    # 价格异常:B0TRACK0001 先正常再暴跌再回来,演化出"变化史"
    "B0TRACK0001": {
        "domain": "amazon.com", "url": "https://www.amazon.com/dp/B0TRACK0001/",
        "title": "Wireless Earbuds with Charging Case",
        "parent_asin": "B0PARENT001",
        "steps": [0, 1, 1, 4],        # 基线→价-12%→价-12%回到基线
    },
    # 丢 BuyBox:B0TRACK0002 BuyBox 从一个卖家换成空(被人抢走)
    "B0TRACK0002": {
        "domain": "amazon.in", "url": "https://www.amazon.in/dp/B0TRACK0002/",
        "title": "Stainless Steel Water Bottle 1L",
        "steps": [0, 0, 2, 2],
    },
    # 新增差评:B0TRACK0003 首页差评从 0 涨到 5
    "B0TRACK0003": {
        "domain": "amazon.com.au", "url": "https://www.amazon.com.au/dp/B0TRACK0003/",
        "title": "Portable Blender Juicer Cup",
        "steps": [0, 3, 3, 3],
    },
    # 上下架删除:B0TRACK0004 从在售变已删(最严重)
    "B0TRACK0004": {
        "domain": "amazon.co.jp", "url": "https://www.amazon.co.jp/dp/B0TRACK0004/",
        "title": "LED Desk Lamp with USB Port",
        "steps": [0, 0, 5],
    },
    # 正常(在售,无异常):用足够的拍,演示"正常项收进折叠区"
    "B0TRACK0005": {
        "domain": "amazon.com.mx", "url": "https://www.amazon.com.mx/dp/B0TRACK0005/",
        "title": "Soporte para Laptop de Aluminio",
        "steps": [0, 0, 0, 0],
    },
    "B0TRACK0006": {
        "domain": "amazon.com.br", "url": "https://www.amazon.com.br/dp/B0TRACK0006/",
        "title": "Fone de Ouvido Bluetooth TWS",
        "steps": [0, 0, 0],
    },
    # 全程断货:B0TRACK0007 整个观测期每次检查都是 unavailable(step 6),
    # 触发新规则 "unavailable_period"(该规则默认关闭,这里单独开启以演示)
    "B0TRACK0007": {
        "domain": "amazon.com.mx", "url": "https://www.amazon.com.mx/dp/B0TRACK0007/",
        "title": "Purificador de Aire HEPA (断货演示)",
        "steps": [6, 6, 6, 6],
        "metric_config": {
            "unavailable_period": {"enabled": True, "min_period_checks": 2,
                                  "severity": "warning"},
        },
    },
}

# 时间线基座:每拍相隔 2 小时,从"昨天"开始往今天推
_HOUR = 3600
_TS_FMT = "%Y-%m-%d %H:%M:%S"


def seed_demo(db_path, now: float | None = None) -> int:
    """写入演示数据(幂等:先清本批 ASIN 再重建),返回写入的快照条数。"""
    import time
    store.init_db(db_path)
    now = now or time.time()
    n = 0

    asins = list(TIMELINES)
    # 清旧:删 snapshots + anomalies + profiles(只碰这批演示 ASIN)
    store.delete_by_asins(db_path, asins)

    # 每 ASIN:先建 profile,再逐拍平铺快照;每拍插入后跑一次规则(模拟每次检查)
    for i, (asin, spec) in enumerate(TIMELINES.items()):
        add_profile(
            db_path, asin=asin, domain=spec["domain"], url=spec["url"],
            title=spec["title"], parent_asin=spec.get("parent_asin", ""),
            metric_config=spec.get("metric_config"))
        steps = spec["steps"]
        base_t = now - (len(steps) - 1) * 2 * _HOUR
        for j, step in enumerate(steps):
            ts = time.strftime(_TS_FMT, time.localtime(base_t + j * 2 * _HOUR))
            snap = snapshot_for_step(asin, spec["domain"], spec["url"], step)
            snap.checked_at = ts
            snap.title = spec["title"]
            snap_id = store.insert_snapshot(db_path, snap)
            n += 1
            # 首拍自动设基线(动态类才有对比对象)
            if j == 0:
                store.set_baseline(db_path, asin, spec["domain"], snap_id)
            # 每拍跑一次规则:模拟"这次检查发现异常"
            _run_rules_for(db_path, asin, spec["domain"])

        latest = store.latest_snapshot(db_path, asin, spec["domain"])
        store.update_last_checked(db_path, asin, spec["domain"], latest["checked_at"])

    return n


def _run_rules_for(db_path, asin: str, domain: str) -> None:
    """对某 ASIN 的最新一拍拍规则,把命中写入 anomalies(演示闭环)。

    用"当前已有历史"判定:上次拍=prev,基线=baseline,最新拍=now。
    首次调用 prev 为 None(稳定类跳过),基线上文由首拍设基线保证。
    """
    from .baseline import current_baseline
    from .rules import detect_snapshot
    from .model import default_metric_config

    snaps = store.snapshots_for(db_path, asin, domain)
    if not snaps:
        return
    now = snaps[-1]
    prev = snaps[-2] if len(snaps) >= 2 else None
    base = current_baseline(db_path, asin, domain) or now
    profile = store.get_profile(db_path, asin, domain)
    if profile is None:
        return
    profile.setdefault("metric_config", default_metric_config())
    # 挂上整个观测期快照,供"全程断货"规则评估(与 pipeline.run_round 行为一致)
    now = {**now, "period_snapshots": snaps}
    det = detect_snapshot(profile, base, prev, now)
    for a in det:
        a["checked_at"] = now["checked_at"]
        store.insert_anomaly(db_path, a)
