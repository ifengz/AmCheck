"""表格导入解析(review_import)的回归测试。

对应需求:「导入按钮 → 取 A 列刷单编号 / D 列产品型号 / F 列订单号 / M 列测评链接,
**没有评价链接的行不加进来**」。

全部用内存/临时文件构造样本,不依赖任何真实订单表。
"""

import sys
import tempfile
import unittest
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE_DIR))

import review_import  # noqa: E402

# 与业务方表格一致的 13 列(A~M)
HEAD = ["刷单编号", "营销费用编号", "营销费用类型", "产品型号", "ASIN",
        "订单号", "下单日期(当地)", "订单金额(当地币种)", "中介",
        "佣金/手续费", "汇率", "支付金额(RMB)", "评论URL"]

LINK_IN = "https://www.amazon.in/gp/customer-reviews/RK9C8GOCVBBSE?ref=x"
LINK_PORTAL = "https://www.amazon.in/portal/customer-reviews/srp/-/RYODA2Q3GE0DU/ref=cm_cr"


def _row(ref="20321", model="风扇2023-KF-LY700", order_no="403-6215176-3035513",
         url=LINK_IN):
    r = [""] * 13
    r[0], r[3], r[5], r[12] = ref, model, order_no, url
    return r


def _write_xlsx(path, data_rows, header=True):
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    if header:
        ws.append(HEAD)
    for r in data_rows:
        ws.append(r)
    wb.save(str(path))
    return path


def _write_xls(path, data_rows, header=True):
    """旧版 .xls(BIFF)。需要 xlwt,只在装了的时候用。"""
    import xlwt
    wb = xlwt.Workbook()
    ws = wb.add_sheet("Sheet1")
    if header:
        for c, h in enumerate(HEAD):
            ws.write(0, c, h)
    off = 1 if header else 0
    for i, r in enumerate(data_rows):
        for c, v in enumerate(r):
            if v not in ("", None):
                ws.write(i + off, c, v)
    wb.save(str(path))
    return path


class ParseOrderFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: None)

    def _p(self, name):
        return self.tmp / name

    def test_header_names_map_to_a_d_f_m(self):
        """表头名能对上时,取的是 A/D/F/M 四列的值。"""
        p = _write_xlsx(self._p("a.xlsx"), [
            _row(),                                        # 正常一条
            _row(ref="20318", model="IN33-4-3-Black", order_no="404-7353226",
                 url=""),                                  # 无链接 → 丢弃
        ])
        rows, stats = review_import.parse_order_file(p)
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["valid"], 1)
        self.assertEqual(stats["no_link"], 1)
        self.assertEqual(stats["header_row"], 1)
        self.assertEqual(rows[0], {
            "review_id": "RK9C8GOCVBBSE",
            "domain": "amazon.in",
            "url": "https://www.amazon.in/gp/customer-reviews/RK9C8GOCVBBSE/",
            "order_ref": "20321",
            "order_no": "403-6215176-3035513",
            "model": "风扇2023-KF-LY700",
        })

    def test_rows_without_link_are_dropped(self):
        """需求核心:没有评价链接的行一律不加进来。"""
        p = _write_xlsx(self._p("b.xlsx"), [
            _row(ref="1", url=""),
            _row(ref="2", url="None"),                  # 字面量 None
            _row(ref="3", url="https://www.amazon.in/"),  # 有域名但没有 review id
            _row(ref="4", url="https://www.example.com/gp/customer-reviews/RAAAAAAAAAAA/"),
            _row(ref="5", url=LINK_PORTAL),             # 唯一合法的一条
        ])
        rows, stats = review_import.parse_order_file(p)
        self.assertEqual(stats["valid"], 1)
        self.assertEqual(stats["no_link"], 4)
        self.assertEqual(rows[0]["order_ref"], "5")

    def test_duplicate_links_in_same_file_kept_once(self):
        p = _write_xlsx(self._p("c.xlsx"), [
            _row(ref="1"), _row(ref="2"),               # 同一条评价链接出现两次
            _row(ref="3", url=LINK_PORTAL),
        ])
        rows, stats = review_import.parse_order_file(p)
        self.assertEqual(stats["valid"], 2)
        self.assertEqual(stats["dup"], 1)

    def test_portal_and_short_link_forms_are_accepted(self):
        p = _write_xlsx(self._p("d.xlsx"), [
            _row(ref="1", url=LINK_PORTAL),
            _row(ref="2", url="https://www.amazon.in/review/R1J8FCECAW9CZ2/ref=x"),
        ])
        rows, stats = review_import.parse_order_file(p)
        self.assertEqual(stats["valid"], 2)
        self.assertEqual({r["review_id"] for r in rows},
                         {"RYODA2Q3GE0DU", "R1J8FCECAW9CZ2"})

    def test_falls_back_to_a_d_f_m_when_no_header(self):
        """没有表头 → 退回固定列位 A/D/F/M,且第 1 行当数据(header_row=0)。"""
        p = _write_xlsx(self._p("e.xlsx"), [
            _row(), _row(ref="20318", url=""),
        ], header=False)
        rows, stats = review_import.parse_order_file(p)
        self.assertEqual(stats["header_row"], 0)
        self.assertEqual(stats["valid"], 1)
        self.assertEqual(rows[0]["order_ref"], "20321")
        self.assertEqual(rows[0]["model"], "风扇2023-KF-LY700")

    def test_numeric_cells_do_not_get_float_tail(self):
        """Excel 里数字型编号会读成 float,不能变成 20321.0。"""
        p = _write_xlsx(self._p("f.xlsx"), [_row(ref=20321)])
        rows, _ = review_import.parse_order_file(p)
        self.assertEqual(rows[0]["order_ref"], "20321")

    def test_blank_rows_are_ignored(self):
        p = _write_xlsx(self._p("g.xlsx"), [[""] * 13, _row(), [""] * 13])
        _, stats = review_import.parse_order_file(p)
        self.assertEqual(stats["total"], 1)

    def test_accepts_bytes_and_file_objects(self):
        """上传组件给的是 bytes,命令行给的是路径,两条入口都要能用。"""
        p = _write_xlsx(self._p("h.xlsx"), [_row()])
        raw = Path(p).read_bytes()
        for src in (raw, bytearray(raw), Path(p)):
            rows, stats = review_import.parse_order_file(src)
            self.assertEqual(stats["valid"], 1, f"source={type(src)}")
            self.assertEqual(rows[0]["review_id"], "RK9C8GOCVBBSE")

    def test_non_excel_input_raises_readable_error(self):
        with self.assertRaises(ValueError) as ctx:
            review_import.parse_order_file(b"this is not a spreadsheet")
        self.assertIn("Excel", str(ctx.exception))

    def test_summarize_text(self):
        _, stats = review_import.parse_order_file(
            _write_xlsx(self._p("i.xlsx"), [_row(), _row(ref="2", url="")]))
        text = review_import.summarize(stats)
        self.assertIn("共 2 行", text)
        self.assertIn("有效 1 条", text)


def _has(mod):
    try:
        __import__(mod)
        return True
    except ImportError:
        return False


class LegacyXlsTests(unittest.TestCase):
    """旧版 .xls 走 xlrd;没装 xlrd 时必须是可读的提示而不是崩栈。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    @unittest.skipUnless(_has("xlwt"), "需要 xlwt 生成 .xls 样本")
    def test_xls_is_read_via_xlrd(self):
        if not _has("xlrd"):
            self.skipTest("没装 xlrd")
        p = _write_xls(self.tmp / "old.xls", [_row(), _row(ref="2", url="")])
        rows, stats = review_import.parse_order_file(p)
        self.assertEqual(stats["valid"], 1)
        self.assertEqual(rows[0]["order_ref"], "20321")
        self.assertEqual(rows[0]["review_id"], "RK9C8GOCVBBSE")

    def test_xls_without_xlrd_gives_actionable_message(self):
        """.xls 魔数正确但 xlrd 缺失 → ValueError 里要告诉用户另存为 .xlsx。"""
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *a, **kw):
            if name == "xlrd":
                raise ImportError("blocked for test")
            return real_import(name, *a, **kw)

        builtins.__import__ = fake_import
        try:
            with self.assertRaises(ValueError) as ctx:
                review_import.parse_order_file(
                    review_import.OLE2_MAGIC + b"\x00" * 64)
        finally:
            builtins.__import__ = real_import
        self.assertIn(".xlsx", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
