"""AmReview Streamlit 主页:粘贴评价链接 → 批量检测 → 结果 + CSV。

布局规范(只用 Streamlit 公共组件,不手搓 HTML 组件):
- 主体:一行工具条 + 一张全字段记录表(可排序/可点开验证),表外不铺内容
- 验证:选中行 → 行内详情条(判定依据 + 原页面链接 + 截图弹窗 + 历史轨迹)
- 辅助:分站统计、状态汇总、截图证据、演示数据、账号与维护 → 弹窗或侧边栏
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

# SaaS 密度:只压间距,不改组件外观(组件一律用 Streamlit 原生)
st.markdown("""<style>
/* 顶距压到 1rem:原 2.4rem 在标题上方留了大片空白,空间让给表格 */
.block-container {padding-top: 1rem; padding-bottom: .8rem; max-width: 1500px;}
[data-testid="stSidebarUserContent"] {padding-top: 1rem;}
[data-testid="stVerticalBlockBorderWrapper"] h1,
[data-testid="stVerticalBlockBorderWrapper"] h2,
[data-testid="stVerticalBlockBorderWrapper"] h3 {margin-top: 0;}
h1, h2, h3 {letter-spacing: -.01em;}
/* 共用标题条:固定行高,三页基线一致;替代 subheader 的 62px 高度 */
.pg-title {font-size: 1.32rem; font-weight: 700; line-height: 1.9rem;
           letter-spacing: -.01em; margin: 0;}
.pg-meta {font-size: .78rem; line-height: 1.1rem; opacity: .6; margin: .1rem 0 0;}
/* 表格上方的操作提示:与副行同字号同弱化,右对齐贴住它描述的那张表。
   行高取 segmented_control 的 40px,并清掉 Streamlit 给 markdown 容器的
   -16px 下边距 —— 否则该列量出来只有 24px,列的 center 对齐会低 8px。 */
.pg-hint {font-size: .78rem; opacity: .6; margin: 0; text-align: right;
          line-height: 40px;}
[data-testid="stMarkdownContainer"]:has(.pg-hint) {margin-bottom: 0;}
/* 标题条锁定 48px:无按钮的页面(如跟踪页)列高不会塌,三页基线严格对齐 */
[data-testid="stHorizontalBlock"]:has(.pg-title) {min-height: 48px; gap: .5rem;}
/* 文字类分区收紧,把纵向空间让给表格 */
[data-testid="stCaptionContainer"] p {font-size: .78rem; line-height: 1.25;
                                      margin-bottom: 0;}
[data-testid="stAlert"] {padding: 0; margin: .35rem 0;}
[data-testid="stAlert"] p {font-size: .82rem; line-height: 1.3; margin-bottom: 0;}
/* 提示条真实高度来自内层容器(图标撑起 52px),压这里才有效 */
[data-testid="stAlert"] .stAlertContainer {padding: .45rem .7rem; min-height: 0;}
[data-testid="stAlert"] [data-testid="stMarkdownContainer"] {min-height: 0;}
[data-testid="stElementContainer"]:has(> [data-testid="stMarkdownContainer"] .pg-title)
    {margin-bottom: 0;}
