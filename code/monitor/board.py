"""monitor.board —— 链接监控统一看板(Streamlit 视图)。

对应架构文档 doc/06 §7。用 monitor 包的数据(不入 app.py 的 history.db),
把"该盯的链接"以**一张直观表格**呈现:
- 每行 = 一个被监控 ASIN 的最新快照,带异常徽标(🚨 价格 / ⚠ 新增差评 / 正常)
- 异常优先,再按跟踪时间倒序;支持站点筛选 + 全文搜索(标题/ASIN/URL)
- 点选某行 → 弹窗看这条的变化史(历次快照)+ 当前 vs 基线对比 + 确认无误
- 基线确认:异常行给"确认无误→前移基线"按钮(闭环关键操作)

纯渲染,不碰写入;数据由调用方传入(依赖 board.get_board_data 聚合)。
"""

from __future__ import annotations

import streamlit as st

from . import store
from .baseline import current_baseline
from .rules import METRIC_LABELS

_SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}

# 站点显示为两位国家码,与 ui.py 的 DOMAIN_SHORT 保持同一套映射
DOMAIN_CC = {"amazon.com": "US", "amazon.co.uk": "UK", "amazon.de": "DE",
             "amazon.co.jp": "JP", "amazon.com.au": "AU", "amazon.in": "IN",
             "amazon.com.mx": "MX", "amazon.com.br": "BR", "amazon.es": "ES",
             "amazon.it": "IT", "amazon.fr": "FR", "amazon.ca": "CA"}

# monitor 用 alive/deleted/blocked/unavailable;engine 用 alive/deleted/blocked/
# login_expired/unknown。统一映射,避免 unavailable 这类裸显成英文字符串。
_STATUS_TEXT = {
    "alive": "正常",
    "deleted": "已删",
    "blocked": "被拦截",
    "unavailable": "下架",
    "login_expired": "登录失效",
    "unknown": "未知",
}


def _status_label(s: str | None) -> str:
    """把两套状态词统一成中文标签;未知值原样返回。"""
    if not s:
        return "未知"
    return _STATUS_TEXT.get(s, str(s))


def _latest_by_profile(db_path) -> dict:
    """{asin: 最新快照 dict}。只取启用的 profile。"""
    out = {}
    for p in store.list_profiles(db_path):
        s = store.latest_snapshot(db_path, p["asin"], p["domain"])
        if s:
            out[(p["asin"], p["domain"])] = s
    return out


def _anomaly_rows(db_path) -> list[dict]:
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
    rows.sort(key=lambda r: _SEVERITY_RANK.get(r["anomaly"]["severity"], 9))
    return rows


def get_board_data(db_path) -> dict:
    """聚合看板需要的全部数据(异常优先区 + 正常区 + 摘要)。"""
    latest = _latest_by_profile(db_path)
    anomalies = _anomaly_rows(db_path)
    anom_keys = {(a["anomaly"]["asin"], a["anomaly"]["domain"]) for a in anomalies}

    abnormal, normal = [], []
    for (asin, domain), snap in latest.items():
        row = {"asin": asin, "domain": domain, "snap": snap}
        if (asin, domain) in anom_keys:
            abnormal.append(row)
        else:
            normal.append(row)

    def short(d):
        return DOMAIN_CC.get(d, d.replace("amazon.", ""))
    domains = sorted({short(d) for d in {k[1] for k in latest}})

    return {
        "latest": latest,
        "anomalies": anomalies,
        "abnormal": abnormal,
        "normal": normal,
        "domains": domains,
        "total": len(latest),
        "abnormal_count": len(abnormal),
    }


