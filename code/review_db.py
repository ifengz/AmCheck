"""review_db —— 评价链接检测库(history.db)统一数据层。

两张表分工:

- ``history``      一次检测 = 一行(追加式,不覆盖)。检测历史页按时间倒序读。
- ``review_meta``  一个评价链接 = 一行(主键 review_id)。存"这个链接是谁"的业务
  字段:刷单编号 / 订单号 / 产品型号,以及定时跟踪状态(是否跟踪、连续已删次数、
  停止原因)。表格导入写这里;直接粘链接检测时也会自动补一行(source='link')。

这样"直接链接添加 → 之后表格上传到同一个评价链接"就能把空字段回填,
而不会因为一条链接被检测过多次而重复写业务字段。

settings(定时开关等)不在本库,沿用 monitor.db 的 settings 表。
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

DB = Path(__file__).parent / "history.db"

# 连续多少次"已删(变狗)"后停止跟踪。跟踪是每日一次,即约 5 天后放弃。
STOP_STREAK = 5


def connect() -> sqlite3.Connection:
    return sqlite3.connect(DB, timeout=10)


def init_db() -> None:
    """建表 + 补列,幂等。"""
    with connect() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS history (
            review_id TEXT, domain TEXT, url TEXT, status TEXT,
            stars TEXT, title TEXT, author TEXT, review_date TEXT,
            note TEXT, checked_at TEXT)""")
        # 旧的 tracking 表保留(历史遗留,已无写入方)
        conn.execute("""CREATE TABLE IF NOT EXISTS tracking (
            asin TEXT, domain TEXT, url TEXT, status TEXT,
            title TEXT, price TEXT, rating TEXT, review_count TEXT,
            availability TEXT, note TEXT, checked_at TEXT,
            prev_status TEXT, prev_price TEXT, prev_time TEXT)""")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_hist_review "
                     "ON history (review_id, checked_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_hist_time "
                     "ON history (checked_at)")

        # 评价链接台账:业务字段 + 跟踪状态
        conn.execute("""CREATE TABLE IF NOT EXISTS review_meta (
            review_id TEXT PRIMARY KEY,
            domain TEXT DEFAULT '',
            url TEXT DEFAULT '',
            order_ref TEXT DEFAULT '',      -- 刷单编号(表格 A 列)
            order_no TEXT DEFAULT '',       -- 订单号(表格 F 列)
            model TEXT DEFAULT '',          -- 产品型号(表格 D 列)
            source TEXT DEFAULT 'link',     -- link=直接粘链接 / table=表格导入
            created_at TEXT DEFAULT '',
            track_enabled INTEGER DEFAULT 1,
            stop_reason TEXT DEFAULT '',
            deleted_streak INTEGER DEFAULT 0,
            track_count INTEGER DEFAULT 0,
            last_tracked_at TEXT DEFAULT ''
        )""")


# ---------- history ----------

def save_history(results: list[dict], auto_meta: bool = True) -> None:
    """写一批检测结果。

    auto_meta=True 时顺带保证每个 review_id 在台账里有一行 —— 即"直接粘链接
    检测过的链接也自动纳入每日跟踪"。演示数据装载时传 False,免得把
    假 review_id 塞进跟踪队列。
    """
    if not results:
        return
    with connect() as conn:
        conn.executemany(
            """INSERT INTO history (review_id, domain, url, status, stars, title,
               author, review_date, note, checked_at)
               VALUES (:review_id, :domain, :url, :status, :stars, :title,
                       :author, :review_date, :note, :checked_at)""",
            results)
        if not auto_meta:
            return
        for r in results:
            rid = r.get("review_id")
            if not rid:
                continue
            row = conn.execute("SELECT review_id, domain, url FROM review_meta "
                               "WHERE review_id=?", (rid,)).fetchone()
            if row is None:
                conn.execute(
                    """INSERT INTO review_meta (review_id, domain, url, source,
                           created_at, track_enabled)
                       VALUES (?,?,?,'link',? ,1)""",
                    (rid, r.get("domain", ""), r.get("url", ""),
                     r.get("checked_at", "")))
            elif not row[1] or not row[2]:
                conn.execute(
                    "UPDATE review_meta SET domain=?, url=? WHERE review_id=?",
                    (row[1] or r.get("domain", ""), row[2] or r.get("url", ""), rid))


