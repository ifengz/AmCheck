"""monitor.address —— 采集接口 + 适配器(采集层与判定层解耦的落点)。

对应架构文档 doc/06 §5。核心:采集做成接口(fetch() 返回 SnapshotRecord),
具体来源可替换。

阶段 1-3 用 MockAdapter(造历史,不碰真实抓取);阶段 4 换 PlaywrightAdapter
自研解析 Amazon 商品页(BuyBox/变体/首页差评)。接口签名不变,上层无感。
"""

from __future__ import annotations

import re
import time
from abc import ABC, abstractmethod

from .model import SnapshotRecord


def now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class BaseAdapter(ABC):
    """采集适配器接口:给一个 ASIN,返回一次结构化的 SnapshotRecord。"""

    @abstractmethod
    def fetch(self, asin: str, domain: str, url: str = "") -> SnapshotRecord:
        """抓取一次该 ASIN 当前状态,返回结构化快照。"""

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class MockAdapter(BaseAdapter):
    """演示适配器:按预置脚本返回该 ASIN 某一"拍"的快照。

    阶段 1-3 专用。seed_demo() 不用它逐拍推进,而是直接构造整条历史;
    此适配器保留给"点一次跑一轮"的交互(每次 fetch 返回能触发异常的一拍)。
    """

    DEFAULT = {
        "B0TRACK0001": ("amazon.com", 29.99, "$"),
        "B0TRACK0002": ("amazon.in", 799.0, "₹"),
        "B0TRACK0003": ("amazon.com.au", 39.0, "A$"),
        "B0TRACK0004": ("amazon.co.jp", 2980.0, "¥"),
        "B0TRACK0005": ("amazon.com.mx", 599.0, "MX$"),
        "B0TRACK0006": ("amazon.com.br", 89.9, "R$"),
    }

    def __init__(self, step: int = 1):
        """step: 返回制造哪类状态的一拍。0=基线,1=价格-12%,2=丢BuyBox,
        3=新增差评,4=回到基线,>=5=上下架删除。"""
        self._step = step

    def fetch(self, asin: str, domain: str, url: str = "") -> SnapshotRecord:
        return snapshot_for_step(asin, domain, self._step)


def snapshot_for_step(asin: str, domain: str, url: str = "",
                      step: int = 0) -> SnapshotRecord:
    """构造某个 ASIN 某一"拍"的快照(seed 与 MockAdapter 共用一份脚本)。"""
    dom, base_price, currency = MockAdapter.DEFAULT.get(asin, (domain, 19.99, "$"))
    title = f"Demo Product {asin}"
    price, rating, rc, buybox, avail, deal = (
        base_price, 4.4, 12847, "Amazon.com", "In Stock", "")
    status, home_bad = "alive", 0

    if step >= 6:
        status, avail, price, rating, rc = "unavailable", "Currently unavailable", 0.0, 0.0, 0
    elif step >= 5:
        status, avail, price, rating, rc = "deleted", "", 0.0, 0.0, 0
    elif step == 4:
        pass                                   # 回到基线
    elif step == 3:
        home_bad = 5                           # 新增差评
    elif step == 2:
        buybox = ""                            # 丢 BuyBox
    elif step == 1:
        price = round(base_price * 0.88, 2)    # 价格 -12%

    return SnapshotRecord(
        asin=asin, domain=domain,
        checked_at=now_str(),
        title=title, buybox=buybox, variations=[],
        price=f"{currency}{price:,.2f}" if price else "",
        price_value=price or None, currency=currency,
        rating=rating or None, review_count=rc or None,
        bsr=1000 + int(abs(hash(asin)) % 90000) if status == "alive" else None,
        deal_tag=deal, availability=avail, status=status,
        home_reviews={"recent_bad": home_bad, "stars_breakdown": {}},
        note="",
    )