def render(db_path) -> None:
    """渲染链接监控统一表格。db_path 为 monitor 三表的库文件路径。

    数据源:get_board_data(最新快照 + 异常优先) + snapshots_for(上次对比)。
    交互:筛选/搜索 → 排序 → 表格 → 点选行 → _history_dialog(含确认无误)。
    """
    data = get_board_data(db_path)
    latest = data["latest"]

    # ── 标题条 + 摘要 + 站点筛选 ──
    right = _page_header("链接监控",
                         f"监控 {data['total']} 条 · 异常 {data['abnormal_count']} 条")
    with right:
        doms = data["domains"]
        ALL = "全部站点"
        left_c, right_c = st.columns([1.0, 0.9], vertical_alignment="center")
        with left_c:
            sel = st.selectbox("站点", [ALL] + doms, key="mon_dom",
                               label_visibility="collapsed",
                               help="按国家/站点筛选,默认聚合所有站点")
        with right_c:
            q = st.text_input("搜索", "", key="mon_q",
                              placeholder="搜索标题 / ASIN / URL …",
                              label_visibility="collapsed")

    if not latest:
        st.info("暂无监控数据。先在侧边栏载入演示数据,或跑一轮采集。",
                icon=":material/monitoring:")
        return

    # ── 聚合每行为 dict(最新快照 + 上次对比 + 异常徽标) ──
    def _in_domain(d, key_domain):
        return sel == ALL or (DOMAIN_CC.get(key_domain, key_domain.replace("amazon.", "")) == d)

    def _match(row, qq):
        if not qq:
            return True
        qq = qq.lower()
        return any(qq in str(row.get(k) or "").lower()
                   for k in ("title", "asin", "url"))

    rows = []
    anom_by_key = {(a["anomaly"]["asin"], a["anomaly"]["domain"]): a["anomaly"]
                   for a in data["anomalies"]}
    for (asin, domain), snap in latest.items():
        if not _in_domain(sel, domain):
            continue
        row = _table_row(db_path, asin, domain, snap, anom_by_key)
        if _match(row, q):
            rows.append(row)

    # ── 排序:异常优先(按严重度),再按最新快照时间倒序 ──
    rows.sort(key=lambda r: (r["_sev"], -r["_ts"]))

    # ── 摘要(基于筛选后):未加筛选时与标题副行重复,不重复展示 ──
    filtered = sel != ALL or bool(q)
    abnormal = sum(1 for r in rows if r["_sev"] < 9)
    if filtered:
        st.markdown(
            f'<div class="pg-meta">筛选后 {len(rows)} 条 · 异常 {abnormal} 条</div>',
            unsafe_allow_html=True)

    if not rows:
        st.info("没有符合当前筛选条件的记录", icon=":material/search_off:")
        return

    # ── 点选行 → 弹窗 ──
    sel_state = st.dataframe(
        _table_frame(rows), use_container_width=True, hide_index=True,
        row_height=28,
        height=min(1000, 40 + 28 * len(rows)),
        on_select="rerun", selection_mode="single-row",
        column_config={
            "状态": st.column_config.TextColumn(width="small", pinned=True),
            "ASIN": st.column_config.TextColumn(width="small", pinned=True),
            "站点": st.column_config.TextColumn(width="small"),
            "标题": st.column_config.TextColumn(width="large"),
            "价格": st.column_config.TextColumn(width="small"),
            "评分": st.column_config.TextColumn(width="small"),
            "评价数": st.column_config.TextColumn(width="small"),
            "BuyBox": st.column_config.TextColumn(width="small"),
            "上下架": st.column_config.TextColumn(width="small"),
            "上次": st.column_config.TextColumn(
                width="small", help="上次快照的状态与价格,⚠ 表示与本次不同"),
            "跟踪时间": st.column_config.TextColumn(width="small"),
            "原页面": st.column_config.LinkColumn(
                width="small", display_text="打开", help="在 Amazon 打开原页面复核"),
        })
    sel_rows = (sel_state or {}).get("selection", {}).get("rows", [])
    if sel_rows:
        idx = sel_rows[0]
        asin = rows[idx]["_asin"]
        domain = rows[idx]["_domain"]
        _history_dialog(db_path, asin, domain)


def _table_row(db_path, asin: str, domain: str,
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
        last = f"{'⚠ ' if changed else ''}{_status_label(pstatus)} · {fmt(p_price)}"
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
        "状态": _status_label(status),
        "ASIN": asin,
        "站点": DOMAIN_CC.get(domain, domain.replace("amazon.", "")),
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
        "_sev": _SEVERITY_RANK.get(sev, 9),
        "_ts": ts_num,
    }


