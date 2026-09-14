"""monitor.model —— 数据契约(结构化字段,采集/存储/规则/看板共用一份)。

对应架构文档 doc/06 §3:两个核心实体。
- ProductProfile:一条 = 一个要监控的 ASIN(profiles 表一行)。
- SnapshotRecord:一次检查的结构化快照(snapshots 表一行,追加式)。

字段语义对齐架构文档 §3.2 的两类指标:
- 稳定类:title / image_url / buybox / parent_asin / variations(自己不该动)
- 动态类:price / rating / review_count / bsr / deal_tag / home_reviews(会正常波动)

采集层(address)产出 SnapshotRecord;存储层(store)序列化;规则层(rules)
拿它对比基线;看板(board)只读它呈现。契约定在这里,避免各处散落硬编码。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# 域名 → 两位国家码。**全项目唯一一份**,别在别处再抄一遍
# (曾经 ui.py / board.py / notify.py / 本文件各有一份,改一处漏三处)。
DOMAIN_CC = {"amazon.com": "US", "amazon.co.uk": "UK", "amazon.de": "DE",
             "amazon.co.jp": "JP", "amazon.com.au": "AU", "amazon.in": "IN",
             "amazon.com.mx": "MX", "amazon.com.br": "BR", "amazon.es": "ES",
             "amazon.it": "IT", "amazon.fr": "FR", "amazon.ca": "CA"}


def short_domain(domain: str) -> str:
    """站点显示名:收录的走国家码,未收录的回退为去掉 amazon. 前缀。"""
    if not domain:
        return ""
    return DOMAIN_CC.get(domain, str(domain).replace("amazon.", ""))


@dataclass
class ProductProfile:
    """一个被监控的 ASIN。asinc+domain 复合主键。"""
    asin: str
    domain: str
    url: str
    parent_asin: str = ""
    title: str = ""
    monitor_enabled: int = 1
    metric_config: dict = field(default_factory=dict)   # 每指标开关+阈值(JSON)
    baseline_snapshot_id: int | None = None              # 基点在哪个快照上
    last_checked_at: str = ""
    id: int | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["metric_config"] = self.metric_config
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ProductProfile":
        return cls(
            asin=d["asin"], domain=d["domain"], url=d.get("url", ""),
            parent_asin=d.get("parent_asin", ""), title=d.get("title", ""),
            monitor_enabled=d.get("monitor_enabled", 1),
            metric_config=d.get("metric_config") or {},
            baseline_snapshot_id=d.get("baseline_snapshot_id"),
            last_checked_at=d.get("last_checked_at", ""),
            id=d.get("id"),
        )


@dataclass
class SnapshotRecord:
    """一次检查的结构化快照。asinc+domain+checked_at 唯一标识一次采集。"""
    asin: str
    domain: str
    checked_at: str                                        # "YYYY-MM-DD HH:MM:SS"
    title: str = ""
    model_number: str = ""                                 # 商品页 Model Number(型号),推送里当 SKU 用
    image_url: str = ""
    buybox: str = ""                                       # 当前 BuyBox 归属(卖家名),空=无人持有
    parent_asin: str = ""
    variations: list = field(default_factory=list)         # 变体 ASIN 集合
    price: str = ""                                        # 展示原串,如 "$29.99"
    price_value: float | None = None                       # 数值,阈值判定用
    currency: str = ""
    rating: float | None = None
    review_count: int | None = None
    bsr: int | None = None                                 # Best Sellers Rank
    bsr_cat: str = ""                                      # BSR 大类名,如 "Home"
    bsr_sub: str = ""                                      # BSR 小类名,如 "Desk Lamps"
    deal_tag: str = ""                                     # Lightning Deal / Deal of the Day
    availability: str = ""                                 # In Stock / Currently unavailable
    status: str = "alive"                                  # alive/deleted/blocked/unavailable
    bullets: list = field(default_factory=list)            # 五点描述(BP)原文列表
    description: str = ""                                  # 产品描述(DP)纯文本,截断存储
    home_reviews: dict = field(default_factory=dict)       # {"recent_bad": int, "stars_breakdown": {...}}
    note: str = ""
    id: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "SnapshotRecord":
        return cls(
            asin=d["asin"], domain=d["domain"], checked_at=d["checked_at"],
            title=d.get("title", ""), image_url=d.get("image_url", ""),
            buybox=d.get("buybox", ""), parent_asin=d.get("parent_asin", ""),
            variations=d.get("variations") or [],
            price=d.get("price", ""), price_value=d.get("price_value"),
            currency=d.get("currency", ""), rating=d.get("rating"),
            review_count=d.get("review_count"), bsr=d.get("bsr"),
            bsr_cat=d.get("bsr_cat", ""), bsr_sub=d.get("bsr_sub", ""),
            deal_tag=d.get("deal_tag", ""),
            availability=d.get("availability", ""),
            status=d.get("status", "alive"),
            bullets=d.get("bullets") or [],
            description=d.get("description", ""),
            home_reviews=d.get("home_reviews") or {},
            note=d.get("note", ""), id=d.get("id"),
        )


def default_metric_config() -> dict:
    """每指标默认开关+阈值(规则引擎的落点,卖家可自行调)。

    阈值含义:
    - price:            |现价-基线价|/基线价 > 5% 才报(避免自己改价/汇率浮动误报)
    - rating:           评分差 > 0.3 才报
    - review_count:     评价数差 > 10% 才报
    - bsr:              排名差 > 50% 才报(排名波动大,阈值放宽)
    - home_reviews:     首页差评较基线新增 > 2 条才报
    - title/buybox/variations/status: 稳定类,任何变化 = 异常,无阈值
    """
    return {
        "price":        {"enabled": True, "threshold_pct": 5.0},
        "rating":       {"enabled": True, "threshold": 0.3},
        "review_count": {"enabled": True, "threshold_pct": 10.0},
        "bsr":          {"enabled": True, "threshold_pct": 50.0},
        "deal_tag":     {"enabled": True},
        "home_reviews": {"enabled": True, "max_new_bad": 2},
        "title":        {"enabled": True},
        "buybox":       {"enabled": True},
        "variations":   {"enabled": True},
        "status":       {"enabled": True},
        # 断货全程规则:整个观测期内每次检查都为 unavailable 才通过。
        # min_period_checks: 至少要有多少条历史快照才下结论(默认 2,避免单点误判)
        "unavailable_period": {"enabled": False, "min_period_checks": 2,
                              "severity": "warning"},
    }


def snapshot_to_dict(snap: SnapshotRecord) -> dict:
    """供 board 渲染用的展示字典(把 None 规范化,避免 dataframe 显示 None)。"""
    def _cell(v, default="—"):
        if v is None or v == "":
            return default
        return v
    return {
        "ASIN": snap.asin,
        "站点": short_domain(snap.domain),
        "状态": snap.status,
        "标题": _cell(snap.title),
        "价格": _cell(snap.price),
        "评分": _cell(snap.rating),
        "评价数": _cell(snap.review_count),
        "BuyBox": _cell(snap.buybox),
        "上下架": _cell(snap.availability),
        "DealTag": _cell(snap.deal_tag),
        "跟踪时间": (snap.checked_at or "")[5:16] or "—",
    }