def last_status_map(refs) -> dict:
    """这批 refs 各自最近一次的状态(结果页「上次检测」列)。"""
    if not refs or not DB.exists():
        return {}
    ids = [r.review_id for r in refs]
    with connect() as conn:
        rows = conn.execute(
            """SELECT review_id, status, checked_at FROM history h
               WHERE checked_at = (SELECT MAX(checked_at) FROM history
                                   h2 WHERE h2.review_id = h.review_id)
               AND review_id IN (%s)""" % ",".join("?" * len(ids)),
            ids).fetchall()
    return {rid: (s, t) for rid, s, t in rows}


def _meta_join_sql(where: str) -> str:
    """检测记录 + 台账字段的合并查询。

    台账里已有、但还没检测过的链接(刚导入/刚粘贴)也要出现在历史页,
    状态记为空串,由展示层写成「待检测」。
    """
    return f"""
        SELECT * FROM (
            SELECT h.review_id AS review_id, h.domain AS domain, h.url AS url,
                   h.status AS status, h.stars AS stars, h.title AS title,
                   h.author AS author, h.review_date AS review_date,
                   h.note AS note, h.checked_at AS checked_at,
                   COALESCE(m.order_ref,'') AS order_ref,
                   COALESCE(m.order_no,'')  AS order_no,
                   COALESCE(m.model,'')     AS model,
                   COALESCE(m.track_enabled,1) AS track_enabled,
                   COALESCE(m.stop_reason,'')  AS stop_reason,
                   COALESCE(m.track_count,0)   AS track_count,
                   COALESCE(m.deleted_streak,0) AS deleted_streak,
                   0 AS pending
            FROM history h
            LEFT JOIN review_meta m ON m.review_id = h.review_id
            UNION ALL
            SELECT m.review_id, m.domain, m.url, '', '', '', '', '',
                   '', m.created_at,
                   m.order_ref, m.order_no, m.model,
                   m.track_enabled, m.stop_reason, m.track_count,
                   m.deleted_streak, 1
            FROM review_meta m
            WHERE NOT EXISTS (SELECT 1 FROM history h WHERE h.review_id = m.review_id)
        ) {where} ORDER BY checked_at DESC, review_id LIMIT ?"""


def recent_history(limit: int = 500, days: int | None = None) -> list[dict]:
    if not DB.exists():
        return []
    where, params = "", []
    if days:
        where = "WHERE checked_at >= datetime('now', ?)"
        params = [f"-{days} days"]
    with connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(_meta_join_sql(where), params + [limit]).fetchall()
    return [dict(r) for r in rows]


def history_stats(days: int | None = None) -> list:
    if not DB.exists():
        return []
    where, params = "", []
    if days:
        where = "WHERE checked_at >= datetime('now', ?)"
        params = [f"-{days} days"]
    with connect() as conn:
        return conn.execute(
            f"SELECT status, COUNT(*) FROM history {where} GROUP BY status",
            params).fetchall()


def heat_stats() -> list:
    if not DB.exists():
        return []
    with connect() as conn:
        return conn.execute("""
            SELECT domain, COUNT(*), SUM(status='blocked')
            FROM history WHERE checked_at >= datetime('now','-1 day')
            GROUP BY domain""").fetchall()


