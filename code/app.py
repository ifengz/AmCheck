"""AmReview Streamlit 主页:粘贴评价链接 → 批量检测 → 结果 + CSV。

布局规范(公共组件,不手搓):
- 顶栏:标题 + 站点登录状态灯 + 「Amazon 账号登录」按钮(弹窗 @st.dialog)
- 热度条:近 24h 各站拦截率色点(🟢🟡🔴)
- 卡片:st.container(border=True) + st.metric,统一留白
"""

from __future__ import annotations

import csv
import importlib.metadata
import io
import json
import os
import sqlite3
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

import weblogin
from engine import STATUS_LABEL, ReviewChecker, parse_links

DB = Path(__file__).parent / "history.db"
ACCOUNTS_FILE = Path(__file__).parent / "accounts.json"
PROFILE_ROOT = Path.home() / ".amreview" / "profile"
DOMAINS = ["amazon.com", "amazon.com.mx", "amazon.com.br", "amazon.in",
           "amazon.com.au", "amazon.co.jp"]

st.set_page_config(page_title="AmReview 评价检测", page_icon="🔍", layout="wide")


# ---------- 数据层 ----------

def _db() -> sqlite3.Connection:
    """带超时的连接:多会话并发写时避免偶发 database is locked。"""
    return sqlite3.connect(DB, timeout=10)


def init_db():
    with _db() as conn:
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
    with _db() as conn:
        rows = conn.execute(
            """SELECT review_id, status, checked_at FROM history h
               WHERE checked_at = (SELECT MAX(checked_at) FROM history
                                   h2 WHERE h2.review_id = h.review_id)
               AND review_id IN (%s)""" % ",".join("?" * len(ids)),
            ids,
        ).fetchall()
    return {rid: (STATUS_LABEL.get(s, s), t) for rid, s, t in rows}


def save_history(results):
    with _db() as conn:
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
    with _db() as conn:
        return conn.execute("""
            SELECT domain, COUNT(*), SUM(status='blocked')
            FROM history WHERE checked_at >= datetime('now','-1 day')
            GROUP BY domain""").fetchall()


def login_status() -> dict[str, dict]:
    """各站点登录状态(结构化,展示由渲染层决定):{domain: {"ok": bool, "days": int|None}}

    判定依据:storage_state.json 里是否存在未过期的 at-/x- 登录 cookie。
    只看文件时间只能证明"登录过",不能证明 cookie 还活着。
    """
    out = {}
    now = time.time()
    for d in DOMAINS:
        ss = PROFILE_ROOT / d / "storage_state.json"
        if ss.exists():
            ok = False
            try:
                cookies = json.loads(ss.read_text()).get("cookies", [])
                ok = any(
                    c.get("name", "").startswith(("at-", "x-"))
                    and (c.get("expires", -1) < 0 or c.get("expires", 0) > now)
                    for c in cookies
                )
            except Exception:
                pass
            out[d] = {"ok": ok, "days": int((now - ss.stat().st_mtime) / 86400)}
        else:
            out[d] = {"ok": False, "days": None}
    return out


def recent_history(limit: int = 50):
    """最近 N 条检测记录(历史 Tab 用)"""
    if not DB.exists():
        return []
    with _db() as conn:
        return conn.execute("""
            SELECT review_id, domain, status, title, checked_at
            FROM history ORDER BY checked_at DESC LIMIT ?""", (limit,)).fetchall()


def history_stats():
    """全量状态分布统计(历史 Tab 用)"""
    if not DB.exists():
        return []
    with _db() as conn:
        return conn.execute(
            "SELECT status, COUNT(*) FROM history GROUP BY status").fetchall()


def db_info():
    """数据库大小与记录数(设置 Tab 用);未创建时返回 None"""
    if not DB.exists():
        return None
    with _db() as conn:
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


def _panel_password() -> str:
    """登录面板访问口令:环境变量优先,其次 accounts.json 顶层 _panel_password 键;未配置则不开门禁。"""
    pw = os.environ.get("AMREVIEW_PANEL_PASSWORD", "").strip()
    if not pw:
        pw = str(ACCOUNTS.get("_panel_password", "")).strip()
    return pw


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