[data-testid="stMetricValue"] {font-size: 1.35rem;}
[data-testid="stMetricLabel"] p {font-size: .78rem;}
/* 分段控件原生 32px,按钮/popover 40px,同排会高低不齐 → 统一到 40 */
[data-testid="stButtonGroup"] [data-baseweb="button-group"],
[data-testid="stButtonGroup"] [data-baseweb="button-group"] button {height: 40px;}
</style>""", unsafe_allow_html=True)

# 状态元数据:表格文案 / 徽标配色 / 汇总顺序共用一份,避免各处硬编码分叉
STATUS_META = {
    "alive": ("正常", "green"),
    "deleted": ("已删", "red"),
    "blocked": ("被拦截", "orange"),
    "login_expired": ("登录失效", "violet"),
    "unknown": ("未知", "gray"),
}
STATUS_ORDER = ["alive", "deleted", "blocked", "login_expired", "unknown"]


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


def recent_history(limit: int = 500, days: int | None = None):
    """最近 N 条检测记录(历史回顾用);days=None 表示全部"""
    if not DB.exists():
        return []
    where, params = "", []
    if days:
        where = "WHERE checked_at >= datetime('now', ?)"
        params = [f"-{days} days"]
    with _db() as conn:
        return conn.execute(
            f"""SELECT review_id, domain, status, title, checked_at
                FROM history {where} ORDER BY checked_at DESC LIMIT ?""",
            params + [limit]).fetchall()


def history_stats(days: int | None = None):
    """状态分布统计(历史回顾用,可按时间范围)"""
    if not DB.exists():
        return []
    where, params = "", []
    if days:
        where = "WHERE checked_at >= datetime('now', ?)"
        params = [f"-{days} days"]
    with _db() as conn:
        return conn.execute(
            f"SELECT status, COUNT(*) FROM history {where} GROUP BY status",
            params).fetchall()


def review_history_timeline(review_id: str, limit: int = 20):
    """单条链接的历史检测轨迹(倒序):结果页异常卡片展示"什么时候变狗的" """
    if not DB.exists():
        return []
    with _db() as conn:
        return conn.execute(
            """SELECT status, stars, title, checked_at FROM history
               WHERE review_id = ? ORDER BY checked_at DESC LIMIT ?""",
            (review_id, limit)).fetchall()


def db_info():
    """数据库大小与记录数(设置 Tab 用);未创建时返回 None"""
    if not DB.exists():
        return None
    with _db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM history").fetchone()[0]
    return DB.stat().st_size / 1024, count


# ---------- 演示数据(仅用于预览界面效果,不参与真实检测流程) ----------

# 结果视图示例:覆盖全部 5 种状态、6 个站点,含上次对比、异常详情、分站统计与截图证据
MOCK_RESULTS = [
    {
        "review_id": "R1ALIVE1234", "domain": "amazon.com", "status": "alive",
        "stars": "4", "title": "Great product, works as expected",
        "author": "John D.", "review_date": "2026年8月10日", "body": "Worth every penny.",
        "verified": True, "note": "", "shot_kind": "", "checked_at": "2026-08-22 03:00:05",
        "url": "https://www.amazon.com/gp/customer-reviews/R1ALIVE1234/",
        "prev_status": None, "prev_time": "",
    },
    {
        "review_id": "R2DELETED6789", "domain": "amazon.in", "status": "deleted",
        "stars": "", "title": "", "author": "", "review_date": "", "body": "",
        "verified": False, "note": "正常·08-1 HTTP 404 · Page Not Found",
        "shot_kind": "deleted", "checked_at": "2026-08-22 03:00:00",
        "url": "https://www.amazon.in/gp/customer-reviews/R2DELETED6789/",
        "prev_status": "✅ 正常", "prev_time": "2026-08-22 02:00:30",
    },
    {
        "review_id": "R3BLOCKED1111", "domain": "amazon.com.au", "status": "blocked",
        "stars": "", "title": "", "author": "", "review_date": "", "body": "",
        "verified": False, "note": "重试 3 次仍被拦截(guard/captcha 拦截),建议稍后复测",
        "shot_kind": "", "checked_at": "2026-08-22 02:30:00",
        "url": "https://www.amazon.com.au/gp/customer-reviews/R3BLOCKED1111/",
        "prev_status": None, "prev_time": "",
    },
    {
        "review_id": "R4LOGIN2222", "domain": "amazon.co.jp", "status": "login_expired",
        "stars": "", "title": "", "author": "", "review_date": "", "body": "",
        "verified": False, "note": "跳转登录页,需重新引导登录该站点 Amazon 账号",
        "shot_kind": "", "checked_at": "2026-08-22 02:01:00",
        "url": "https://www.amazon.co.jp/gp/customer-reviews/R4LOGIN2222/",
        "prev_status": None, "prev_time": "",
    },
    {
        "review_id": "R5UNKNOWN3333", "domain": "amazon.com.mx", "status": "unknown",
        "stars": "", "title": "", "author": "", "review_date": "", "body": "",
        "verified": False, "note": "已删·08-1 HTTP 200 · 无法识别的页面形态",
        "shot_kind": "unknown", "checked_at": "2026-08-22 02:00:30",
        "url": "https://www.amazon.com.mx/gp/customer-reviews/R5UNKNOWN3333/",
        "prev_status": "🐕 已删", "prev_time": "2026-08-22 01:30:00",
    },
    {
        "review_id": "R6ALIVE4444", "domain": "amazon.com.br", "status": "alive",
        "stars": "5", "title": "Excelente produto!", "author": "Maria S.",
        "review_date": "2026年8月5日", "body": "Recomendo.", "verified": False,
        "note": "", "shot_kind": "", "checked_at": "2026-08-22 01:30:30",
        "url": "https://www.amazon.com.br/gp/customer-reviews/R6ALIVE4444/",
        "prev_status": None, "prev_time": "",
    },
]


def _mock_font(size: int):
    """跨平台找一个可用的 TrueType 字体,找不到再退回默认字体。"""
    from PIL import ImageFont
    for p in ("/System/Library/Fonts/Helvetica.ttc",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
              "/usr/share/fonts/dejavu/DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            pass
    try:
        return ImageFont.load_default(size)
    except Exception:
        return ImageFont.load_default()


def _make_mock_shot(review_id: str, kind: str) -> str:
    """为演示结果生成一张占位截图(已删/未知页样式),落盘到 screenshots/
    同名目录、固定文件名,多次点击复用不堆积。"""
    shot_dir = Path(__file__).parent / "screenshots"
    shot_dir.mkdir(parents=True, exist_ok=True)
    path = shot_dir / f"{review_id}_mock.png"
    if path.exists():
        return str(path)
    try:
        from PIL import Image, ImageDraw
        W, H = 900, 400
        img = Image.new("RGB", (W, H), (255, 255, 255))
        d = ImageDraw.Draw(img)
        # 顶部工具栏
        d.rectangle([0, 0, W, 70], fill=(35, 47, 62))
        d.rectangle([0, 0, 150, 70], fill=(68, 71, 85))
        d.rectangle([160, 20, 300, 50], fill=(255, 255, 255))
        d.rectangle([330, 20, 430, 50], fill=(255, 255, 255))
        # 主体:蓝色横幅 + 文案(贴 Amazon SORRY 页风格)
        d.rectangle([120, 130, 780, 330], outline=(221, 221, 221), width=2)
        d.rectangle([120, 130, 780, 210], fill=(160, 174, 192))
        d.text((150, 158), "Sorry", fill=(255, 255, 255), font=_mock_font(36))
        d.text((150, 240), "we couldn't find that page",
               fill=(17, 94, 89), font=_mock_font(28))
        d.text((150, 285), f"演示截图 · {review_id}", fill=(102, 102, 102),
               font=_mock_font(16))
        img.save(path)
    except Exception:
        pass
    return str(path)


def load_mock_results():
    """载入演示结果:覆盖全状态/全站点,写入本轮结果与对比数据并展示。"""
    results = []
    for src in MOCK_RESULTS:
        r = dict(src)
        r["screenshot"] = _make_mock_shot(r["review_id"], r["shot_kind"]) \
            if r["shot_kind"] else ""
        r.pop("shot_kind", None)
        r.pop("prev_status", None)
        r.pop("prev_time", None)
        results.append(r)
    st.session_state["results"] = results
    st.session_state["prev"] = {
        m["review_id"]: (m["prev_status"], m["prev_time"])
        for m in MOCK_RESULTS if m["prev_status"]
    }
    save_history(results)
    st.rerun()


# 历史视图示例:68 条记录(固定18条 + 追加50条),用于检查长表格展示
# 元组:(review_id, domain, status, stars, title, author, check_time)
MOCK_HISTORY = [
    ("R1ALIVEDDDD", "amazon.com.au", "alive", "5", "Excellent quality, fast shipping", "Tom H.", "2026-08-22 03:00:30"),
    ("R1LOGINCCCC", "amazon.com", "login_expired", "", "", "", "2026-08-22 03:00:00"),
    ("R1ALIVEBBBB", "amazon.com.mx", "alive", "5", "Muy buen producto, lo recomiendo", "Laura G.", "2026-08-22 02:30:30"),
    ("R1BLOCKEDAAAA", "amazon.com", "blocked", "", "", "", "2026-08-22 02:30:00"),
    ("R1UNKNOWN9999", "amazon.co.jp", "unknown", "", "", "", "2026-08-22 02:01:00"),
    ("R1ALIVE8888", "amazon.in", "alive", "4", "बहुत अच्छा उत्पाद, धन्यवाद", "Priya S.", "2026-08-22 02:00:30"),
    ("R9BLOCKED7777", "amazon.com.au", "blocked", "", "", "", "2026-08-22 02:00:00"),
    ("R8DELETED6666", "amazon.com", "deleted", "", "", "", "2026-08-22 01:30:30"),
    ("R7ALIVE5555", "amazon.com", "alive", "5", "Perfect, arrived on time", "Alex K.", "2026-08-22 01:30:00"),
    ("R1ALIVE1234", "amazon.com", "alive", "4", "Great product, works as expected", "John D.", "2026-08-22 01:00:05"),
    ("R2DELETED6789", "amazon.in", "deleted", "", "", "", "2026-08-22 01:00:00"),
    ("R5ALIVE9999", "amazon.com.br", "alive", "5", "Produto excelente!", "Maria S.", "2026-08-21 20:30:00"),
    ("R6ALIVE1212", "amazon.in", "alive", "4", "Good value for money", "Rohan V.", "2026-08-21 19:00:00"),
    ("R7ALIVE3434", "amazon.co.jp", "alive", "5", "期待通りの商品でした", "佐藤", "2026-08-21 18:00:00"),
    ("R4LOGIN2222", "amazon.co.jp", "login_expired", "", "", "", "2026-08-21 15:40:00"),
    ("R9ALIVE7878", "amazon.com.au", "alive", "4", "Average quality, could be better", "Sam T.", "2026-08-20 10:05:00"),
    ("R8ALIVE5656", "amazon.com", "alive", "5", "Fast delivery, happy", "Lily W.", "2026-08-20 09:00:00"),
    ("R5UNKNOWN3333", "amazon.com.mx", "unknown", "", "", "", "2026-08-20 08:00:00"),
]


def _make_mock_history_rows(count: int = 50):
    """生成固定的长列表演示数据,不使用随机值,便于重复预览。"""
    statuses = ("alive", "alive", "deleted", "blocked", "login_expired", "unknown")
    domains = tuple(DOMAINS)
    titles = {
        "alive": ("Reliable product, would buy again", "Good quality and quick delivery"),
        "deleted": ("Review no longer available", "Page removed by the reviewer"),
        "blocked": ("", ""),
        "login_expired": ("", ""),
        "unknown": ("", ""),
    }
    rows = []
    for i in range(1, count + 1):
        status = statuses[(i - 1) % len(statuses)]
        title = titles[status][(i - 1) % len(titles[status])]
        alive = status == "alive"
        rows.append((
            f"RMOCK{i:04d}",
            domains[(i - 1) % len(domains)],
            status,
            str(3 + i % 3) if alive else "",
            title,
            f"Demo User {i:02d}" if alive else "",
            f"2026-08-{22 - (i - 1) // 10:02d} {((i - 1) % 10) * 2:02d}:15:00",
        ))
    return rows


MOCK_HISTORY.extend(_make_mock_history_rows())


def load_mock_history():
    """载入演示历史:先清理本批演示记录再写入,保证幂等且不误伤真实数据。"""
    ids = [h[0] for h in MOCK_HISTORY]
    with _db() as conn:
        conn.executemany("DELETE FROM history WHERE review_id = ?",
                         [(i,) for i in ids])
        conn.executemany(
            """INSERT INTO history (review_id, domain, url, status, stars, title,
               author, review_date, note, checked_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, '', '', ?)""",
            [(rid, domain, f"https://www.{domain}/gp/customer-reviews/{rid}/",
              status, stars, title, author, check_time)
             for rid, domain, status, stars, title, author, check_time in MOCK_HISTORY],
        )


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

def page_header(title: str, meta: str = ""):
    """所有页面共用的标题条:左标题+副行,右侧留给操作区。

    三页走同一函数,标题基线与内容起点才会一致,切换页面不会错位。
    返回右侧列,调用方把按钮放进去即可。

    左列固定宽:标题+副行实测最宽 258px,给 300px 即可不换行;
    余量全留给右侧工具条(历史页 3 个控件需 469px,50/50 分栏会挤到第二行,
    把标题行从 48px 顶到 96px,切页就是肉眼可见的错位)。
    """
    left, right = st.columns([300, 560], vertical_alignment="center")
    with left:
        st.markdown(f'<div class="pg-title">{title}</div>', unsafe_allow_html=True)
        if meta:
            st.markdown(f'<div class="pg-meta">{meta}</div>', unsafe_allow_html=True)
    return right


def page_reviews():
    """一屏一件事:无结果→紧凑输入卡,有结果→工具条 + 记录表"""
    if st.session_state.get("results"):
        render_results()
    else:
        render_check_input()


def page_history():
    render_history()


def page_link_tracking():
    page_header("页面链接跟踪", "规划中 · 后端就绪后开放")
    st.info("跟踪产品/链接页面的快照与状态变化(价格、评分、评价数、上下架等)。",
            icon=":material/construction:")


MAX_BATCH = 50  # 批量边界:限速 3~5s/条,50 条约 4 分钟,更多请分批防 IP 过热


def render_check_input():
    """紧凑输入卡:标题条 + 粘贴框 + 解析摘要 + 主按钮,辅助入口收在右侧"""
    right = page_header("评价链接批量检测", "粘贴链接 · 每条 3~5 秒 · 支持六国站点混贴")
    with right:
        with st.container(horizontal=True, horizontal_alignment="right"):
            with st.popover("演示数据", icon=":material/science:"):
                st.caption("填充覆盖全部状态/站点的示例数据,仅预览界面,不联网、不影响真实检测")
                if st.button("载入 6 条演示结果", use_container_width=True):
                    load_mock_results()

    with st.container(border=True):
        # 手动写回 session_state:widget 状态在切页不渲染时会被框架清理,
        # 手动保存的值才能跨页保留(切页往返输入不丢)
        text = st.text_area(
            "链接", value=st.session_state.get("input_text", ""), height=150,
            label_visibility="collapsed",
            placeholder="每行一条,六国站点可混贴\n"
                        "https://www.amazon.com/gp/customer-reviews/R1XXXXXXX/\n"
                        "https://www.amazon.in/review/R2XXXXXXX/")
        st.session_state["input_text"] = text
        refs = parse_links(text)
        if len(refs) > MAX_BATCH:
            st.warning(f"一次最多 {MAX_BATCH} 条(每条 3~5 秒,更多会把 IP 跑热);"
                       f"已截取前 {MAX_BATCH} 条,其余请分批。")
            refs = refs[:MAX_BATCH]

        bar = st.columns([0.62, 0.38], vertical_alignment="center")
        with bar[0]:
            if refs:
                doms = sorted({r.domain for r in refs})
                with st.container(horizontal=True):
                    st.badge(f"{len(refs)} 条链接", color="blue",
                             icon=":material/link:")
                    for d in doms:
                        st.badge(d.replace("amazon.", ""), color="gray")
            else:
                st.caption("支持 /gp/customer-reviews/、/review/、portal 三种格式")
        ok = bar[1].button("开始检测", type="primary", icon=":material/play_arrow:",
                           disabled=not refs, use_container_width=True)

    if ok:
        run_check(refs)


@st.dialog("证据查看", width="large")
def shot_dialog(r):
    """抽查校验:大图 + 判定依据 + 原页面链接 + 历史轨迹"""
    label, color = STATUS_META.get(r["status"], (r["status"], "gray"))
    with st.container(horizontal=True):
        st.badge(label, color=color)
        st.badge(r["review_id"], color="gray")
        st.badge(r["domain"].replace("amazon.", ""), color="gray")
    if r["note"]:
        st.caption(f"判定依据:{r['note']}")
    # 抓到的原文摘要:表格列放不下,放弹窗做人工核对的第一手依据
    if r.get("body"):
        meta = " · ".join(x for x in (
            f"{r['stars']} ★" if str(r["stars"]).isdigit() else "",
            r["author"], r["review_date"],
            "Verified Purchase" if r["verified"] else "") if x)
        if meta:
            st.caption(meta)
        if r["title"]:
            st.markdown(f"**{r['title']}**")
        st.text(r["body"])
    st.link_button("打开原页面", r["url"], icon=":material/open_in_new:")
    if r.get("screenshot") and Path(r["screenshot"]).exists():
        st.image(r["screenshot"], use_container_width=True,
                 caption=f"检测时页面快照 · {r['checked_at']}")
    else:
        st.caption("本条无截图(仅正常状态或截图失败时出现)")
    tl = review_history_timeline(r["review_id"])
    if len(tl) > 1:
        st.dataframe(
            [{"检测时间": t[3], "状态": STATUS_LABEL.get(t[0], t[0]),
              "星级": f"{t[1]} ★" if str(t[1]).isdigit() else "—",
              "标题": t[2] or "—"} for t in tl],
            hide_index=True, use_container_width=True, height=200)


@st.dialog("分站统计", width="medium")
def domain_stats_dialog(results):
    """分站维度的量与存活率:用 dataframe + ProgressColumn,不手搓进度条"""
    agg = {}
    for r in results:
        d = r["domain"].replace("amazon.", "")
        a = agg.setdefault(d, {"total": 0, "alive": 0})
        a["total"] += 1
        a["alive"] += r["status"] == "alive"
    st.dataframe(
        [{"站点": d, "条数": a["total"], "存活": a["alive"],
          "存活率": a["alive"] / a["total"]}
         for d, a in sorted(agg.items(), key=lambda kv: -kv[1]["total"])],
        hide_index=True, use_container_width=True,
        column_config={
            "存活率": st.column_config.ProgressColumn(
                format="percent", min_value=0, max_value=1),
        })


@st.dialog("截图证据", width="large")
def evidence_dialog(shots):
    """本轮全部截图集中查看,不铺在主页面"""
    st.caption(f"共 {len(shots)} 张 · 点开原页面可二次核验")
    for r in shots:
        label, color = STATUS_META.get(r["status"], (r["status"], "gray"))
        with st.container(border=True):
            with st.container(horizontal=True):
                st.badge(label, color=color)
                st.badge(r["review_id"], color="gray")
                st.link_button("原页面", r["url"], icon=":material/open_in_new:")
            st.image(r["screenshot"], use_container_width=True)


def _results_csv(results, prev) -> bytes:
    """导出全字段 CSV(含 URL 与截图文件名,便于线下核验)"""
    rows = [{
        "状态": STATUS_META.get(r["status"], (r["status"], ""))[0],
        "Review ID": r["review_id"],
        "站点": r["domain"].replace("amazon.", ""),
        "星级": r["stars"] or "",
        "标题": r["title"] or "",
        "作者": r["author"] or "",
        "评价日期": r["review_date"] or "",
        "VP": "是" if r["verified"] else "",
        "正文": r.get("body") or "",
        "上次状态": (prev.get(r["review_id"]) or ("", ""))[0],
        "上次时间": (prev.get(r["review_id"]) or ("", ""))[1],
        "判定依据": r["note"] or "",
        "检测时间": r["checked_at"],
        "URL": r["url"],
        "截图": Path(r["screenshot"]).name if r.get("screenshot") else "",
    } for r in results]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8-sig")


def render_results():
    """结果视图:工具条 + 全字段记录表;验证细节走行选中与弹窗,不铺主页面。"""
    results = st.session_state.get("results") or []
    prev = st.session_state.get("prev", {})
    counts = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    # ── 工具条:标题 / 状态筛选 / 统计与导出入口 / 新一轮 ──
    # 副行只放本轮事实(条数+时间);"点选行看依据"是操作提示,
    # 放这里会把副行挤成两行、标题基线从 39px 抬到 32px,与其余三页错位,
    # 故移到筛选行右侧——紧贴它描述的那张表。
    right = page_header("评价链接批量检测",
                        f"本轮 {len(results)} 条 · {results[0]['checked_at']}")
    with right:
        with st.container(horizontal=True, horizontal_alignment="right"):
            st.download_button(
                "导出 CSV", _results_csv(results, prev),
                file_name=f"amreview_{datetime.now():%Y%m%d_%H%M}_{len(results)}条.csv",
                mime="text/csv", icon=":material/download:")
            if st.button("分站统计", icon=":material/bar_chart:"):
                domain_stats_dialog(results)
            shots = [r for r in results
                     if r.get("screenshot") and Path(r["screenshot"]).exists()]
            if shots and st.button(f"截图 {len(shots)}", icon=":material/image:"):
                evidence_dialog(shots)
            if st.button("新一轮", type="primary", icon=":material/refresh:"):
                st.session_state.update(results=[], prev={})
                st.rerun()

    expired = sorted({r["domain"] for r in results if r["status"] == "login_expired"})
    if expired:
        st.warning("登录失效:"
                   + "、".join(d.replace("amazon.", "") for d in expired)
                   + " → 侧边栏「账号登录」后重测", icon=":material/cookie:")

    # 状态筛选:只列出本轮出现过的状态,标签带条数
    label_to_status = {f"{STATUS_META[s][0]} {counts[s]}": s
                       for s in STATUS_ORDER if counts.get(s)}
    fl, fr = st.columns([0.7, 0.3], vertical_alignment="center")
    with fl:
        sel = st.segmented_control("状态筛选", ["全部"] + list(label_to_status),
                                   default="全部", key="res_filter",
                                   label_visibility="collapsed")
    with fr:
        st.markdown('<div class="pg-hint">点选表格行查看判定依据与截图</div>',
                    unsafe_allow_html=True)
    keep = label_to_status.get(sel)
    shown = [r for r in results if keep is None or r["status"] == keep]

    # ── 全字段记录表(主体):信息齐全 + 原页面直达,可排序可选中 ──
    def prev_cell(r) -> str:
        p = prev.get(r["review_id"])
        if not p:
            return "—"
        changed = p[0] != STATUS_LABEL[r["status"]]
        return f"{'⚠ ' if changed else ''}{p[0].split(' ')[-1]} · {p[1][5:16]}"

    table = [{
        "状态": STATUS_LABEL.get(r["status"], r["status"]),
        "Review ID": r["review_id"],
        "站点": r["domain"].replace("amazon.", ""),
        # 文本列而非数字列:星级只有 1~5 单字符,排序结果一致,空值不会渲染成 None
        "星级": f"{r['stars']} ★" if str(r["stars"]).isdigit() else "—",
        "VP": bool(r["verified"]),
        "标题": r["title"] or "—",
        "作者": r["author"] or "—",
        "评价日期": r["review_date"] or "—",
        "上次": prev_cell(r),
        "判定依据": r["note"] or "—",
        "检测时间": r["checked_at"][5:16],
        "原页面": r["url"],
    } for r in shown]

    picked = st.dataframe(
        table, use_container_width=True, hide_index=True, row_height=34,
        # 行少时按内容收紧,行多时给大屏一个高值填满视口(dataframe 不支持 stretch)
        height=min(1000, 44 + 34 * len(table)),
        key="res_table", on_select="rerun", selection_mode="single-row",
        column_config={
            # 12 列要在一屏内不横向截断:仅标题给 medium,其余压到 small
            # emoji + 4 字(登录失效)在 small 下会截断 → medium
            "状态": st.column_config.TextColumn(width="medium", pinned=True),
            # 主键要能整串核对,不能截断 → medium(已 pinned,横滚时仍可见)
            "Review ID": st.column_config.TextColumn(width="medium", pinned=True),
            "站点": st.column_config.TextColumn(width="small"),
            "星级": st.column_config.TextColumn(width="small"),
            "VP": st.column_config.CheckboxColumn(
                width="small", help="Verified Purchase 已验证购买"),
            "标题": st.column_config.TextColumn(width="medium"),
            "作者": st.column_config.TextColumn(width="small"),
            "评价日期": st.column_config.TextColumn(width="small"),
            "上次": st.column_config.TextColumn(
                width="small", help="上一轮该 ID 的状态与时间,⚠ 表示本轮有变化"),
            "判定依据": st.column_config.TextColumn(
                width="small", help="状态判定的原始依据,点行看完整内容"),
            "检测时间": st.column_config.TextColumn(width="small"),
            "原页面": st.column_config.LinkColumn(
                width="small", display_text="打开", help="在 Amazon 打开原页面复核"),
        })

    # ── 选中行 → 行内验证条(判定依据 / 原页面 / 证据弹窗) ──
    rows = picked.selection.rows if hasattr(picked, "selection") else []
    if rows:
        r = shown[rows[0]]
        label, color = STATUS_META.get(r["status"], (r["status"], "gray"))
        with st.container(border=True):
            bar = st.columns([0.72, 0.28], vertical_alignment="center")
            with bar[0]:
                with st.container(horizontal=True):
                    st.badge(label, color=color)
                    st.badge(r["review_id"], color="gray")
                    st.badge(r["domain"].replace("amazon.", ""), color="gray")
                st.caption(r["note"] or (r["title"] or "无附加判定说明"))
            with bar[1]:
                with st.container(horizontal=True, horizontal_alignment="right"):
                    st.link_button("原页面", r["url"],
                                   icon=":material/open_in_new:")
                    if st.button("证据", type="primary", icon=":material/fact_check:",
                                 key=f"ev_{r['review_id']}"):
                        shot_dialog(r)


@st.dialog("历史状态统计", width="medium")
def history_stats_dialog(days):
    stats = history_stats(days)
    total = sum(c for _, c in stats) or 1
    st.dataframe(
        [{"状态": STATUS_LABEL.get(s, s), "条数": c, "占比": c / total}
         for s, c in sorted(stats, key=lambda kv: -kv[1])],
        hide_index=True, use_container_width=True,
        column_config={"占比": st.column_config.ProgressColumn(
            format="percent", min_value=0, max_value=1)})


def render_history():
    """历史回顾:工具条 + 明细表;统计与演示数据收进弹窗。"""
    days_map = {"近 7 天": 7, "近 30 天": 30, "全部": None}

    right = page_header("检测历史", "每次检测自动留存,可按站点/状态排序核对变化")
    with right:
        with st.container(horizontal=True, horizontal_alignment="right"):
            sel = st.segmented_control("时间范围", list(days_map), default="近 7 天",
                                       key="hist_range", label_visibility="collapsed")
            days = days_map.get(sel, 7)
            if st.button("统计", icon=":material/bar_chart:"):
                history_stats_dialog(days)
            with st.popover("演示数据", icon=":material/science:"):
                st.caption("追加 68 条固定演示记录,用于预览长列表效果")
                if st.button("载入演示历史", use_container_width=True):
                    load_mock_history()
                    st.rerun()

    rows = recent_history(500, days)
    if not rows:
        st.info("暂无历史记录,完成第一次检测后会自动保存",
                icon=":material/history:")
        return

    st.dataframe(
        [{"检测时间": r[4], "Review ID": r[0],
          "站点": r[1].replace("amazon.", ""),
          "状态": STATUS_LABEL.get(r[2], r[2]),
          "标题": r[3] or "—",
          "原页面": f"https://www.{r[1]}/gp/customer-reviews/{r[0]}/"}
         for r in rows],
        use_container_width=True, hide_index=True, row_height=34,
        # 行少时按内容收紧,行多时给大屏一个高值填满视口(dataframe 不支持 stretch)
        height=min(1000, 44 + 34 * len(rows)),
        column_config={
            "检测时间": st.column_config.TextColumn(width="small", pinned=True),
            # 主键要能整串核对,不能截断 → medium(已 pinned,横滚时仍可见)
            "Review ID": st.column_config.TextColumn(width="medium", pinned=True),
            "站点": st.column_config.TextColumn(width="small"),
            "状态": st.column_config.TextColumn(width="small"),
            "标题": st.column_config.TextColumn(width="medium"),
            "原页面": st.column_config.LinkColumn(width="small", display_text="打开"),
        })
    st.caption(f"共 {len(rows)} 条(最多显示 500 条)")

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

@st.dialog("IP 热度(近 24h)", width="medium")
def heat_dialog():
    heat = heat_stats()
    if not heat:
        st.caption("暂无检测数据")
        return
    st.dataframe(
        [{"站点": d.replace("amazon.", ""), "检测": total,
          "被拦截": blocked or 0, "拦截率": (blocked or 0) / total}
         for d, total, blocked in heat],
        hide_index=True, use_container_width=True,
        column_config={"拦截率": st.column_config.ProgressColumn(
            format="percent", min_value=0, max_value=1)})
    st.caption("拦截率 <5% 正常;5~20% 建议降频;>20% 暂停或更换出口 IP")


NAV_PAGES = {
    "评价链接检测": ":material/fact_check:",
    "检测历史": ":material/history:",
    "页面链接跟踪": ":material/track_changes:",
}

with st.sidebar:
    st.markdown("#### AmReview")
    st.caption("Amazon 评价链接批量检测")

    # 页面切换:radio 保持在同一 session 内切换,输入与检测结果不丢失
    # (st.navigation 会整页重载并重置 session_state,实测不可用)
    page = st.radio("页面", list(NAV_PAGES), key="nav_page",
                    format_func=lambda p: p, label_visibility="collapsed")

    st.divider()

    status = login_status()
    online = sum(1 for v in status.values() if v["ok"])
    st.caption("账号与运行状态")
    if st.button(f"账号登录 {online}/{len(status)}", icon=":material/key:",
                 use_container_width=True,
                 help="维护各站点 Amazon 登录态"):
        login_dialog()
    if st.button("IP 热度", icon=":material/speed:", use_container_width=True,
                 help="近 24h 各站拦截率,IP 是否被加热的早期信号"):
        heat_dialog()
    if st.button("系统维护", icon=":material/settings:", use_container_width=True,
                 help="Playwright 升级、数据库信息与服务重启"):
        system_dialog()

    if online < len(status):
        st.caption(f"{len(status) - online} 个站点未登录,检测会判为登录失效")

# ---------- 页面渲染 ----------

if page == "评价链接检测":
    page_reviews()
elif page == "检测历史":
    page_history()
else:
    page_link_tracking()
