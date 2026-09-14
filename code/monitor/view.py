"""monitor.view —— 看板的数据聚合与展示整形(**纯逻辑,零框架依赖**)。

这一层刻意不 import streamlit / nicegui:它产出的是普通 dict / list,
谁想渲染谁自己渲染。原因见 2026-09-14 的解耦审计:

    board.py 原本把「聚合查询」和「Streamlit 渲染」写在同一个模块里,而
    NiceGUI 的 ui.py 需要 `untracked_profiles` / `get_board_data` ——
    于是 `ui.py` 一渲染 /monitor 就被迫加载整个 Streamlit 栈
    (实测 `import monitor.board` → `'streamlit' in sys.modules == True`),
    streamlit 也从"legacy 可选"变成了删不掉的硬依赖。

所以:**查询与整形放这里,渲染放 board.py(Streamlit)/ ui.py(NiceGUI)**。
新增的看板逻辑请写在本模块,别再写回 board.py。

依赖方向:view → store / rules / model,不反向。
"""

from __future__ import annotations

from . import store
from .model import short_domain
from .rules import METRIC_LABELS, snapshot_usable

SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}

# monitor 用 alive/deleted/blocked/unavailable;engine 用 alive/deleted/blocked/
# login_expired/unknown。统一映射,避免 unavailable 这类裸显成英文字符串。
STATUS_TEXT = {
    "alive": "正常",
    "deleted": "已删",
    "blocked": "被拦截",
    "unavailable": "下架",
    "login_expired": "登录失效",
    "unknown": "未知",
}

# 表格列顺序(表头 → 取值 key)。board.py 与 ui.py 共用,避免两版列序漂移。
TABLE_COLUMNS = ["异常", "状态", "ASIN", "站点", "标题", "价格", "评分",
                 "评价数", "BuyBox", "上下架", "上次", "跟踪时间", "原页面"]


def status_label(s: str | None) -> str:
    """把两套状态词统一成中文标签;未知值原样返回。"""
    if not s:
        return "未知"
    return STATUS_TEXT.get(s, str(s))


def latest_by_profile(db_path) -> dict:
    """{asin: 最新**可用**快照 dict}。只取启用的 profile。

    残缺拍(风控页/加载不全)不当展示数据:最新一拍落在风控页时,
    整行字段都是空的,看着像"数据丢了" —— 取最近一条抓到真数据的。
    """
    out = {}
    for p in store.list_profiles(db_path):
        s = store.latest_usable_snapshot(db_path, p["asin"], p["domain"],
                                         snapshot_usable)
        if s:
            out[(p["asin"], p["domain"])] = s
    return out


def untracked_profiles(db_path) -> list[dict]:
    """profiles 里还没有任何**可用**快照的链接,即「已添加、未采集」。

    添加监控只写 profiles、不产生快照,而看板的数据源是快照 —— 不把这批
    单独捞出来,用户加完链接在页面上看不出任何变化(空态时尤其明显:
    页面照旧写着「还没有监控数据」),表现就是「点了添加没反应」。
    只有残缺拍(第一轮全落在风控页)的链接同样没进看板,得留在这里,
    否则用户以为链接加丢了。

    **别用 latest_by_profile 反推**:它只遍历启用的 profile,会把「停用但
    有快照」的链接误判成未采集。这里直接按 profile 问有没有可用快照。
    """
    out = []
    for p in store.list_profiles(db_path, only_enabled=False):
        if not store.latest_usable_snapshot(db_path, p["asin"], p["domain"],
                                            snapshot_usable):
            out.append(p)
    return out


def anomaly_rows(db_path) -> list[dict]:
    """未确认异常(按严重度排序),每个 (asin, metric) 只保留最新一条(去重)。

    架构文档 §6 的"去重"落地:同一指标的异常未确认前,后续每次检查都会再写一条,
    但看板只展示最新一条,避免同一问题刷屏;确认后基线前移,该异常自然消失。
    """
    rows = []
    seen: dict[tuple, dict] = {}
    for a in store.unconfirmed_anomalies(db_path, limit=500):
        key = (a["asin"], a["metric"])
        # anomalies 返回按 id DESC,已是最新在前;保留每个 key 的第一条
        if key in seen:
            continue
        seen[key] = a
        p = store.get_profile(db_path, a["asin"], a["domain"])
        rows.append({
            "anomaly": a,
            "title": (p or {}).get("title", a["asin"]),
            "url": (p or {}).get("url", ""),
        })
    rows.sort(key=lambda r: SEVERITY_RANK.get(r["anomaly"]["severity"], 9))
    return rows


