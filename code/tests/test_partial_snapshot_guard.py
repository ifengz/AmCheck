"""首加链接第一轮采集落在风控页(残缺快照)的回归测试。

现象:新加的链接第一轮 cron 采集经常落在 Amazon "Continue shopping"
风控页,标题之外什么都抓不到。旧逻辑照样把这条空壳拍设成基线,
第二轮抓到真数据 → 价格/标题/BuyBox/状态全线"有更新"+ 一堆假异常。

新逻辑的契约:
1. 残缺快照只入库留档,不设基线、不做异常对比、不回写型号/变体;
2. 第二轮真数据成为基线,看板干净(0 异常、矩阵全"基准");
3. 库里已有脏基线(修复前加的数据)时,下一轮可用采集自动前移基线
   并清掉该链接的未确认假异常;
4. 只有残缺拍的链接仍算「已添加未采集」,看板取最近一条可用快照。
"""

import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE_DIR))

from monitor import store
from monitor.model import SnapshotRecord
from monitor.pipeline import run_round
from monitor.rules import snapshot_usable


def _full_snap(asin, domain, price=19.99, buybox="Amazon.com", title="T"):
    return SnapshotRecord(asin=asin, domain=domain,
                          checked_at="2026-09-14 10:00:00",
                          title=title, price=f"${price:.2f}", price_value=price,
                          buybox=buybox, rating=4.4, review_count=100)


def _gate_snap(asin, domain):
    """风控页形态:只有标题,价格/评分/BuyBox 全空。"""
    return SnapshotRecord(asin=asin, domain=domain,
                          checked_at="2026-09-14 09:00:00",
                          title="Amazon")


class ScriptedAdapter:
    """按脚本逐拍返回快照;脚本用完重复最后一拍。"""

    def __init__(self, snaps):
        self.snaps = list(snaps)
        self.i = 0

    def fetch(self, asin, domain, url=""):
        s = self.snaps[min(self.i, len(self.snaps) - 1)]
        self.i += 1
        return s


class SnapshotUsableTests(unittest.TestCase):
    def test_gate_page_shell_is_not_usable(self):
        self.assertFalse(snapshot_usable(_gate_snap("B0X", "amazon.com").to_dict()))

    def test_price_or_rating_makes_it_usable(self):
        self.assertTrue(snapshot_usable(_full_snap("B0X", "amazon.com").to_dict()))
        d = _gate_snap("B0X", "amazon.com").to_dict()
        d["rating"] = 4.2
        self.assertTrue(snapshot_usable(d))

    def test_zero_price_deleted_page_is_usable(self):
        """下架/删除拍字段全 0 也是有效观测(上下架规则要用)。"""
        d = _gate_snap("B0X", "amazon.com").to_dict()
        d["status"] = "deleted"
        self.assertTrue(snapshot_usable(d))
        d["status"] = "alive"
        d["price_value"] = 0.0
        self.assertTrue(snapshot_usable(d), "0 价也是'抓到了',别用真值判断")

    def test_none_is_not_usable(self):
        self.assertFalse(snapshot_usable(None))


