"""AmReview NiceGUI 版 — 界面层整体重写,业务层(engine/weblogin/monitor)零改动复用。

布局规范(polabel2 DESIGN.md 对齐):
- 侧边栏 220px 白底右描边;主区 #f1f5f9 + 20px 点阵
- 页头 44px:左标题(17px/700)+副行(12px),右操作区
- 表格 13px、行高 30px、粘性表头;状态列彩色药丸徽标
- 令牌:--pri #2563eb --line #e2e8f0 --ink #1e293b --body #f1f5f9

运行: .venv/bin/python ui.py  (端口 8765;旧 Streamlit 版改用 ./start.sh legacy)
"""

from __future__ import annotations

import asyncio
import csv
import html as html_mod
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import nicegui.ui as ui
from nicegui import app, run

import weblogin
import review_db
import review_import
import review_track
from engine import STATUS_LABEL, ReviewChecker, parse_links
from monitor import store as monitor_store
from monitor import demo as monitor_demo

DB = Path(__file__).parent / "history.db"
MONITOR_DB = Path(__file__).parent / "monitor.db"
ACCOUNTS_FILE = Path(__file__).parent / "accounts.json"
PROFILE_ROOT = Path.home() / ".amreview" / "profile"
DOMAINS = ["amazon.com", "amazon.com.mx", "amazon.com.br", "amazon.in",
           "amazon.com.au", "amazon.co.jp"]
# 站点显示为两位国家码(US/UK/JP/...),未收录域名回退为去 amazon. 前缀
DOMAIN_CC = {"amazon.com": "US", "amazon.co.uk": "UK", "amazon.de": "DE",
             "amazon.co.jp": "JP", "amazon.com.au": "AU", "amazon.in": "IN",
             "amazon.com.mx": "MX", "amazon.com.br": "BR", "amazon.es": "ES",
             "amazon.it": "IT", "amazon.fr": "FR", "amazon.ca": "CA"}
DOMAIN_SHORT = lambda d: DOMAIN_CC.get(d, d.replace("amazon.", ""))
MAX_BATCH = 50

STATUS_META = {  # status -> (中文, tailwind 药丸 class)
    "alive": ("正常", "bg-[#dcfce7] text-[#15803d]"),
    "deleted": ("已删", "bg-[#fee2e2] text-[#b91c1c]"),
    "blocked": ("被拦截", "bg-[#fef3c7] text-[#b45309]"),
    "login_expired": ("登录失效", "bg-[#ede9fe] text-[#6d28d9]"),
    "unknown": ("未知", "bg-[#e2e8f0] text-[#475569]"),
    "pending": ("待检测", "bg-[#e2e8f0] text-[#475569]"),
}
# 站点内展示顺序:问题状态(已删/被拦截/登录失效/未知)在前,正常在后
STATUS_ORDER = ["deleted", "blocked", "login_expired", "unknown", "alive", "pending"]

with open(Path(__file__).parent / "style.css") as f:
    _css = f.read()
app.add_static_files("/screenshots", str(Path(__file__).parent / "screenshots"))
# CSS 内联注入:内容随文件改动即变,且无独立 CSS 文件可被浏览器缓存
ui.add_head_html(f"<style>{_css}</style>", shared=True)
# 表格「链接」列的全局辅助:复制到剪贴板(http 回退)+ 轻提示
ui.add_head_html("""<style>
.link-act {display:inline-flex;align-items:center;gap:2px;}
.link-act button {border:none;background:none;padding:2px 3px;cursor:pointer;
  color:#64748b;border-radius:4px;line-height:1;font-size:13px;}
.link-act button:hover {background:#eff6ff;color:#2563eb;}
.copy-toast {position:fixed;top:18px;left:50%;transform:translateX(-50%);
  background:#1e293b;color:#fff;font-size:12px;padding:5px 12px;border-radius:6px;
  z-index:9999;opacity:0;transition:opacity .15s;pointer-events:none;}
/* AG Grid v34 delay-render shim:列含 flex 时 AG Grid 会先隐藏表格、等
   ResizeObserver 回调再显示,但该回调在本环境不触发,表格永久空白(实测)。
   CSS 保底强制可见;下面的轮询在出现 250ms 后摘掉 ag-delay-render 类,
   正常流程 AG Grid 自己会先摘,轮询只兜底卡死的场景。 */
:where(.ag-delay-render) .ag-row, :where(.ag-delay-render) .ag-cell,
:where(.ag-delay-render) .ag-header-cell {visibility:visible !important;}
</style>""", shared=True)
ui.add_body_html("""<script>
function copyText(t) {
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(t); return;
  }
  var ta = document.createElement('textarea');
  ta.value = t; ta.style.position = 'fixed'; ta.style.opacity = '0';
  document.body.appendChild(ta); ta.select();
  try { document.execCommand('copy'); } catch (e) {}
  document.body.removeChild(ta);
}
function showCopyToast(msg) {
  var el = document.createElement('div');
  el.className = 'copy-toast'; el.textContent = msg;
  document.body.appendChild(el);
  requestAnimationFrame(function() { el.style.opacity = '1'; });
  setTimeout(function() { el.style.opacity = '0'; }, 1200);
  setTimeout(function() { el.remove(); }, 1500);
}
(function pollDelayRender() {
  document.querySelectorAll('.ag-delay-render').forEach(function(el) {
    setTimeout(function() { el.classList.remove('ag-delay-render'); }, 250);
  });
  setTimeout(pollDelayRender, 500);
})();
</script>""", shared=True)


def link_cell(url: str) -> str:
    """表格「链接」列:新窗口打开 + 一键复制(行内小图标)。

    事件带 stopPropagation,避免触发 AGGrid 的 cellClicked 行详情弹窗。
    """
    u = html_mod.escape(url, quote=True)
    return (f'<span class="link-act">'
            f'<button title="打开原页面" onclick="window.open(\'{u}\',\'_blank\')">'
            f'↗</button>'
            f'<button title="复制链接" '
            f'onclick="copyText(\'{u}\');showCopyToast(\'链接已复制\')">'
            f'⧉</button>'
            f'</span>')

# ---------- 数据层 ----------
# history.db 的读写集中在 review_db(检测历史 + 评价链接台账 review_meta),
# 定时跟踪器与界面共用同一套 SQL,避免两处各写一份。


def _db():
    return review_db.connect()


def init_db():
    review_db.init_db()


def save_history(results, auto_meta=True):
    """写检测历史。auto_meta=True 时把新链接自动纳入每日跟踪队列。"""
    review_db.save_history(results, auto_meta=auto_meta)


def last_status_map(refs):
    """结果页「上次检测」列:状态转中文标签。"""
    return {rid: (STATUS_LABEL.get(s, s), t)
            for rid, (s, t) in review_db.last_status_map(refs).items()}


def recent_history(limit=500, days=None):
    """检测历史(含台账里已登记但尚未检测过的链接,状态为空串 → 展示为「待检测」)。"""
    return review_db.recent_history(limit, days)


def history_stats(days=None):
    return review_db.history_stats(days)


def heat_stats():
    return review_db.heat_stats()


def review_history_timeline(review_id, limit=60):
    return review_db.review_history_timeline(review_id, limit)


def login_status():
    """各站点登录状态;结果缓存 30s,避免每次侧边栏渲染/弹窗打开都读 6 份 cookie 文件。"""
    global _login_status_cache
    now = time.time()
    if _login_status_cache and now - _login_status_cache[0] < 30:
        return _login_status_cache[1]
    out = {}
    for d in DOMAINS:
        ss = PROFILE_ROOT / d / "storage_state.json"
        if ss.exists():
            ok = False
            try:
                cookies = json.loads(ss.read_text()).get("cookies", [])
                ok = any(c.get("name", "").startswith(("at-", "x-"))
                         and (c.get("expires", -1) < 0 or c.get("expires", 0) > now)
                         for c in cookies)
            except Exception:
                pass
            out[d] = {"ok": ok, "days": int((now - ss.stat().st_mtime) / 86400)}
        else:
            out[d] = {"ok": False, "days": None}
    _login_status_cache = (now, out)
    return out


_login_status_cache = None


def load_accounts():
    if ACCOUNTS_FILE.exists():
        try:
            return json.loads(ACCOUNTS_FILE.read_text())
        except Exception:
            return {}
    return {}


ACCOUNTS = load_accounts()

# ---------- 演示数据(直接复用旧 app.py 的构造逻辑) ----------

MOCK_RESULTS = [
    {"review_id": "R1ALIVE1234", "domain": "amazon.com", "status": "alive",
     "stars": "4", "title": "Great product, works as expected",
     "author": "John D.", "review_date": "2026年8月10日", "body": "Worth every penny.",
     "verified": True, "note": "", "shot_kind": "", "checked_at": "2026-08-22 03:00:05",
     "url": "https://www.amazon.com/gp/customer-reviews/R1ALIVE1234/",
     "prev_status": None, "prev_time": ""},
    {"review_id": "R2DELETED6789", "domain": "amazon.in", "status": "deleted",
     "stars": "", "title": "", "author": "", "review_date": "", "body": "",
     "verified": False, "note": "正常·08-1 HTTP 404 · Page Not Found",
     "shot_kind": "deleted", "checked_at": "2026-08-22 03:00:00",
     "url": "https://www.amazon.in/gp/customer-reviews/R2DELETED6789/",
     "prev_status": "✅ 正常", "prev_time": "2026-08-22 02:00:30"},
    {"review_id": "R3BLOCKED1111", "domain": "amazon.com.au", "status": "blocked",
     "stars": "", "title": "", "author": "", "review_date": "", "body": "",
     "verified": False, "note": "重试 3 次仍被拦截(guard/captcha 拦截),建议稍后复测",
     "shot_kind": "", "checked_at": "2026-08-22 02:30:00",
     "url": "https://www.amazon.com.au/gp/customer-reviews/R3BLOCKED1111/",
     "prev_status": None, "prev_time": ""},
    {"review_id": "R4LOGIN2222", "domain": "amazon.co.jp", "status": "login_expired",
     "stars": "", "title": "", "author": "", "review_date": "", "body": "",
     "verified": False, "note": "跳转登录页,需重新引导登录该站点 Amazon 账号",
     "shot_kind": "", "checked_at": "2026-08-22 02:01:00",
     "url": "https://www.amazon.co.jp/gp/customer-reviews/R4LOGIN2222/",
     "prev_status": None, "prev_time": ""},
    {"review_id": "R5UNKNOWN3333", "domain": "amazon.com.mx", "status": "unknown",
     "stars": "", "title": "", "author": "", "review_date": "", "body": "",
     "verified": False, "note": "已删·08-1 HTTP 200 · 无法识别的页面形态",
     "shot_kind": "unknown", "checked_at": "2026-08-22 02:00:30",
     "url": "https://www.amazon.com.mx/gp/customer-reviews/R5UNKNOWN3333/",
     "prev_status": "🐕 已删", "prev_time": "2026-08-22 01:30:00"},
    {"review_id": "R6ALIVE4444", "domain": "amazon.com.br", "status": "alive",
     "stars": "5", "title": "Excelente produto!", "author": "Maria S.",
     "review_date": "2026年8月5日", "body": "Recomendo.", "verified": False,
     "note": "", "shot_kind": "", "checked_at": "2026-08-22 01:30:30",
     "url": "https://www.amazon.com.br/gp/customer-reviews/R6ALIVE4444/",
     "prev_status": None, "prev_time": ""},
]


def _mock_font(size):
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


def _make_mock_shot(review_id, kind):
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
        d.rectangle([0, 0, W, 70], fill=(35, 47, 62))
        d.rectangle([0, 0, 150, 70], fill=(68, 71, 85))
        d.rectangle([160, 20, 300, 50], fill=(255, 255, 255))
        d.rectangle([330, 20, 430, 50], fill=(255, 255, 255))
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
    results = []
    for src in MOCK_RESULTS:
        r = dict(src)
        r["screenshot"] = _make_mock_shot(r["review_id"], r["shot_kind"]) \
            if r["shot_kind"] else ""
        r.pop("shot_kind", None)
        r.pop("prev_status", None)
        r.pop("prev_time", None)
        results.append(r)
    app.storage.user["results"] = results
    app.storage.user["prev"] = {
        m["review_id"]: (m["prev_status"], m["prev_time"])
        for m in MOCK_RESULTS if m["prev_status"]}
    save_history(results, auto_meta=False)   # 演示 ID 不进跟踪队列


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


def _make_mock_history_rows(count=50):
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
             for rid, domain, status, stars, title, author, check_time in MOCK_HISTORY])


MOCK_IDS = tuple({r["review_id"] for r in MOCK_RESULTS}
                 | {h[0] for h in MOCK_HISTORY})


def _delete_mock_rows():
    with _db() as conn:
        conn.executemany("DELETE FROM history WHERE review_id = ?",
                         [(i,) for i in MOCK_IDS])


def load_all_mock():
    load_mock_results()
    load_mock_history()
    monitor_demo.seed_demo(MONITOR_DB)


def unload_all_mock():
    app.storage.user["results"] = []
    app.storage.user["tracking"] = []
    app.storage.user.pop("prev", None)
    _delete_mock_rows()
    monitor_store.delete_by_asins(MONITOR_DB, list(monitor_demo.TIMELINES))


init_db()
monitor_store.init_db(MONITOR_DB)   # 监控库补列迁移(bsr_cat/bsr_sub 等)在启动时跑

# 应用内定时采集:后台守护线程,间隔/开关存 settings 表,UI 改完即生效
from monitor.scheduler import Scheduler as _MonitorScheduler
monitor_scheduler = _MonitorScheduler(MONITOR_DB)

# 评价链接每日跟踪:同样是后台守护线程,把全部链接分散到 24 小时里各查一次
review_tracker = review_track.ReviewTracker(DB, MONITOR_DB)


