"""AmReview Streamlit 主页:粘贴评价链接 → 批量检测 → 结果 + CSV。

布局规范(公共组件,不手搓):
- 顶栏:标题 + 站点登录状态灯 + 「小号登录」按钮(弹窗 @st.dialog)
- 热度条:近 24h 各站拦截率色点(🟢🟡🔴)
- 卡片:st.container(border=True) + st.metric,统一留白
"""

from __future__ import annotations

import csv
import importlib.metadata
import io
import json
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import streamlit as st

import weblogin
from engine import STATUS_LABEL, ReviewChecker, parse_links

DB = Path(__file__).parent / "history.db"
ACCOUNTS_FILE = Path(__file__).parent / "accounts.json"
PROFILE_ROOT = Path.home() / ".amreview" / "profile"
DOMAINS = ["amazon.com", "amazon.com.mx", "amazon.com.br", "amazon.in",
           "amazon.com.au", "amazon.co.jp"]
LOGIN_VALID_DAYS = 30  # storage_state 超过此天数视为可能过期,仍算"绿"但标时间

st.set_page_config(page_title="AmReview 评价检测", page_icon="🔍", layout="wide")

# 轻量全局样式:压缩默认留白,metric 数字放大,状态徽章,Tab 优化
st.markdown("""
<style>
  .block-container {padding-top: 1.2rem; padding-bottom: 2rem; max-width: 1200px;}
  [data-testid="stMetric"] {background:#F7F8FA; border:1px solid #E5E7EB;
    border-radius:10px; padding:12px 14px; transition: all 0.2s ease;}
  [data-testid="stMetric"]:hover {box-shadow: 0 2px 8px rgba(0,0,0,0.08);}
  [data-testid="stMetricLabel"] {font-size:.82rem; color:#6B7280;}
  [data-testid="stMetricValue"] {font-size:1.45rem; font-weight:700;}
  .status-badge {display:inline-block; padding:3px 10px; border-radius:12px;
    font-size:.75rem; font-weight:600; margin:2px 4px;}
  .badge-online {background:#D1FAE5; color:#065F46;}
  .badge-offline {background:#F3F4F6; color:#6B7280;}
  .badge-old {background:#FEF3C7; color:#92400E;}
  /* 优化表格样式 */
  [data-testid="stDataFrame"] {border-radius:8px; overflow:hidden;}
  /* 优化容器边框 */
  [data-testid="stHorizontalBlock"] > div[data-testid="column"] > div {border-radius:10px;}
  /* Tab 标签页优化 */
  .stTabs [data-baseweb="tab-list"] {gap: 8px; background:#F9FAFB; padding:8px;
    border-radius:12px; margin-bottom:1rem;}
  .stTabs [data-baseweb="tab"] {height: 50px; background:#FFF; border-radius:8px;
    padding: 0 24px; font-weight:500; transition: all 0.2s;}
  .stTabs [aria-selected="true"] {background:#FF9900; color:#FFF;}
  .stTabs [data-baseweb="tab"]:hover {box-shadow: 0 2px 6px rgba(0,0,0,0.08);}
</style>""", unsafe_allow_html=True)


# ---------- 数据层 ----------

def init_db():
    with sqlite3.connect(DB) as conn:
        # 评价检测历史表(当前使用)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS history (
                review_id TEXT, domain TEXT, url TEXT, status TEXT,
                stars TEXT, title TEXT, author TEXT, review_date TEXT,
                note TEXT, checked_at TEXT
            )""")

        # 为未来扩展预留：产品检测历史表
        # conn.execute("""
        #     CREATE TABLE IF NOT EXISTS product_history (
        #         asin TEXT, domain TEXT, url TEXT, status TEXT,
        #         title TEXT, price TEXT, currency TEXT, deal_tag TEXT,
        #         sold_by TEXT, rating REAL, review_count INTEGER,
        #         main_image TEXT, availability TEXT,
        #         note TEXT, checked_at TEXT
        #     )""")


