"""E2E:真实抓取两条商品链接,验证 BP/DP 采集入库。"""
import sys
sys.path.insert(0, ".")
from pathlib import Path

from monitor import store as ms
from monitor.address import PlaywrightAdapter
from monitor.pipeline import add_profile, run_round

DB = Path("monitor.db")
LINKS = [
    ("B0FL7PT77Y", "amazon.com",
     "https://www.amazon.com/dp/B0FL7PT77Y/"),
    ("B0FPLRXRCP", "amazon.com.au",
     "https://www.amazon.com.au/dp/B0FPLRXRCP/"),
    ("B0D62853SR", "amazon.co.jp",
     "https://www.amazon.co.jp/dp/B0D62853SR/"),
]

ms.init_db(DB)
for asin, dom, url in LINKS:
    if not ms.get_profile(DB, asin, dom):
        add_profile(DB, asin=asin, domain=dom, url=url)
        print(f"profile 已添加: {asin} @ {dom}")
    else:
        print(f"profile 已存在: {asin} @ {dom}")

for asin, dom, url in LINKS:
    prof = ms.get_profile(DB, asin, dom)
    adapter = PlaywrightAdapter(dom)
    try:
        r = run_round(DB, adapter, profiles=[prof])
        print(f"{asin} @ {dom}: checked={r['checked']} anomalies={r['anomalies']}")
    finally:
        adapter.close()

print("\n===== 快照落库结果 =====")
for asin, dom, url in LINKS:
    s = ms.latest_snapshot(DB, asin, dom)
    if not s:
        print(f"{asin}: 无快照!")
        continue
    print(f"\n--- {asin} @ {dom} ({s['checked_at']}) ---")
    print("status      :", s.get("status"))
    print("title       :", (s.get("title") or "")[:80])
    print("price       :", s.get("price"), "| rating:", s.get("rating"),
          "| rc:", s.get("review_count"), "| bsr:", s.get("bsr"))
    print("buybox      :", s.get("buybox"))
    print("availability:", s.get("availability"))
    print("deal_tag    :", s.get("deal_tag"))
    print("home_reviews:", s.get("home_reviews"))
    bl = s.get("bullets") or []
    print(f"bullets({len(bl)} 条):")
    for b in bl[:6]:
        print("   •", b[:100])
    d = s.get("description") or ""
    print("description :", (d[:200] + "…") if len(d) > 200 else d or "(空)")
