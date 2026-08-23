"""页面链接跟踪视图:筛选 + 模糊搜索 + 快照表。

从 app.py 解耦出的独立模块,只负责把 rows(跟踪快照列表)渲染成
"页面链接跟踪"页:标题条 + 筛选工具条 + 统计 + 明细表。
不接触数据库(数据由调用方传入),避免与 app.py 循环依赖。
"""

from __future__ import annotations

import streamlit as st

from engine import STATUS_LABEL

_STATUS_FALLBACK = "unknown"


def _domains(rows: list[dict]) -> list[str]:
    """站点列表(去重、排序):amazon.com -> com。"""
    return sorted({(r.get("domain") or "").replace("amazon.", "")
                   for r in rows if r.get("domain")})


def _statuses(rows: list[dict]) -> list[str]:
    """状态列表(去重),保留一处顺序便于筛选。"""
    seen: list[str] = []
    for r in rows:
        s = r.get("status") or _STATUS_FALLBACK
        if s not in seen:
            seen.append(s)
    return seen


def _match(row: dict, q: str) -> bool:
    """模糊搜索:对标题 / ASIN / URL 做大小写不敏感的子串匹配。"""
    if not q:
        return True
    q = q.lower()
    return any(q in str(row.get(k) or "").lower()
               for k in ("title", "asin", "url"))


def render(rows: list[dict]) -> None:
    """渲染页面链接跟踪页。rows 非空时调用,无数据由调用方先行提示。"""
    left, right = st.columns([300, 560], vertical_alignment="center")
    with left:
        st.markdown('<div class="pg-title">页面链接跟踪</div>', unsafe_allow_html=True)
        st.markdown(
            '<div class="pg-meta">跟踪产品页面快照与状态变化(价格/评分/评价数/上下架)</div>',
            unsafe_allow_html=True)
    with right:
        with st.container(horizontal=True, horizontal_alignment="right"):
            st.button("刷新", icon=":material/refresh:", use_container_width=True)

    # ---- 筛选条:站点 / 状态 / 模糊搜索(紧凑单行) ----
    ALL_DOM = "全部站点"
    ALL_ST = "全部状态"
    doms = _domains(rows)
    stats = _statuses(rows)
    top = st.columns([0.2, 0.2, 0.6], vertical_alignment="center")
    with top[0]:
        sel_dom = st.selectbox("站点", [ALL_DOM] + doms, key="trk_dom",
                               label_visibility="collapsed", help="按国家/站点筛选")
    with top[1]:
        sel_status = st.selectbox(
            "状态", [ALL_ST] + stats, key="trk_status",
            format_func=lambda s: ALL_ST if s == ALL_ST else STATUS_LABEL.get(s, s),
            label_visibility="collapsed", help="按状态筛选")
    with top[2]:
        q = st.text_input("搜索", "", key="trk_q",
                          placeholder="搜索标题 / ASIN / URL …",
                          label_visibility="collapsed")

    # ---- 过滤 ----
    filtered = []
    for r in rows:
        d = (r.get("domain") or "").replace("amazon.", "")
        s = r.get("status") or _STATUS_FALLBACK
        if sel_dom != ALL_DOM and d != sel_dom:
            continue
        if sel_status != ALL_ST and s != sel_status:
            continue
        if not _match(r, q):
            continue
        filtered.append(r)

    # ---- 统计:基于筛选后结果 ----
    alive = sum(1 for r in filtered if r.get("status") == "alive")
    changed = sum(1 for r in filtered
                  if r.get("prev_status") and r["status"] != r["prev_status"])
    st.markdown(
        f'<div class="pg-meta">共 {len(filtered)} 条 · 在售 {alive} · '
        f'较上次变化 {changed}</div>', unsafe_allow_html=True)

    if not filtered:
        st.info("没有符合当前筛选条件的记录", icon=":material/search_off:")
        return

    def prev_cell(r: dict) -> str:
        ps = r.get("prev_status")
        if not ps:
            return "—"
        pprice = r.get("prev_price") or ""
        changed = ps != r["status"] or (pprice and pprice != r.get("price"))
        return f"{'⚠ ' if changed else ''}{STATUS_LABEL.get(ps, ps)} · {pprice}"

    table = [{
        "状态": STATUS_LABEL.get(r.get("status"), r.get("status") or _STATUS_FALLBACK),
        "ASIN": r.get("asin"),
        "站点": (r.get("domain") or "").replace("amazon.", ""),
        "价格": r.get("price") or "—",
        "评分": r.get("rating") or "—",
        "评价数": r.get("review_count") or "—",
        "上下架": r.get("availability") or "—",
        "标题": r.get("title") or "—",
        "上次": prev_cell(r),
        "跟踪时间": (r.get("checked_at") or "")[5:16],
        "原页面": r.get("url"),
    } for r in filtered]

    st.dataframe(
        table, use_container_width=True, hide_index=True, row_height=34,
        height=min(1000, 44 + 34 * len(table)),
        column_config={
            "状态": st.column_config.TextColumn(width="small", pinned=True),
            "ASIN": st.column_config.TextColumn(width="medium", pinned=True),
            "站点": st.column_config.TextColumn(width="small"),
            "价格": st.column_config.TextColumn(width="small"),
            "评分": st.column_config.TextColumn(width="small"),
            "评价数": st.column_config.TextColumn(width="small"),
            "上下架": st.column_config.TextColumn(width="small"),
            "标题": st.column_config.TextColumn(width="medium"),
            "上次": st.column_config.TextColumn(
                width="small", help="上次快照的状态与价格,⚠ 表示与本次不同"),
            "跟踪时间": st.column_config.TextColumn(width="small"),
            "原页面": st.column_config.LinkColumn(
                width="small", display_text="打开", help="在 Amazon 打开原页面复核"),
        })