def review_history_timeline(review_id: str, limit: int = 60) -> list[dict]:
    """某个评价链接按时间倒序的全部检测记录(抽屉里的"按日期跟踪历史")。"""
    if not DB.exists():
        return []
    with connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT status, stars, title, note, checked_at FROM history
               WHERE review_id = ? ORDER BY checked_at DESC LIMIT ?""",
            (review_id, limit)).fetchall()
    return [dict(r) for r in rows]


# ---------- review_meta ----------

def get_meta(review_id: str) -> dict | None:
    if not DB.exists():
        return None
    with connect() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM review_meta WHERE review_id=?",
                           (review_id,)).fetchone()
    return dict(row) if row else None


def all_meta() -> dict[str, dict]:
    if not DB.exists():
        return {}
    with connect() as conn:
        conn.row_factory = sqlite3.Row
        return {r["review_id"]: dict(r)
                for r in conn.execute("SELECT * FROM review_meta").fetchall()}


def upsert_review_meta(rows: list[dict], source: str = "table") -> dict:
    """表格导入落库。

    - 新链接:插一行(source='table')
    - 已存在:只回填空字段(刷单编号/订单号/产品型号),**不覆盖已有值**,
      这样"直接粘链接加进来的"之后被表格补上业务字段,而重复导入不冲掉手工值。

    返回 {"added", "filled", "unchanged"}。
    """
    stat = {"added": 0, "filled": 0, "unchanged": 0}
    if not rows:
        return stat
    with connect() as conn:
        for r in rows:
            rid = r["review_id"]
            cur = conn.execute(
                "SELECT order_ref, order_no, model, domain, url FROM review_meta "
                "WHERE review_id=?", (rid,)).fetchone()
            if cur is None:
                conn.execute(
                    """INSERT INTO review_meta (review_id, domain, url, order_ref,
                           order_no, model, source, created_at, track_enabled)
                       VALUES (?,?,?,?,?,?,?,?,1)""",
                    (rid, r.get("domain", ""), r.get("url", ""),
                     str(r.get("order_ref", "") or ""), str(r.get("order_no", "") or ""),
                     str(r.get("model", "") or ""), source,
                     r.get("created_at", "")))
                stat["added"] += 1
                continue
            old_ref, old_no, old_model, old_dom, old_url = cur
            new_ref = old_ref or str(r.get("order_ref", "") or "")
            new_no = old_no or str(r.get("order_no", "") or "")
            new_model = old_model or str(r.get("model", "") or "")
            new_dom = old_dom or r.get("domain", "")
            new_url = old_url or r.get("url", "")
            if (new_ref, new_no, new_model, new_dom, new_url) == \
                    (old_ref, old_no, old_model, old_dom, old_url):
                stat["unchanged"] += 1
            else:
                conn.execute(
                    """UPDATE review_meta SET order_ref=?, order_no=?, model=?,
                           domain=?, url=? WHERE review_id=?""",
                    (new_ref, new_no, new_model, new_dom, new_url, rid))
                stat["filled"] += 1
    return stat


def list_tracked() -> list[dict]:
    """参与定时跟踪的链接(未停止跟踪的全部)。"""
    if not DB.exists():
        return []
    with connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM review_meta WHERE track_enabled=1 "
            "ORDER BY review_id").fetchall()
    return [dict(r) for r in rows]


def list_stopped() -> list[dict]:
    if not DB.exists():
        return []
    with connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM review_meta WHERE track_enabled=0 "
            "ORDER BY last_tracked_at DESC").fetchall()
    return [dict(r) for r in rows]


def set_track_enabled(review_id: str, enabled: bool, reason: str = "") -> None:
    with connect() as conn:
        if enabled:
            # 恢复跟踪时清零连续已删计数,给它重新计数的机会
            conn.execute("UPDATE review_meta SET track_enabled=1, stop_reason='', "
                         "deleted_streak=0 WHERE review_id=?", (review_id,))
        else:
            conn.execute("UPDATE review_meta SET track_enabled=0, stop_reason=? "
                         "WHERE review_id=?", (reason or "手动停止", review_id))


def update_track_state(review_id: str, status: str, checked_at: str) -> dict:
    """一次跟踪检测后刷新台账。连续 STOP_STREAK 次「已删」自动停止跟踪。

    返回 {"stopped": bool, "streak": int, "count": int}。
    """
    with connect() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM review_meta WHERE review_id=?",
                           (review_id,)).fetchone()
        if row is None:
            return {"stopped": False, "streak": 0, "count": 0}
        streak = (row["deleted_streak"] or 0) + 1 if status == "deleted" else 0
        count = (row["track_count"] or 0) + 1
        stopped = streak >= STOP_STREAK
        reason = (f"连续 {STOP_STREAK} 次已删(变狗),已停止跟踪" if stopped
                  else (row["stop_reason"] or ""))
        conn.execute(
            """UPDATE review_meta SET deleted_streak=?, track_count=?,
                   last_tracked_at=?, stop_reason=?, track_enabled=?
               WHERE review_id=?""",
            (streak, count, checked_at, reason,
             0 if stopped else row["track_enabled"], review_id))
    return {"stopped": stopped, "streak": streak, "count": count}


# ---------- 定时分散计划 ----------

def daily_slots(review_ids: list[str]) -> dict[str, int]:
    """把一批链接均匀铺到一天的 0~86399 秒上,返回 {review_id: 当日秒偏移}。

    先按 review_id 的 md5 稳定排序再等分,所以链接集合不变时每天的次序一致,
    链接之间不会挤在一起(防风控);新增链接只影响等分间隔,不会让全部链接同时开跑。
    再叠加 ±7 分钟的确定性抖动,避免每天都精确落在同一秒。
    """
    if not review_ids:
        return {}
    ordered = sorted(review_ids,
                     key=lambda r: hashlib.md5(r.encode()).hexdigest())
    n = len(ordered)
    out = {}
    for i, rid in enumerate(ordered):
        base = int((i + 0.5) * 86400 / n)
        jitter = int(hashlib.md5((rid + "|slot").encode()).hexdigest()[:4], 16)
        out[rid] = max(0, min(86399, base + (jitter % 900) - 450))
    return out