async def _start_background_jobs():
    """服务真正起来时才拉起后台线程。

    **不能在模块导入期启动**:测试(test_login_thread 等)会 exec 本文件
    (截到 ui.run 之前),导入期起线程会让「跑一次单测」顺手触发真实的
    Amazon 采集与评价链接检测。
    """
    monitor_scheduler.start()
    review_tracker.start()


app.on_startup(_start_background_jobs)

# ---------- 设计系统:可复用的小组件 ----------

# ui.html 在 NiceGUI 3.x 必须显式给 sanitize;本应用的 html() 只承载
# 内部构造的标记(状态药丸/标题/KPI),用户输入一律走 ui.label/ui.input,
# 故统一 sanitize=False。
html = lambda content: ui.html(content, sanitize=False)


def pill(status: str):
    """状态彩色药丸。"""
    label, cls = STATUS_META.get(status, (status, "bg-[#e2e8f0] text-[#475569]"))
    return (f'<span class="pill {cls}" style="display:inline-block;padding:1px 8px;'
            f'border-radius:999px;font-size:12px;font-weight:700;line-height:18px;">'
            f'{label}</span>')


def status_text(status: str) -> str:
    """状态列(极简):纯文字 + 状态色,不要药丸底色。"""
    label = STATUS_META.get(status, (status, ""))[0]
    color = {"alive": "#15803d", "deleted": "#b91c1c", "blocked": "#b45309",
             "login_expired": "#6d28d9"}.get(status, "#475569")
    return f'<span style="color:{color};font-weight:600">{label}</span>'


def stars_html(stars) -> str:
    """星级着色:4~5 星绿色(好评),1~3 星红色(差评),无星级灰色破折号。"""
    if not str(stars).isdigit():
        return '<span style="color:#94a3b8">—</span>'
    n = int(stars)
    color = "#15803d" if n >= 4 else "#b91c1c"
    return f'<span style="color:{color};font-weight:600">{n} ★</span>'


def page_header(title: str, meta: str = ""):
    """44px 页头:左标题+副行,右操作区由调用方 fill。"""
    with ui.row().classes("w-full items-center justify-between min-h-[44px] gap-3"):
        with ui.column().classes("gap-0"):
            html(f'<div class="pg-title">{title}</div>')
            if meta:
                html(f'<div class="pg-meta">{meta}</div>')
        yield


def kpi_card(label: str, value: str, tone: str = "ink"):
    """内联统计块:数字+标签横排,六个并排只占一条细卡,不与表格抢空间。"""
    color = {"ink": "#1e293b", "ok": "#15803d", "danger": "#b91c1c",
             "warn": "#b45309", "violet": "#6d28d9"}.get(tone, "#1e293b")
    html(f'<div class="kpi-inline"><span class="kpi-num" '
         f'style="color:{color}">{value}</span>'
         f'<span class="kpi-tag">{label}</span></div>')


# ---------- 页面:检测 ----------


@ui.page("/")
def page_check():
    results = app.storage.user.get("results") or []

    with build_shell("/"):
        if not results:
            # ── 输入卡视图:页头 + 紧凑输入卡 ──
            with ui.row().classes("w-full items-center justify-between gap-3 mb-2"):
                html('<div><div class="pg-title">评价链接批量检测</div>'
                     '<div class="pg-meta">粘贴链接 · 每条 3~5 秒 · 支持六国站点混贴</div></div>')
            with ui.card().classes("app-card w-full"):
                html('<div class="card-title">待检测链接</div>')
                ta = ui.textarea(placeholder="每行一条,六国站点可混贴\n"
                                            "https://www.amazon.com/gp/customer-reviews/R1XXXXXXX/")
                ta.classes("w-full").props("outlined dense rows=7").style("font-size:13px")
                with ui.row().classes("w-full items-center justify-between"):
                    html('<div class="pg-meta">支持 /gp/customer-reviews/、/review/、'
                         'portal 三种格式</div>')
                    with ui.row().classes("items-center gap-2"):
                        # 导入测评订单表格:评价链接自动进系统并纳入每日跟踪
                        ui.button("导入表格", icon="upload_file",
                                  on_click=lambda: import_table_dialog(
                                      lambda: ui.navigate.to("/history"))) \
                            .props("outline no-caps")
                        btn = ui.button("开始检测", icon="play_arrow") \
                            .props("unelevated no-caps")

                def do_check():
                    refs = parse_links(ta.value or "")
                    if not refs:
                        ui.notify("未解析到有效链接", type="negative")
                        return
                    if len(refs) > MAX_BATCH:
                        refs = refs[:MAX_BATCH]
                        ui.notify(f"一次最多 {MAX_BATCH} 条,已截取", type="warning")
                    btn.props("disable loading")
                    prog_row.set_visibility(True)
                    stats_text.set_visibility(True)
                    prog.set_value(0)
                    prog_text.set_text("准备中…")
                    stats_text.set_text("")
                    prev = last_status_map(refs)
                    checker = ReviewChecker()

                    async def _run():
                        # 工作线程只写 state,ui.timer 轮询画到屏幕(与监控页同款)
                        state = {"done": 0, "line": "", "tally": "", "error": ""}

                        def _work():
                            out = []
                            counts = {}
                            def on_result(i, ref, r):
                                out.append(r)
                                counts[r["status"]] = counts.get(r["status"], 0) + 1
                                state["done"] = i + 1
                                state["line"] = (
                                    f"[{i + 1}/{len(refs)}] {DOMAIN_SHORT(ref.domain)} · "
                                    f"{ref.review_id} → "
                                    f"{STATUS_LABEL.get(r['status'], r['status'])}")
                                tally = " · ".join(
                                    f"{STATUS_LABEL.get(s, s)} {n}"
                                    for s, n in sorted(counts.items(),
                                                       key=lambda kv: -kv[1]))
                                state["tally"] = (f"剩余 {len(refs) - (i + 1)} 条 · "
                                                  + tally)
                            try:
                                checker.check_batch(refs, on_result=on_result)
                            except Exception as e:
                                state["error"] = str(e)
                            finally:
                                checker.close()
                            return out

                        def _poll():
                            prog.set_value(state["done"] / len(refs))
                            prog_text.set_text(state["line"] or "准备中…")
                            stats_text.set_text(state["tally"])

                        timer = ui.timer(0.2, _poll)
                        results_new = await run.io_bound(_work)
                        timer.cancel()
                        if state["error"]:
                            ui.notify(f"检测中断:{state['error']}", type="negative")
                        btn.props(remove="disable loading")
                        if results_new:
                            save_history(results_new)
                            app.storage.user["results"] = results_new
                            app.storage.user["prev"] = prev
                        ui.navigate.to("/")

                    return _run()

                prog_row = ui.row().classes("w-full")
                with prog_row:
                    prog = ui.linear_progress(value=0, show_value=False).classes("flex-grow")
                    prog_text = ui.label("").classes("pg-meta")
                stats_text = ui.label("").classes("pg-meta")
                prog_row.set_visibility(False)
                stats_text.set_visibility(False)
                btn.on("click", do_check)
        else:
            # ── 结果视图 ──
            prev = app.storage.user.get("prev", {})
            counts = {}
            for r in results:
                counts[r["status"]] = counts.get(r["status"], 0) + 1

            # 页头:左「大标题+meta」右「搜索框」;KPI 卡下移与按钮同一行
            # (版式与历史/监控页一致)
            filt = app.storage.user.setdefault("res_filter", "all")
            res_q = app.storage.user.setdefault("res_q", "")

            def apply_filter(key):
                app.storage.user["res_filter"] = key
                ui.navigate.reload()

            def apply_search():
                app.storage.user["res_q"] = q.value
                ui.navigate.reload()

            with ui.row().classes("w-full items-center justify-between gap-3 mb-2"):
                meta = html(f'<div><div class="pg-title">评价链接批量检测</div>'
                            f'<div class="pg-meta">本轮 {len(results)} 条 · '
                            f'{results[0]["checked_at"]}</div></div>')
                q = ui.input(value=res_q, placeholder="搜索 ID / 标题 / 作者 …") \
                    .props("outlined dense hide-bottom-space") \
                    .classes("w-64").style("font-size:13px")
                q.on("keydown",
                     lambda e: apply_search() if e.args.get("key") == "Enter" else None)
            # KPI 卡 + 操作按钮同一行:左 KPI 卡,右按钮(与历史/监控页同版式)
            with ui.row().classes("w-full items-center justify-between gap-3 mb-2 no-wrap"):
                # KPI 卡即筛选器:点状态卡只看该状态,再点恢复全部
                def kpi_btn(label, value, tone, key):
                    active = filt == key
                    color = {"ink": "#1e293b", "ok": "#15803d", "danger": "#b91c1c",
                             "warn": "#b45309", "violet": "#6d28d9"}.get(tone, "#1e293b")
                    bg = "#eff6ff" if active else "#fff"
                    bd = "#2563eb" if active else "#e2e8f0"
                    b = ui.button().props("flat no-caps dense")
                    b.on("click", lambda e, k=key: apply_filter(
                        "all" if filt == k else k))
                    b.style(f"background:{bg};border:1px solid {bd};border-radius:6px;"
                            "padding:4px 9px;min-height:0;height:32px;cursor:pointer;"
                            "box-shadow:none;")
                    with b:
                        html(f'<span class="kpi-num" style="color:{color}">{value}</span>'
                             f'<span class="kpi-tag">{label}</span>')
                with ui.row().classes("items-center gap-2"):
                    kpi_btn("本轮总数", str(len(results)), "ink", "all")
                    kpi_btn("正常", str(counts.get("alive", 0)), "ok", "alive")
                    kpi_btn("已删", str(counts.get("deleted", 0)), "danger", "deleted")
                    kpi_btn("被拦截", str(counts.get("blocked", 0)), "warn", "blocked")
                    kpi_btn("登录失效", str(counts.get("login_expired", 0)), "violet",
                            "login_expired")
                    kpi_btn("未知", str(counts.get("unknown", 0)), "ink", "unknown")
                with ui.row().classes("items-center gap-2 flex-none"):
                    # 两个按钮锁同宽,免得「新一轮」字短显得一宽一窄
                    ui.button("导出 CSV", icon="download") \
                        .props("outline no-caps dense").classes("w-[116px]")
                    ui.button("新一轮", icon="refresh", on_click=lambda: (
                        app.storage.user.update(results=[], prev={}, res_filter="all",
                                                res_q=""),
                        ui.navigate.to("/")
                    )).props("unelevated no-caps dense color=primary") \
                        .classes("w-[116px]")

            # 结果表(AGGrid:13px、药丸徽标、行点选;受 KPI 卡筛选+搜索框过滤)
            shown = [r for r in results if filt == "all" or r["status"] == filt]
            kw = (res_q or "").strip().lower()
            if kw:
                shown = [r for r in shown if kw in " ".join(
                    (r.get(k) or "") for k in
                    ("review_id", "url", "title", "author", "note")).lower()]
                meta.set_content(
                    f'<div><div class="pg-title">评价链接批量检测</div>'
                    f'<div class="pg-meta">本轮 {len(results)} 条 · '
                    f'{results[0]["checked_at"]} · 搜索“{res_q.strip()}” · '
                    f'显示 {len(shown)} 条</div></div>')
            # 输入乱序时展示仍按国家聚拢(AU/BR/IN/JP/MX/US...),
            # 同站点内问题状态(已删/被拦截/登录失效/未知)在前,正常在后,其余保持输入顺序
            shown.sort(key=lambda r: (DOMAIN_SHORT(r["domain"]),
                                      STATUS_ORDER.index(r["status"])
                                      if r["status"] in STATUS_ORDER else 99))
            rows = []
            for r in shown:
                p = prev.get(r["review_id"])
                rows.append({
                    "status_text": status_text(r["status"]),
                    "status_sort": r["status"],
                    "review_id": r["review_id"],
                    "link": link_cell(r["url"]),
                    "domain": DOMAIN_SHORT(r["domain"]),
                    "stars": stars_html(r["stars"]),
                    "vp": "✓" if r["verified"] else "",
                    "title": r["title"] or "—",
                    "author": r["author"] or "—",
                    "review_date": r["review_date"] or "—",
                    "last": (f"⚠ {p[0].split(' ')[-1]} · {p[1][5:16]}" if p else "—"),
                    "note": r["note"] or "—",
                    "checked_at": r["checked_at"][5:16],
                    "_url": r["url"],
                    "_id": r["review_id"],
                })
            grid = ui.aggrid({
                "columnDefs": [
                    {"headerName": "状态", "field": "status_text", "width": 80,
                     "pinned": "left"},
                    {"headerName": "Review ID", "field": "review_id", "width": 148,
                     "pinned": "left", "tooltipField": "review_id",
                     "cellClass": "rid-copy"},
                    {"headerName": "链接", "field": "link", "width": 68, "sortable": False},
                    {"headerName": "站点", "field": "domain", "width": 62},
                    {"headerName": "星级", "field": "stars", "width": 62},
                    {"headerName": "VP", "field": "vp", "width": 56},
                    {"headerName": "标题", "field": "title", "minWidth": 160, "flex": 3},
                    {"headerName": "作者", "field": "author", "width": 92},
                    {"headerName": "评价日期", "field": "review_date", "width": 134},
                    {"headerName": "上次检测", "field": "last", "width": 200},
                    {"headerName": "判定依据", "field": "note", "minWidth": 160, "flex": 2},
                    {"headerName": "检测时间", "field": "checked_at", "width": 168},
                ],
                "rowData": rows,
                "defaultColDef": {"sortable": True, "resizable": True,
                                  "suppressMovable": True},
                "rowHeight": 30,
            }, html_columns=[0, 2, 4]).classes("w-full ag-dense ag-fill")
            def on_cell_click(e):
                rid = e.args["data"]["_id"]
                col = e.args.get("colId")
                if col == "review_id":
                    # 点 ID 格 = 复制完整 ID(显示截断不影响),不开详情
                    ui.run_javascript(
                        f"copyText({json.dumps(rid)});showCopyToast('ID 已复制')")
                elif col != "link":
                    detail_dialog(next(r for r in results
                                       if r["review_id"] == rid))
            grid.on("cellClicked", on_cell_click)