class PlaywrightAdapter(BaseAdapter):
    """真实采集适配器:用已登录的持久化浏览器档案抓 Amazon 商品页。

    对应架构文档 doc/06 §5:阶段 1-3 用 MockAdapter;阶段 4 换本类自研解析
    Amazon 商品页(BuyBox/变体/首页差评)。接口签名与 MockAdapter 一致,上层无感。

    复用 engine.py 的两条核心经验:
    - 登录态存 ~/.amreview/profile/<域名>/,按域名开一个持久化 context(BuyBox 等
      字段匿名也可读,但带登录态更稳、更少触发风控)。
    - Amazon 会先抛一张风控中间页("Click the button below to continue shopping /
      Continue shopping"),点击后回首页。因此对每个 ASIN 做"两趟":第一趟点掉
      continue shopping 拿 session,再重新 goto 同一条 /dp/ 才拿得到真实商品页。
    """

    def __init__(self, domain: str, profile_root: Path | None = None,
                 headless: bool = True, shot_dir: Path | None = None,
                 max_retry: int = 2, delay: tuple[float, float] = (1.5, 3.0)):
        self.domain = domain
        self.headless = headless
        self.profile_root = Path(profile_root) if profile_root else _default_profile_root()
        self.shot_dir = Path(shot_dir) if shot_dir else _default_shot_dir()
        self.shot_dir.mkdir(parents=True, exist_ok=True)
        self.max_retry = max_retry
        self.delay = delay
        self._pw = None
        self._ctx = None

    def _ensure_ctx(self):
        if self._pw is None:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
        if self._ctx is None:
            profile = self.profile_root / self.domain
            profile.mkdir(parents=True, exist_ok=True)
            kwargs = dict(user_data_dir=str(profile), headless=self.headless,
                          locale="en-US", viewport={"width": 1280, "height": 900},
                          args=_browser_args())
            # 优先本机 Chrome(实测可穿 edgex guard),无 Chrome 环境回退内置 Chromium
            try:
                self._ctx = self._pw.chromium.launch_persistent_context(
                    channel="chrome", **kwargs)
            except Exception:
                self._ctx = self._pw.chromium.launch_persistent_context(**kwargs)
        return self._ctx

    def _warmup(self, page) -> None:
        """热身首页拿 session cookie,顺带被风控页拦截时点掉 continue shopping。"""
        try:
            page.goto(f"https://www.{self.domain}/", wait_until="domcontentloaded",
                      timeout=25000)
            page.wait_for_timeout(1200)
        except Exception:
            pass

    def _bypass_gate(self, page) -> None:
        """若落在 "Continue shopping" 风控页,点掉它(Amazon 会跳到首页)。"""
        try:
            body = page.inner_text("body")
        except Exception:
            body = ""
        if "Continue shopping" in body:
            try:
                page.click("button:has-text('Continue shopping'), "
                           "a:has-text('Continue shopping'), input[type=submit]")
            except Exception:
                pass
            page.wait_for_timeout(2500)

    def fetch(self, asin: str, domain: str = "", url: str = "",
              shot: bool = True) -> SnapshotRecord:
        """抓取一次该 ASIN 当前状态,返回结构化快照。"""
        import time as _t
        from playwright.sync_api import TimeoutError as PwTimeout
        ctx = self._ensure_ctx()
        page = ctx.new_page()
        domain = domain or self.domain
        base = f"https://www.{domain}"
        prod_url = f"{base}/dp/{asin}"
        try:
            page.goto(prod_url, wait_until="domcontentloaded", timeout=40000)
        except PwTimeout:
            pass
        page.wait_for_timeout(1500)
        self._bypass_gate(page)
        # 第二趟:风控过后重新 goto,这次才是真实商品页
        try:
            page.goto(prod_url, wait_until="domcontentloaded", timeout=40000)
        except PwTimeout:
            pass
        page.wait_for_timeout(3000)

        snap = self._parse_product(page, asin, domain)
        # 首页差评走 /gp/aw/ol/{asin}(样式精简,稳定返回星级直方图)
        if shot:
            self._maybe_shot(page, asin)
        self._fetch_home_reviews(page, asin, domain, snap)
        page.close()
        return snap

    def _parse_product(self, page, asin, domain) -> SnapshotRecord:
        title = ""
        el = page.query_selector("#productTitle")
        if el:
            try:
                title = el.inner_text().strip()
            except Exception:
                title = ""
        if not title:
            t = page.title()
            if t and t.lower() not in ("amazon.in", "amazon"):
                title = t.strip()

        price_text, price_val, currency = _extract_price(page)

        rating = None
        el = page.query_selector("#acrPopover")
        if el:
            lbl = (el.get_attribute("title") or el.get_attribute("aria-label") or "")
            m = re.search(r"(\d+(?:\.\d+)?)", lbl)
            if m:
                rating = float(m.group(1))

        review_count = None
        el = page.query_selector("#acrCustomerReviewText, #acrCustomerReviewLink")
        if el:
            s = (el.inner_text() or "").strip()
            m = re.search(r"([\d,]+)", s)
            if m:
                review_count = int(m.group(1).replace(",", ""))

        buybox = ""
        sold = page.query_selector(
            "#sellerProfileTriggerId, #merchantInfoFeature_feature_div, "
            "div#buyboxRightColumn, #desktop_buybox")
        if sold:
            txt = _clean(sold.inner_text())
            # 先切掉买盒尾部的噪声("Payment / Gift / Add to Wish List / See more")
            for _cut in ("Payment", "Gift options", "Add to", "Secure", "Available at", "See more"):
                idx = txt.find(_cut)
                if idx > 0:
                    txt = txt[:idx]
                    break
            # BuyBox 归属 = "Sold by" 的卖家(谁持有 BuyBox),"Ships from" 只是履约方
            m = re.search(r"Sold by\s*([A-Za-z0-9][^\s]{0,60})", txt)
            if m:
                buybox = m.group(1).strip()
            else:
                m = re.search(r"Ships from\s*([A-Za-z0-9][^\s]{0,60})", txt)
                if m:
                    buybox = m.group(1).strip()
                else:
                    buybox = txt[:80]

        availability = ""
        el = page.query_selector("#availability, #availability span")
        if el:
            availability = _clean(el.inner_text())

        deal_tag = ""
        for sel in ('.dealBadge, #dealBadge, .lightsOutBadge, .aok-relative .a-color-secondary',
                    'span:has-text("Lightning Deal"), span:has-text("Deal of the Day")'):
            el = page.query_selector(sel)
            if el:
                txt = _clean(el.inner_text())
                if txt and "M.R.P" not in txt:
                    deal_tag = txt[:40]
                    break

        bsr = None
        # BSR 常在详情折叠区:多种形态都试一遍
        for sel in ('tr:has-text("Best Sellers Rank")',
                    '#productDetails_db_sections',
                    'th:has-text("Best Sellers Rank")',
                    '#detailBulletsWrapper_feature_div li:has-text("Best Sellers Rank")',
                    '#productDetails_techSpec_section_1'):
            el = page.query_selector(sel)
            if not el:
                continue
            txt = _clean(el.inner_text())[:400]
            # 优先抓 "#数字" 形态(排名)
            m = re.search(r"#([\d,]+)", txt)
            if m:
                bsr = int(m.group(1).replace(",", ""))
                break
            m = re.search(r"Best Sellers Rank[:\s]*#?\s*([\d,]+)", txt)
            if m:
                bsr = int(m.group(1).replace(",", ""))
                break

        parent_asin = ""
        variations = []
        for a in page.query_selector_all("a[href*='/gp/aw/ol/'], a[href*='/product-reviews/']"):
            href = a.get_attribute("href") or ""
            m = re.search(r"(?:product-reviews|gp/aw/ol)/([A-Z0-9]{10})", href)
            if m and m.group(1) != asin:
                if m.group(1) not in variations:
                    variations.append(m.group(1))
                    if not parent_asin:
                        parent_asin = m.group(1)

        status = "alive"
        body = ""
        try:
            body = page.inner_text("body")[:1200]
        except Exception:
            pass
        low = body.lower()
        if "currently unavailable" in low or "we're sorry" in low:
            status = "unavailable"
        elif "looking for something" in low or "the web address you entered" in low:
            status = "deleted"

        return SnapshotRecord(
            asin=asin, domain=domain, checked_at=now_str(),
            title=title, buybox=buybox, parent_asin=parent_asin,
            variations=variations, price=price_text, price_value=price_val,
            currency=currency, rating=rating, review_count=review_count,
            bsr=bsr, deal_tag=deal_tag, availability=availability,
            status=status, home_reviews={}, note="",
        )

    def _fetch_home_reviews(self, page, asin, domain, snap) -> None:
        """从 /gp/aw/ol/{asin} 取星级直方图,折算首页差评占比。

        Amazon.in 的 /product-reviews/ 常 404;真实差评数据挂在 /gp/aw/ol/{asin}
        (移动端样式精简页),#histogramTable 提供 5/4/3/2/1 星占比。recent_bad 存
        "1 星+2 星占比%"作为差评信号,stars_breakdown 存完整直方图。
        """
        try:
            aw_url = f"https://www.{domain}/gp/aw/ol/{asin}"
            page.goto(aw_url, wait_until="domcontentloaded", timeout=40000)
            page.wait_for_timeout(2500)
        except Exception:
            return
        hist = page.query_selector("#histogramTable")
        breakdown = {}
        if hist:
            txt = _clean(hist.inner_text())
            for m in re.finditer(r"(\d)\s*star\s+([\d,]+)%", txt):
                breakdown[f"{m.group(1)}star"] = int(m.group(2).replace(",", ""))
        bad = (breakdown.get("1star", 0) or 0) + (breakdown.get("2star", 0) or 0)
        snap.home_reviews = {"recent_bad": bad, "stars_breakdown": breakdown} \
            if breakdown else {}

    def _maybe_shot(self, page, asin) -> None:
        try:
            page.screenshot(path=str(self.shot_dir / f"{asin}_{int(time.time() * 1000)}.png"))
        except Exception:
            pass

    def close(self):
        try:
            if self._ctx:
                self._ctx.close()
        except Exception:
            pass
        if self._pw:
            self._pw.stop()
            self._pw = None
        self._ctx = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _default_profile_root() -> Path:
    import os
    from pathlib import Path as P
    return P(os.path.expanduser("~")) / ".amreview" / "profile"