class FirstRoundGateTests(unittest.TestCase):
    """核心场景:第一轮风控页 → 第二轮真数据。"""

    def setUp(self):
        self.db = Path(tempfile.mkdtemp()) / "monitor.db"
        store.init_db(self.db)
        store.upsert_profile(self.db, {
            "asin": "B0GATE0001", "domain": "amazon.com",
            "url": "https://www.amazon.com/dp/B0GATE0001"})

    def _anomalies(self):
        with sqlite3.connect(self.db) as conn:
            return conn.execute("SELECT metric, severity FROM anomalies").fetchall()

    def _baseline_snap(self):
        p = store.get_profile(self.db, "B0GATE0001", "amazon.com")
        if not p or not p.get("baseline_snapshot_id"):
            return None
        for s in store.snapshots_for(self.db, "B0GATE0001", "amazon.com"):
            if s["id"] == p["baseline_snapshot_id"]:
                return s
        return None

    def test_partial_round_sets_no_baseline_and_no_anomalies(self):
        r = run_round(self.db, ScriptedAdapter([_gate_snap("B0GATE0001", "amazon.com")]))
        self.assertEqual(r["checked"], 1)
        self.assertEqual(r["anomalies"], 0)
        self.assertIsNone(self._baseline_snap(),
                          "残缺拍不能当基线,否则下一轮全线假变化")

    def test_second_real_round_becomes_clean_baseline(self):
        run_round(self.db, ScriptedAdapter([_gate_snap("B0GATE0001", "amazon.com")]))
        r = run_round(self.db, ScriptedAdapter(
            [_full_snap("B0GATE0001", "amazon.com")]))
        self.assertEqual(r["anomalies"], 0,
                         "第一轮残缺不该让第二轮报'丢失 BuyBox/价格变化'")
        base = self._baseline_snap()
        self.assertIsNotNone(base)
        self.assertEqual(base["price_value"], 19.99, "基线必须是真数据那条")

    def test_partial_snapshots_never_enter_the_compare_chain(self):
        """可用→残缺→可用:残缺拍两头都不能当 prev/基线对比对象。"""
        a = ScriptedAdapter([_full_snap("B0GATE0001", "amazon.com", price=19.99)])
        run_round(self.db, a)
        run_round(self.db, ScriptedAdapter([_gate_snap("B0GATE0001", "amazon.com")]))
        r = run_round(self.db, ScriptedAdapter(
            [_full_snap("B0GATE0001", "amazon.com", price=19.99)]))
        self.assertEqual(r["anomalies"], 0,
                         "价格其实没变,残缺拍混进对比链就会假报")

    def test_dirty_baseline_self_heals_on_next_good_round(self):
        """修复前入库的脏基线:下一轮可用采集自动前移 + 清掉未确认假异常。"""
        gate_id = store.insert_snapshot(
            self.db, _gate_snap("B0GATE0001", "amazon.com"))
        store.set_baseline(self.db, "B0GATE0001", "amazon.com", gate_id)
        store.insert_anomaly(self.db, {
            "asin": "B0GATE0001", "domain": "amazon.com", "metric": "buybox",
            "change_type": "buybox_changed", "old_value": "", "new_value": "",
            "severity": "critical", "checked_at": "2026-09-14 10:00:00"})
        with sqlite3.connect(self.db) as conn:
            conn.execute("INSERT INTO anomalies (asin, domain, metric, "
                         "confirmed) VALUES ('B0GATE0001', 'amazon.com', "
                         "'user_confirmed', 1)")   # 用户确认过的要留下

        run_round(self.db, ScriptedAdapter(
            [_full_snap("B0GATE0001", "amazon.com")]))
        base = self._baseline_snap()
        self.assertTrue(snapshot_usable(base), "脏基线应被前移到可用快照")
        self.assertEqual(self._anomalies(), [("user_confirmed", None)],
                         "未确认假异常清掉,确认过的留痕不动")

    def test_link_with_only_partial_shots_stays_untracked(self):
        """只有残缺拍 = 还没真正采到:留在「已添加未采集」,别从页面消失。"""
        from monitor import board
        run_round(self.db, ScriptedAdapter([_gate_snap("B0GATE0001", "amazon.com")]))
        pend = board.untracked_profiles(self.db)
        self.assertEqual([p["asin"] for p in pend], ["B0GATE0001"])
        data = board.get_board_data(self.db)
        self.assertEqual(data["total"], 0, "残缺拍不进看板")

        run_round(self.db, ScriptedAdapter(
            [_full_snap("B0GATE0001", "amazon.com")]))
        self.assertEqual(board.untracked_profiles(self.db), [])
        self.assertEqual(board.get_board_data(self.db)["total"], 1)


class FieldFlapTests(unittest.TestCase):
    """字段级抓漏抖动:「有值↔空」不算变化(假异常的第二个成因)。

    真实数据实测:同一链接的 buybox/price 在「有值↔空」之间反复跳
    (懒加载/A-B 版式/软风控只挡某个模块),把空当一个值去比,
    每跳一次报一次「丢失 BuyBox」。规则层与显示层同一口径。
    """

    def _prof(self):
        return {"asin": "B0FLAP0001", "domain": "amazon.com", "metric_config": {}}

    def test_value_to_blank_and_back_is_not_anomaly(self):
        from monitor.rules import detect_snapshot
        good = _full_snap("B0FLAP0001", "amazon.com").to_dict()
        miss = dict(good, buybox="", price="", price_value=None)
        # 有值→空(抓漏)、空→有值(抓回来了)都不报
        self.assertEqual(detect_snapshot(self._prof(), good, good, miss), [])
        self.assertEqual(detect_snapshot(self._prof(), good, miss, good), [])

    def test_real_buybox_handover_still_reports(self):
        """真易主(卖家A→卖家B,两头都有值)必须照报,别把功能修丢。"""
        from monitor.rules import detect_snapshot
        a = _full_snap("B0FLAP0001", "amazon.com", buybox="SellerA").to_dict()
        b = _full_snap("B0FLAP0001", "amazon.com", buybox="SellerB").to_dict()
        det = detect_snapshot(self._prof(), a, a, b)
        self.assertEqual([d["metric"] for d in det], ["buybox"])

    def test_matrix_cell_skips_blank_side(self):
        """显示层同口径:矩阵格「空→有值」不亮「有更新」。"""
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
        from test_login_thread import _load_ui_module
        ns = _load_ui_module()
        good = _full_snap("B0X", "amazon.com").to_dict()
        miss = dict(good, buybox="")
        getter, fmt = (lambda s: s.get("buybox")), (lambda v: v or "无")
        self.assertFalse(ns["_field_changed"](getter, fmt, good, miss))
        self.assertFalse(ns["_field_changed"](getter, fmt, miss, good))
        other = dict(good, buybox="SellerB")
        self.assertTrue(ns["_field_changed"](getter, fmt, other, good),
                        "两头有值的真变化还得报")


if __name__ == "__main__":
    unittest.main()
