"""mockdata —— 演示数据(界面「载入演示数据」开关背后的全部内容)。

ui.py(NiceGUI)与 app.py(Streamlit)共用本模块。此前两边各写一遍 ~330 行,
`MOCK_RESULTS` **逐字节相同**、函数只差文档字符串 —— 改一处漏一处只是时间问题。

**框架相关的东西刻意不在这里**:往 `app.storage.user` / `st.session_state`
写"本轮结果"留在各自的 UI 文件里。本模块只负责两件事:
1. 产出演示数据(含占位截图);
2. 读写 history.db / monitor.db(经 review_db / monitor.store,不手写 SQL)。
"""

from __future__ import annotations

from pathlib import Path

import review_db
from engine import DOMAINS
from monitor import demo as monitor_demo
from monitor import store as monitor_store

# ---------- 本轮结果演示数据(覆盖全状态 / 全站点) ----------

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


def mock_font(size: int):
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


def make_mock_shot(review_id: str, kind: str) -> str:
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
        d.text((150, 158), "Sorry", fill=(255, 255, 255), font=mock_font(36))
        d.text((150, 240), "we couldn't find that page",
               fill=(17, 94, 89), font=mock_font(28))
        d.text((150, 285), f"演示截图 · {review_id}", fill=(102, 102, 102),
               font=mock_font(16))
        img.save(path)
    except Exception:
        pass
    return str(path)


# ---------- 历史视图演示数据 ----------

# 固定 18 条(覆盖全状态 / 全站点)+ 追加 50 条长列表,共 68 条
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


def make_mock_history_rows(count: int = 50):
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


MOCK_HISTORY.extend(make_mock_history_rows())

# 全站演示数据的全集:结果视图 + 历史视图的所有 review_id,卸载时据此清空
MOCK_IDS = tuple({r["review_id"] for r in MOCK_RESULTS} | {h[0] for h in MOCK_HISTORY})


# ---------- 数据装配(框架无关部分) ----------

def build_results() -> list[dict]:
    """本轮结果:补上截图路径,并摘掉仅构造期用的字段。"""
    results = []
    for src in MOCK_RESULTS:
        r = dict(src)
        r["screenshot"] = make_mock_shot(r["review_id"], r["shot_kind"]) \
            if r["shot_kind"] else ""
        r.pop("shot_kind", None)
        r.pop("prev_status", None)
        r.pop("prev_time", None)
        results.append(r)
    return results


def prev_map() -> dict:
    """结果页「上次检测」列:{review_id: (上次状态, 上次时间)}。"""
    return {m["review_id"]: (m["prev_status"], m["prev_time"])
            for m in MOCK_RESULTS if m["prev_status"]}


def load_history() -> None:
    """把演示历史写进 history.db(幂等:先清本批 ID 再写,不误伤真实数据)。"""
    review_db.delete_history([h[0] for h in MOCK_HISTORY])
    review_db.save_history(
        [{"review_id": rid, "domain": domain,
          "url": f"https://www.{domain}/gp/customer-reviews/{rid}/",
          "status": status, "stars": stars, "title": title, "author": author,
          "review_date": "", "note": "", "checked_at": check_time}
         for rid, domain, status, stars, title, author, check_time in MOCK_HISTORY],
        auto_meta=False)   # 演示 ID 不进每日跟踪队列


def delete_rows() -> int:
    """删掉 history.db 里的演示记录,返回删除行数。"""
    return review_db.delete_history(list(MOCK_IDS))


def seed_monitor(monitor_db) -> None:
    """装载链接监控演示数据。"""
    monitor_demo.seed_demo(monitor_db)


def clear_monitor(monitor_db) -> None:
    """清掉链接监控演示数据。"""
    monitor_store.delete_by_asins(monitor_db, list(monitor_demo.TIMELINES))
