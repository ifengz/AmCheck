"""检测历史页展示契约的回归测试。

用户明确要求过的几件事,一旦被顺手改掉就会「悄悄回退」,
所以用测试钉死:

1. 刷单编号 / 订单号 / 国家 / 跟踪 四列固定展示,不被裁剪
   —— 固定 + suppressSizeToFit,且订单号宽度要放得下 19 字符;
2. agGrid 必须关掉 sizeColumnsToFit,否则非固定列会被压成省略号;
3. 搜索索引要覆盖 Review ID / 刷单编号 / 订单号 / 产品型号;
4. 抽屉里的表格要自适应拉伸到底部(用 flex 撑,不是写死高度);
5. 抽屉按钮文案:打开评价页面 / 复制评价链接。
"""

import inspect
import sys
import unittest
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE_DIR))

UI_PY = CODE_DIR / "ui.py"

_NS = None


def _load_ui_module():
    """exec ui.py(截到 ui.run 之前),拿到里面的函数。

    与 test_background_startup.py 同一套做法:ui.py 是脚本式入口,
    测试要复用它的函数只能 exec 源码。
    """
    global _NS
    if _NS is None:
        src = UI_PY.read_text(encoding="utf-8")
        src = src[:src.index("\nui.run(")]
        ns = {"__name__": "ui_under_test", "__file__": str(UI_PY)}
        exec(compile(src, str(UI_PY), "exec"), ns)
        _NS = ns
    return _NS


def _by_field(cols):
    return {c["field"]: c for c in cols}


class HistoryColumnTests(unittest.TestCase):
    def setUp(self):
        self.cols = _load_ui_module()["history_columns"]()
        self.by = _by_field(self.cols)

    def test_required_columns_are_pinned(self):
        """刷单编号/订单号 左固定,国家/跟踪 右固定。"""
        for field in ("order_ref", "order_no"):
            self.assertEqual(self.by[field].get("pinned"), "left",
                             f"{field} 必须左固定,否则横滚时会被滚走/裁掉")
        for field in ("domain", "track"):
            self.assertEqual(self.by[field].get("pinned"), "right",
                             f"{field} 必须右固定,否则横滚时会被滚走/裁掉")

    def test_pinned_columns_suppress_size_to_fit(self):
        """固定列要显式拒绝 auto-size,否则仍可能被压窄。"""
        for field in ("order_ref", "order_no", "domain", "track"):
            self.assertTrue(self.by[field].get("suppressSizeToFit"),
                            f"{field} 必须 suppressSizeToFit,否则会被压缩")

    def test_pinned_columns_are_contiguous_at_both_ends(self):
        """agGrid 的硬约束:左固定必须从第 0 列起连续,右固定必须贴到最后一列。

        不满足的话固定分组会失效(列被渲染到错误的容器里)。
        """
        pins = [c.get("pinned") for c in self.cols]
        left = [i for i, p in enumerate(pins) if p == "left"]
        right = [i for i, p in enumerate(pins) if p == "right"]
        self.assertEqual(left, list(range(len(left))),
                         "左固定列必须是 columnDefs 开头的一段")
        self.assertEqual(right, list(range(len(pins) - len(right), len(pins))),
                         "右固定列必须是 columnDefs 结尾的一段")

    def test_widths_fit_real_content(self):
        """宽度要按真实内容给:刷单编号 6 位数字、订单号 19 字符。"""
        self.assertGreaterEqual(self.by["order_ref"]["width"], 90,
                                "刷单编号列要放得下 6 位数字")
        self.assertGreaterEqual(self.by["order_no"]["width"], 186,
                                "订单号形如 403-6215176-3035513(19 字符),窄了会出省略号")

    def test_all_fields_present(self):
        for field in ("checked", "review_id", "order_ref", "order_no", "model",
                      "link", "domain", "status_text", "track", "stars",
                      "title", "author", "review_date", "note"):
            self.assertIn(field, self.by, f"缺列 {field}")


class HistoryGridOptionTests(unittest.TestCase):
    def test_auto_size_columns_disabled(self):
        """关掉 sizeColumnsToFit,列才保持声明宽度、放不下就横向滚动。

        开着的时候 nicegui 会调 api.sizeColumnsToFit(),把可滚动区的列
        按剩余宽度硬压(实测 36px,表头全是省略号)—— 这正是用户反馈的
        「被裁剪」。
        """
        src = inspect.getsource(_load_ui_module()["page_history"])
        self.assertIn("auto_size_columns=False", src,
                      "历史页 agGrid 必须关掉 auto_size_columns")

    def test_html_columns_point_at_html_fields(self):
        """html_columns 的索引必须落在 链接/状态/星级 三列上。

        列序调整过(加了固定列),索引很容易忘记同步 —— 一旦错位,
        单元格会显示成原始 HTML 字符串。
        """
        ns = _load_ui_module()
        cols = ns["history_columns"]()
        src = inspect.getsource(ns["page_history"])
        marker = "html_columns=["
        start = src.index(marker) + len(marker)
        idxs = [int(x) for x in src[start:src.index("]", start)].split(",")]
        self.assertEqual([cols[i]["field"] for i in idxs],
                         ["link", "status_text", "stars"])


class HistorySearchTests(unittest.TestCase):
    def setUp(self):
        self.f = _load_ui_module()["history_search_index"]
        self.row = {
            "review_id": "RYODA2Q3GE0DU",
            "url": "https://www.amazon.in/gp/customer-reviews/RYODA2Q3GE0DU/",
            "order_ref": "20321",
            "order_no": "403-6215176-3035513",
            "model": "风扇2023-KF-LY700",
            "title": "很好用",
            "author": "张三",
            "note": "五星好评",
        }

    def test_indexes_the_four_required_fields(self):
        hay = self.f(self.row)
        for kw in ("ryoda2q3ge0du",      # Review ID
                   "20321",              # 刷单编号
                   "403-6215176-3035513",  # 订单号
                   "风扇2023-kf-ly700"):   # 产品型号
            self.assertIn(kw.lower(), hay, f"搜索索引缺 {kw}")

    def test_case_insensitive(self):
        self.assertIn("kf-ly700", self.f(self.row))

    def test_tolerates_missing_business_fields(self):
        """直接粘链接进来的记录没有业务字段,不能炸。"""
        hay = self.f({"review_id": "R1", "url": "", "order_ref": None,
                      "order_no": None, "model": None,
                      "title": "", "author": "", "note": ""})
        self.assertIn("r1", hay)

    def test_url_override_used(self):
        """url 为空时页面会补兜底链接,补进去的值也要进索引(带出域名)。"""
        row = dict(self.row, url="")
        self.assertIn("amazon.in", self.f(row, "https://www.amazon.in/x"))


class DrawerTests(unittest.TestCase):
    def setUp(self):
        self.src = inspect.getsource(_load_ui_module()["fill_review_drawer"])

    def test_button_labels_renamed(self):
        self.assertIn("打开评价页面", self.src)
        self.assertIn("复制评价链接", self.src)
        self.assertNotIn("打开原页面", self.src)
        self.assertNotIn('"复制链接"', self.src)

    def test_timeline_grid_stretches_not_fixed_height(self):
        """抽屉里的跟踪历史表格靠 flex 吃满剩余高度,不能再写死 320px。"""
        self.assertIn("ag-drawer-fill", self.src)
        self.assertNotIn("height:320px", self.src)


if __name__ == "__main__":
    unittest.main()