def _interp() -> str | None:
    """当前解释器;路径已失效(如项目目录被重命名)时返回 None,提示重启。"""
    exe = sys.executable
    if exe and Path(exe).exists():
        return exe
    return None


def _latest_pypi(pkg: str) -> tuple[str, str] | None:
    """查 PyPI:返回 (当前 Python 可装的最新版, 全平台最新版)。
    会话内缓存 10 分钟(弹窗每次重跑都会调用,不能裸查)。
    失败(离线/被墙)返回 None,不影响后续升级——pip 自己还会再查一次。
    注:不能只看 info.version,如 playwright 1.61+ 要求 Python≥3.10,
    在 3.9 环境里 pip 会自动过滤,pip 视角的"最新"是 1.60.0。"""
    key = f"pypi_latest_{pkg}"
    hit = st.session_state.get(key)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    result = None
    try:
        import packaging.specifiers, packaging.version
        with urllib.request.urlopen(f"https://pypi.org/pypi/{pkg}/json", timeout=5) as r:
            data = json.load(r)
        top = data["info"]["version"]
        cur_py = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        compat = top
        for v in sorted(data["releases"], key=packaging.version.Version, reverse=True):
            rps = {f.get("requires_python") for f in data["releases"][v]}
            if any(not rp or packaging.specifiers.SpecifierSet(rp).contains(cur_py) for rp in rps):
                compat = v
                break
        result = (compat, top)
    except Exception:
        pass
    st.session_state[key] = (time.time(), result)
    return result