def last_status_map(refs):
    if not refs or not DB.exists():
        return {}
    ids = [r.review_id for r in refs]
    with sqlite3.connect(DB) as conn:
        rows = conn.execute(
            """SELECT review_id, status, checked_at FROM history h
               WHERE checked_at = (SELECT MAX(checked_at) FROM history
                                   h2 WHERE h2.review_id = h.review_id)
               AND review_id IN (%s)""" % ",".join("?" * len(ids)),
            ids,
        ).fetchall()
    return {rid: (STATUS_LABEL.get(s, s), t) for rid, s, t in rows}


def save_history(results):
    with sqlite3.connect(DB) as conn:
        conn.executemany(
            """INSERT INTO history (review_id, domain, url, status, stars, title,
               author, review_date, note, checked_at)
               VALUES (:review_id, :domain, :url, :status, :stars, :title,
                       :author, :review_date, :note, :checked_at)""",
            results,
        )


def heat_stats():
    """近 24h 各站拦截率 —— IP 被加热的早期信号。"""
    if not DB.exists():
        return []
    with sqlite3.connect(DB) as conn:
        return conn.execute("""
            SELECT domain, COUNT(*), SUM(status='blocked')
            FROM history WHERE checked_at >= datetime('now','-1 day')
            GROUP BY domain""").fetchall()


def login_status() -> dict[str, dict]:
    """各站点登录状态(结构化,展示由渲染层决定):{domain: {"ok": bool, "days": int|None}}"""
    out = {}
    for d in DOMAINS:
        ss = PROFILE_ROOT / d / "storage_state.json"
        if ss.exists():
            out[d] = {"ok": True,
                      "days": int((time.time() - ss.stat().st_mtime) / 86400)}
        else:
            out[d] = {"ok": False, "days": None}
    return out


def status_badges_html(status: dict[str, dict]) -> str:
    """站点登录状态徽章(登录弹窗/设置页共用,数据与展示分离)"""
    badges = []
    for d, s in status.items():
        short = d.replace("amazon.", "")
        if s["ok"]:
            cls = "badge-online" if s["days"] < LOGIN_VALID_DAYS else "badge-old"
            badges.append(f'<span class="status-badge {cls}">{short} ✓ {s["days"]}d</span>')
        else:
            badges.append(f'<span class="status-badge badge-offline">{short} ○</span>')
    return "".join(badges)


def recent_history(limit: int = 50):
    """最近 N 条检测记录(历史 Tab 用)"""
    if not DB.exists():
        return []
    with sqlite3.connect(DB) as conn:
        return conn.execute("""
            SELECT review_id, domain, status, title, checked_at
            FROM history ORDER BY checked_at DESC LIMIT ?""", (limit,)).fetchall()


def history_stats():
    """全量状态分布统计(历史 Tab 用)"""
    if not DB.exists():
        return []
    with sqlite3.connect(DB) as conn:
        return conn.execute(
            "SELECT status, COUNT(*) FROM history GROUP BY status").fetchall()


def db_info():
    """数据库大小与记录数(设置 Tab 用);未创建时返回 None"""
    if not DB.exists():
        return None
    with sqlite3.connect(DB) as conn:
        count = conn.execute("SELECT COUNT(*) FROM history").fetchone()[0]
    return DB.stat().st_size / 1024, count


init_db()
if "results" not in st.session_state:
    st.session_state["results"] = []


def load_accounts() -> dict:
    if ACCOUNTS_FILE.exists():
        try:
            return json.loads(ACCOUNTS_FILE.read_text())
        except Exception:
            return {}
    return {}


ACCOUNTS = load_accounts()


# ---------- 系统维护弹窗(升级 Playwright 等) ----------

def _run_stream(cmd: list[str], out: st.delta_generator.DeltaGenerator) -> int:
    """逐行流式执行命令,输出实时显示在弹窗里。"""
    buf = []
    box = out.empty()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    for line in proc.stdout:
        buf.append(line.rstrip())
        box.code("\n".join(buf[-15:]), language=None)
    proc.wait()
    return proc.returncode