def detail_dialog(r: dict):
    """行点选 → 结果详情弹窗。"""
    label, cls = STATUS_META.get(r["status"], (r["status"], ""))
    with ui.dialog() as d, ui.card().classes("app-card w-[640px]"):
        with ui.row().classes("w-full items-center justify-between"):
            html(f'<div class="card-title">{label} · {r["review_id"]}</div>')
            ui.button(icon="close", on_click=d.close).props("flat round dense")
        if r.get("note"):
            html(f'<div class="pg-meta">判定依据:{r["note"]}</div>')
        meta = " · ".join(x for x in (
            f"{r['stars']} ★" if str(r["stars"]).isdigit() else "",
            r["author"], r["review_date"],
            "Verified Purchase" if r["verified"] else "") if x)
        if meta:
            html(f'<div class="pg-meta">{meta}</div>')
        if r.get("title"):
            html(f'<b style="font-size:13.5px">{r["title"]}</b>')
        if r.get("body"):
            ui.label(r["body"]).classes("text-[13px] text-[#475569]")
        if r.get("screenshot") and Path(r["screenshot"]).exists():
            ui.image(r["screenshot"]).classes("w-full rounded-md")
        with ui.row():
            ui.button("打开原页面", on_click=lambda: ui.open(r["url"], new_tab=True)) \
                .props("outline no-caps dense icon=open_in_new")
            if len(review_history_timeline(r["review_id"])) > 1:
                ui.button("历史轨迹").props("outline no-caps dense")
    d.open()


# ---------- 弹窗:导入测评订单表格 ----------


def import_table_dialog(on_done=None):
    """导入测评订单表格(xlsx/xls)。

    取 刷单编号(A列) / 产品型号(D列) / 订单号(F列) / 测评链接(M列):
    - 以有评价链接为首要条件:没有链接的行不加进来;
    - 已加进来的不重复加,只把空的业务字段用表格数据补齐;
    - 可选导入后立即跑一轮单次检测(与检测页同款逻辑),结果进检测历史。
    """
    state = {"rows": [], "stats": None}

    with ui.dialog() as d, ui.card().classes("app-card w-[620px]"):
        with ui.row().classes("w-full items-center justify-between"):
            html('<div class="card-title">导入测评订单表格</div>')
            ui.button(icon="close", on_click=d.close).props("flat round dense")
        html('<div class="pg-meta">读取表格的 <b>刷单编号(A列)</b>、'
             '<b>产品型号(D列)</b>、<b>订单号(F列)</b>、<b>测评链接(M列)</b>。'
             '没有评价链接的行不会加进来;已在系统里的链接不重复加,'
             '只补齐它空着的业务字段。</div>')

        preview = ui.column().classes("w-full gap-1")
        msg = ui.label("").classes("pg-meta")

        async def _on_upload(e):
            # NiceGUI 的 UploadEventArguments.file 是 Starlette UploadFile:
            # 异步 read();兜底读它内部的 SpooledTemporaryFile
            up = getattr(e, "file", None)
            raw = b""
            if up is not None:
                try:
                    raw = await up.read()
                except Exception:
                    raw = b""
                if not raw:
                    inner = getattr(up, "file", None)
                    if inner is not None:
                        try:
                            inner.seek(0)
                            raw = inner.read()
                        except Exception:
                            raw = b""
            if not raw:
                msg.set_text("读取上传文件失败,请重新选择")
                return
            try:
                rows, stats = review_import.parse_order_file(raw)
            except ValueError as ex:
                # review_import 抛的文案本身就是给用户看的(如旧版 .xls 提示)
                state["rows"], state["stats"] = [], None
                preview.clear()
                msg.set_text(str(ex))
                return
            except Exception as ex:
                state["rows"], state["stats"] = [], None
                preview.clear()
                msg.set_text(f"解析失败:{ex.__class__.__name__}: {ex}")
                return
            state["rows"], state["stats"] = rows, stats
            msg.set_text("")
            preview.clear()
            with preview:
                html(f'<div class="text-[13px]" style="font-weight:600">'
                     f'{review_import.summarize(stats)}</div>')
                if not rows:
                    html('<div class="pg-meta">没有解析到带评价链接的行,'
                         '请确认表格的 M 列是 Amazon 评价链接。</div>')
                    return
                head = ('<tr>' + ''.join(f'<th>{h}</th>' for h in
                        ("刷单编号", "订单号", "产品型号", "评价链接"))
                        + '</tr>')
                body = ""
                for r in rows[:6]:
                    body += ('<tr>'
                             + ''.join(f'<td>{html_mod.escape(str(r[k] or "—"))}</td>'
                                       for k in ("order_ref", "order_no", "model"))
                             + f'<td>{html_mod.escape(r["url"])}</td></tr>')
                more = (f'<div class="pg-meta">…另有 {len(rows) - 6} 条</div>'
                        if len(rows) > 6 else "")
                html('<div class="imp-wrap"><table class="imp-tb">'
                     + head + body + '</table></div>' + more)

        ui.upload(label="选择 .xlsx / .xls 文件", auto_upload=True,
                  max_file_size=20_000_000, on_upload=_on_upload) \
            .props('accept=".xlsx,.xls" flat bordered').classes("w-full")

        immediate = ui.switch("导入后立即检测一轮(单次)", value=True)

        prog_row = ui.row().classes("w-full")
        with prog_row:
            prog = ui.linear_progress(value=0, show_value=False).classes("flex-grow")
            prog_text = ui.label("").classes("pg-meta")
        prog_row.set_visibility(False)

        async def _confirm():
            rows = state["rows"]
            if not rows:
                msg.set_text("请先选择一个表格文件")
                return
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for r in rows:
                r["created_at"] = stamp
            stat = review_db.upsert_review_meta(rows, source="table")
            summary = (f'已加入 {stat["added"]} 条新链接 · 回填业务字段 '
                       f'{stat["filled"]} 条 · 已存在 {stat["unchanged"]} 条')
            if not immediate.value:
                d.close()
                ui.notify(summary + ",已纳入每日定时跟踪", type="positive")
                if on_done:
                    on_done()
                return

            refs = parse_links("\n".join(r["url"] for r in rows))
            if not refs:
                d.close()
                ui.notify(summary, type="positive")
                if on_done:
                    on_done()
                return
            confirm_btn.props("disable loading")
            prog_row.set_visibility(True)
            prog.set_value(0)
            prog_text.set_text(f"准备检测 {len(refs)} 条…")
            state_now = {"done": 0, "tally": {}, "error": ""}

            def _work():
                checker = ReviewChecker()

                def on_result(i, ref, r):
                    state_now["done"] = i + 1
                    t = state_now["tally"]
                    t[r["status"]] = t.get(r["status"], 0) + 1
                try:
                    return checker.check_batch(refs, on_result=on_result)
                finally:
                    checker.close()

            def _poll():
                prog.set_value(state_now["done"] / len(refs))
                tally = " · ".join(
                    f"{STATUS_LABEL.get(s, s)} {n}"
                    for s, n in sorted(state_now["tally"].items(), key=lambda kv: -kv[1]))
                prog_text.set_text(f"[{state_now['done']}/{len(refs)}] {tally}")

            timer = ui.timer(0.2, _poll)
            try:
                results_new = await run.io_bound(_work)
            except Exception as ex:
                state_now["error"] = f"{ex.__class__.__name__}: {ex}"
                results_new = []
            timer.cancel()
            if results_new:
                review_db.save_history(results_new)
                for r in results_new:
                    review_db.update_track_state(r["review_id"], r["status"],
                                                 r["checked_at"])
            d.close()
            tail = (f",检测 {len(results_new)} 条" if results_new
                    else (f",检测中断:{state_now['error']}" if state_now["error"] else ""))
            ui.notify(summary + tail + " · 已纳入每日定时跟踪", type="positive")
            if on_done:
                on_done()

        with ui.row().classes("w-full justify-end gap-2 mt-1"):
            ui.button("取消", on_click=d.close).props("outline no-caps dense")
            confirm_btn = ui.button("导入", on_click=_confirm) \
                .props("unelevated no-caps dense color=primary")
    d.open()


# ---------- 弹窗:评价链接详情(检测历史右侧抽屉) ----------


def fill_review_drawer(body, drawer, review_id, on_refresh=None):
    """抽屉内容:这个评价链接的档案 + 按日期的跟踪历史。

    与检测页的详情弹窗同源,但补上业务字段(刷单编号/订单号/产品型号)、
    跟踪状态(跟踪中 / 已停止)与逐日轨迹,信息一次看全。
    """
    meta = review_db.get_meta(review_id) or {}
    domain = meta.get("domain") or ""
    url = meta.get("url") or ""
    timeline = review_db.review_history_timeline(review_id, limit=60)
    tracked = bool(meta.get("track_enabled", 1))

    body.clear()
    with body:
        with ui.row().classes("w-full items-center justify-between"):
            with ui.column().classes("gap-0"):
                html(f'<div class="card-title">评价链接详情</div>'
                     f'<div class="pg-meta">{review_id} · '
                     f'{DOMAIN_SHORT(domain) if domain else "未知站点"}</div>')
            ui.button(icon="close", on_click=drawer.hide).props("flat round dense")

        # 业务字段:表格来的有值,直接粘链接来的显示「—」
        with ui.row().classes("w-full gap-6 items-start"):
            for label, key in (("刷单编号", "order_ref"), ("订单号", "order_no"),
                               ("产品型号", "model")):
                v = str(meta.get(key) or "").strip()
                html(f'<div class="field-cell"><div class="field-k">{label}</div>'
                     f'<div class="field-v">{html_mod.escape(v) if v else "—"}</div></div>')

        if tracked:
            tag = ('<span class="pill bg-[#dcfce7] text-[#15803d]">跟踪中</span>')
        else:
            tag = ('<span class="pill bg-[#fee2e2] text-[#b91c1c]">已停止</span>')
        reason = meta.get("stop_reason") or ""
        html(f'<div class="pg-meta" style="display:flex;gap:8px;align-items:center">'
             f'{tag}<span>已跟踪 {meta.get("track_count", 0)} 次 · '
             f'连续已删 {meta.get("deleted_streak", 0)} 次'
             f'{" · " + html_mod.escape(reason) if reason else ""}'
             f' · 上次 {html_mod.escape((meta.get("last_tracked_at") or "未跟踪"))}'
             f'</span></div>')
        html('<div class="pg-meta">每日定时跟踪一次,全部链接分散在 24 小时内轮流检测;'
             '连续 5 次判定为已删(变狗)会自动停止跟踪。</div>')

        with ui.row().classes("items-center gap-2"):
            if url:
                ui.button("打开评价页面", on_click=lambda u=url: ui.open(u, new_tab=True)) \
                    .props("outline no-caps dense icon=open_in_new")
                ui.button("复制评价链接",
                          on_click=lambda u=url: (
                              ui.run_javascript(
                                  f"copyText({json.dumps(u)});showCopyToast('评价链接已复制')"))) \
                    .props("outline no-caps dense icon=content_copy")
            check_btn = ui.button("立即检测一次", icon="play_arrow") \
                .props("unelevated no-caps dense color=primary")

            async def _check_now():
                refs = parse_links(url)
                if not refs:
                    ui.notify("这条链接无法解析", type="negative")
                    return
                check_btn.props("disable loading")

                def _work():
                    checker = ReviewChecker()
                    try:
                        return checker.check_batch(refs)
                    finally:
                        checker.close()
                try:
                    res = await run.io_bound(_work)
                except Exception as ex:
                    ui.notify(f"检测失败:{ex.__class__.__name__}: {ex}",
                              type="negative")
                    check_btn.props(remove="disable loading")
                    return
                review_db.save_history(res)
                for r in res:
                    review_db.update_track_state(r["review_id"], r["status"],
                                                 r["checked_at"])
                check_btn.props(remove="disable loading")
                if res:
                    st = STATUS_LABEL.get(res[0]["status"], res[0]["status"])
                    ui.notify(f"检测完成:{st}", type="positive")
                fill_review_drawer(body, drawer, review_id, on_refresh)
                if on_refresh:
                    on_refresh()

            check_btn.on("click", _check_now)

            def _toggle_track():
                review_db.set_track_enabled(review_id, not tracked)
                ui.notify("已恢复跟踪" if not tracked else "已停止跟踪",
                          type="positive")
                fill_review_drawer(body, drawer, review_id, on_refresh)
                if on_refresh:
                    on_refresh()
            ui.button("恢复跟踪" if not tracked else "停止跟踪",
                      on_click=_toggle_track).props("outline no-caps dense")

        ui.separator()
        html(f'<div class="card-title" style="font-size:14px">'
             f'按日期的跟踪历史({len(timeline)} 次)</div>')
        if not timeline:
            ui.label("还没有检测记录。点「立即检测一次」,或等每日定时跟踪跑到它。") \
                .classes("pg-meta")
            return
        rows = []
        for t in timeline:
            rows.append({
                "checked": t["checked_at"],
                "status_text": status_text(t["status"]),
                "stars": stars_html(t["stars"]),
                "title": t["title"] or "—",
                "note": t["note"] or "—",
                "_status": t["status"],
            })
        ui.aggrid({
            "columnDefs": [
                {"headerName": "检测时间", "field": "checked", "width": 158},
                {"headerName": "状态", "field": "status_text", "width": 78},
                {"headerName": "星级", "field": "stars", "width": 64},
                {"headerName": "标题", "field": "title", "minWidth": 140, "flex": 2},
                {"headerName": "判定依据", "field": "note", "minWidth": 140, "flex": 2},
            ],
            "rowData": rows,
            "defaultColDef": {"sortable": True, "resizable": True},
            "rowHeight": 28,
        }, html_columns=[1, 2]).classes("w-full ag-dense ag-drawer-fill")


# ---------- 页面:历史 ----------