def _table_frame(rows: list[dict]) -> list[dict]:
    """剥掉私有字段,只下发可展示列(顺序即表格列序)。"""
    keys = ["异常", "状态", "ASIN", "站点", "标题", "价格", "评分", "评价数",
            "BuyBox", "上下架", "上次", "跟踪时间", "原页面"]
    return [{k: r[k] for k in keys} for r in rows]


def _do_confirm(db_path, a: dict) -> None:
    """确认无误:把基线前移到该 ASIN 最新快照,并清掉该链路全部未确认异常。

    a 只需含 asin/domain(id 仅供参考;确认按链路清,不按单条)。
    """
    from .pipeline import confirm_and_move_baseline
    confirm_and_move_baseline(db_path, a["asin"], a["domain"])
    for an in store.unconfirmed_anomalies(db_path, limit=500):
        if an["asin"] == a["asin"] and an["domain"] == a["domain"]:
            store.confirm_anomaly(db_path, an["id"])
    st.rerun()


@st.dialog("变化史", width="large")
def _history_dialog(db_path, asin: str, domain: str):
    """点开看这条 ASIN 的变化史(历次快照)+ 当前 vs 基线对比。"""
    snaps = store.snapshots_for(db_path, asin, domain)
    if not snaps:
        st.caption("暂无快照")
        return
    base = current_baseline(db_path, asin, domain)
    p = store.get_profile(db_path, asin, domain)
    title = (p or {}).get("title", asin)

    st.markdown(f"**{title}**  ·  {asin}  ·  "
                f"{DOMAIN_CC.get(domain, domain.replace('amazon.', ''))}  ·  "
                f"共 {len(snaps)} 次快照")

    # 当前 vs 基线对比:压缩为单行内联 diff(字段没变只显示一次,变了才显示 基线 → 当前)
    b = base or snaps[0]
    cur = snaps[-1]
    t0 = (b.get("checked_at") or "")[5:16]
    t1 = (cur.get("checked_at") or "")[5:16]
    ts = f"{t0} → {t1}" if t0 != t1 else t1
    st.markdown(f"`基线 {ts}`  ·  " + _diff_line(b, cur))

    st.divider()
    st.markdown("**变化史(历次快照)**")
    # 倒序展示(最新在上),只挑动态字段列
    rows = []
    for s in reversed(snaps):
        rows.append({
            "时间": (s.get("checked_at") or "")[5:16],
            "状态": s.get("status"),
            "价格": s.get("price") or "—",
            "评分": s.get("rating") or "—",
            "评价数": s.get("review_count") or "—",
            "BuyBox": s.get("buybox") or "—",
            "差评": (s.get("home_reviews") or {}).get("recent_bad", 0),
        })
    st.dataframe(rows, hide_index=True, use_container_width=True, height=200)

    # 原页面 + 确认无误(闭环:确认 → 前移基线 → 重跑,异常自动消失)
    cols = st.columns([1.0, 1.0])
    with cols[0]:
        if p and p.get("url"):
            st.link_button("打开原页面", p["url"], icon=":material/open_in_new:")
    with cols[1]:
        if _has_unconfirmed(db_path, asin, domain):
            if st.button("确认无误 → 前移基线",
                         icon=":material/verified:",
                         help="把基线前移到最新快照,并清掉该链路的未确认异常"):
                _do_confirm(db_path, {
                    "asin": asin, "domain": domain, "id": 0})
        else:
            st.caption("该链路当前无未确认异常")


def _has_unconfirmed(db_path, asin: str, domain: str) -> bool:
    """某 ASIN 在当前站点是否有未确认异常(供弹窗判断要不要给确认按钮)。"""
    for a in store.unconfirmed_anomalies(db_path, limit=500):
        if a["asin"] == asin and a["domain"] == domain:
            return True
    return False


def _diff_line(base: dict, cur: dict) -> str:
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


def _page_header(title: str, meta: str = ""):
    """与 app.py 共用同款标题条样式,保持三页基线一致。"""
    left, right = st.columns([300, 560], vertical_alignment="center")
    with left:
        st.markdown(f'<div class="pg-title">{title}</div>', unsafe_allow_html=True)
        if meta:
            st.markdown(f'<div class="pg-meta">{meta}</div>', unsafe_allow_html=True)
    return right