@st.dialog("⚙️ 系统维护")
def system_dialog():
    try:
        cur = importlib.metadata.version("playwright")
    except Exception:
        cur = "未知"
    st.markdown(f"**Playwright 当前版本:** `{cur}`")
    st.caption("升级 = 更新 pip 包 + 下载匹配的 Chromium,几分钟;完成后需重启服务生效"
               "(宝塔:项目管理器重启;本地:Ctrl-C 后重新 streamlit run)")

    if st.button("⬆️ 一键升级 Playwright", type="primary", use_container_width=True):
        ok1 = _run_stream([sys.executable, "-m", "pip", "install", "-U", "playwright"], st)
        ok2 = _run_stream([sys.executable, "-m", "playwright", "install", "chromium"], st)
        new = "未知"
        try:
            new = importlib.metadata.version("playwright")
        except Exception:
            pass
        if ok1 == 0 and ok2 == 0:
            # 自检:用新版本起一个浏览器(子进程,拿到的一定是新装版本)
            code = ("from playwright.sync_api import sync_playwright;"
                    "p=sync_playwright().start();b=p.chromium.launch();"
                    "b.close();p.stop();print('浏览器启动自检 OK')")
            chk = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
            if chk.returncode == 0:
                st.success(f"✅ 升级完成:{cur} → {new};浏览器自检通过。"
                           f"**重启服务后生效**")
            else:
                st.warning(f"⚠️ 升级到 {new},但浏览器自检失败:{chk.stdout}{chk.stderr[:200]}"
                           f"——把上面的输出发给维护者")
        else:
            st.error(f"❌ 升级命令失败(pip:{ok1},chromium:{ok2}),"
                     f"看上方输出定位;服务器无法联网时属正常,可稍后再试")


# ---------- 登录弹窗(公共组件 @st.dialog,不手搓) ----------

@st.dialog("🍪 小号登录管理", width="large")
def login_dialog():
    st.caption("浏览器与登录态全在服务器端 · 登录一次长期有效 · 检测出现 🍪 时来这里重登")
    st.markdown("**站点登录状态:** " + status_badges_html(login_status()),
                unsafe_allow_html=True)

    domain = st.selectbox("站点", sorted(set(DOMAINS) | set(ACCOUNTS.keys())), key="lg_domain")
    acct = ACCOUNTS.get(domain, {})
    a, p = acct.get("account", ""), acct.get("password", "")

    col_a, col_b, col_c = st.columns(3)
    account = col_a.text_input("小号账号", value=a, key="lg_account",
                               placeholder="未配置 accounts.json 时手动填写")
    password = col_b.text_input("小号密码", value=p, type="password", key="lg_password")
    totp = col_c.text_input("TOTP 密钥(可选)", value=acct.get("totp_secret", ""),
                            type="password", key="lg_totp",
                            help="开两步验证时『无法扫描?』里的字母密钥;配了它 OTP 全自动")

    c1, c2, c3 = st.columns(3)
    code = st.text_input("验证码(仅未配 TOTP 且停在验证码页时)", key="lg_code")

    try:
        if c1.button("🚀 开始登录", type="primary", use_container_width=True):
            if account and password:
                weblogin.close_all()
                msg, img = weblogin.get_session(domain).auto_login(
                    account, password, totp.strip())
                st.session_state["lg_shot"] = img
                st.session_state["lg_msg"] = msg
            else:
                st.session_state["lg_msg"] = "先填小号账号/密码(或配置 accounts.json)"
        if c2.button("提交验证码", use_container_width=True):
            sess = weblogin.get_session(domain)
            if not code:
                st.session_state["lg_msg"] = "先在上方填验证码"
            else:
                kind = "otp" if sess.page.query_selector(
                    "#auth-mfa-otpcode, input[name='otpCode']") is not None else "captcha"
                st.session_state["lg_shot"] = sess.submit_code(kind, code)
        if c3.button("刷新截图 / 检测登录态", use_container_width=True):
            sess = weblogin.get_session(domain)
            if sess.logged_in():
                sess.finish()
                st.session_state["lg_shot"] = None
                st.session_state["lg_msg"] = f"✅ {domain} 登录态已保存"
                st.rerun()
            st.session_state["lg_shot"] = sess.shot()

        sess = weblogin.ACTIVE.get(domain)
        if sess and sess.alive and sess.logged_in():
            sess.finish()
            st.session_state["lg_shot"] = None
            st.session_state["lg_msg"] = f"✅ {domain} 登录态已保存"
            st.rerun()
    except Exception as e:
        st.session_state["lg_msg"] = f"登录会话出错:{e.__class__.__name__}: {e}"

    if st.session_state.get("lg_msg"):
        st.info(st.session_state["lg_msg"])
    if st.session_state.get("lg_shot"):
        st.image(st.session_state["lg_shot"], caption=f"{domain} 登录页实时截图(服务器端浏览器)")


