"""monitor.store —— SQLite 三表(profiles / snapshots / anomalies)追加式写。

对应架构文档 doc/06 §3。核心是 snapshots 表:**每次检查 INSERT 一行,历史 =
按时间查所有行**,不做 prev_* 覆盖(这是对现有 tracking 表的推倒重来)。

所有写用上下文管理器;读默认 sqlite3.Row 转 dict,避免调用方踩元组索引。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .model import SnapshotRecord

# 默认库文件路径(与 app.py 的 history.db 同级,但独立三表,不复用 tracking/history)
DEFAULT_DB = Path(__file__).parent.parent / "monitor.db"


def _connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path, timeout=10)


def init_db(path: Path = DEFAULT_DB) -> None:
    """建三张表(幂等)。约束与索引放在建表处,写路径统一走本模块。"""
    with _connect(path) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asin TEXT, domain TEXT, url TEXT,
                parent_asin TEXT, title TEXT,
                monitor_enabled INTEGER DEFAULT 1,
                metric_config TEXT DEFAULT '{}',
                baseline_snapshot_id INTEGER,
                last_checked_at TEXT,
                CHECK (asin <> '')
            )""")
        # 复合唯一:同一 ASIN 在同一站点只监控一次
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_profile_asdomain "
                     "ON profiles (asin, domain)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_profile_domain "
                     "ON profiles (domain)")

        # ★ 追加式快照:一条 = 一次检查。不覆盖,历史靠时间查询
        conn.execute("""
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asin TEXT, domain TEXT, checked_at TEXT,
                title TEXT, image_url TEXT, buybox TEXT, parent_asin TEXT,
                variations TEXT DEFAULT '[]',
                price TEXT, price_value REAL, currency TEXT,
                rating REAL, review_count INTEGER, bsr INTEGER,
                bsr_cat TEXT DEFAULT '', bsr_sub TEXT DEFAULT '',
                deal_tag TEXT, availability TEXT, status TEXT,
                bullets TEXT DEFAULT '[]',
                description TEXT DEFAULT '',
                home_reviews TEXT DEFAULT '{}',
                note TEXT
            )""")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_snap_asdomain_time "
                     "ON snapshots (asin, domain, checked_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_snap_time "
                     "ON snapshots (checked_at)")
        # 旧库补列:bullets/description 是后加的采集字段;
        # bsr_cat/bsr_sub = BSR 大类/小类名(如 Home / Desk Lamps)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(snapshots)")}
        if "bullets" not in cols:
            conn.execute("ALTER TABLE snapshots ADD COLUMN bullets TEXT DEFAULT '[]'")
        if "description" not in cols:
            conn.execute("ALTER TABLE snapshots ADD COLUMN description TEXT DEFAULT ''")
        if "bsr_cat" not in cols:
            conn.execute("ALTER TABLE snapshots ADD COLUMN bsr_cat TEXT DEFAULT ''")
        if "bsr_sub" not in cols:
            conn.execute("ALTER TABLE snapshots ADD COLUMN bsr_sub TEXT DEFAULT ''")

        # 检测出的异常(用于去重/通知/已读)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS anomalies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asin TEXT, domain TEXT, metric TEXT, change_type TEXT,
                old_value TEXT, new_value TEXT, severity TEXT,
                checked_at TEXT, notified INTEGER DEFAULT 0,
                confirmed INTEGER DEFAULT 0
            )""")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_anom_asdomain_time "
                     "ON anomalies (asin, domain, checked_at)")

        # 应用设置(键值):定时采集间隔/开关、钉钉 webhook、通知静默期等
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY, value TEXT NOT NULL)""")


# ---------- settings ----------

def get_setting(path: Path, key: str, default: str = "") -> str:
    with _connect(path) as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?",
                           (key,)).fetchone()
    return row[0] if row else default


def set_setting(path: Path, key: str, value: str) -> None:
    with _connect(path) as conn:
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, str(value)))


# ---------- profiles ----------