def _restart_service(py: str) -> None:
    """网页一键重启:拉起脱离本进程的"保姆"子进程,由它负责
    杀掉当前服务 → 等端口释放 → 重新 streamlit run。
    宝塔等有守护的场景:守护会自动拉起,保姆检测到端口被占就退出,不会起双份。"""
    port = st.config.get_option("server.port") or 8501
    app_path = Path(__file__).resolve()
    helper = (
        "import os, signal, socket, subprocess, sys, time\n"
        f"me = {os.getpid()}\n"
        f"cmd = [{py!r}, '-m', 'streamlit', 'run', {str(app_path)!r}, '--server.port', {str(port)!r}]\n"
        f"cwd = {str(app_path.parent)!r}\n"
        "time.sleep(1)\n"                       # 给页面留时间收到"正在重启"提示
        "try:\n"
        "    os.kill(me, signal.SIGTERM)\n"     # 温和停止,等价于 Ctrl-C
        "except ProcessLookupError:\n"
        "    pass\n"
        "for _ in range(100):\n"                # 等旧进程退出(≤20s)
        "    try:\n"
        "        os.kill(me, 0); time.sleep(0.2)\n"
        "    except ProcessLookupError:\n"
        "        break\n"
        "for _ in range(150):\n"                # 等端口释放(≤30s);被占说明守护已拉起
        "    s = socket.socket()\n"
        "    try:\n"
        f"        s.connect(('127.0.0.1', {port})); s.close(); time.sleep(0.2)\n"
        "    except OSError:\n"
        "        s.close(); break\n"
        "else:\n"
        "    sys.exit(0)\n"
        "log = open('/tmp/amcheck_restart.log', 'ab')\n"
        "subprocess.Popen(cmd, cwd=cwd, stdout=log, stderr=subprocess.STDOUT,\n"
        "                 start_new_session=True)\n"
    )
    subprocess.Popen([py, "-c", helper], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@st.dialog("系统维护")
def system_dialog():
    try:
        cur = importlib.metadata.version("playwright")
    except Exception:
        cur = "未知"
    latest = _latest_pypi("playwright")
    ver_line = f"**Playwright 当前版本:** `{cur}`"
    if latest:
        compat, top = latest
        ver_line += f" · 可装最新版: `{compat}`"
        if compat != top:
            ver_line += (f"(PyPI 最新 `{top}` 需更高 Python 版本,"
                         f"当前 {sys.version_info.major}.{sys.version_info.minor})")
    st.markdown(ver_line)
    info = db_info()
    if info:
        st.caption(f"数据库: {info[0]:.1f} KB · {info[1]} 条记录")
    st.caption("升级 = 更新 pip 包 + 下载匹配的 Chromium,几分钟;完成后点下方按钮重启生效"
               "(也可宝塔项目管理器重启 / 本地 Ctrl-C 后重新 streamlit run)")

    if st.button("升级 Playwright", type="primary", use_container_width=True):
        py = _interp()
        if py is None:
            st.error(f"❌ 当前服务的解释器路径已失效:`{sys.executable}`"
                     "——项目目录被重命名/移动后服务未重启。"
                     "请 Ctrl-C 停掉服务,在新目录下重新 `streamlit run app.py`。")
            return
        if latest and latest[0] == cur:
            st.success(f"✅ 已是当前 Python 环境可装的最新版本 {cur},无需升级")
            return
        ok1 = _run_stream([py, "-m", "pip", "install", "-U", "playwright"], st)
        ok2 = _run_stream([py, "-m", "playwright", "install", "chromium"], st)
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
            chk = subprocess.run([py, "-c", code], capture_output=True, text=True)
            if chk.returncode == 0:
                st.success(f"✅ 升级完成:{cur} → {new};浏览器自检通过。"
                           f"**重启服务后生效**")
            else:
                st.warning(f"⚠️ 升级到 {new},但浏览器自检失败:{chk.stdout}{chk.stderr[:200]}"
                           f"——把上面的输出发给维护者")
        else:
            st.error(f"❌ 升级命令失败(pip:{ok1},chromium:{ok2}),"
                     f"看上方输出定位;服务器无法联网时属正常,可稍后再试")

    st.divider()
    if st.button("重启服务(升级后需重启生效)", use_container_width=True):
        py = _interp()
        if py is None:
            st.error(f"❌ 解释器路径已失效(`{sys.executable}`),无法自动重启;"
                     "请手动停掉服务后在新目录重新 `streamlit run app.py`。")
            return
        _restart_service(py)
        st.warning("🔄 服务正在重启,页面约 6 秒后自动恢复;若未恢复请手动刷新。")
        components.html("<script>setTimeout(() => location.reload(), 6000)</script>",
                        height=0)


# ---------- 登录弹窗(公共组件 @st.dialog,不手搓) ----------

# 站点中文名(登录入口展示用;结果表格等其他位置仍用短域名)
DOMAIN_LABELS = {
    "amazon.com": "美国站",
    "amazon.com.mx": "墨西哥站",
    "amazon.com.br": "巴西站",
    "amazon.in": "印度站",
    "amazon.com.au": "澳洲站",
    "amazon.co.jp": "日本站",
}
# 登录入口的站点按钮展示顺序(业务习惯,不按域名排序)
DOMAIN_ORDER = ["amazon.in", "amazon.com", "amazon.com.au",
                "amazon.co.jp", "amazon.com.br", "amazon.com.mx"]


@st.dialog("Amazon 账号登录管理", width="medium")
def login_dialog():
    gate = _panel_password()
    if gate and not st.session_state.get("lg_unlocked"):
        st.caption("此面板包含 Amazon 账号凭据,需输入访问口令")
        pw = st.text_input("访问口令", type="password", label_visibility="collapsed",
                           placeholder="输入访问口令")
        if st.button("解锁", type="primary", use_container_width=True):
            if pw == gate:
                st.session_state["lg_unlocked"] = True
                st.rerun(scope="fragment")
            else:
                st.error("口令错误")
        return

    st.caption("浏览器与登录态均在服务器端,登录一次长期有效")

    # 站点选择:下拉选项内带登录状态(已登录 Xd / 未登录)
    status = login_status()
    acct_keys = {k for k in ACCOUNTS if not k.startswith("_")}  # 排除 _panel_password 等元键
    all_domains = set(DOMAINS) | acct_keys
    domains = [d for d in DOMAIN_ORDER if d in all_domains] + sorted(all_domains - set(DOMAIN_ORDER))
    if "lg_domain" not in st.session_state:
        st.session_state["lg_domain"] = "amazon.com" if "amazon.com" in domains else domains[0]
    domain = st.session_state["lg_domain"]

    options = []
    for d in domains:
        s = status.get(d)
        if s and s["ok"]:
            options.append(f"{DOMAIN_LABELS.get(d, d)} · 已登录({s['days']}d)")
        else:
            options.append(f"{DOMAIN_LABELS.get(d, d)} · 未登录")
    sel = st.selectbox("站点", options, index=domains.index(domain),
                       label_visibility="collapsed")
    domain = domains[options.index(sel)]
    st.session_state["lg_domain"] = domain
    acct = ACCOUNTS.get(domain, {})

    # 当前站点登录状态提示
    s = status.get(domain)
    if s and s["ok"]:
        st.success(f"{DOMAIN_LABELS.get(domain, domain)} 已登录({s['days']} 天前保存),通常无需重新登录")
    else:
        st.info(f"{DOMAIN_LABELS.get(domain, domain)} 尚未登录,填写下方凭据后开始登录")

    # 账号信息(原生竖排输入)
    account = st.text_input("Amazon 账号", value=acct.get("account", ""),
                            key="lg_account")
    password = st.text_input("账号密码", value=acct.get("password", ""),
                             type="password", key="lg_password")
    totp = st.text_input("TOTP 密钥(可选)", value=acct.get("totp_secret", ""),
                         type="password", key="lg_totp",
                         help="开两步验证时『无法扫描?』里的字母密钥;配了它 OTP 全自动")
    code = st.text_input("验证码", key="lg_code",
                         placeholder="仅未配 TOTP 且停在验证码页时需要")

    c1, c2 = st.columns(2)
    try:
        if c1.button("开始登录", type="primary", use_container_width=True):
            if account and password:
                weblogin.close_domains({domain})
                msg, img = weblogin.get_session(domain).auto_login(
                    account, password, totp.strip())
                st.session_state["lg_shot"] = img
                st.session_state["lg_msg"] = msg
            else:
                st.session_state["lg_msg"] = "先填 Amazon 账号和密码"
        if c2.button("提交验证码", use_container_width=True):
            sess = weblogin.get_session(domain)
            if not code:
                st.session_state["lg_msg"] = "先在上方填验证码"
            else:
                kind = "otp" if sess.page.query_selector(
                    "#auth-mfa-otpcode, input[name='otpCode']") is not None else "captcha"
                st.session_state["lg_shot"] = sess.submit_code(kind, code)
        if st.button("检测登录态", use_container_width=True):
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
        st.image(st.session_state["lg_shot"], caption=f"{domain} 登录页实时截图")


# ---------- 顶栏 ----------

# ---------- 页面 ----------

def page_reviews():
    st.markdown("## 评价链接批量检测")
    render_check_input()
    render_results()


def page_history():
    st.markdown("## 检测历史")
    render_history()


def page_link_tracking():
    st.markdown("## 页面链接跟踪")
    st.info("规划中:跟踪产品/链接页面的快照与状态变化(价格、评分、评价数、上下架等),后端就绪后开放。")

def render_check_input():
    """渲染检测输入区域"""
    with st.container(border=True):
        st.markdown("**待检测链接**")
        st.caption("每行一条,支持 /gp/customer-reviews/、/review/、portal 三种格式,六国站点可混贴")
        # 手动写回 session_state:widget 状态在切页不渲染时会被框架清理,
        # 手动保存的值才能跨页保留(切页往返输入不丢)
        text = st.text_area("链接", value=st.session_state.get("input_text", ""),
                            height=140, label_visibility="collapsed",
                            placeholder="https://www.amazon.com/gp/customer-reviews/R1XXXXXXX/\nhttps://www.amazon.in/review/R2XXXXXXX/")
        st.session_state["input_text"] = text
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
                f"开始检测({len(refs)} 条 · {domain_labels})",
                type="primary", use_container_width=True
            )
            btn_col2.write("")  # 占位
        else:
            st.button("开始检测", type="primary", disabled=True, use_container_width=True)
            ok = False

        if ok:
            run_check(refs)