@ui.page("/history")
def page_history():
    # 右侧抽屉:点任一条记录 → 看这个评价链接的档案(刷单编号/订单号/产品型号)
    # 与按日期的跟踪历史。q-drawer 属顶层布局元素,必须建在页面函数体里。
    with ui.drawer("right", value=False, bordered=True) as rev_drawer:
        rev_drawer.props("width=880 breakpoint=9999")
        # h-full + min-height:0 → 抽屉内容撑满整高,内部「跟踪历史」表格靠 flex 拉伸到底部
        rev_body = ui.column().classes("w-full gap-2 p-4 h-full").style("min-height:0")

    with build_shell("/history"):
        days = {"kw": 7}
        # 切卡筛选:国家 + 状态(全部/正常/已删),数据在前端按已加载行过滤
        CC_ORDER = ["全部", "IN", "AU", "US", "JP", "MX", "BR"]
        CC_FLAGS = {"IN": "🇮🇳", "AU": "🇦🇺", "US": "🇺🇸", "JP": "🇯🇵",
                    "MX": "🇲🇽", "BR": "🇧🇷"}
        ST_CARDS = [("全部", "all"), ("正常", "alive"), ("已删", "deleted")]
        cur = {"cc": "全部", "st": "all"}
        rows_all = []  # 当前时间范围内的全部行(含原始字段)

        # 页头:左「标题+副行」上下两行,右「搜索框」(时间/统计按钮下移到切卡同一行,
        # 版式与监控页一致:搜索框在按钮区上方)
        with ui.row().classes("w-full items-center justify-between gap-3 mb-2"):
            with ui.column().classes("gap-0"):
                html('<div class="pg-title">评价链接检测历史</div>')
                meta = html('')
            # 搜索索引见 h_row 的 _hay:Review ID / 刷单编号 / 订单号 / 产品型号
            # (标题/作者/备注/链接也一并命中,只是提示词只放常用的四个)
            q = ui.input(placeholder="搜索 Review ID / 刷单编号 / 订单号 / 产品型号 …") \
                .props("outlined dense hide-bottom-space") \
                .classes("w-80").style("font-size:13px")

        # 切卡 + 时间范围/统计按钮同一行:左切卡,右按钮(与监控页国家卡+按钮同版式)
        with ui.row().classes("w-full items-center justify-between gap-3 mb-2 no-wrap"):
            cards_row = ui.row().classes("items-center gap-2 flex-grow")
            with ui.row().classes("items-center gap-2 flex-none"):
                # 导入测评订单表格:刷单编号/产品型号/订单号随评价链接一起进系统
                ui.button("导入表格", icon="upload_file",
                          on_click=lambda: import_table_dialog(load_rows)) \
                    .props("unelevated no-caps dense color=primary")
                # 时间范围:三个独立按钮,选中态 = 浅蓝底+蓝字(非实心,与空心按钮同族)
                # 四个按钮锁同宽,字数不齐也排整齐;80px 给左侧 10 张切卡腾位
                range_btns = {}
                for val, label in [(7, "近 7 天"), (30, "近 30 天"), (None, "全部")]:
                    def _pick(v=val):
                        days["kw"] = v
                        for vv, bb in range_btns.items():
                            if vv == v:
                                bb.classes(add="bg-[#eff6ff] text-[#2563eb]")
                            else:
                                bb.classes(remove="bg-[#eff6ff] text-[#2563eb]")
                        load_rows()
                    range_btns[val] = ui.button(label, on_click=_pick) \
                        .props("outline no-caps dense").classes("w-[80px]")
                range_btns[7].classes(add="bg-[#eff6ff] text-[#2563eb]")
                ui.button("统计", icon="bar_chart", on_click=lambda: stats_dialog(days["kw"])) \
                    .props("outline no-caps dense").classes("w-[80px]")

        def h_row(r):
            rid = r["review_id"]
            url = r["url"] or f"https://www.{r['domain']}/gp/customer-reviews/{rid}/"
            status = r["status"] or "pending"   # 台账里有、但还没检测过的链接
            tracked = bool(r.get("track_enabled", 1))
            return {
                "checked": r["checked_at"] or "",
                "review_id": rid,
                "order_ref": r.get("order_ref") or "—",
                "order_no": r.get("order_no") or "—",
                "model": r.get("model") or "—",
                "link": link_cell(url),
                "domain": DOMAIN_SHORT(r["domain"]),
                "status_text": status_text(status),
                "track": "跟踪中" if tracked else "已停止",
                "stars": stars_html(r["stars"]),
                "title": r["title"] or "—",
                "author": r["author"] or "—",
                "review_date": r["review_date"] or "—",
                "note": r["note"] or ("尚未检测,等每日定时跟踪跑到它"
                                      if not r["status"] else "—"),
                "_status": status,
                # 搜索索引:原始字段小写拼接(ID/URL/业务字段/标题/作者/备注)
                "_hay": " ".join(str(x or "").lower() for x in
                                 (rid, url, r.get("order_ref"), r.get("order_no"),
                                  r.get("model"), r["title"], r["author"], r["note"])),
            }

        def set_meta(n):
            days_txt = {7: "近 7 天", 30: "近 30 天", None: "全部时间"}[days["kw"]]
            filt = ""
            if cur["cc"] != "全部":
                filt += f" · {cur['cc']}"
            if cur["st"] != "all":
                filt += f" · {STATUS_LABEL.get(cur['st'], cur['st'])}"
            kw = (q.value or "").strip()
            if kw:
                filt += f" · 搜索“{kw}”"
            meta.set_content(
                f'<div class="pg-meta">{days_txt}{filt} · 显示 {n} 条'
                f'(范围共 {len(rows_all)} 条,最多 500) · 每次检测自动留存</div>')

        def build_cards():
            # 数量按当前时间范围统计;国家卡固定显示(0 条也显示,避免空范围下卡片全消失)
            cards_row.clear()
            with cards_row:
                cc_counts = {}
                st_counts = {}
                for r in rows_all:
                    cc_counts[r["domain"]] = cc_counts.get(r["domain"], 0) + 1
                    st_counts[r["_status"]] = st_counts.get(r["_status"], 0) + 1

                def seg(label, num, key, kind):
                    active = cur[kind] == key
                    bg = "#eff6ff" if active else "#fff"
                    bd = "#2563eb" if active else "#e2e8f0"
                    num_color = "#2563eb" if active else "#1e293b"
                    b = ui.button(on_click=lambda e: pick(kind, key)) \
                        .props("flat no-caps dense")
                    b.style(f"background:{bg};border:1px solid {bd};border-radius:6px;"
                            "padding:4px 9px;min-height:0;height:32px;cursor:pointer;"
                            "box-shadow:none;")
                    with b:
                        flag = CC_FLAGS.get(key, "")
                        html(f'<span class="kpi-num" style="color:{num_color}">'
                             f'{num}</span>'
                             f'<span class="kpi-tag">{label}{(" " + flag) if flag else ""}</span>')

                for cc in CC_ORDER:
                    seg(cc, len(rows_all) if cc == "全部" else cc_counts.get(cc, 0),
                        cc, "cc")
                ui.separator().props("vertical").classes("self-stretch bg-[#e2e8f0]")
                for lab, key in ST_CARDS:
                    seg(lab, len(rows_all) if key == "all" else st_counts.get(key, 0),
                        key, "st")

        def pick(kind, key):
            cur[kind] = key
            apply_filter()

        def apply_filter():
            kw = (q.value or "").strip().lower()
            shown = [r for r in rows_all
                     if (cur["cc"] == "全部" or r["domain"] == cur["cc"])
                     and (cur["st"] == "all" or r["_status"] == cur["st"])
                     and (not kw or kw in r["_hay"])]
            grid.options["rowData"] = shown
            grid.update()
            build_cards()
            set_meta(len(shown))

        # 列序:左固定(检测时间/Review ID/刷单编号/订单号) → 可滚动主区 →
        # 右固定(国家/跟踪)。agGrid 要求固定列在 columnDefs 里连续且分居两端,
        # 所以业务字段必须紧跟 Review ID、国家/跟踪必须排到最后。
        # auto_size_columns=False 是关键:nicegui 默认会调 sizeColumnsToFit(),
        # 把非固定列按剩余宽度硬压(实测被压到 36px、表头全是省略号),
        # 关掉后各列保持声明宽度、放不下就横向滚动,固定列始终完整可见。
        grid = ui.aggrid({
            "columnDefs": [
                {"headerName": "检测时间", "field": "checked", "width": 168,
                 "pinned": "left", "suppressSizeToFit": True},
                {"headerName": "Review ID", "field": "review_id", "width": 136,
                 "pinned": "left", "tooltipField": "review_id",
                 "cellClass": "rid-copy", "suppressSizeToFit": True},
                {"headerName": "刷单编号", "field": "order_ref", "width": 90,
                 "pinned": "left", "suppressSizeToFit": True},
                {"headerName": "订单号", "field": "order_no", "width": 186,
                 "pinned": "left", "suppressSizeToFit": True},
                {"headerName": "产品型号", "field": "model", "width": 180},
                {"headerName": "链接", "field": "link", "width": 68, "sortable": False},
                {"headerName": "状态", "field": "status_text", "width": 84},
                {"headerName": "星级", "field": "stars", "width": 66},
                {"headerName": "标题", "field": "title", "minWidth": 160, "flex": 3},
                {"headerName": "作者", "field": "author", "width": 90},
                {"headerName": "评价日期", "field": "review_date", "width": 130},
                {"headerName": "判定依据", "field": "note", "minWidth": 160, "flex": 2},
                {"headerName": "国家", "field": "domain", "width": 70,
                 "pinned": "right", "suppressSizeToFit": True},
                {"headerName": "跟踪", "field": "track", "width": 80,
                 "pinned": "right", "suppressSizeToFit": True},
            ],
            "rowData": [],
            "defaultColDef": {"sortable": True, "resizable": True},
            "rowHeight": 30,
        }, html_columns=[5, 6, 7], auto_size_columns=False) \
            .classes("w-full ag-dense ag-fill")

        def on_cell_click(e):
            data = e.args["data"]
            if e.args.get("colId") == "review_id":
                # 点 ID 格 = 复制完整 ID(显示截断不影响),不开抽屉
                ui.run_javascript(
                    f"copyText({json.dumps(data['review_id'])});"
                    f"showCopyToast('ID 已复制')")
                return
            # 其余任意格 → 右侧抽屉:链接档案 + 按日期的跟踪历史
            fill_review_drawer(rev_body, rev_drawer, data["review_id"],
                               on_refresh=load_rows)
            rev_drawer.show()

        grid.on("cellClicked", on_cell_click)

        def load_rows():
            # 换时间范围时重置切卡筛选,重新统计卡片数量;搜索词保留继续生效
            cur["cc"] = "全部"
            cur["st"] = "all"
            rows_all.clear()
            rows_all.extend(h_row(r) for r in recent_history(500, days["kw"]))
            apply_filter()

        # 搜索回车触发(与监控页一致)
        q.on("keydown", lambda e: apply_filter() if e.args.get("key") == "Enter" else None)

        load_rows()


def stats_dialog(days):
    stats = history_stats(days)
    total = sum(c for _, c in stats) or 1
    with ui.dialog() as d, ui.card().classes("app-card w-[420px]"):
        html('<div class="card-title">历史状态统计</div>')
        for s, c in sorted(stats, key=lambda kv: -kv[1]):
            label, cls = STATUS_META.get(s, (s, ""))
            pct = c / total
            with ui.row().classes("w-full items-center gap-2"):
                html(f'<span class="pill {cls}">{label}</span>')
                ui.linear_progress(value=pct, show_value=False).classes("flex-grow h-1")
                ui.label(f"{c} · {pct:.0%}").classes("text-[12px] text-[#64748b]")
    d.open()


# ---------- 页面:监控 ----------

# 监控字段契约:主表矩阵列 + 抽屉切卡共用一份
# (key, 列名, 取值, 格式化, 值类型)。key 同时用作 agGrid field 前缀与抽屉 tab name。
# 值类型:"num" = 数字(抽屉里变化内容列省掉,上下两行直接看);"text" = 文案(词级 diff)。
MON_FIELDS = [
    ("title", "标题", lambda s: s.get("title"), lambda v: v or "—", "text"),
    ("price", "价格", lambda s: s.get("price"), lambda v: v or "—", "num"),
    ("rating", "评分", lambda s: s.get("rating"),
     lambda v: "—" if v is None else f"{v:g}", "num"),
    ("review_count", "评价数", lambda s: s.get("review_count"),
     lambda v: "—" if v is None else f"{v:,}", "num"),
    ("bsr", "BSR", lambda s: s.get("bsr"),
     lambda v: "—" if v is None else f"{v:,}", "num"),
    ("buybox", "BuyBox", lambda s: s.get("buybox"), lambda v: v or "无", "text"),
    ("availability", "上下架", lambda s: s.get("availability"), lambda v: v or "—", "text"),
    ("status", "页面状态", lambda s: s.get("status"), lambda v: v or "—", "text"),
    ("deal_tag", "Deal", lambda s: s.get("deal_tag"), lambda v: v or "无", "text"),
    ("recent_bad", "差评数", lambda s: (s.get("home_reviews") or {}).get("recent_bad"),
     lambda v: "—" if v is None else str(v), "num"),
    ("bullets", "BP 五点", lambda s: s.get("bullets") or [],
     lambda v: ("\n".join(v) if isinstance(v, list) and v else
                (v if isinstance(v, str) and v else "—")), "text"),
    ("description", "DP 描述", lambda s: s.get("description"),
     lambda v: v or "—", "text"),
]
MON_FIELD_LABELS = {k: lab for k, lab, _, _, _ in MON_FIELDS}
MON_FIELD_KIND = {k: kind for k, _, _, _, kind in MON_FIELDS}

