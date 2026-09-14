"""monitor.board —— 链接监控统一看板(**Streamlit 渲染层**)。

对应架构文档 doc/06 §7。用 monitor 包的数据(不入 app.py 的 history.db),
把"该盯的链接"以**一张直观表格**呈现:
- 每行 = 一个被监控 ASIN 的最新快照,带异常徽标(🚨 价格 / ⚠ 新增差评 / 正常)
- 异常优先,再按跟踪时间倒序;支持站点筛选 + 全文搜索(标题/ASIN/URL)
- 点选某行 → 弹窗看这条的变化史(历次快照)+ 当前 vs 基线对比 + 确认无误
- 基线确认:异常行给"确认无误→前移基线"按钮(闭环关键操作)

**本模块只做 Streamlit 渲染,不 import 任何非 Streamlit 的消费方。**
数据聚合与展示整形在 monitor/view.py(纯逻辑、零框架依赖)——需要查询/整形
请 import view,别 import 本模块,否则会把整个 Streamlit 栈拖进调用方进程
(2026-09-14 解耦审计的根因,详见 view.py 顶部说明)。
"""

from __future__ import annotations

import streamlit as st

from . import store
from .baseline import current_baseline
from .model import short_domain
from .view import (diff_line, get_board_data, has_unconfirmed, table_frame,
                   table_row)

ALL_DOMAINS = "全部站点"


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
        left_c, right_c = st.columns([1.0, 0.9], vertical_alignment="center")
        with left_c:
            sel = st.selectbox("站点", [ALL_DOMAINS] + doms, key="mon_dom",
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
        return sel == ALL_DOMAINS or short_domain(key_domain) == d

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
        row = table_row(db_path, asin, domain, snap, anom_by_key)
        if _match(row, q):
            rows.append(row)

    # ── 排序:异常优先(按严重度),再按最新快照时间倒序 ──
    rows.sort(key=lambda r: (r["_sev"], -r["_ts"]))

    # ── 摘要(基于筛选后):未加筛选时与标题副行重复,不重复展示 ──
    filtered = sel != ALL_DOMAINS or bool(q)
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
        table_frame(rows), use_container_width=True, hide_index=True,
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
                f"{short_domain(domain)}  ·  "
                f"共 {len(snaps)} 次快照")

    # 当前 vs 基线对比:压缩为单行内联 diff(字段没变只显示一次,变了才显示 基线 → 当前)
    b = base or snaps[0]
    cur = snaps[-1]
    t0 = (b.get("checked_at") or "")[5:16]
    t1 = (cur.get("checked_at") or "")[5:16]
    ts = f"{t0} → {t1}" if t0 != t1 else t1
    st.markdown(f"`基线 {ts}`  ·  " + diff_line(b, cur))

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
        if has_unconfirmed(db_path, asin, domain):
            if st.button("确认无误 → 前移基线",
                         icon=":material/verified:",
                         help="把基线前移到最新快照,并清掉该链路的未确认异常"):
                _do_confirm(db_path, {
                    "asin": asin, "domain": domain, "id": 0})
        else:
            st.caption("该链路当前无未确认异常")


def _page_header(title: str, meta: str = ""):
    """与 app.py 共用同款标题条样式,保持三页基线一致。"""
    left, right = st.columns([300, 560], vertical_alignment="center")
    with left:
        st.markdown(f'<div class="pg-title">{title}</div>', unsafe_allow_html=True)
        if meta:
            st.markdown(f'<div class="pg-meta">{meta}</div>', unsafe_allow_html=True)
    return right