def render_results():
    """渲染检测结果区域"""
    results = st.session_state.get("results") or []
    if not results:
        st.info("粘贴链接后点击「开始检测」。浏览器与登录态在服务器端,历史自动留存可对比。")
        return

    prev = st.session_state.get("prev", {})
    expired = sorted({r["domain"] for r in results if r["status"] == "login_expired"})
    if expired:
        st.warning("登录态缺失/失效:" + "、".join(d.replace("amazon.", "") for d in expired)
                   + " → 点右上「Amazon 账号登录」完成登录后重测")

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
        col.metric(label, count)

    # 多站点时显示分站统计
    if len(domain_stats) > 1:
        with st.expander(f"分站统计({len(domain_stats)} 个站点)", expanded=False):
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
        "截图": Path(r["screenshot"]).name if r["screenshot"] else "",
    } for r in results]

    display_cols = ["状态", "站点", "星级", "标题", "作者", "日期", "VP", "上次", "备注"]
    st.dataframe([{k: row[k] for k in display_cols} for row in table],
                 use_container_width=True, hide_index=True,
                 height=min(35 + 35 * len(table), 420))

    bottom = st.columns([0.25, 0.75])
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(table[0].keys()))
    writer.writeheader()
    writer.writerows(table)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M")
    filename = f"amreview_{timestamp}_{len(results)}条.csv"
    bottom[0].download_button("导出 CSV", buf.getvalue().encode("utf-8-sig"),
                              file_name=filename, mime="text/csv", use_container_width=True)

    shots = [r for r in results if r["screenshot"] and Path(r["screenshot"]).exists()]
    if shots:
        with bottom[1].expander(f"截图证据({len(shots)} 张)"):
            cols = st.columns(3)
            for i, r in enumerate(shots):
                with cols[i % 3]:
                    st.markdown(f"**{r['review_id']}** {STATUS_LABEL[r['status']]}")
                    st.image(r["screenshot"])