def get_board_data(db_path) -> dict:
    """聚合看板需要的全部数据(异常优先区 + 正常区 + 摘要)。"""
    latest = latest_by_profile(db_path)
    anomalies = anomaly_rows(db_path)
    anom_keys = {(a["anomaly"]["asin"], a["anomaly"]["domain"]) for a in anomalies}

    abnormal, normal = [], []
    for (asin, domain), snap in latest.items():
        row = {"asin": asin, "domain": domain, "snap": snap}
        if (asin, domain) in anom_keys:
            abnormal.append(row)
        else:
            normal.append(row)

    domains = sorted({short_domain(d) for d in {k[1] for k in latest}})

    return {
        "latest": latest,
        "anomalies": anomalies,
        "abnormal": abnormal,
        "normal": normal,
        "domains": domains,
        "total": len(latest),
        "abnormal_count": len(abnormal),
    }


def table_row(db_path, asin: str, domain: str,
              snap: dict, anom_by_key: dict) -> dict:
    """把一条最新快照转成表格行 dict(带上次对比与异常徽标)。

    私有字段用 _ 前缀(不下发到 dataframe),供排序与点选还原用。
    """
    snaps = store.snapshots_for(db_path, asin, domain, limit=2)
    prev = snaps[-2] if len(snaps) >= 2 else None
    a = anom_by_key.get((asin, domain))
    sev = (a or {}).get("severity", "ok")

    def fmt(v):
        return "—" if v in (None, "") else v

    status = snap.get("status") or "alive"
    if prev:
        pstatus, p_price = prev.get("status"), prev.get("price")
        changed = (pstatus != status) or (p_price and p_price != snap.get("price"))
        last = f"{'⚠ ' if changed else ''}{status_label(pstatus)} · {fmt(p_price)}"
        _ts = prev.get("checked_at") or ""
    else:
        last = "—"
        _ts = ""

    if a:
        label = METRIC_LABELS.get(a["metric"], a["metric"])
        # severity 映射徽标颜色
        if sev == "critical":
            badge = f"🚨 {label}"
        elif sev == "warning":
            badge = f"⚠ {label}"
        else:
            badge = f"ℹ {label}"
    else:
        badge = "正常"

    cur_ts = snap.get("checked_at") or ""
    ts_num = 0
    try:
        import datetime as _dt
        ts_num = _dt.datetime.strptime(str(cur_ts).split(".")[0],
                                       "%Y-%m-%d %H:%M:%S").timestamp()
    except Exception:
        ts_num = 0

    return {
        "异常": badge,
        "状态": status_label(status),
        "ASIN": asin,
        "站点": short_domain(domain),
        "标题": snap.get("title") or asin,
        "价格": fmt(snap.get("price")),
        "评分": fmt(snap.get("rating")),
        "评价数": fmt(snap.get("review_count")),
        "BuyBox": fmt(snap.get("buybox")),
        "上下架": fmt(snap.get("availability")),
        "上次": last,
        "跟踪时间": (cur_ts or "")[5:16] or "—",
        "原页面": (store.get_profile(db_path, asin, domain) or {}).get("url", ""),
        # 私有字段
        "_asin": asin,
        "_domain": domain,
        "_sev": SEVERITY_RANK.get(sev, 9),
        "_ts": ts_num,
    }


def table_frame(rows: list[dict]) -> list[dict]:
    """剥掉私有字段,只下发可展示列(顺序即表格列序)。"""
    return [{k: r[k] for k in TABLE_COLUMNS} for r in rows]


def has_unconfirmed(db_path, asin: str, domain: str) -> bool:
    """某 ASIN 在当前站点是否有未确认异常(供弹窗判断要不要给确认按钮)。"""
    for a in store.unconfirmed_anomalies(db_path, limit=500):
        if a["asin"] == asin and a["domain"] == domain:
            return True
    return False


def diff_line(base: dict, cur: dict) -> str:
    """压缩基线 vs 当前:字段没变只显示当前值,变了显示 基线 → 当前。
    base/cur 是 store 查询的 dict 行,字段名即 snapshot 列名(price/rating/...)。"""
    def _fmt(v):
        return "—" if v in (None, "") else f"{v}"
    parts = []
    for label, key in (("价格", "price"), ("评分", "rating"), ("评价数", "review_count")):
        ov, nv = _fmt(base.get(key)), _fmt(cur.get(key))
        parts.append(f"{label} {' → '.join([ov, nv]) if ov != nv else nv}")
    bb_o, bb_n = _fmt(base.get("buybox")), _fmt(cur.get("buybox"))
    parts.append(f"BuyBox {' → '.join([bb_o, bb_n]) if bb_o != bb_n else bb_n}")
    bad_o = (base.get("home_reviews") or {}).get("recent_bad", 0)
    bad_n = (cur.get("home_reviews") or {}).get("recent_bad", 0)
    parts.append(f"差评 {' → '.join([str(bad_o), str(bad_n)]) if bad_o != bad_n else str(bad_n)}")
    return "  ·  ".join(parts)