# 抽屉专属字段(不进主表矩阵):BSR 大类/小类名
MON_EXTRA_FIELDS = {
    "bsr_cat": ("bsr_cat", "大类", lambda s: s.get("bsr_cat"),
                lambda v: v or "—", "text"),
    "bsr_sub": ("bsr_sub", "小类", lambda s: s.get("bsr_sub"),
                lambda v: v or "—", "text"),
    "model_number": ("model_number", "型号", lambda s: s.get("model_number"),
                     lambda v: v or "—", "text"),
}

# 抽屉切卡分组:同类字段合并一张卡,明细表里一字段一列并排看
MON_GROUPS = [
    ("title", "标题", ["title"]),
    ("price", "价格", ["price", "buybox", "deal_tag"]),
    ("reviews", "评价", ["rating", "review_count", "recent_bad"]),
    ("bsr", "BSR", ["bsr", "bsr_cat", "bsr_sub"]),
    ("status", "状态", ["availability", "status", "model_number"]),
    ("copy", "文案", ["bullets", "description"]),
]
MON_FIELD_GROUP = {fk: gk for gk, _, fks in MON_GROUPS for fk in fks}
MON_BY_KEY = {**MON_EXTRA_FIELDS,
              **{k: m for m in MON_FIELDS for k in [m[0]]}}

# 变化矩阵单元格:有更新=橙,无变化=灰,基准(仅一次快照)=浅灰
_CHG_YES = '<span style="color:#b45309;font-weight:700">有更新</span>'
_CHG_NO = '<span style="color:#94a3b8">无变化</span>'
_CHG_BASE = '<span style="color:#cbd5e1">基准</span>'


def _field_changed(getter, fmt, snap, prev) -> bool:
    """该字段本次快照较上一条是否变化。展示串相同不算变(避开 None/"" 等价)。"""
    if prev is None:
        return False
    v, pv = fmt(getter(snap)), fmt(getter(prev))
    return v != pv


def _matrix_row(db_path, asin, domain, snap, anom_by_key) -> dict:
    """一行 = 一个 ASIN:ASIN/最后更新/每字段是否变化/异常徽标。

    变化判定取最近两拍快照(本次采集 vs 上次采集)。
    """
    from monitor.board import _status_label
    snaps = monitor_store.snapshots_for(db_path, asin, domain, limit=2)
    prev = snaps[-2] if len(snaps) >= 2 else None
    a = anom_by_key.get((asin, domain))
    sev = (a or {}).get("severity", "ok")

    img = snap.get("image_url") or ""
    thumb = (f'<img src="{html_mod.escape(img)}" class="asin-thumb" alt="">'
             if img.startswith("http") else
             '<span class="asin-thumb asin-thumb-none"></span>')
    row = {
        "asin_html": (f'<span class="asin-cell">{thumb}'
                      f'<b style="font-family:ui-monospace,monospace">{asin}</b></span>'),
        "ts": (snap.get("checked_at") or "")[5:16] or "—",
        "title": (snap.get("title") or asin)[:40],
        "_asin": asin, "_domain": domain,
        "_title": snap.get("title") or asin,
        "_url": (monitor_store.get_profile(db_path, asin, domain) or {}).get("url", ""),
        "_sev": {"critical": 0, "warning": 1, "info": 2}.get(sev, 9),
    }
    ts_num = 0
    try:
        ts_num = datetime.strptime(str(snap.get("checked_at") or "").split(".")[0],
                                   "%Y-%m-%d %H:%M:%S").timestamp()
    except Exception:
        pass
    row["_ts"] = ts_num
    for key, _lab, getter, fmt, _kind in MON_FIELDS:
        if prev is None:
            row[f"chg_{key}"] = _CHG_BASE
        else:
            g = (lambda s: _status_label(s.get("status"))) if key == "status" else getter
            f2 = (lambda v: v or "—") if key == "status" else fmt
            row[f"chg_{key}"] = _CHG_YES if _field_changed(g, f2, snap, prev) else _CHG_NO
    if a:
        from monitor.rules import METRIC_LABELS
        label = METRIC_LABELS.get(a["metric"], a["metric"])
        icon = {"critical": "🚨", "warning": "⚠"}.get(sev, "ℹ")
        cls = {"critical": "bg-[#fee2e2] text-[#b91c1c]",
               "warning": "bg-[#fef3c7] text-[#b45309]",
               "info": "bg-[#dbeafe] text-[#1d4ed8]"}[sev]
        row["anomaly_html"] = f'<span class="pill {cls}">{icon} {label}</span>'
    else:
        row["anomaly_html"] = '<span style="color:#15803d;font-weight:600">正常</span>'
    return row


def _word_diff(pv: str, v: str) -> str:
    """文案类变化内容:只展示变了的片段。

    替换 = 旧 → 新;新增 = +词;删除 = -词。不重复全文。
    """
    import difflib
    a, b = str(pv).split(), str(v).split()
    sm = difflib.SequenceMatcher(None, a, b)
    parts = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        old = " ".join(a[i1:i2])
        new = " ".join(b[j1:j2])
        if tag == "insert":
            parts.append(f"+{new}")
        elif tag == "delete":
            parts.append(f"-{old}")
        else:
            parts.append(f"{old} → {new}")
    return "；".join(parts) if parts else "—"


def _highlight_diff(pv: str, v: str) -> str:
    """全文高亮:与上一条对比,把变了的词段标橙底。

    按"词+空白"分词,换行符原样保留(BP 每点一行不受影响)。
    """
    import difflib
    import re as _re
    tok = lambda s: _re.split(r"(\s+)", str(s))
    a, b = tok(pv), tok(v)
    sm = difflib.SequenceMatcher(None, a, b)
    out = []
    for tag, _i1, _i2, j1, j2 in sm.get_opcodes():
        seg = "".join(b[j1:j2])
        esc = html_mod.escape(seg)
        out.append(esc if tag == "equal"
                   else f'<mark class="mon-diff">{esc}</mark>')
    return "".join(out)


def _detail_rows(snaps, fields) -> list[dict]:
    """一组字段的历史明细(最新在上):时间 + 每字段一列当时值 + 是否变化 + 变化内容。

    单字段组:更新时间 | 值 | 是否有变化 | 变化内容(同旧版)。
    多字段组(评价/状态/报价/文案):更新时间 | 字段A | 字段B | … | 是否有变化 | 变化内容。
    任一字段变即算该行有变化;变化内容按字段前缀列出各字段的变化。
    数字类字段变化内容留空(上下行"当时值"直接对比);文案类走词级 diff。
    """
    ordered = list(reversed(snaps))
    rows = []
    for i, s in enumerate(ordered):
        prev = ordered[i + 1] if i + 1 < len(ordered) else None
        row = {"time": (s.get("checked_at") or "")[5:16]}
        any_chg = False
        diffs = []
        for key, lab, getter, fmt, kind in fields:
            v = fmt(getter(s))
            row[f"v_{key}"] = v
            if prev is None:
                continue
            if _field_changed(getter, fmt, s, prev):
                any_chg = True
                if kind == "text":
                    d = _word_diff(fmt(getter(prev)), v)
                    if d != "—":
                        seg = d if len(fields) == 1 else f"{lab}:{d}"
                        diffs.append(seg)
        if prev is None:
            row["chg"] = _CHG_BASE
            row["diff"] = '<span style="color:#cbd5e1">—</span>'
        elif any_chg:
            row["chg"] = _CHG_YES
            body = "；".join(diffs)
            row["diff"] = (f'<span style="color:#b45309;font-weight:600">{body}</span>'
                           if body else '<span style="color:#cbd5e1">—</span>')
        else:
            row["chg"] = _CHG_NO
            row["diff"] = '<span style="color:#cbd5e1">—</span>'
        rows.append(row)
    return rows


