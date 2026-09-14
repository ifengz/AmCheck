"""评价链接台账(review_db)与每日分散排期(review_track)的回归测试。

覆盖需求:
- 直接粘链接检测 → 自动进台账(业务字段为空);
- 之后表格上传到同一条评价链接 → **只回填空字段,不覆盖已有值**,重复导入幂等;
- 连续 5 次判定「已删(变狗)」→ 自动停止跟踪,中途出现正常则计数清零;
- 全部链接每天查一次,但彼此在 24 小时里错开(防风控)。

全部在临时库上跑,不碰 code/history.db、code/monitor.db。
"""

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE_DIR))

import review_db  # noqa: E402
import review_track  # noqa: E402
from monitor import store as monitor_store  # noqa: E402

URL = "https://www.amazon.in/gp/customer-reviews/{}/"


def _hist(rid, status="alive", at="2026-09-01 10:00:00"):
    return {"review_id": rid, "domain": "amazon.in", "url": URL.format(rid),
            "status": status, "stars": "5", "title": "t", "author": "a",
            "review_date": "2026-08-01", "note": "", "checked_at": at}


def _meta_row(rid, ref="", no="", model="", at="2026-09-14 12:00:00"):
    return {"review_id": rid, "domain": "amazon.in", "url": URL.format(rid),
            "order_ref": ref, "order_no": no, "model": model, "created_at": at}