def render_history():
    """渲染历史记录"""
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
    st.markdown("### 历史统计")
    stat_cols = st.columns(len(stats) if stats else 1)
    for col, (status, count) in zip(stat_cols, stats):
        col.metric(STATUS_LABEL.get(status, status), count)


def run_check(refs):
    """执行检测任务"""
    weblogin.close_domains({r.domain for r in refs})
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


# ---------- 侧边栏 ----------

with st.sidebar:
    st.markdown("### AmReview")
    st.caption("Amazon 评价链接批量检测")
    st.divider()

    status = login_status()
    online = sum(1 for v in status.values() if v["ok"])
    if st.button(f"Amazon 账号登录 ({online}/{len(status)})", use_container_width=True):
        login_dialog()
    if st.button("系统维护", use_container_width=True):
        system_dialog()
    st.divider()

    # 页面切换:radio 在同一 session 内切换,输入与检测结果不丢失
    # (st.navigation 会整页重载并重置 session_state,实测不可用)
    page = st.radio("页面", ["评价链接检测", "检测历史", "页面链接跟踪"],
                    label_visibility="collapsed", key="nav_page")

    with st.expander("IP 热度（近 24h）", expanded=False):
        heat = heat_stats()
        if heat:
            for domain, total, blocked in heat:
                rate = (blocked or 0) / total
                st.markdown(f"- {domain}: 拦截率 {rate:.0%}（{blocked or 0}/{total} 条）")
            st.caption("拦截率 <5% 正常;5~20% 建议降频;>20% 暂停或更换出口 IP")
        else:
            st.caption("暂无检测数据")

# ---------- 页面渲染 ----------

if page == "评价链接检测":
    page_reviews()
elif page == "检测历史":
    page_history()
else:
    page_link_tracking()
