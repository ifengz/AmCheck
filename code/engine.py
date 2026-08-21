"""AmReview 检测引擎:Amazon 单条评价 permalink 存活检测。

判定状态机与选择器依据 doc/04-终版方案(两轮实测收敛):
- edgex guard 可被真 Chrome headless 穿透,无需 stealth 加持
- 已删评价 404 判定不需要登录态;活评价页需登录(持久化档案)
- "Server Busy" 为软拦截,热身首页拿 cookie 后重访即破

扩展预留:
- 未来将支持产品链接检测(ASIN),提取标题/BP/价格/Deal Tag/Sold by/评分/评价数/首图等
- 架构已按域名分档案,可平滑扩展多检测类型
"""

from __future__ import annotations

import os
import random
import re
import time
from dataclasses import dataclass, asdict
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

PROFILE_ROOT = Path.home() / ".amreview" / "profile"
SHOT_DIR = Path(__file__).parent / "screenshots"


def _browser_args() -> list[str]:
    """服务器常以 root 运行浏览器,不带 --no-sandbox 会被 Chromium/Chrome 拒绝启动。"""
    return ["--no-sandbox"] if hasattr(os, "geteuid") and os.geteuid() == 0 else []

STATUS_LABEL = {
    "alive": "✅ 正常",
    "deleted": "🐕 已删",
    "blocked": "🤖 被拦截",
    "login_expired": "🍪 登录失效",
    "unknown": "❓ 未知",
}

# 兼容经典 /gp/customer-reviews/、短格式 /review/、portal 新格式;输入必须是带域名的完整链接
REVIEW_ID_PATTERNS = [
    re.compile(r"/gp/customer-reviews/([A-Za-z0-9]{10,})"),
    re.compile(r"/review/([A-Za-z0-9]{10,})"),
    re.compile(r"/portal/customer-reviews/srp/-/([A-Za-z0-9]{10,})"),
]
DOMAIN_RE = re.compile(r"(amazon\.[a-z.\-]+)", re.IGNORECASE)
# 站点 allowlist(评审项:防止构造任意 amazon.* 域名出站);扩站点时同步 accounts.json
ALLOWED_DOMAINS = {"amazon.com", "amazon.com.mx", "amazon.com.br",
                   "amazon.in", "amazon.com.au", "amazon.co.jp"}

DELETED_TEXTS = (
    "Looking for something?",
    "Sorry, we couldn't find that page",
    "ページが見つかりません",          # 日本
    "お探しのページは見つかりません",   # 日本(变体)
    "no pudimos encontrar la página",   # 墨西哥(西语)
    "encontrar essa página",            # 巴西(葡语)
)

MAX_RETRY = 3          # Server Busy / guard 各自的重试上限
BACKOFFS = (8, 20, 40)  # guard 退避秒数
# 登录墙判定:实测 Amazon 存在 ap/signin 与 gp/sign-in.html 两种跳转形态
SIGNIN_PATHS = ("/ap/signin", "/gp/sign-in.html")


@dataclass
class ReviewRef:
    raw: str
    review_id: str
    domain: str
    url: str  # 规范化后的经典 permalink


# 为未来扩展预留：产品链接引用类型
# @dataclass
# class ProductRef:
#     raw: str
#     asin: str
#     domain: str
#     url: str
#
#     检测字段预留:
#     - title: 产品标题
#     - bullet_points: BP 列表
#     - price: 价格(含货币)
#     - deal_tag: Deal 标签(Lightning Deal, Deal of the Day 等)
#     - sold_by: 卖家名称
#     - rating: 评分(如 4.5)
#     - review_count: 评价数
#     - top_review: 首页评价预览
#     - main_image: 主图 URL
#     - availability: 库存状态


def parse_link(raw: str, default_domain: str = "amazon.com") -> ReviewRef | None:
    """从完整链接(必须带域名)中提取 review ID,规范化为经典 permalink。"""
    raw = raw.strip().rstrip("/") + "/"
    review_id = None
    for pat in REVIEW_ID_PATTERNS:
        m = pat.search(raw)
        if m:
            review_id = m.group(1)
            break
    if not review_id:
        return None
    m = DOMAIN_RE.search(raw)
    if not m:
        return None  # 没有域名的输入一律不收
    domain = m.group(1).lower()
    if domain not in ALLOWED_DOMAINS:
        return None  # 六国之外的域名一律不收,不出站
    return ReviewRef(
        raw=raw,
        review_id=review_id,
        domain=domain,
        url=f"https://www.{domain}/gp/customer-reviews/{review_id}/",
    )


