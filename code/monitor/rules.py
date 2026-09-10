"""monitor.rules —— 判定引擎:每指标开关/阈值/基线逻辑。你的核心 Know-how。

对应架构文档 doc/06 §4。两类指标分开处理:
- 稳定类(title/buybox/variations/status):对比"上次快照",任何变化 = 异常。
- 动态类(price/rating/review_count/bsr/deal_tag/home_reviews):对比"基线",
  变化超阈值才报。

产出 anomaly dict 列表,交给 store 落库、notify 推钉钉、board 展示。
"""

from __future__ import annotations

from typing import Any

from .model import default_metric_config

# 严重度分级(通知优先级):丢 BuyBox/价格剧烈 > 差评 > 小波动
SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}


def _parse_float(v) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _pct_change(new: float, base: float) -> float | None:
    if base == 0:
        return None
    return abs(new - base) / abs(base) * 100


def _new_bad_reviews(home: dict) -> int | None:
    """home_reviews.recent_bad 存的是"最近 N 条差评数据";判定要"较基线新增"。
    若结构里存了累计条数,新-基即为新增数。"""
    if not home:
        return None
    cur = home.get("recent_bad")
    if cur is None:
        return None
    try:
        return int(cur)
    except (TypeError, ValueError):
        return None


def _stable_changed(old, new) -> bool:
    """稳定类:任何可见变化都算异常(标题/图片/BuyBox 自己不该动)。"""
    if old is None:
        return False
    return (str(old) != str(new))