def upsert_profile(path: Path, p: dict) -> int:
    """INSERT 或更新 profile,返回其 id。metric_config/baseline 走这个口。"""
    import json
    with _connect(path) as conn:
        conn.execute("""
            INSERT INTO profiles (asin, domain, url, parent_asin, title,
                                  monitor_enabled, metric_config,
                                  baseline_snapshot_id, last_checked_at)
            VALUES (:asin, :domain, :url, :parent_asin, :title,
                    :monitor_enabled, :metric_config, :baseline_snapshot_id,
                    :last_checked_at)
            ON CONFLICT (asin, domain) DO UPDATE SET
                url=excluded.url, parent_asin=excluded.parent_asin,
                title=excluded.title, monitor_enabled=excluded.monitor_enabled,
                metric_config=excluded.metric_config,
                baseline_snapshot_id=excluded.baseline_snapshot_id,
                last_checked_at=excluded.last_checked_at
            """, {
                "asin": p["asin"], "domain": p["domain"], "url": p.get("url", ""),
                "parent_asin": p.get("parent_asin", ""), "title": p.get("title", ""),
                "monitor_enabled": p.get("monitor_enabled", 1),
                "metric_config": json.dumps(p.get("metric_config") or {}),
                "baseline_snapshot_id": p.get("baseline_snapshot_id"),
                "last_checked_at": p.get("last_checked_at", ""),
            })
        return conn.execute(
            "SELECT id FROM profiles WHERE asin=? AND domain=?",
            (p["asin"], p["domain"])).fetchone()[0]