def parse_links(text: str, default_domain: str = "amazon.com") -> list[ReviewRef]:
    refs, seen = [], set()
    for line in text.splitlines():
        for token in line.split():
            ref = parse_link(token, default_domain)
            if ref and ref.review_id not in seen:
                seen.add(ref.review_id)
                refs.append(ref)
    return refs


def _txt(page: Page, selector: str) -> str:
    el = page.query_selector(selector)
    return el.inner_text().strip() if el else ""


def _shot(page: Page, shot_dir: Path, review_id: str) -> str:
    path = shot_dir / f"{review_id}_{int(time.time() * 1000)}.png"  # 毫秒级,避免同秒重查覆盖
    try:
        page.screenshot(path=str(path))
        return str(path)
    except Exception:
        return ""


def _warmup(page: Page, domain: str) -> None:
    """热身首页拿 session cookie,破 "Server Busy" 软拦截。"""
    try:
        page.goto(f"https://www.{domain}/", wait_until="domcontentloaded", timeout=20000)
    except Exception:
        pass


def _is_deleted(resp, title: str, body: str, page: Page) -> bool:
    if resp is not None and resp.status == 404:
        return True
    if "Page Not Found" in title:
        return True
    if any(t in body for t in DELETED_TEXTS):
        return True
    try:
        return "/dogs/" in page.content()
    except Exception:
        return False


def _extract_fields(page: Page) -> dict:
    stars = ""
    el = page.query_selector(
        '[data-hook="review-star-rating"], [data-hook="cr-review-info-star-rating"]'
    )
    if el:
        cls = el.get_attribute("class") or ""
        m = re.search(r"a-star-(\d)", cls)
        if m:
            stars = m.group(1)
        else:
            label = el.get_attribute("aria-label") or el.get_attribute("title") or ""
            m = re.search(r"^(\d)", label.strip())
            if m:
                stars = m.group(1)

    # permalink 页标题锚点内含 [星级span, 标题span],取最后一个 span
    title = ""
    el = page.query_selector('a[data-hook="review-title"]')
    if el:
        spans = el.query_selector_all("span")
        title = spans[-1].inner_text().strip() if spans else el.inner_text().strip()
    if not title:
        title = _txt(page, '[data-hook="review-title"]')

    return {
        "stars": stars,
        "title": title,
        "author": _txt(page, "span.a-profile-name") or _txt(page, '[data-hook="review-user"]'),
        "review_date": _txt(page, 'span[data-hook="review-date"]'),
        "body": _txt(page, 'span[data-hook="review-body"]')[:200],
        "verified": bool(page.query_selector('[data-hook="avp-badge"]')),
    }