def _default_shot_dir() -> Path:
    from pathlib import Path as P
    return P(__file__).parent / "screenshots"


def _clean(s) -> str:
    import re as _re
    return _re.sub(r"\s+", " ", (s or "")).strip()


def _extract_price(page) -> tuple[str, float | None, str]:
    """从商品页取价格串/数值/货币。优先买盒价,退而取 any a-offscreen。"""
    for sel in ('#corePrice_feature_div .a-offscreen',
                '#corePriceDisplay_desktop_feature_div .a-offscreen',
                '#corePriceDisplay_mobile_feature_div .a-offscreen',
                '.a-price .a-offscreen', '#priceblock_dealprice',
                '#priceblock_ourprice'):
        el = page.query_selector(sel)
        if el:
            s = (el.inner_text() or "").strip()
            if s:
                return _parse_price(s)
    return "", None, ""


def _parse_price(s: str) -> tuple[str, float | None, str]:
    import re as _re
    m = _re.search(r"([^\d\s,.]*)\s*([\d,]+(?:\.\d+)?)", s)
    if not m:
        return s, None, ""
    cur, num = m.group(1), m.group(2)
    val = float(num.replace(",", ""))
    # 货币符号在数字前后都可能有
    cur = cur.strip()
    if not cur:
        return s, val, _guess_currency(num)
    return s, val, cur


def _guess_currency(num: str) -> str:
    return "₹"  # 默认印度卢比(当前场景全走 amazon.in)


def _browser_args() -> list[str]:
    import os
    return ["--no-sandbox"] if hasattr(os, "geteuid") and os.geteuid() == 0 else []


def make_adapter(kind: str = "mock", **kwargs) -> BaseAdapter:
    """工厂:按 kind 返回适配器。阶段 1-3 一律 mock;阶段 4 用 'playwright'。"""
    if kind == "mock":
        return MockAdapter(**kwargs)
    if kind == "playwright":
        return PlaywrightAdapter(**kwargs)
    raise ValueError(f"未知采集适配器: {kind}")
