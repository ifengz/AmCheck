"""monitor.pipeline —— 一轮采集→判定→落库的链路(被调度/按钮触发的入口)。

对应架构文档 doc/06 §0 一句话架构:采集 → 结构化快照(追加) → 规则判定
→ 通知+看板。这里把前三步串起来(通知 notify.py 在阶段 5 才做)。

阶段 4 换 Playwright 适配器后,上层的 run_round() 不用改——它只认
BaseAdapter.fetch 返回 SnapshotRecord。
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from .address import BaseAdapter, now_str
from .baseline import current_baseline, ensure_baseline
from .model import default_metric_config
from . import store
from .rules import detect_snapshot, snapshot_usable


def run_round(db_path, adapter: BaseAdapter, profiles=None,
              on_item=None) -> dict:
    """跑一轮:对每个 profile 采集一次、判定、写 snapshots 与 anomalies。

    返回 {"checked": n, "anomalies": n},供看板/调度展示。
    用 mock 适配器时,adapter.fetch 每次返回不同"拍",天然造历史。
    on_item(profile, checked, anomalies) 在每条处理完后回调(进度展示用)。

    残缺快照(风控页/加载不全,价格评分全抓不到)只入库留档,
    不设基线、不做对比 —— 否则首加链接第一轮落在风控页,第二轮
    抓到真数据就会全线"有更新"+ 一堆假异常(首加误报的根因)。
    """
    store.init_db(db_path)
    profs = profiles if profiles is not None else store.list_profiles(db_path)
    checked = anomalies = 0
    for p in profs:
        if not p.get("monitor_enabled", 1):
            continue
        snap = adapter.fetch(p["asin"], p["domain"], p.get("url", ""))
        snap_dict = snap.to_dict()   # 注意:to_dict 里 checked_at 已是 now
        usable = snapshot_usable(snap_dict)
        if not usable:
            snap.note = "残缺快照(风控页/加载不完整),不参与对比与基线"
        snap_id = store.insert_snapshot(db_path, snap)
        checked += 1
        store.update_last_checked(db_path, p["asin"], p["domain"], snap.checked_at)
        if not usable:
            # 型号/变体/判定全部跳过:残缺页什么都不可信,
            # 让下一轮成功采集来接管这条链接的基线与对比
            if on_item:
                on_item(p, 1, 0)
            continue
        # 页面上抓到的型号回写 profile,推送/看板当 SKU 展示
        store.set_model_number(db_path, p["asin"], p["domain"],
                               getattr(snap, "model_number", ""))
        # 变体自动登记:种子 ASIN 抓到的变体登记为子体(默认不采集,
        # 由用户在监控页勾选启用);子体不递归扩散,避免一传十
        if not p.get("seed_asin"):
            store.register_variants(db_path, p["asin"], p["domain"],
                                    list(snap.variations or []))

        prev_snaps = store.snapshots_for(db_path, p["asin"], p["domain"])
        # 含刚写回的本条:period = 全部历史快照 = 该 ASIN 的观测期
        period_snaps = prev_snaps
        # 稳定类对比取「上一条可用快照」—— 残缺快照(风控页)当 prev
        # 会把假变化(空→真值)报成异常,必须跳过
        usable_snaps = [s for s in prev_snaps if snapshot_usable(s)]
        prev = usable_snaps[-2] if len(usable_snaps) >= 2 else None
        base = current_baseline(db_path, p["asin"], p["domain"])

        # 基线本身是残缺快照(修复前的旧数据):前移到本条,并清掉
        # 由脏基线比出来的未确认异常,否则看板永远挂着一堆假异常
        if base is not None and not snapshot_usable(base):
            store.set_baseline(db_path, p["asin"], p["domain"], snap_id)
            store.clear_unconfirmed_anomalies(db_path, p["asin"], p["domain"])
            base = None

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
        if on_item:
            on_item(p, 1, len(det))

    return {"checked": checked, "anomalies": anomalies}


def run_round_parallel(db_path, adapter_factory, profiles=None,
                       on_progress=None, max_workers: int = 6) -> dict:
    """按站点并行跑一轮:每站点一个适配器(独立浏览器),站点间互不阻塞。

    adapter_factory(domain) → BaseAdapter,由调用方决定真实/模拟。
    on_progress(done, total, domain, profile) 在每条完成后回调(UI 进度用;
    回调在工作线程触发,自己保证线程安全——UI 侧只改共享 dict)。
    SQLite 写路径每条独立短事务,并发安全;站点内仍逐条保持节奏防风控。
    """
    store.init_db(db_path)
    profs = profiles if profiles is not None else store.list_profiles(db_path)
    enabled = [p for p in profs if p.get("monitor_enabled", 1)]
    by_dom: dict[str, list[dict]] = {}
    for p in enabled:
        by_dom.setdefault(p["domain"], []).append(p)
    total = len(enabled)
    if not total:
        return {"checked": 0, "anomalies": 0}

    state = {"done": 0, "anomalies": 0}
    lock = threading.Lock()

    def _dom_job(domain: str) -> None:
        adapter = adapter_factory(domain)

        def _item(profile, checked, anomalies):
            # 每完成一条:累计并回调(UI 显示当前 ASIN;加锁保计数一致)
            with lock:
                state["done"] += checked
                state["anomalies"] += anomalies
                if on_progress:
                    on_progress(state["done"], total, domain, profile)

        try:
            run_round(db_path, adapter, profiles=by_dom[domain], on_item=_item)
        finally:
            adapter.close()

    with ThreadPoolExecutor(max_workers=min(max_workers, len(by_dom) or 1)) as ex:
        for dom in list(by_dom):
            ex.submit(_dom_job, dom)
    return {"checked": state["done"], "anomalies": state["anomalies"]}


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