@ui.page("/monitor")
def page_monitor():
    # 字段抽屉:q-drawer 属顶层布局元素,必须建在页面函数体(直接挂 client
    # 的 q-layout,不随页面内容嵌套),内容在点击时重建。
    with ui.drawer("right", value=False, bordered=True) as field_drawer:
        # 宽度走 q-drawer 的 width prop(内联 style 会被组件自己的 style 覆盖);
        # breakpoint 设超大值 → 任何屏宽下抽屉都是浮层(带遮罩),
        # 不挤压主表,点遮罩即关
        field_drawer.props("width=980 breakpoint=9999")
        drawer_body = ui.column().classes("w-full gap-2 p-4")

    def open_field_drawer(asin, domain, field_key="title"):
        """点 ASIN 或任意矩阵格 → 右侧抽屉:纵向切卡(每字段一张)+ 明细表。"""
        from monitor.baseline import current_baseline
        snaps = monitor_store.snapshots_for(MONITOR_DB, asin, domain)
        if not snaps:
            ui.notify("暂无快照")
            return
        p = monitor_store.get_profile(MONITOR_DB, asin, domain) or {}
        title = p.get("title") or asin
        base = current_baseline(MONITOR_DB, asin, domain) or snaps[0]
        cur = snaps[-1]
        from monitor.board import _diff_line, _status_label, _has_unconfirmed
        drawer_body.clear()
        with drawer_body:
            with ui.row().classes("w-full items-center justify-between"):
                with ui.column().classes("gap-0"):
                    html(f'<div class="card-title">{title}</div>'
                         f'<div class="pg-meta">{asin} · {DOMAIN_SHORT(domain)} · '
                         f'共 {len(snaps)} 次检查</div>')
                ui.button(icon="close", on_click=field_drawer.hide) \
                    .props("flat round dense")
            html(f'<div class="pg-meta">基线 {(base.get("checked_at") or "")[5:16]} → '
                 f'{(cur.get("checked_at") or "")[5:16]} · {_diff_line(base, cur)}</div>')
            ui.separator()
            # 操作区:原页面 / 确认基线 / 删除监控(放在切卡上方,不用滚到底)
            with ui.row().classes("w-full items-center justify-between"):
                if p.get("url"):
                    ui.button("打开原页面",
                              on_click=lambda u=p["url"]: ui.open(u, new_tab=True)) \
                        .props("outline no-caps dense icon=open_in_new")
                with ui.row().classes("items-center gap-2"):
                    if _has_unconfirmed(MONITOR_DB, asin, domain):
                        def _confirm():
                            from monitor.pipeline import confirm_and_move_baseline
                            confirm_and_move_baseline(MONITOR_DB, asin, domain)
                            for an in monitor_store.unconfirmed_anomalies(
                                    MONITOR_DB, limit=500):
                                if an["asin"] == asin and an["domain"] == domain:
                                    monitor_store.confirm_anomaly(MONITOR_DB, an["id"])
                            field_drawer.hide()
                            ui.notify("已确认,基线前移", type="positive")
                            ui.navigate.reload()
                        ui.button("确认无误 → 前移基线", on_click=_confirm) \
                            .props("unelevated no-caps dense color=primary icon=verified")
                    else:
                        ui.label("当前无未确认异常").classes("pg-meta")

                    def _delete():
                        monitor_store.delete_profile(MONITOR_DB, asin, domain)
                        field_drawer.hide()
                        ui.notify(f"已删除监控 {asin}", type="positive")
                        ui.navigate.reload()
                    armed = {"ok": False}
                    del_btn = ui.button("删除监控", icon="delete",
                                        on_click=lambda: _del_click()) \
                        .props("outline no-caps dense color=negative")

                    def _del_click():
                        if not armed["ok"]:
                            armed["ok"] = True
                            del_btn.set_text("再点一次确认删除")
                            del_btn.props("unelevated")
                        else:
                            _delete()
            # 变体族:种子 + 自动登记的子体,勾选哪些参与采集(默认不勾)
            seed = monitor_store.family_seed_of(MONITOR_DB, asin, domain)
            fam = monitor_store.family_members(MONITOR_DB, seed, domain)
            if len(fam) > 1:
                html(f'<div class="card-title" style="font-size:14px;margin-top:6px">'
                     f'变体族 · 共 {len(fam)} 个</div>')
                html('<div class="pg-meta">采集时自动发现的子体默认不采集;'
                     '勾选后才进监控和告警。取消勾选即停采集(历史数据保留)。</div>')
                with ui.column().classes("w-full gap-0"):
                    for m in fam:
                        is_seed = not (m.get("seed_asin") or "")
                        with ui.row().classes("w-full items-center gap-2"):
                            sw = ui.switch(value=bool(m["monitor_enabled"])) \
                                .props("dense")
                            def _toggle(e, mem=m, s=sw):
                                monitor_store.set_profile_enabled(
                                    MONITOR_DB, mem["asin"], mem["domain"],
                                    1 if s.value else 0)
                                ui.notify(
                                    f"{'启用' if s.value else '停用'} {mem['asin']}",
                                    type="positive")
                            sw.on_value_change(_toggle)
                            label = m["asin"] + ("（主商品）" if is_seed else "")
                            html(f'<span style="font-family:ui-monospace,monospace;'
                                 f'font-size:12px">{label}</span>'
                                 + (f'<span class="pg-meta"> {m["title"][:28]}</span>'
                                    if m.get("title") else ""))
            # 本链接专属 AI 解读规范(优先于国家/全局);空=不单独定制
            with ui.row().classes("w-full items-end gap-2 mt-1"):
                ap = ui.input("本链接 AI 解读规范(留空=用国家/全局)",
                              value=p.get("ai_prompt") or "") \
                    .props("outlined dense").classes("flex-grow")
                def _save_ap():
                    monitor_store.set_profile_ai_prompt(
                        MONITOR_DB, asin, domain, ap.value or "")
                    ui.notify("已保存该链接的解读规范", type="positive")
                ui.button("保存", on_click=_save_ap) \
                    .props("outline no-caps dense")
            # 纵向切卡:同类字段合并一张卡,明细表一字段一列并排
            def _grp_fields(fkeys):
                out = []
                for fk in fkeys:
                    k, lab, getter, fmt, kind = MON_BY_KEY[fk]
                    if k == "status":  # 原始状态码 → 中文标签
                        getter = lambda s: _status_label(s.get("status"))
                        fmt = lambda v: v or "—"
                    out.append((k, lab, getter, fmt, kind))
                return out

            with ui.row().classes("w-full items-stretch gap-3 no-wrap"):
                with ui.tabs().props("vertical") as tabs:
                    for gk, glab, _ in MON_GROUPS:
                        ui.tab(gk, label=glab)
                init_group = MON_FIELD_GROUP.get(field_key, "title")
                with ui.tab_panels(tabs, value=init_group).classes("flex-grow min-w-0"):
                    for gk, glab, fkeys in MON_GROUPS:
                        with ui.tab_panel(gk):
                            fields = _grp_fields(fkeys)
                            val_cols = [{"headerName": lab, "field": f"v_{k}",
                                         "minWidth": 110, "flex": 2}
                                        for k, lab, _, _, _ in fields]
                            detail = _detail_rows(snaps, fields)
                            ui.aggrid({
                                "columnDefs": [
                                    {"headerName": "更新时间", "field": "time",
                                     "width": 110},
                                    *val_cols,
                                    {"headerName": "是否有变化", "field": "chg",
                                     "width": 96},
                                    {"headerName": "变化内容", "field": "diff",
                                     "minWidth": 170, "flex": 2},
                                ],
                                "rowData": detail,
                                "rowHeight": 30,
                                "defaultColDef": {"sortable": True, "resizable": True},
                            }, html_columns=[1 + len(fields), 2 + len(fields)],
                                auto_size_columns=False) \
                                .classes("w-full ag-dense") \
                                .style(f"height:{52 + 30 * min(max(len(detail), 10), 14)}px")
                            # 表头34 + 每行30:默认至少露 10 行空间,快照多时到 14 行为止
                            # 长文案字段:明细表下方空白区放最新全文,逐字段列出;
                            # 与上一条快照对比,变了的词段标橙底
                            prev_snap = snaps[-2] if len(snaps) >= 2 else None
                            for k, lab, getter, fmt, kind in fields:
                                if kind != "text":
                                    continue
                                val = str(fmt(getter(cur)))
                                if len(val) <= 40:
                                    continue  # 短值(上下架/BuyBox…)表里已完整
                                if prev_snap is not None:
                                    pv = str(fmt(getter(prev_snap)))
                                    body = (_highlight_diff(pv, val)
                                            if pv != val else
                                            html_mod.escape(val).replace(chr(10), "<br>"))
                                    tag = ("" if pv == val else
                                           ' <span style="font-weight:400;'
                                           f'color:#b45309">(橙底 = 较 '
                                           f'{(prev_snap.get("checked_at") or "")[5:16]}'
                                           ' 变化)</span>')
                                else:
                                    body = html_mod.escape(val).replace(chr(10), "<br>")
                                    tag = ""
                                html(f'<div class="mon-txt-label">{lab} · 最新全文 '
                                     f'{(cur.get("checked_at") or "")[5:16]}{tag}</div>'
                                     f'<div class="mon-txt-body">{body}</div>')
        field_drawer.set_value(True)

    with build_shell("/monitor"):

        def refresh():
            ui.navigate.to("/monitor")

        from monitor import board as monitor_board

        # 有数据 / 无数据共用同一套页头版式(以前空态另起一套,位置跳来跳去)
        has_data = (MONITOR_DB.exists()
                    and monitor_store.count_snapshots(MONITOR_DB) > 0)

        # 页头:左「标题+副行摘要」,右搜索框(操作按钮下移到国家切卡同一行)
        with ui.row().classes("w-full items-center justify-between gap-3 mb-2"):
            with ui.column().classes("gap-0"):
                html('<div class="pg-title">链接监控</div>')
                summary = html('')
            with ui.row().classes("items-center gap-2"):
                if has_data:
                    q = ui.input(placeholder="搜索标题 / ASIN / URL …") \
                        .props("outlined dense hide-bottom-space") \
                        .classes("w-64").style("font-size:13px")

        # 采集进度条:平时隐藏,「跑一轮采集」时出现,完成后停留显示结果
        prog_row = ui.row().classes("w-full items-center gap-3 mb-1")
        with prog_row:
            prog = ui.linear_progress(value=0, show_value=False).classes("flex-grow")
            prog_text = ui.label("").classes("pg-meta")
        prog_row.set_visibility(False)

        # 国家切卡 + 操作按钮同一行:左侧国家卡,右侧三个按钮(右缘与搜索框对齐)
        cur = {"cc": "全部"}
        with ui.row().classes("w-full items-center justify-between gap-3 mb-2 no-wrap"):
            card_holder = ui.row().classes("items-center gap-2 flex-grow")
            with ui.row().classes("items-center gap-2 flex-none"):
                btn_run = ui.button(
                    "跑一轮采集",
                    on_click=lambda: run_monitor_round(
                        refresh, btn_run, prog, prog_text, prog_row)) \
                    .props("outline no-caps dense").classes("w-[116px]")
                ui.button("添加监控", icon="add_link",
                          on_click=lambda: add_monitor_dialog(refresh)) \
                    .props("unelevated no-caps dense color=primary") \
                    .classes("w-[116px]")
                ui.button("定时与通知", icon="schedule",
                          on_click=schedule_dialog) \
                    .props("outline no-caps dense").classes("w-[116px]")

        if not has_data:
            # 空态只换正文区,页头/按钮位不动;给真实入口
            summary.set_content('<div class="pg-meta">盯住产品页变化:价格 / 评分 / '
                                '评价数 / 上下架</div>')
            html('<div class="pg-meta" style="margin:8px 0 12px">还没有监控数据:'
                 '先「添加监控」粘贴商品链接,再「跑一轮采集」生成看板;'
                 '或先打开演示数据看效果。</div>')
            with ui.row().classes("items-center gap-2"):
                ui.button("打开演示数据", icon="science", on_click=toggle_mock) \
                    .props("outline no-caps dense").classes("w-[116px]")
            return

        # 国家切卡:全部 + IN/AU/US/JP/MX/BR,点某国只看该国,再点恢复全部
        grid_holder = ui.column().classes("w-full flex-grow min-h-0")
        # 表格下方文案区:点行后展示该 ASIN 的标题 / BP / DP 全文
        text_holder = ui.column().classes("w-full")

        def show_text_panel(asin, domain):
            snap = monitor_store.latest_snapshot(MONITOR_DB, asin, domain) or {}
            text_holder.clear()
            with text_holder:
                with ui.card().classes("app-card w-full mt-2"):
                    with ui.row().classes("w-full items-center justify-between"):
                        html(f'<div class="card-title">文案 · {asin}'
                             f' <span class="pg-meta">'
                             f'{(snap.get("checked_at") or "")[5:16]}</span></div>')
                        ui.button(icon="close",
                                  on_click=text_holder.clear) \
                            .props("flat round dense")
                    html(f'<div class="mon-txt-block"><div class="mon-txt-label">'
                         f'标题</div><div class="mon-txt-body">'
                         f'{html_mod.escape(snap.get("title") or "—")}</div></div>')
                    bl = snap.get("bullets") or []
                    lis = "".join(f"<li>{html_mod.escape(b)}</li>" for b in bl) \
                        or '<li style="color:#94a3b8">—</li>'
                    html(f'<div class="mon-txt-block"><div class="mon-txt-label">'
                         f'BP 五点</div><ul class="mon-txt-body">{lis}</ul></div>')
                    html(f'<div class="mon-txt-block"><div class="mon-txt-label">'
                         f'DP 描述</div><div class="mon-txt-body">'
                         f'{html_mod.escape(snap.get("description") or "—")}</div></div>')
        CC_ORDER = ["全部", "IN", "AU", "US", "JP", "MX", "BR"]
        CC_FLAGS = {"IN": "🇮🇳", "AU": "🇦🇺", "US": "🇺🇸", "JP": "🇯🇵",
                    "MX": "🇲🇽", "BR": "🇧🇷"}

        def pick(cc):
            # 点已选中的卡恢复全部;换卡直接切换
            cur["cc"] = "全部" if cur["cc"] == cc else cc
            rebuild()

        def rebuild():
            # 每次都重取:跑完一轮采集 / 添加链接 / 确认基线后,卡片与表格都是最新
            data_now = monitor_board.get_board_data(MONITOR_DB)
            counts = {}
            for domain in (k[1] for k in data_now["latest"]):
                cc = DOMAIN_SHORT(domain)
                counts[cc] = counts.get(cc, 0) + 1
            card_holder.clear()
            with card_holder:
                order = CC_ORDER + [c for c in sorted(counts) if c not in CC_ORDER]
                for cc in order:
                    if cc != "全部" and counts.get(cc, 0) == 0:
                        continue  # 没有链接的国家不显示卡片
                    active = cur["cc"] == cc
                    bg = "#eff6ff" if active else "#fff"
                    bd = "#2563eb" if active else "#e2e8f0"
                    num_color = "#2563eb" if active else "#1e293b"
                    num = data_now["total"] if cc == "全部" else counts.get(cc, 0)
                    b = ui.button(on_click=lambda e, k=cc: pick(k)) \
                        .props("flat no-caps dense")
                    b.style(f"background:{bg};border:1px solid {bd};border-radius:6px;"
                            "padding:4px 9px;min-height:0;height:32px;cursor:pointer;"
                            "box-shadow:none;")
                    with b:
                        flag = CC_FLAGS.get(cc, "")
                        html(f'<span class="kpi-num" style="color:{num_color}">'
                             f'{num}</span>'
                             f'<span class="kpi-tag">{cc}{(" " + flag) if flag else ""}</span>')

            anom_by_key = {(a["anomaly"]["asin"], a["anomaly"]["domain"]): a["anomaly"]
                           for a in data_now["anomalies"]}
            rows = []
            for (asin, domain), snap in data_now["latest"].items():
                if cur["cc"] != "全部" and DOMAIN_SHORT(domain) != cur["cc"]:
                    continue
                row = _matrix_row(MONITOR_DB, asin, domain, snap, anom_by_key)
                hay = (row["_asin"] + " " + row["_title"] + " "
                       + (row["_url"] or "")).lower()
                if q.value and q.value.lower() not in hay:
                    continue
                rows.append(row)
            rows.sort(key=lambda r: (r["_sev"], -r["_ts"]))
            summary.set_content(
                f'<div class="pg-meta">监控 {data_now["total"]} 条 · 异常 '
                f'{data_now["abnormal_count"]} 条 · 当前显示 {len(rows)} 条 · '
                f'点 ASIN 或任意格看字段明细,点「产品」看文案全文</div>')
            grid_holder.clear()
            with grid_holder:
                make_grid(rows)
            text_holder.clear()   # 换筛选/重采后旧的文案区不再对应,清掉

        def make_grid(rows):
            # 变化矩阵:ASIN | 最后更新 | 异常 | 每字段(有更新/无变化) | 产品
            # 全部行平铺直接显示;排序保持异常在前(rebuild 已按 _sev/_ts 排好)
            col_defs = [
                # ASIN 不 pinned:agGrid 只在主区触发 cellClicked,
                # 钉住的格子点了不会开抽屉
                # 宽度留足:ASIN 10-11 位等宽粗体、时间 "MM-DD HH:MM" 12 字符,
                # 都要容得下完整内容 + 单元格左右内边距,不能被省略号截断
                {"headerName": "ASIN", "field": "asin_html", "width": 170},
                {"headerName": "最后更新", "field": "ts", "width": 132},
                {"headerName": "异常", "field": "anomaly_html", "width": 118},
            ] + [
                {"headerName": lab, "field": f"chg_{key}", "width": 74,
                 "sortable": False, "cellClass": "mon-mtx",
                 "headerClass": "mon-mtx"}
                for key, lab, _, _, _ in MON_FIELDS
            ] + [
                {"headerName": "产品", "field": "title", "minWidth": 160, "flex": 1},
            ]
            html_cols = [0, 2] + [3 + i for i in range(len(MON_FIELDS))]
            # 高度随行数走:表头 34 + 每行 30,不再用 ag-fill 撑满屏高,
            # 否则行少时表格下方一大片空白,文案区被推到屏幕外
            g = ui.aggrid({
                "columnDefs": col_defs,
                "rowData": rows,
                "defaultColDef": {"sortable": True, "resizable": True},
                "rowHeight": 30,
            }, html_columns=html_cols, auto_size_columns=False) \
                .classes("w-full ag-dense ag-fill")

            def _click(e):
                data = e.args.get("data") or {}
                if not data.get("_asin"):
                    return
                col = e.args.get("colId") or ""
                if col == "title":
                    # 点「产品」文字列 → 表格下方文案区看标题/BP/DP 全文
                    show_text_panel(data["_asin"], data["_domain"])
                    return
                # 点中哪个字段列,抽屉就默认打开哪个切卡
                key = col.replace("chg_", "")
                if key not in MON_FIELD_LABELS:
                    key = "title"
                open_field_drawer(data["_asin"], data["_domain"], key)
            g.on("cellClicked", _click)

        q.on("keydown", lambda e: rebuild() if e.args.get("key") == "Enter" else None)
        rebuild()


