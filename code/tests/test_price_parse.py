"""价格解析回归(2026-09-15 生产假「价格异常」的根治)。

生产事故回放:US 站抓到欧式小数逗号 "105,99",旧解析把逗号当千分位
剥掉 → 存成 10599;还有 ".a-price" 整块 inner_text 把小数点挤丢的
残渣 "US$49" + "97" → 4997。脏值进了基线,此后每轮真价 $105.99
都 vs 基线 $10599 = 假「🚨 价格」告警 + 假钉钉推送。

覆盖 6 个上线站点(US/IN/AU/BR/MX/JP)的真实页价格格式:
- US/AU: $1,059.99 / A$105.99      点=小数,逗号=千分位
- IN:    ₹1,05,999.00(印度分组) / ₹105.99
- JP:    ¥10,599(千分位逗号,无小数)
- MX:    $1,299.00(美系) 和 $1.299,00(欧式)都出现在实测页
- BR:    R$ 105,99 / R$ 1.299,90    逗号=小数,点=千分位
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor.address import _extract_price, _parse_price  # noqa: E402


class NumValueTests(unittest.TestCase):
    """各站点文本 → 正确数值。"""

    def test_us_dot_decimal(self):
        _, val, cur = _parse_price("US$49.97", "amazon.com")
        self.assertEqual(val, 49.97)
        self.assertEqual(cur, "US$")

    def test_us_thousands(self):
        _, val, _ = _parse_price("$1,059.99", "amazon.com")
        self.assertEqual(val, 1059.99)
        _, val, _ = _parse_price("$10,599", "amazon.com")   # 整万价,逗号千分位
        self.assertEqual(val, 10599)

    def test_euro_comma_decimal(self):
        """生产事故原型:"105,99" 曾解析成 10599。"""
        _, val, cur = _parse_price("R$ 105,99", "amazon.com.br")
        self.assertEqual(val, 105.99)
        self.assertEqual(cur, "R$")
        _, val, _ = _parse_price("$1.299,00", "amazon.com.mx")
        self.assertEqual(val, 1299.00)

    def test_in_grouping(self):
        """印度式分组 1,05,999.00 → 105999。"""
        _, val, _ = _parse_price("₹1,05,999.00", "amazon.in")
        self.assertEqual(val, 105999.0)
        _, val, _ = _parse_price("₹1,05,999", "amazon.in")   # 无小数的分组
        self.assertEqual(val, 105999)

    def test_jp_yen_no_decimal(self):
        _, val, cur = _parse_price("¥10,599", "amazon.co.jp")
        self.assertEqual(val, 10599)
        self.assertEqual(cur, "¥")
        _, val, _ = _parse_price("¥500", "amazon.co.jp")
        self.assertEqual(val, 500)

    def test_bare_currency_fallback_by_domain(self):
        """符号全丢 → 按站点兜底,不再硬编码 ₹。"""
        _, val, cur = _parse_price("105.99", "amazon.com")
        self.assertEqual((val, cur), (105.99, "$"))
        _, _, cur = _parse_price("105.99", "amazon.com.au")
        self.assertEqual(cur, "A$")
        _, _, cur = _parse_price("105.99", "amazon.in")
        self.assertEqual(cur, "₹")

    def test_unparseable(self):
        _, val, _ = _parse_price("此商品暂无库存", "amazon.com")
        self.assertIsNone(val)


class ExtractPreferCleanTests(unittest.TestCase):
    """多候选时挑「数字[.,]数字」的完整价,不拿第一个。"""

    class _El:
        def __init__(self, text):
            self._t = text

        def inner_text(self):
            return self._t

    class _Page:
        def __init__(self, mapping):
            self._m = mapping

        def query_selector_all(self, sel):
            return [ExtractPreferCleanTests._El(t)
                    for t in self._m.get(sel, [])]

    def test_prefers_dotted_over_concatenated(self):
        # 首位选择器只有残渣 "US$4997"(whole+fraction 挤一起),
        # 后面的 .a-price .a-offscreen 才有完整 "US$49.97"
        page = self._Page({
            '#corePrice_feature_div .a-offscreen': ["US$4997"],
            '.a-price .a-offscreen': ["US$49.97"],
        })
        text, val, _ = _extract_price(page, "amazon.com")
        self.assertEqual(val, 49.97)
        self.assertIn("49.97", text)

    def test_all_bare_still_returns_first(self):
        # 全站无小数点(JP ¥500 合法):退回第一个候选,别丢数
        page = self._Page({
            '#corePrice_feature_div .a-offscreen': ["¥500"],
        })
        _, val, cur = _extract_price(page, "amazon.co.jp")
        self.assertEqual(val, 500)
        self.assertEqual(cur, "¥")

    def test_empty_page(self):
        _, val, cur = _extract_price(self._Page({}), "amazon.com")
        self.assertEqual((val, cur), (None, ""))


if __name__ == "__main__":
    unittest.main()