# ---------- 顶栏 ----------

st.markdown("### 🔍 AmReview · Amazon 评价链接批量检测")
top_row = st.columns([0.6, 0.4])
with top_row[0]:
    st.caption("粘贴单条评价 permalink,逐条判定 ✅存活 / 🐕已删 / 🤖被拦 / 🍪登录失效 / ❓未知")
with top_row[1]:
    status = login_status()
    online = sum(1 for v in status.values() if v["ok"])
    row = st.columns([0.72, 0.28])
    # 登录状态按钮动态显示：全部在线(绿)、部分在线(黄)、全部离线(灰)
    btn_type = "primary" if online == len(status) else "secondary"
    btn_label = f"🍪 小号登录 {online}/{len(status)}"
    if row[0].button(btn_label, use_container_width=True, type=btn_type):
        login_dialog()
    if row[1].button("⚙️", use_container_width=True, help="系统维护:升级 Playwright 等"):
        system_dialog()

def render_check_input():
    """渲染检测输入区域"""
    with st.container(border=True):
        # 为未来扩展预留：检测类型选择器
        # check_type = st.radio(
        #     "检测类型",
        #     options=["评价链接", "产品链接(ASIN)"],
        #     horizontal=True,
        #     help="评价链接：判定存活状态并提取星级/标题/作者等\n产品链接：提取标题/价格/评分/Deal Tag/首图等"
        # )

        st.markdown("**📝 待检测链接**(每行一条,支持 /gp/customer-reviews/、/review/、portal 格式,六国混贴)")
        text = st.text_area("链接", value=st.session_state.get("input_text", ""), height=140,
                            label_visibility="collapsed",
                            placeholder="https://www.amazon.com/gp/customer-reviews/R1XXXXXXX/\nhttps://www.amazon.in/review/R2XXXXXXX/")
        refs = parse_links(text)
        MAX_BATCH = 50  # 评审项:批量边界;限速 3~5s/条,50 条约 4 分钟,更多请分批防 IP 过热
        if len(refs) > MAX_BATCH:
            st.warning(f"一次最多检测 {MAX_BATCH} 条(每条 3~5 秒,更多会把 IP 跑热);"
                       f"已截取前 {MAX_BATCH} 条,其余请分批。")
            refs = refs[:MAX_BATCH]
        if refs:
            domains = sorted({r.domain for r in refs})
            domain_labels = '、'.join(d.replace('amazon.','') for d in domains)
            btn_col1, btn_col2 = st.columns([0.75, 0.25])
            ok = btn_col1.button(
                f"🚀 开始检测({len(refs)} 条 · {domain_labels})",
                type="primary", use_container_width=True
            )
            btn_col2.write("")  # 占位
        else:
            st.button("🚀 开始检测", type="primary", disabled=True, use_container_width=True)
            ok = False

        if ok:
            run_check(refs)