def schedule_dialog():
    """定时采集 + 钉钉通知设置:配置存 settings 表,后台线程实时读取生效。"""
    from monitor import notify as mn
    sget, sset = monitor_store.get_setting, monitor_store.set_setting
    cfg = mn.get_config(MONITOR_DB)
    with ui.dialog() as d, ui.card().classes("app-card w-[520px]"):
        with ui.row().classes("w-full items-center justify-between"):
            html('<div class="card-title">定时采集与通知</div>')
            ui.button(icon="close", on_click=d.close).props("flat round dense")

        # 定时采集
        enabled = ui.switch("开启定时采集",
                            value=sget(MONITOR_DB, "schedule_enabled", "0") == "1")
        with ui.row().classes("w-full items-center gap-2"):
            ui.label("采集间隔(小时)").classes("pg-meta")
            interval = ui.number(value=float(sget(MONITOR_DB,
                                                  "schedule_interval_h", "6") or 6),
                                 min=0.5, max=168, step=0.5, format="%.1f") \
                .props("outlined dense").classes("w-32")
        last_run = sget(MONITOR_DB, "schedule_last_run", "")
        last_stat = sget(MONITOR_DB, "schedule_last_stat", "")
        html(f'<div class="pg-meta">上次:{last_run or "未跑过"}'
             f'{(" · " + last_stat) if last_stat else ""}'
             f'<br>仅采集真实链接(演示数据自动跳过),按站点并行无头浏览器。</div>')

        ui.separator()
        # 评价链接每日跟踪:全部链接分散在 24 小时内轮流检测(防风控)
        html('<div class="card-title" style="font-size:14px">评价链接每日跟踪</div>')
        rt_enabled = ui.switch(
            "开启评价链接每日跟踪",
            value=sget(MONITOR_DB, "review_track_enabled", "1") == "1")
        with ui.row().classes("w-full items-center gap-2"):
            ui.label("每轮最多检测(条)").classes("pg-meta")
            rt_batch = ui.number(
                value=float(sget(MONITOR_DB, "review_track_batch", "3") or 3),
                min=1, max=20, step=1, format="%.0f") \
                .props("outlined dense").classes("w-28")
            ui.label(f"当前跟踪 {len(review_db.list_tracked())} 条 · "
                     f"已停止 {len(review_db.list_stopped())} 条").classes("pg-meta")
        rt_last = sget(MONITOR_DB, "review_track_last_run", "")
        rt_stat = sget(MONITOR_DB, "review_track_last_stat", "")
        html(f'<div class="pg-meta">上次:{rt_last or "未跑过"}'
             f'{(" · " + rt_stat) if rt_stat else ""}<br>'
             f'每天一次,全部链接按哈希均匀铺到 24 小时里轮流检测(彼此错开,'
             f'避免同一时刻集中请求);每轮小批量;连续 5 次判定为已删(变狗)'
             f'的链接自动停止跟踪。</div>')

        async def _run_reviews():
            d.close()
            ui.notify("评价链接跟踪进行中,完成后自动刷新…", type="info")
            try:
                r = await run.io_bound(review_track.run_once, MONITOR_DB,
                                       MONITOR_DB, None, True)
                if r.get("checked"):
                    msg = (f"跟踪完成:检查 {r['checked']} 条"
                           + (f",停止跟踪 {len(r['stopped'])} 条"
                              if r.get("stopped") else ""))
                else:
                    msg = r.get("note", "本轮没有需要检测的链接")
                ui.notify(msg, type="positive")
                ui.navigate.reload()
            except Exception as e:
                ui.notify(f"跟踪失败:{e.__class__.__name__}: {e}", type="negative")

        ui.separator()
        # 钉钉通知:群机器人 webhook(简单) / 企业内部应用机器人单聊(私聊)
        html('<div class="card-title" style="font-size:14px">钉钉通知</div>')
        mode_sel = ui.radio({"group": "群机器人 Webhook", "app": "企业内部应用"},
                            value=sget(MONITOR_DB, "notify_mode", "group")) \
            .props("inline dense")

        group_box = ui.column().classes("w-full gap-1")
        with group_box:
            wh = ui.input("Webhook 地址",
                          value=sget(MONITOR_DB, "notify_webhook", cfg["webhook"]),
                          placeholder="https://oapi.dingtalk.com/robot/send?access_token=***") \
                .props("outlined dense").classes("w-full")
            sec = ui.input("加签密钥(SEC 开头,未加签留空)",
                           value=sget(MONITOR_DB, "notify_secret", cfg["secret"])) \
                .props("outlined dense").classes("w-full")
            html('<div class="pg-meta">钉钉群 → 群设置 → 智能群助手 → 添加机器人 → '
                 '自定义,拿到 Webhook 粘这里。</div>')

        app_box = ui.column().classes("w-full gap-1")
        with app_box:
            cid = ui.input("Client ID(AppKey)",
                           value=sget(MONITOR_DB, "notify_client_id", "")) \
                .props("outlined dense").classes("w-full")
            csec = ui.input("Client Secret",
                            value=sget(MONITOR_DB, "notify_client_secret", ""),
                            password=True).props("outlined dense").classes("w-full")
            rcode = ui.input("Robot Code(留空则同 Client ID)",
                             value=sget(MONITOR_DB, "notify_robot_code", "")) \
                .props("outlined dense").classes("w-full")
            uids = ui.input("收件人 userid(多个逗号分隔)",
                            value=sget(MONITOR_DB, "notify_user_ids", ""),
                            placeholder="manager1234,user5678") \
                .props("outlined dense").classes("w-full")
            html('<div class="pg-meta">私聊推给指定人。userid 需在开放平台开通'
                 '通讯录权限后才能查到(手机号换 userid 接口)。</div>')

        def _sync_mode():
            group_box.set_visibility(mode_sel.value == "group")
            app_box.set_visibility(mode_sel.value == "app")
        mode_sel.on_value_change(lambda e: _sync_mode())
        _sync_mode()

        mute = ui.number("同一异常静默期(小时)",
                         value=float(sget(MONITOR_DB, "notify_mute_h", "12") or 12),
                         min=0, max=720, step=1) \
            .props("outlined dense").classes("w-40")
        html('<div class="pg-meta">有新增异常时推送;静默期内同一 ASIN 的'
             '同类变化只提醒一次,防刷屏。</div>')

        ui.separator()
        # AI 解读:推送里每个 ASIN 下加一句人话总结;不配 key 则整段跳过
        html('<div class="card-title" style="font-size:14px">AI 解读(可选)</div>')
        from monitor import ai as _ai
        aicfg = _ai.get_ai_config(MONITOR_DB)
        akey = ui.input("API Key(留空则关闭 AI 解读)",
                        value=aicfg["key"], password=True) \
            .props("outlined dense").classes("w-full")
        with ui.row().classes("w-full items-center gap-2"):
            abase = ui.input("Base URL", value=aicfg["base"]) \
                .props("outlined dense").classes("flex-grow")
            amodel = ui.input("模型", value=aicfg["model"]) \
                .props("outlined dense").classes("w-48")
        aprompt = ui.textarea("全局解读规范(告诉 AI 什么该强调、什么别当回事)",
                              value=sget(MONITOR_DB, "ai_prompt", "")) \
            .props("outlined dense rows=3").classes("w-full")
        aprompt.placeholder = "例:价格降5%以内只提一句;BuyBox易主必须点名新卖家;结论优先说要不要人工介入"
        acountry = ui.textarea("分国家解读规范(每行一条:国家码: 规范)",
                               value="\n".join(
                                   f"{k[10:]}: {v}" for k, v in
                                   sorted(monitor_store.list_settings(MONITOR_DB))
                                   if k.startswith("ai_prompt_") and v.strip())) \
            .props("outlined dense rows=2").classes("w-full")
        acountry.placeholder = "US: 严格,任何波动都点名\nAU: 宽松,只报下架和BuyBox"
        html('<div class="pg-meta">优先级:单链接(点表格行在详情里配)&gt;国家&gt;全局。'
             '改完即生效,只影响推送「解读:」那一行,不影响告警触发。</div>')

        def _save():
            sset(MONITOR_DB, "schedule_enabled", "1" if enabled.value else "0")
            sset(MONITOR_DB, "schedule_interval_h", str(interval.value or 6))
            sset(MONITOR_DB, "review_track_enabled",
                 "1" if rt_enabled.value else "0")
            sset(MONITOR_DB, "review_track_batch", str(int(rt_batch.value or 3)))
            sset(MONITOR_DB, "notify_mode", mode_sel.value or "group")
            sset(MONITOR_DB, "notify_webhook", (wh.value or "").strip())
            sset(MONITOR_DB, "notify_secret", (sec.value or "").strip())
            sset(MONITOR_DB, "notify_client_id", (cid.value or "").strip())
            sset(MONITOR_DB, "notify_client_secret", (csec.value or "").strip())
            sset(MONITOR_DB, "notify_robot_code", (rcode.value or "").strip())
            sset(MONITOR_DB, "notify_user_ids", (uids.value or "").strip())
            sset(MONITOR_DB, "notify_mute_h", str(mute.value or 12))
            sset(MONITOR_DB, "ai_api_key", (akey.value or "").strip())
            sset(MONITOR_DB, "ai_base_url", (abase.value or "").strip())
            sset(MONITOR_DB, "ai_model", (amodel.value or "").strip())
            sset(MONITOR_DB, "ai_prompt", (aprompt.value or "").strip())
            # 分国家:先清旧键再按行写入(留空=删除该国家定制,回退全局)
            for k in [k for k, _ in monitor_store.list_settings(MONITOR_DB)
                      if k.startswith("ai_prompt_")]:
                monitor_store.del_setting(MONITOR_DB, k)
            for ln in (acountry.value or "").splitlines():
                cc, _, txt = ln.partition(":")
                cc = cc.strip().upper()
                if cc and txt.strip():
                    sset(MONITOR_DB, f"ai_prompt_{cc}", txt.strip())
            ui.notify("已保存,定时采集按新配置运行", type="positive")
            d.close()

        async def _test():
            ok, msg = await run.io_bound(
                mn.push_text, MONITOR_DB,
                "AmCheck 测试消息:通知配置 OK ✅")
            ui.notify(msg, type="positive" if ok else "negative")

        async def _run_now():
            # 立即跑一轮(复用调度器逻辑:真实链接采集 + 通知推送)
            from monitor.scheduler import run_once
            d.close()
            ui.notify("采集进行中,完成后自动刷新…", type="info")
            try:
                r = await run.io_bound(run_once, MONITOR_DB)
                ui.notify(f"采集完成:检查 {r['checked']} 条,"
                          f"异常 {r['anomalies']} 条",
                          type="warning" if r["anomalies"] else "positive")
                ui.navigate.reload()
            except Exception as e:
                ui.notify(f"采集失败:{e.__class__.__name__}: {e}",
                          type="negative")

        with ui.row().classes("w-full justify-end gap-2 mt-1"):
            ui.button("测试推送", on_click=_test).props("outline no-caps dense")
            ui.button("立即采集一轮", on_click=_run_now) \
                .props("outline no-caps dense")
            ui.button("立即跟踪一轮", on_click=_run_reviews) \
                .props("outline no-caps dense")
            ui.button("保存", on_click=_save) \
                .props("unelevated no-caps dense color=primary")
    d.open()


def add_monitor_dialog(on_done):
    """添加监控弹窗:粘贴 Amazon 商品页链接(或手填 ASIN),入库 profiles。"""
    with ui.dialog() as d, ui.card().classes("app-card w-[520px]"):
        with ui.row().classes("w-full items-center justify-between"):
            html('<div class="card-title">添加监控链接</div>')
            ui.button(icon="close", on_click=d.close).props("flat round dense")
        html('<div class="pg-meta">粘贴 Amazon 商品页链接(自动识别 ASIN 与站点)'
             ',可一次多行批量添加</div>')
        ta = ui.textarea(placeholder="https://www.amazon.com/dp/B0XXXXXXXX/\n"
                                     "https://www.amazon.in/dp/B0YYYYYYYY/")
        ta.classes("w-full").props("outlined dense rows=5").style("font-size:13px")
        msg = ui.label("").classes("pg-meta")

        def _add():
            text = ta.value or ""
            found, errs = [], []
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                # 域名与 /dp/ 之间可能有标题 slug(如 /LUXTER-Cordless.../dp/ASIN),
                # 所以域名和 ASIN 分别单独匹配,不要求相邻
                dm = re.search(r"amazon\.([a-z.]+?)(?:/|$|[?#])", line, re.I)
                am = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:\b|[/?#]|$)", line, re.I)
                if dm and am:
                    found.append((am.group(1).upper(), f"amazon.{dm.group(1).lower()}", line))
                else:
                    errs.append(line)
            if not found:
                msg.set_text("未解析到有效商品链接(/dp/ASIN 格式)")
                return
            from monitor import store as ms
            from monitor.pipeline import add_profile
            added = skipped = 0
            for asin, domain, url in found:
                if ms.get_profile(MONITOR_DB, asin, domain):
                    skipped += 1          # 已在监控中,不覆盖其配置
                    continue
                add_profile(MONITOR_DB, asin=asin, domain=domain, url=url)
                added += 1
            d.close()
            ui.notify(f"已添加 {added} 条监控" +
                      (f",跳过已存在 {skipped} 条" if skipped else ""),
                      type="positive")
            on_done()

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("取消", on_click=d.close).props("outline no-caps dense")
            ui.button("添加", on_click=_add).props("unelevated no-caps dense color=primary")
    d.open()


def run_monitor_round(on_done, btn, prog, prog_text, prog_row):
    """跑一轮采集:真实抓取 profiles 里启用的 ASIN,按站点并行。

    交互:按钮进入 loading → 页内进度条按站点粒度推进(显示各站点进行中)
    → 完成后进度条报告结果并自动刷新表格;全程不弹窗不跳页。

    控件句柄由 page_monitor 传入(本函数是模块级,拿不到页面局部变量)。
    工作线程只写共享 dict,UI 更新统一由 0.2s 的 ui.timer 拉取,
    避免跨线程直接操作元素。返回协程交给 NiceGUI 调度。
    """
    from monitor import store as ms
    from monitor.pipeline import run_round_parallel
    from monitor.address import PlaywrightAdapter
    from monitor.scheduler import _demo_asins

    # 演示 ASIN 跳过真实抓取(会把种子时间线打成脏数据)
    profs = [p for p in ms.list_profiles(MONITOR_DB)
             if p["asin"] not in _demo_asins()]
    if not profs:
        ui.notify("还没有监控链接,先点「添加监控」", type="warning")
        return

    async def _run():
        enabled = [p for p in profs if p.get("monitor_enabled", 1)]
        domains = sorted({p["domain"] for p in enabled})
        total = len(enabled)

        state = {"done": 0, "anom": 0, "line": f"准备并行采集 {total} 条 / {len(domains)} 站…",
                 "finished": False, "error": "", "pushed": 0}

        btn.props("disable loading")
        prog_row.set_visibility(True)
        prog.set_value(0)
        prog_text.set_text(state["line"])

        def on_progress(done, tot, domain, profile):
            # 每站点完成一条回调一次(工作线程):只写 state,不碰 UI
            state["done"] = done
            label = (profile.get("title") or "")[:20] or profile["asin"]
            state["line"] = (
                f"[{done}/{tot}] {DOMAIN_SHORT(domain)} · "
                f"{profile['asin']} {label}"
                + (f" · 异常 {state['anom']}" if state["anom"] else ""))

        def _work():
            r = run_round_parallel(
                MONITOR_DB, lambda dom: PlaywrightAdapter(dom),
                profiles=profs, on_progress=on_progress)
            state["anom"] = r["anomalies"]
            if r["anomalies"]:
                # 工作线程里推钉钉(网络调用不碰 UI);未配置 webhook 自动跳过
                try:
                    from monitor.notify import notify_new_anomalies
                    state["pushed"] = notify_new_anomalies(MONITOR_DB,
                                                           r["anomalies"])
                except Exception:
                    pass
            return r["checked"]

        async def _poll():
            # 工作线程只改 state;这里负责把它画到屏幕上
            prog.set_value(state["done"] / total if total else 1)
            prog_text.set_text(state["line"])
            if state["finished"]:
                timer.cancel()
                if state["error"]:
                    prog_text.set_text(
                        f'<span style="color:#b91c1c">采集失败:{state["error"]}</span>')
                    ui.notify(f"采集失败:{state['error']}", type="negative")
                else:
                    color = "#b45309" if state["anom"] else "#15803d"
                    prog_text.set_text(
                        f'<span style="color:{color};font-weight:600">'
                        f'采集完成 {state["done"]} 条 · 异常 {state["anom"]} 条</span>')
                    ui.notify(f"采集完成:{state['done']} 条,"
                              f"发现异常 {state['anom']} 条"
                              + (f",已推送 {state['pushed']} 条通知"
                                 if state["pushed"] else ""),
                              type="warning" if state["anom"] else "positive")
                btn.props(remove="disable loading")
                on_done()  # 重建表格与切卡(不跳页,筛选状态保留)

        timer = ui.timer(0.2, _poll)

        try:
            await run.io_bound(_work)
        except Exception as e:
            state["error"] = f"{e.__class__.__name__}: {e}"
        finally:
            state["finished"] = True

    return _run()




# ---------- 弹窗:登录 / IP 热度 / 系统维护 ----------


# 登录会话的专属工作线程。
# Playwright 同步 API 有两条硬约束:①不能在 asyncio 事件循环里调用(会直接抛
# "It looks like you are using Playwright Sync API inside the asyncio loop");
# ②同一个会话必须在同一个线程里用,换线程会 "Cannot switch to a different thread"。
# 因此按域名各配一个单线程池:同域名串行且固定线程,不同域名互不阻塞。
_LOGIN_POOLS: dict[str, ThreadPoolExecutor] = {}


async def _login_bound(dom: str, fn, *args):
    """把同步的 Playwright 操作丢进该域名专属线程执行,不阻塞 UI 事件循环。"""
    pool = _LOGIN_POOLS.get(dom)
    if pool is None:
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"login-{dom}")
        _LOGIN_POOLS[dom] = pool
    return await asyncio.get_running_loop().run_in_executor(pool, fn, *args)