class _TempDbCase(unittest.TestCase):
    """把 review_db.DB 指向临时库;monitor.db 的设置表也换成临时的。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig_db = review_db.DB
        review_db.DB = self.tmp / "history.db"
        review_db.init_db()
        self.settings_db = self.tmp / "settings.db"
        monitor_store.init_db(self.settings_db)
        self.addCleanup(self._restore)

    def _restore(self):
        review_db.DB = self._orig_db


class MetaLedgerTests(_TempDbCase):
    def test_link_check_creates_ledger_row_with_empty_business_fields(self):
        review_db.save_history([_hist("RAAA")])
        m = review_db.get_meta("RAAA")
        self.assertIsNotNone(m)
        self.assertEqual((m["order_ref"], m["order_no"], m["model"]), ("", "", ""))
        self.assertEqual(m["source"], "link")
        self.assertEqual(m["track_enabled"], 1)

    def test_same_link_checked_twice_keeps_one_ledger_row(self):
        review_db.save_history([_hist("RAAA", at="2026-09-01 10:00:00")])
        review_db.save_history([_hist("RAAA", at="2026-09-02 10:00:00")])
        self.assertEqual(len(review_db.all_meta()), 1)
        self.assertEqual(len(review_db.review_history_timeline("RAAA")), 2)

    def test_table_import_backfills_empty_fields_only(self):
        """直接粘链接 → 表格补上业务字段(需求:空值用表格数据补充)。"""
        review_db.save_history([_hist("RAAA")])
        stat = review_db.upsert_review_meta(
            [_meta_row("RAAA", ref="20321", no="403-6215176", model="风扇-KF")])
        self.assertEqual(stat, {"added": 0, "filled": 1, "unchanged": 0})
        m = review_db.get_meta("RAAA")
        self.assertEqual((m["order_ref"], m["order_no"], m["model"]),
                         ("20321", "403-6215176", "风扇-KF"))

    def test_reimport_is_idempotent(self):
        rows = [_meta_row("RAAA", ref="20321")]
        self.assertEqual(review_db.upsert_review_meta(rows)["added"], 1)
        self.assertEqual(review_db.upsert_review_meta(rows)["unchanged"], 1)
        self.assertEqual(review_db.upsert_review_meta(rows)["added"], 0)

    def test_existing_business_value_is_not_overwritten(self):
        """台账里已有手工填的值时,表格不能冲掉它(只填空字段)。"""
        review_db.upsert_review_meta([_meta_row("RAAA", ref="手工值")])
        review_db.upsert_review_meta([_meta_row("RAAA", ref="表格值",
                                                model="表格型号")])
        m = review_db.get_meta("RAAA")
        self.assertEqual(m["order_ref"], "手工值")     # 已有值保住
        self.assertEqual(m["model"], "表格型号")       # 空字段被补上

    def test_recent_history_includes_pending_imported_rows(self):
        """刚导入、还没检测过的链接也要出现在检测历史里(状态空 → 待检测)。"""
        review_db.upsert_review_meta([_meta_row("RNEW", ref="9")])
        review_db.save_history([_hist("ROLD")])
        by_id = {r["review_id"]: r for r in review_db.recent_history(limit=50)}
        self.assertIn("RNEW", by_id)
        self.assertEqual(by_id["RNEW"]["status"], "")
        self.assertEqual(by_id["RNEW"]["pending"], 1)
        self.assertEqual(by_id["RNEW"]["order_ref"], "9")
        self.assertEqual(by_id["ROLD"]["pending"], 0)


class TrackStateTests(_TempDbCase):
    def test_five_consecutive_deleted_stops_tracking(self):
        review_db.upsert_review_meta([_meta_row("RAAA")])
        for i in range(review_db.STOP_STREAK - 1):
            st = review_db.update_track_state("RAAA", "deleted",
                                              f"2026-09-{10 + i:02d} 08:00:00")
            self.assertFalse(st["stopped"], f"第 {i + 1} 次不该停")
        self.assertEqual(review_db.get_meta("RAAA")["track_enabled"], 1)

        st = review_db.update_track_state("RAAA", "deleted", "2026-09-15 08:00:00")
        self.assertTrue(st["stopped"])
        m = review_db.get_meta("RAAA")
        self.assertEqual(m["track_enabled"], 0)
        self.assertIn("停止跟踪", m["stop_reason"])
        self.assertIn("RAAA", [r["review_id"] for r in review_db.list_stopped()])

    def test_alive_resets_the_deleted_streak(self):
        review_db.upsert_review_meta([_meta_row("RAAA")])
        for _ in range(4):
            review_db.update_track_state("RAAA", "deleted", "2026-09-10 08:00:00")
        self.assertEqual(review_db.get_meta("RAAA")["deleted_streak"], 4)
        review_db.update_track_state("RAAA", "alive", "2026-09-11 08:00:00")
        self.assertEqual(review_db.get_meta("RAAA")["deleted_streak"], 0)
        self.assertEqual(review_db.get_meta("RAAA")["track_enabled"], 1)

    def test_track_count_and_last_tracked_at_advance(self):
        review_db.upsert_review_meta([_meta_row("RAAA")])
        review_db.update_track_state("RAAA", "alive", "2026-09-10 08:00:00")
        review_db.update_track_state("RAAA", "alive", "2026-09-11 08:00:00")
        m = review_db.get_meta("RAAA")
        self.assertEqual(m["track_count"], 2)
        self.assertEqual(m["last_tracked_at"], "2026-09-11 08:00:00")

    def test_manual_stop_and_resume(self):
        review_db.upsert_review_meta([_meta_row("RAAA")])
        review_db.set_track_enabled("RAAA", False)
        self.assertEqual(review_db.get_meta("RAAA")["track_enabled"], 0)
        review_db.update_track_state("RAAA", "deleted", "2026-09-10 08:00:00")
        review_db.set_track_enabled("RAAA", True)      # 恢复时清零计数
        m = review_db.get_meta("RAAA")
        self.assertEqual(m["track_enabled"], 1)
        self.assertEqual(m["deleted_streak"], 0)
        self.assertEqual(m["stop_reason"], "")


class DailySpreadTests(_TempDbCase):
    def test_slots_are_spread_over_the_whole_day(self):
        """N 条链接要铺满 24 小时,不能挤在一起。"""
        ids = [f"R{i:04d}" for i in range(8)]
        slots = review_db.daily_slots(ids)
        self.assertEqual(len(set(slots.values())), 8)
        ordered = sorted(slots.values())
        self.assertTrue(all(0 <= v < 86400 for v in ordered))
        self.assertGreater(ordered[0], 0)          # 不是全部堆在 0 点
        self.assertLess(ordered[-1], 86400)
        # 相邻间隔接近 24h/8 = 3h,放宽到 1~6 小时
        gaps = [b - a for a, b in zip(ordered, ordered[1:])]
        self.assertTrue(all(3600 <= g <= 6 * 3600 for g in gaps), gaps)

    def test_slots_are_stable_for_the_same_set(self):
        ids = [f"R{i:04d}" for i in range(5)]
        self.assertEqual(review_db.daily_slots(ids), review_db.daily_slots(ids))

    def test_one_link_lands_midday(self):
        slots = review_db.daily_slots(["RSOLO"])
        self.assertTrue(0 < slots["RSOLO"] < 86400)

    def test_no_ids(self):
        self.assertEqual(review_db.daily_slots([]), {})


class DueLinkTests(_TempDbCase):
    """后台线程每一轮该挑谁:当日到点 + 今天还没查过。"""

    def setUp(self):
        super().setUp()
        self.ids = [f"R{i:04d}" for i in range(3)]
        review_db.upsert_review_meta([_meta_row(r) for r in self.ids])
        self.slots = review_db.daily_slots(self.ids)

    def _now_at_sec(self, sec, day=14):
        return datetime(2026, 9, day, sec // 3600, (sec % 3600) // 60, sec % 60)

    def test_nothing_due_at_midnight(self):
        due = review_track.due_links(self.tmp / "history.db", self.settings_db,
                                     now=self._now_at_sec(0))
        self.assertEqual(due, [])

    def test_all_due_after_the_last_slot(self):
        latest = max(self.slots.values())
        due = review_track.due_links(self.tmp / "history.db", self.settings_db,
                                     now=self._now_at_sec(min(latest + 1, 86399)))
        self.assertEqual(len(due), 3)
        # 必须按当日排期先后返回
        got = [d["review_id"] for d in due]
        self.assertEqual(got, sorted(got, key=lambda r: self.slots[r]))

    def test_already_checked_today_is_skipped(self):
        latest = max(self.slots.values())
        now = self._now_at_sec(min(latest + 1, 86399))
        for r in self.ids:
            review_db.update_track_state(r, "alive", now.strftime("%F %T"))
        self.assertEqual(
            review_track.due_links(self.tmp / "history.db", self.settings_db, now=now),
            [])

    def test_checked_yesterday_is_due_again_today(self):
        latest = max(self.slots.values())
        now = self._now_at_sec(min(latest + 1, 86399))
        for r in self.ids:
            review_db.update_track_state(r, "alive", "2026-09-13 08:00:00")
        self.assertEqual(
            len(review_track.due_links(self.tmp / "history.db", self.settings_db,
                                       now=now)), 3)

    def test_stopped_links_are_not_picked(self):
        for r in self.ids:
            review_db.set_track_enabled(r, False)
        self.assertEqual(review_db.list_tracked(), [])
        latest = max(self.slots.values())
        self.assertEqual(
            review_track.due_links(self.tmp / "history.db", self.settings_db,
                                   now=self._now_at_sec(min(latest + 1, 86399))),
            [])

    def test_batch_size_from_settings(self):
        cfg = review_track.get_cfg(self.settings_db)
        self.assertTrue(cfg["enabled"])            # 默认开启
        self.assertEqual(cfg["batch"], 3)          # 默认每轮 3 条
        monitor_store.set_setting(self.settings_db, "review_track_batch", "7")
        self.assertEqual(review_track.get_cfg(self.settings_db)["batch"], 7)
        monitor_store.set_setting(self.settings_db, "review_track_batch", "999")
        self.assertEqual(review_track.get_cfg(self.settings_db)["batch"], 20)  # 上限
        monitor_store.set_setting(self.settings_db, "review_track_enabled", "0")
        self.assertFalse(review_track.get_cfg(self.settings_db)["enabled"])


if __name__ == "__main__":
    unittest.main()