def get_profile(path: Path, asin: str, domain: str) -> dict | None:
    import json
    with _connect(path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM profiles WHERE asin=? AND domain=?",
                           (asin, domain)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["metric_config"] = json.loads(d.get("metric_config") or "{}")
        return d


def delete_profile(path: Path, asin: str, domain: str) -> None:
    """彻底移除一条监控:profile 连同它的全部快照与异常。"""
    with _connect(path) as conn:
        conn.execute("DELETE FROM snapshots WHERE asin=? AND domain=?",
                     (asin, domain))
        conn.execute("DELETE FROM anomalies WHERE asin=? AND domain=?",
                     (asin, domain))
        conn.execute("DELETE FROM profiles WHERE asin=? AND domain=?",
                     (asin, domain))


def list_profiles(path: Path, only_enabled: bool = True) -> list[dict]:
    import json
    where = "WHERE monitor_enabled = 1" if only_enabled else ""
    with _connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT * FROM profiles {where} ORDER BY domain, asin").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["metric_config"] = json.loads(d.get("metric_config") or "{}")
            out.append(d)
        return out


def set_baseline(path: Path, asin: str, domain: str, snapshot_id: int) -> None:
    """把基线前移到某次快照(用户"确认无误"的落点)。"""
    with _connect(path) as conn:
        conn.execute(
            "UPDATE profiles SET baseline_snapshot_id=? WHERE asin=? AND domain=?",
            (snapshot_id, asin, domain))


def update_last_checked(path: Path, asin: str, domain: str, ts: str) -> None:
    with _connect(path) as conn:
        conn.execute(
            "UPDATE profiles SET last_checked_at=? WHERE asin=? AND domain=?",
            (ts, asin, domain))


# ---------- snapshots ----------

def insert_snapshot(path: Path, snap: SnapshotRecord) -> int:
    """插入一条快照,返回 id。这是唯一追加口。"""
    import json
    with _connect(path) as conn:
        cur = conn.execute("""
            INSERT INTO snapshots (asin, domain, checked_at, title, image_url,
                buybox, parent_asin, variations, price, price_value, currency,
                rating, review_count, bsr, bsr_cat, bsr_sub,
                deal_tag, availability, status,
                bullets, description, home_reviews, note)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                snap.asin, snap.domain, snap.checked_at, snap.title,
                snap.image_url, snap.buybox, snap.parent_asin,
                json.dumps(snap.variations or []), snap.price, snap.price_value,
                snap.currency, snap.rating, snap.review_count, snap.bsr,
                snap.bsr_cat or "", snap.bsr_sub or "",
                snap.deal_tag, snap.availability, snap.status,
                json.dumps(snap.bullets or [], ensure_ascii=False),
                (snap.description or "")[:4000],
                json.dumps(snap.home_reviews or {}), snap.note,
            ))
        return cur.lastrowid


def delete_by_asins(path: Path, asins: list[str]) -> None:
    """删掉一批 ASIN 的所有数据(三表联动,演示数据重置/卸载用)。"""
    with _connect(path) as conn:
        conn.executemany("DELETE FROM snapshots WHERE asin=?", [(a,) for a in asins])
        conn.executemany("DELETE FROM anomalies WHERE asin=?", [(a,) for a in asins])
        conn.executemany("DELETE FROM profiles WHERE asin=?", [(a,) for a in asins])


def snapshots_for(path: Path, asin: str, domain: str,
                  limit: int | None = None) -> list[dict]:
    """某 ASIN 的全部快照(升序,append 顺序)。limit 取最近 N 条时倒序再翻正。"""
    import json
    with _connect(path) as conn:
        conn.row_factory = sqlite3.Row
        sql = ("SELECT * FROM snapshots WHERE asin=? AND domain=? "
               "ORDER BY id ASC")
        rows = conn.execute(sql, (asin, domain)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["variations"] = json.loads(d.get("variations") or "[]")
        d["bullets"] = json.loads(d.get("bullets") or "[]")
        d["home_reviews"] = json.loads(d.get("home_reviews") or "{}")
        out.append(d)
    if limit:
        out = out[-limit:]
    return out


def latest_snapshot(path: Path, asin: str, domain: str) -> dict | None:
    snaps = snapshots_for(path, asin, domain, limit=1)
    return snaps[-1] if snaps else None


def count_snapshots(path: Path) -> int:
    with _connect(path) as conn:
        return conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]


# ---------- anomalies ----------

def insert_anomaly(path: Path, a: dict) -> int:
    with _connect(path) as conn:
        cur = conn.execute("""
            INSERT INTO anomalies (asin, domain, metric, change_type, old_value,
                new_value, severity, checked_at, notified, confirmed)
            VALUES (:asin, :domain, :metric, :change_type, :old_value, :new_value,
                    :severity, :checked_at, :notified, :confirmed)
            """, {
                "asin": a["asin"], "domain": a["domain"],
                "metric": a.get("metric", ""),
                "change_type": a.get("change_type", ""),
                "old_value": str(a.get("old_value", "")),
                "new_value": str(a.get("new_value", "")),
                "severity": a.get("severity", "info"),
                "checked_at": a.get("checked_at", ""),
                "notified": int(a.get("notified", 0)),
                "confirmed": int(a.get("confirmed", 0)),
            })
        return cur.lastrowid


def list_anomalies(path: Path, asin: str | None = None,
                   domain: str | None = None, limit: int = 200) -> list[dict]:
    with _connect(path) as conn:
        conn.row_factory = sqlite3.Row
        sql = "SELECT * FROM anomalies"
        cond, params = [], []
        if asin:
            cond.append("asin=?")
            params.append(asin)
        if domain:
            cond.append("domain=?")
            params.append(domain)
        if cond:
            sql += " WHERE " + " AND ".join(cond)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def confirm_anomaly(path: Path, anomaly_id: int) -> None:
    """用户确认是一条异常 → 打 confirmed 标记(去重依赖 unseen)。
    基线前移在 rules.set_baseline 里做,这里是给 anomaly 主体打标记。"""
    with _connect(path) as conn:
        conn.execute("UPDATE anomalies SET confirmed=1 WHERE id=?",
                     (anomaly_id,))


def unconfirmed_anomalies(path: Path, limit: int = 500) -> list[dict]:
    """尚未确认的异常(看板"异常优先"区 + 通知去重的主体)。"""
    with _connect(path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            "SELECT * FROM anomalies WHERE confirmed=0 ORDER BY id DESC LIMIT ?",
            (limit,)).fetchall()]