def _open_session(dom: str, tries: int = 3):
    """关掉旧会话后立刻重建;Chromium 退出有延迟,档案锁偶发没释放,重试几次。"""
    for i in range(tries):
        try:
            return weblogin.get_session(dom)
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(1.5)


def _login_step(kind: str, dom: str, sess, acct: str = "", pwd: str = "",
                secret: str = "", manual: str = "") -> tuple[str, bytes | None]:
    """在已就绪的会话上执行一步登录操作,统一返回 (文案, 截图 bytes 或 None)。

    三个分支的返回值必须对齐,否则调用处 `m, img = ...` 会解包失败:
    - auto_login()  返回 (文案, 截图)
    - submit_code() 只返回截图 bytes(Streamlit 版 app.py 直接把它当图用),
      文案在 sess.last_msg 里 —— 这里自己包成二元组,别再解包它的返回值。
    """
    if kind == "code":
        k = "otp" if sess.page.query_selector(
            "#auth-mfa-otpcode, input[name='otpCode']") is not None else "captcha"
        return sess.last_msg, sess.submit_code(k, manual)
    if kind == "check":
        ok = sess.logged_in()
        return (f"✅ {dom} 登录态已保存", None) if ok else ("未登录", sess.shot())
    return sess.auto_login(acct, pwd, secret)


def login_dialog():
    status = login_status()
    acct_keys = {k for k in ACCOUNTS if not k.startswith("_")}
    domains = [d for d in ["amazon.in", "amazon.com", "amazon.com.au",
                           "amazon.co.jp", "amazon.com.br", "amazon.com.mx"]
               if d in (set(DOMAINS) | acct_keys)]
    with ui.dialog() as d, ui.card().classes("app-card w-[440px]"):
        with ui.row().classes("w-full items-center justify-between"):
            html('<div class="card-title">Amazon 账号登录管理</div>')
            ui.button(icon="close", on_click=d.close).props("flat round dense")
        sel = ui.select({d: f"{d} · {'已登录' if status.get(d, {}).get('ok') else '未登录'}"
                         for d in domains}, value=domains[0]) \
            .props("outlined dense").classes("w-full")
        account = ui.input("Amazon 账号").props("outlined dense").classes("w-full")
        password = ui.input("账号密码", password=True).props("outlined dense").classes("w-full")
        totp = ui.input("TOTP 密钥(可选)", password=True).props("outlined dense").classes("w-full")
        code = ui.input("验证码", placeholder="仅未配 TOTP 且停在验证码页时需要") \
            .props("outlined dense").classes("w-full")
        msg = ui.label("").classes("pg-meta")
        img_holder = ui.column().classes("w-full")

        HINT = {
            "login": "正在启动服务器浏览器并自动登录,约 10~40 秒,请勿关闭弹窗…",
            "code": "正在提交验证码…",
            "check": "正在检查登录态…",
        }
        running = {"on": False}

        async def _do(kind):
            """按钮回调:同步 Playwright 调用必须丢进专属线程,否则会卡死整个事件循环。"""
            if running["on"]:
                return
            running["on"] = True
            dom = sel.value
            acct, pwd = account.value or "", password.value or ""
            secret, manual = (totp.value or "").strip(), code.value or ""
            btns = {"login": b_login, "code": b_code, "check": b_check}
            for b in btns.values():
                b.props("disable")
            btns[kind].props("loading")
            t0 = time.time()
            msg.set_text(HINT[kind])

            def _tick():
                msg.set_text(f"{HINT[kind]}(已等待 {int(time.time() - t0)} 秒)")

            ticker = ui.timer(1.0, _tick)

            def _work():
                # 这里跑在该域名的专属线程里,可以安全调用同步 Playwright
                if kind == "login":
                    weblogin.close_domains({dom})  # 先释放档案,免得和检测引擎抢 profile 锁
                return _login_step(kind, dom, _open_session(dom),
                                   acct, pwd, secret, manual)

            try:
                m, img = await _login_bound(dom, _work)
                msg.set_text(m)
                img_holder.clear()
                if img:
                    with img_holder:
                        ui.image(img).classes("w-full rounded-md")
                if kind == "check":
                    ui.notify(m, type="positive" if m.startswith("✅") else "warning")
            except Exception as e:
                msg.set_text(f"出错:{e.__class__.__name__}: {e}")
                ui.notify(f"登录操作失败:{e}", type="negative")
            finally:
                ticker.cancel()
                for b in btns.values():
                    b.props(remove="disable loading")
                running["on"] = False

        with ui.row().classes("w-full gap-2"):
            b_login = ui.button("开始登录", on_click=lambda: _do("login")) \
                .props("unelevated no-caps color=primary").classes("flex-grow")
            b_code = ui.button("提交验证码", on_click=lambda: _do("code")) \
                .props("outline no-caps").classes("flex-grow")
        b_check = ui.button("检测登录态", on_click=lambda: _do("check")) \
            .props("outline no-caps").classes("w-full")
    d.open()


def heat_dialog():
    heat = heat_stats()
    with ui.dialog() as d, ui.card().classes("app-card w-[420px]"):
        html('<div class="card-title">IP 热度(近 24h)</div>')
        if not heat:
            ui.label("暂无检测数据").classes("pg-meta")
        for dom, total, blocked in heat:
            pct = (blocked or 0) / total
            tone = "text-[#b91c1c]" if pct > .2 else ("text-[#b45309]" if pct > .05 else "text-[#15803d]")
            with ui.row().classes("w-full items-center gap-2"):
                ui.label(DOMAIN_SHORT(dom)).classes("w-16 text-[13px]")
                ui.linear_progress(value=pct, show_value=False).classes("flex-grow h-1")
                ui.label(f"{pct:.0%}").classes(f"text-[12px] {tone}")
        html('<div class="pg-meta">拦截率 <5% 正常;5~20% 建议降频;>20% 暂停或更换出口 IP</div>')
    d.open()


def _latest_pypi(pkg):
    try:
        with urllib.request.urlopen(f"https://pypi.org/pypi/{pkg}/json", timeout=5) as r:
            return json.load(r)["info"]["version"]
    except Exception:
        return None


def system_dialog():
    with ui.dialog() as d, ui.card().classes("app-card w-[460px]"):
        with ui.row().classes("w-full items-center justify-between"):
            html('<div class="card-title">系统维护</div>')
            ui.button(icon="close", on_click=d.close).props("flat round dense")
        try:
            import importlib.metadata
            cur = importlib.metadata.version("playwright")
        except Exception:
            cur = "未知"
        # 版本信息先渲染当前版本;PyPI 最新版后台异步查询,不阻塞弹窗打开
        # (pypi.org 国内直连可能数秒,同步查会卡住整个弹窗)
        ver_row = html(f'<div class="text-[13px]">Playwright 当前版本: '
                       f'<code>{cur}</code> · PyPI 最新: <span class="pg-meta">查询中…</span></div>')

        async def _load_latest():
            top = await run.io_bound(_latest_pypi, "playwright")
            if top:
                ver_row.set_content(
                    f'<div class="text-[13px]">Playwright 当前版本: <code>{cur}</code>'
                    f' · PyPI 最新: <code>{top}</code></div>')

        ui.timer(0.1, _load_latest, once=True)
        log = ui.log(max_lines=12).classes("w-full h-48")

        async def do_upgrade():
            proc = subprocess.Popen(
                [sys.executable, "-m", "pip", "install", "-U", "playwright"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

            def _pump():
                for line in proc.stdout:
                    log.push(line.rstrip())
                proc.wait()
                log.push("完成,重启服务后生效")

            await run.io_bound(_pump)

        ui.button("升级 Playwright", on_click=do_upgrade) \
            .props("unelevated no-caps color=primary").classes("w-full")
        ui.button("重启服务", on_click=lambda: (
            os.execv(sys.executable,
                    [sys.executable, "-m", "nicegui", "run", str(Path(__file__))]))) \
            .props("outline no-caps").classes("w-full")
    d.open()


# ---------- 侧边栏与布局 ----------


@ui.refreshable
def sidebar_nav():
    status = login_status()
    online = sum(1 for v in status.values() if v["ok"])
    with ui.column().classes("gap-0 w-full"):
        with ui.row().classes("items-center gap-2.5 px-3 py-2"):
            html('<div class="brand-mark">A</div>')
            html('<div><div class="brand-name">AmReview</div>'
                    '<div class="brand-sub">Amazon 评价链接批量检测</div></div>')
        ui.separator()
        for path, icon, label in [("/", "fact_check", "评价链接检测"),
                                  ("/history", "history", "评价链接检测历史"),
                                  ("/monitor", "monitoring", "链接监控")]:
            active = app.storage.user.get("_nav") == path
            ui.link(label, path).classes(f"nav-item {'nav-active' if active else ''}") \
                .props(f'icon={icon}')
        ui.separator()
        html('<div class="nav-group">演示与辅助</div>')
        ui.button(("关闭演示数据" if app.storage.user.get("mock_on") else "打开演示数据"),
                  icon="science", on_click=toggle_mock).props("flat no-caps align=left")
        ui.separator()
        html('<div class="nav-group">账号与运行状态</div>')
        ui.button(f"账号登录 {online}/{len(status)}", icon="key", on_click=login_dialog) \
            .props("flat no-caps align=left")
        ui.button("IP 热度", icon="speed", on_click=heat_dialog) \
            .props("flat no-caps align=left")
        ui.button("系统维护", icon="settings", on_click=system_dialog) \
            .props("flat no-caps align=left")
        if online < len(status):
            html(f'<div class="pg-meta" style="padding:0 12px">'
                    f'{len(status) - online} 个站点未登录,检测会判为登录失效</div>')


def toggle_mock():
    if app.storage.user.get("mock_on"):
        unload_all_mock()
        app.storage.user["mock_on"] = False
    else:
        load_all_mock()
        app.storage.user["mock_on"] = True
    ui.navigate.reload()


@contextmanager
def build_shell(nav: str):
    """侧边栏 + 主区框架;每页开头 with build_shell(路径): 渲染页面内容。"""
    app.storage.user["_nav"] = nav
    with ui.row().classes("shell-row m-0 p-0 gap-0"):
        with ui.column().classes("sidebar"):
            sidebar_nav()
        with ui.column().classes("main-area flex-grow items-stretch"):
            yield


def _report_exception(e: Exception) -> None:
    """兜底:没被 catch 的界面异常也弹出来,避免用户只看到"点了没反应"。"""
    try:
        ui.notify(f"服务端异常:{e.__class__.__name__}: {e}", type="negative",
                  multi_line=True)
    except Exception:
        pass


app.on_exception(_report_exception)


ui.run(title="AmReview 评价检测", port=8765, reload=False, show=False,
       storage_secret="amreview-secret", favicon="🔍",
       show_welcome_message=False)