def detect_snapshot(profile: dict, baseline: dict | None,
                    prev: dict | None, now: Any) -> list[dict]:
    """对一次新快照跑规则,返回异常列表。

    - profile: profiles 行(dict,含 metric_config)
    - baseline: 基线所在快照(dict),None 表示尚无基线 → 动态类跳过
    - prev: 上一快照(dict),None 表示首拍 → 稳定类跳过
    - now: 当前快照(dict,store 查询结果或 SnapshotRecord.to_dict())

    稳定类对比 prev;动态类对比 baseline(卖家场景:自己改价不误报)。
    """
    cfg = {**default_metric_config(), **(profile.get("metric_config") or {})}
    anomalies: list[dict] = []
    asin, domain = now["asin"], now["domain"]
    ts = now.get("checked_at") or ""

    # ---- 稳定类:对比上次 ----
    if prev is not None:
        stable = [
            ("title", now.get("title"), prev.get("title"), "info"),
            ("buybox", now.get("buybox"), prev.get("buybox"), "critical"),
            ("variations", now.get("variations"), prev.get("variations"), "warning"),
            ("status", now.get("status"), prev.get("status"), "critical"),
        ]
        for metric, cur_v, old_v, sev in stable:
            if not cfg.get(metric, {}).get("enabled", True):
                continue
            if _stable_changed(old_v, cur_v):
                anomalies.append({
                    "asin": asin, "domain": domain, "metric": metric,
                    "change_type": f"{metric}_changed",
                    "old_value": old_v or "",
                    "new_value": cur_v or "",
                    "severity": sev, "checked_at": ts,
                })

    # ---- 动态类:对比基线 ----
    if baseline is not None:
        # 价格
        pc = cfg.get("price", {})
        new_p = _parse_float(now.get("price_value"))
        base_p = _parse_float(baseline.get("price_value"))
        if pc.get("enabled", True) and new_p is not None and base_p is not None:
            chg = _pct_change(new_p, base_p)
            if chg is not None and chg > pc.get("threshold_pct", 5.0):
                sev = "critical" if chg >= 20 else "warning"
                anomalies.append({
                    "asin": asin, "domain": domain, "metric": "price",
                    "change_type": "price_changed",
                    "old_value": baseline.get("price", base_p),
                    "new_value": now.get("price", new_p),
                    "severity": sev, "checked_at": ts,
                    "_pct": round(chg, 1),
                })

        # 评分
        rc = cfg.get("rating", {})
        new_r = _parse_float(now.get("rating"))
        base_r = _parse_float(baseline.get("rating"))
        if (rc.get("enabled", True) and new_r is not None
                and base_r is not None and abs(new_r - base_r) > rc.get("threshold", 0.3)):
            anomalies.append({
                "asin": asin, "domain": domain, "metric": "rating",
                "change_type": "rating_changed",
                "old_value": baseline.get("rating", base_r),
                "new_value": now.get("rating", new_r),
                "severity": "info", "checked_at": ts,
            })

        # 评价数
        vc = cfg.get("review_count", {})
        new_rc = _parse_float(now.get("review_count"))
        base_rc = _parse_float(baseline.get("review_count"))
        if (vc.get("enabled", True) and new_rc is not None and base_rc is not None):
            chg = _pct_change(new_rc, base_rc)
            if chg is not None and chg > vc.get("threshold_pct", 10.0):
                anomalies.append({
                    "asin": asin, "domain": domain, "metric": "review_count",
                    "change_type": "review_count_changed",
                    "old_value": baseline.get("review_count", base_rc),
                    "new_value": now.get("review_count", new_rc),
                    "severity": "warning", "checked_at": ts,
                    "_pct": round(chg, 1),
                })

        # BSR 排名
        bc = cfg.get("bsr", {})
        new_bsr = _parse_float(now.get("bsr"))
        base_bsr = _parse_float(baseline.get("bsr"))
        if (bc.get("enabled", True) and new_bsr is not None and base_bsr is not None):
            chg = _pct_change(new_bsr, base_bsr)
            if chg is not None and chg > bc.get("threshold_pct", 50.0):
                anomalies.append({
                    "asin": asin, "domain": domain, "metric": "bsr",
                    "change_type": "bsr_changed",
                    "old_value": baseline.get("bsr", base_bsr),
                    "new_value": now.get("bsr", new_bsr),
                    "severity": "info", "checked_at": ts,
                    "_pct": round(chg, 1),
                })

        # DealTag 出现/消失
        dc = cfg.get("deal_tag", {})
        old_deal = baseline.get("deal_tag") or ""
        new_deal = now.get("deal_tag") or ""
        if dc.get("enabled", True) and old_deal != new_deal:
            anomalies.append({
                "asin": asin, "domain": domain, "metric": "deal_tag",
                "change_type": "deal_tag_changed",
                "old_value": old_deal, "new_value": new_deal,
                "severity": "info", "checked_at": ts,
            })

        # 首页差评新增
        hc = cfg.get("home_reviews", {})
        cur_bad = _new_bad_reviews(now.get("home_reviews") or {})
        base_bad = _new_bad_reviews(baseline.get("home_reviews") or {})
        if (hc.get("enabled", True) and cur_bad is not None
                and base_bad is not None and cur_bad - base_bad > hc.get("max_new_bad", 2)):
            anomalies.append({
                "asin": asin, "domain": domain, "metric": "home_reviews",
                "change_type": "new_bad_reviews",
                "old_value": base_bad, "new_value": cur_bad,
                "severity": "warning", "checked_at": ts,
                "_count": cur_bad - base_bad,
            })

    # ---- 断货规则(整个观测期持续断货才通过)----
    oos = check_unavailable_period(profile, now, baseline, prev)
    if oos:
        anomalies.append(oos)

    # 清理内部计数字段(不是落库字段)
    for a in anomalies:
        a.pop("_pct", None)
        a.pop("_count", None)
    return anomalies


def _is_unavailable(snap) -> bool:
    """一次快照是否判为断货(不可用)。

    仅 status=='unavailable' 或 availability 文案含 "currently unavailable" 视为断货。
    其余状态(alive=在售 / deleted=下架 / blocked=被拦截 / unknown=未知)一律不算,
    这样"期内某次没确认到断货"就不会被误判为稳定断货。
    """
    if snap is None:
        return False
    if (snap.get("status") or "") == "unavailable":
        return True
    avail = (snap.get("availability") or "").lower()
    return "currently unavailable" in avail or "no disponible" in avail