def check_one(page: Page, ref: ReviewRef, shot_dir: Path) -> dict:
    """核心状态机,返回一条检测结果 dict。"""
    result = {
        "review_id": ref.review_id,
        "domain": ref.domain,
        "url": ref.url,
        "status": "unknown",
        "stars": "",
        "title": "",
        "author": "",
        "review_date": "",
        "body": "",
        "verified": False,
        "note": "",
        "screenshot": "",
        "checked_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    last_block = ""
    last_err = ""  # 导航异常类型:只有真见过 guard/Busy 才算"被拦截",网络故障归"未知"
    signin_warmed = False  # 实测:无任何 cookie 时已删评价也可能先跳登录页,匿名热身复访一次再定论
    for attempt in range(MAX_RETRY + 1):
        try:
            resp = page.goto(ref.url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            last_err = e.__class__.__name__
            result["note"] = f"导航异常: {last_err}"
            continue
        final_url, title = page.url, page.title()

        if any(p in final_url for p in SIGNIN_PATHS):
            if not signin_warmed:
                signin_warmed = True
                _warmup(page, ref.domain)
                continue
            result.update(status="login_expired",
                          note="跳转登录页,需重新引导登录该站点 Amazon 账号")
            return result

        body = ""
        try:
            body = page.inner_text("body")[:4000]
        except Exception:
            pass

        if "Server Busy" in title or "Server Busy" in body:
            last_block = "Server Busy 软拦截"
            _warmup(page, ref.domain)
            continue

        if "/edgex/guard" in final_url or "captcha" in final_url.lower():
            last_block = "guard/captcha 拦截"
            if attempt < MAX_RETRY:
                time.sleep(BACKOFFS[min(attempt, len(BACKOFFS) - 1)])
            continue

        if _is_deleted(resp, title, body, page):
            result.update(status="deleted", note=f"HTTP {resp.status if resp else '?'} · {title[:60]}")
            result["screenshot"] = _shot(page, shot_dir, ref.review_id)
            return result

        if page.query_selector('[data-hook="review"]'):
            result.update(_extract_fields(page))
            result["status"] = "alive"
            return result

        # 未知形态:截图存档供人工/AI 复核
        result["note"] = f"HTTP {resp.status if resp else '?'} · {title[:60]}"
        result["screenshot"] = _shot(page, shot_dir, ref.review_id)
        return result

    if last_block:
        result["status"] = "blocked"
        result["note"] = f"重试 {MAX_RETRY} 次仍被拦截({last_block}),建议稍后复测"
    else:
        # 全程没见到 guard/Server Busy,只是导航一直失败 → 网络/超时问题,不是 Amazon 风控
        result["status"] = "unknown"
        result["note"] = f"连续导航失败({last_err or '未知错误'}),疑似网络/超时,非 Amazon 拦截"
    result["screenshot"] = _shot(page, shot_dir, ref.review_id)
    return result


class ReviewChecker:
    """按域名复用持久化浏览器档案,批量检测。

    当前支持：评价链接存活检测
    未来扩展：产品链接检测（ASIN → 标题/价格/评分/图片等）
    """

    def __init__(self, headless: bool = True,
                 profile_root: Path = PROFILE_ROOT, shot_dir: Path = SHOT_DIR):
        self.headless = headless
        self.profile_root = Path(profile_root)
        self.shot_dir = Path(shot_dir)
        self.shot_dir.mkdir(parents=True, exist_ok=True)
        self._pw = None
        self._contexts: dict[str, object] = {}

    def _context(self, domain: str):
        if self._pw is None:
            self._pw = sync_playwright().start()
        if domain not in self._contexts:
            profile = self.profile_root / domain
            profile.mkdir(parents=True, exist_ok=True)
            kwargs = dict(user_data_dir=str(profile), headless=self.headless,
                          locale="en-US", viewport={"width": 1280, "height": 900},
                          args=_browser_args())
            # 优先本机 Chrome(实测可穿 edgex guard),无 Chrome 环境回退内置 Chromium
            try:
                ctx = self._pw.chromium.launch_persistent_context(channel="chrome", **kwargs)
            except Exception:
                ctx = self._pw.chromium.launch_persistent_context(**kwargs)
            self._contexts[domain] = ctx
        return self._contexts[domain]

    def is_logged_in(self, domain: str) -> bool:
        ctx = self._context(domain)
        return any(c["name"].startswith(("at-", "x-")) for c in ctx.cookies())

    def check_batch(self, refs: list[ReviewRef], delay: tuple = (3, 5),
                    on_result=None) -> list[dict]:
        """批量检测评价链接。"""
        results = []
        for i, ref in enumerate(refs):
            ctx = self._context(ref.domain)
            page = ctx.new_page()
            try:
                r = check_one(page, ref, self.shot_dir)
            finally:
                page.close()
            results.append(r)
            if on_result:
                on_result(i, ref, r)
            if i < len(refs) - 1:
                time.sleep(random.uniform(*delay))
        return results

    # 为未来扩展预留：产品链接批量检测
    # def check_products(self, product_refs: list[ProductRef], delay: tuple = (3, 5),
    #                    on_result=None) -> list[dict]:
    #     """批量检测产品链接,提取标题/价格/评分/图片等字段。
    #
    #     返回格式:
    #     {
    #         "asin": "B08XXX",
    #         "domain": "amazon.com",
    #         "url": "https://...",
    #         "status": "alive" | "deleted" | "blocked" | "unavailable",
    #         "title": "产品标题",
    #         "price": {"amount": "29.99", "currency": "USD"},
    #         "deal_tag": "Lightning Deal" | None,
    #         "sold_by": "Amazon.com" | "第三方卖家名",
    #         "rating": 4.5,
    #         "review_count": 1234,
    #         "top_reviews": [{"author": "...", "stars": 5, "text": "..."}, ...],
    #         "main_image": "https://...",
    #         "availability": "In Stock" | "Currently unavailable",
    #         "note": "",
    #         "checked_at": "2026-08-15 12:00:00",
    #     }
    #     """
    #     pass

    def close(self):
        for ctx in self._contexts.values():
            try:
                ctx.close()
            except Exception:
                pass
        if self._pw:
            self._pw.stop()
            self._pw = None
        self._contexts.clear()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


if __name__ == "__main__":
    import sys, json
    refs = parse_links("\n".join(sys.argv[1:]))
    if not refs:
        print("用法: python engine.py <评价链接>...")
        sys.exit(1)
    with ReviewChecker() as checker:
        for r in checker.check_batch(refs):
            brief = {k: r[k] for k in ("review_id", "domain", "status", "stars",
                                       "title", "author", "review_date", "note")}
            print(json.dumps(brief, ensure_ascii=False))