def render_results():
    """渲染检测结果区域"""
    results = st.session_state.get("results") or []
    if not results:
        st.info("👆 粘贴链接后点击「开始检测」。浏览器与登录态在服务器端,历史自动留存可对比。")
        return

    prev = st.session_state.get("prev", {})
    expired = sorted({r["domain"] for r in results if r["status"] == "login_expired"})
    if expired:
        st.warning("🍪 登录态缺失/失效:" + "、".join(d.replace("amazon.", "") for d in expired)
                   + " → 点右上「小号登录」完成登录后重测")

    # 统计卡片区域
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    # 新增：按域名分组统计
    domain_stats = {}
    for r in results:
        d = r["domain"].replace("amazon.", "")
        if d not in domain_stats:
            domain_stats[d] = {"total": 0, "alive": 0, "deleted": 0, "blocked": 0}
        domain_stats[d]["total"] += 1
        if r["status"] in domain_stats[d]:
            domain_stats[d][r["status"]] += 1

    cards = st.columns(5)
    metric_order = [
        ("alive", "✅ 正常"),
        ("deleted", "🐕 已删"),
        ("blocked", "🤖 被拦截"),
        ("login_expired", "🍪 登录失效"),
        ("unknown", "❓ 未知"),
    ]
    for col, (key, label) in zip(cards, metric_order):
        count = counts.get(key, 0)
        if len(results) > 0:
            pct = f"{count / len(results) * 100:.0f}%"
            col.metric(label, count, delta=pct if count > 0 else None)
        else:
            col.metric(label, count)

    # 多站点时显示分站统计
    if len(domain_stats) > 1:
        with st.expander(f"📊 分站统计({len(domain_stats)} 个站点)", expanded=False):
            stat_cols = st.columns(len(domain_stats))
            for col, (domain, stats) in zip(stat_cols, domain_stats.items()):
                alive_rate = stats.get("alive", 0) / stats["total"] * 100 if stats["total"] > 0 else 0
                col.metric(
                    f"{domain}",
                    f"{stats['total']} 条",
                    delta=f"存活率 {alive_rate:.0f}%"
                )

    def prev_col(r):
        p = prev.get(r["review_id"])
        if not p:
            return "—"
        mark = "" if p[0] == STATUS_LABEL[r["status"]] else " ⚠️"
        return f"{p[0]} · {p[1][5:16]}{mark}"

    # 表格数据
    table = [{
        "状态": STATUS_LABEL[r["status"]],
        "Review ID": r["review_id"],
        "站点": r["domain"].replace("amazon.", ""),
        "星级": r["stars"] if r["stars"] else "—",
        "标题": r["title"] if r["title"] else "—",
        "作者": r["author"] if r["author"] else "—",
        "日期": r["review_date"] if r["review_date"] else "—",
        "VP": "✓" if r["verified"] else "",
        "上次": prev_col(r),
        "备注": r["note"] if r["note"] else "",
        "URL": r["url"],
    } for r in results]

    st.dataframe(table, use_container_width=True, hide_index=True, height=min(35 + 35 * len(table), 420))

    bottom = st.columns([0.25, 0.75])
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(table[0].keys()))
    writer.writeheader()
    writer.writerows(table)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    filename = f"amreview_{timestamp}_{len(results)}条.csv"
    bottom[0].download_button("⬇️ 导出 CSV", buf.getvalue().encode("utf-8-sig"),
                              file_name=filename, mime="text/csv", use_container_width=True)

    shots = [r for r in results if r["screenshot"] and Path(r["screenshot"]).exists()]
    if shots:
        with bottom[1].expander(f"📸 截图证据({len(shots)} 张)"):
            cols = st.columns(3)
            for i, r in enumerate(shots):
                with cols[i % 3]:
                    st.markdown(f"**{r['review_id']}** {STATUS_LABEL[r['status']]}")
                    st.image(r["screenshot"])