def check_unavailable_period(profile: dict, now: dict,
                             baseline: dict | None = None,
                             prev: dict | None = None) -> dict | None:
    """断货规则:只有整个观测期(inspection period,即该 ASIN 的全部历史快照)
    每一次检查都处于 'unavailable' 状态,规则才 PASS。

    关键语义(区别于单次快照判定):
    - 期内只要出现过一次"非断货"(在售/下架/被拦截/未知),规则即 FAIL → 不通过;
    - 快照数 < min_period_checks 视为证据不足 → 返回 None(不通过也不报错);
    - 全期断货 → 返回结论 dict(PASS),由调用方作为一条 anomaly 落库/通知。

    这样把"断货"从"单次读数"升级为"稳定断货"判定,避免偶发风控/网络抖动
    造成的瞬时 unavailable 误报成长期缺货。

    参数:
    - profile: profiles 行(dict,含 metric_config)
    - now:     当前快照(dict,来自 store 查询或 SnapshotRecord.to_dict())
    - baseline / prev: 保留签名兼容 detect_snapshot,本规则实际用的是各自的
                      全量历史,由调用方在 now 上附带 period_snapshots 传入。
    """
    cfg = {**default_metric_config(), **(profile.get("metric_config") or {})}
    rule = cfg.get("unavailable_period", {})
    if not rule.get("enabled", False):
        return None

    period = now.get("period_snapshots") or []
    min_checks = int(rule.get("min_period_checks", 2))
    if len(period) < min_checks:
        return None  # 观测样本不够,不下结论

    # 整个观测期内必须每一次都是断货,否则不是稳定断货
    if not all(_is_unavailable(s) for s in period):
        return None

    # 只在"转为稳定断货"那一刻下发一次,避免每段观测期都重复刷屏通知。
    # 判定方法:去掉最新一拍后,剩余历史是否已经满足全程断货 —— 若已满足,
    # 说明上一轮就报过了,本轮不再重复;只有"本轮刚达到全程断货"才下发。
    prev_period = period[:-1]
    if len(prev_period) >= min_checks and all(_is_unavailable(s) for s in prev_period):
        return None

    asin = now.get("asin", "")
    domain = now.get("domain", "")
    ts = now.get("checked_at") or ""
    return {
        "asin": asin, "domain": domain,
        "metric": "unavailable_period",
        "change_type": "unavailable_for_whole_period",
        "old_value": f"{len(period)} 次检查",
        "new_value": "全程断货",
        "severity": rule.get("severity", "warning"),
        "checked_at": ts,
        "period_checks": len(period),
    }


def summarize(anomalies: list[dict]) -> list[dict]:
    """看板/通知用的摘要视图:把异常转成人类可读的徽标文案。"""
    out = []
    for a in anomalies:
        sev = a.get("severity", "info")
        metric = a.get("metric")
        label = METRIC_LABELS.get(metric, metric)
        old_, new_ = a.get("old_value"), a.get("new_value")
        if metric == "price":
            desc = f"{_fmt(old_)} → {_fmt(new_)}"
        elif metric == "home_reviews":
            desc = f"新增 {a.get('new_value')} 条差评"
        elif metric == "review_count":
            desc = f"{_fmt(old_)} → {_fmt(new_)}"
        else:
            desc = f"{_fmt(old_)} → {_fmt(new_)}"
        out.append({
            "severity": sev,
            "badge": _badge(sev, label),
            "desc": desc,
            "metric": metric,
            "checked_at": a.get("checked_at", ""),
            "raw": a,
        })
    return out


METRIC_LABELS = {
    "title": "标题", "buybox": "丢失 BuyBox", "variations": "变体",
    "status": "上下架", "price": "价格", "rating": "评分",
    "review_count": "评价数", "bsr": "排名", "deal_tag": "Deal",
    "home_reviews": "差评", "unavailable_period": "全程断货",
}


def _fmt(v) -> str:
    if v is None or v == "":
        return "—"
    return str(v)


def _badge(sev: str, label: str) -> str:
    icon = {"critical": "🚨", "warning": "⚠", "info": "ℹ"}.get(sev, "ℹ")
    return f"{icon} {label}"