def render_history():
    """渲染历史记录 Tab"""
    st.markdown("### 📊 检测历史记录")

    rows = recent_history(50)
    if not rows:
        st.info("暂无历史记录，完成第一次检测后会自动保存")
        return

    history_table = [{
        "检测时间": r[4],
        "Review ID": r[0],
        "站点": r[1].replace("amazon.", ""),
        "状态": STATUS_LABEL.get(r[2], r[2]),
        "标题": r[3][:40] + "..." if r[3] and len(r[3]) > 40 else (r[3] or "—"),
    } for r in rows]

    st.dataframe(history_table, use_container_width=True, hide_index=True, height=500)

    stats = history_stats()
    st.markdown("### 📈 历史统计")
    stat_cols = st.columns(len(stats) if stats else 1)
    for col, (status, count) in zip(stat_cols, stats):
        col.metric(STATUS_LABEL.get(status, status), count)


def render_settings():
    """渲染系统设置 Tab"""
    st.markdown("### ⚙️ 系统设置")

    # 登录状态管理
    with st.container(border=True):
        st.markdown("**🍪 登录状态管理**")
        st.markdown(status_badges_html(login_status()), unsafe_allow_html=True)

        if st.button("🍪 打开登录管理面板", use_container_width=True):
            login_dialog()

    # Playwright 版本信息
    with st.container(border=True):
        st.markdown("**🛠️ Playwright 版本**")
        try:
            cur = importlib.metadata.version("playwright")
            st.code(f"当前版本: {cur}")
        except Exception:
            st.code("当前版本: 未知")

        if st.button("⬆️ 升级 Playwright", use_container_width=True):
            system_dialog()

    # 数据库管理
    with st.container(border=True):
        st.markdown("**🗄️ 数据库管理**")
        info = db_info()
        if info:
            size, count = info
            st.caption(f"数据库大小: {size:.1f} KB · 记录数: {count} 条")
        else:
            st.caption("数据库尚未创建")


def run_check(refs):
    """执行检测任务"""
    weblogin.close_all()
    prev = last_status_map(refs)
    progress = st.progress(0.0, text="启动浏览器…")
    results = []

    with ReviewChecker() as checker:
        def on_result(i, ref, r):
            label = STATUS_LABEL[r["status"]]
            domain_short = ref.domain.replace('amazon.', '')
            extra = f" · {r['title'][:20]}…" if r["title"] else ""
            progress_text = f"[{i + 1}/{len(refs)}] {domain_short} · {ref.review_id} → {label}{extra}"
            progress.progress((i + 1) / len(refs), text=progress_text)

        try:
            results = checker.check_batch(refs, on_result=on_result)
        except Exception as e:
            st.error(f"检测中断:{e.__class__.__name__}: {e}")

    progress.empty()
    if results:
        save_history(results)
        st.session_state["results"] = results
        st.session_state["prev"] = prev
    st.rerun()


# ---------- Tab 标签页主体 ----------

tab1, tab2, tab3 = st.tabs(["🚀 批量检测", "📊 历史记录", "⚙️ 系统设置"])

with tab1:
    # 热度条
    heat = heat_stats()
    if heat:
        pills = []
        for domain, total, blocked in heat:
            rate = (blocked or 0) / total
            dot = "🟢" if rate < 0.05 else ("🟡" if rate < 0.2 else "🔴")
            domain_short = domain.replace('amazon.', '')
            pills.append(f"{dot} <code>{domain_short}</code> {rate:.0%} ({blocked or 0}/{total})")
        with st.container(border=True):
            st.markdown("**🌡️ IP 热度(近 24h)**　" + "　".join(pills), unsafe_allow_html=True)
            st.caption("🟢 <5% 正常 · 🟡 5~20% 建议降频 · 🔴 >20% 暂停或换出口;登录通道 IP 必须保持 🟢")

    # 检测输入区域移到这里
    render_check_input()

    # 结果显示区域移到这里
    render_results()

with tab2:
    render_history()

with tab3:
    render_settings()
