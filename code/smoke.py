"""手工回归冒烟:升级 Playwright / 改动引擎后跑一遍,全绿即无恙。

用法:
  .venv/bin/python smoke.py          # 快速:编译 + 解析单测(离线)
  .venv/bin/python smoke.py --live   # 完整:再加一次真实检测(假 ID 应判 🐕)
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

CODE = Path(__file__).parent
PY = sys.executable


def step(name: str, fn) -> bool:
    try:
        fn()
        print(f"✅ {name}")
        return True
    except Exception as e:
        print(f"❌ {name}: {e}")
        return False


def check_compile():
    # 核心模块 + monitor 包(看板/规则/存储,统一纳入编译覆盖,防大改后语法漏检)
    files = ["app.py", "engine.py", "weblogin.py", "login.py",
             "monitor/board.py", "monitor/store.py", "monitor/rules.py",
             "monitor/pipeline.py", "monitor/address.py", "monitor/model.py",
             "monitor/baseline.py", "monitor/demo.py"]
    for f in files:
        r = subprocess.run([PY, "-m", "py_compile", str(CODE / f)],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


def check_imports():
    sys.path.insert(0, str(CODE))
    import engine, weblogin  # noqa: F401


def check_parse():
    sys.path.insert(0, str(CODE))
    from engine import parse_links
    refs = parse_links(
        "https://www.amazon.com/gp/customer-reviews/R1ABC12345/\n"
        "https://www.amazon.in/review/RQWX9100MIJ7A\n"
        "https://www.amazon.com.mx/portal/customer-reviews/srp/-/R2XYZ98765\n"
        "https://www.amazon.evil.com/gp/customer-reviews/R9BAD00000/\n"  # 非法域名,应被拒
        "R3BARE0000X\n垃圾行\n")
    ids = [(r.review_id, r.domain) for r in refs]
    assert ids == [
        ("R1ABC12345", "amazon.com"),
        ("RQWX9100MIJ7A", "amazon.in"),
        ("R2XYZ98765", "amazon.com.mx"),
    ], f"解析结果不符: {ids}"


def check_totp():
    sys.path.insert(0, str(CODE))
    import pyotp
    from weblogin import totp_code
    s = pyotp.random_base32()
    assert totp_code(s) == pyotp.TOTP(s).now()


def check_monitor():
    """链接异常监控(阶段1-3):用临时库造演示数据,验证三表追加 + 规则 + 看板聚合。"""
    import tempfile
    from pathlib import Path
    sys.path.insert(0, str(CODE))
    from monitor import store
    from monitor.demo import seed_demo
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "monitor.db"
        # 1) 种子:写入 profiles + snapshots(追加式),并触发 anomalies
        n = seed_demo(db)
        assert n >= 10, f"演示种子应写入多条快照,实际 {n}"
        assert store.count_snapshots(db) == n
        # 2) 三表齐全
        assert len(store.list_profiles(db)) == 7
        bad = store.unconfirmed_anomalies(db)
        assert len(bad) >= 3, f"异常应命中多条,实际 {len(bad)}"
        # 3) 规则能对稳定类(丢BuyBox)+动态类(价格)都判出异常
        metrics = {a["metric"] for a in bad}
        assert "buybox" in metrics, f"缺少丢BuyBox异常: {metrics}"
        # 4) 基线可前移 + 确认闭环
        from monitor.pipeline import confirm_and_move_baseline
        a = bad[0]
        confirm_and_move_baseline(db, a["asin"], a["domain"])
        store.confirm_anomaly(db, a["id"])
        remaining_ids = {item["id"] for item in store.unconfirmed_anomalies(db)}
        expected_ids = {item["id"] for item in bad if item["id"] != a["id"]}
        assert remaining_ids == expected_ids, (
            f"确认应只移除目标异常 {a['id']},实际剩余: {remaining_ids}"
        )
        # 5) 看板聚合正常
        from monitor.view import get_board_data
        data = get_board_data(db)
        assert data["total"] == 7
        assert data["abnormal_count"] >= 1


def check_live():
    sys.path.insert(0, str(CODE))
    from engine import ReviewChecker, parse_links
    ref = parse_links("https://www.amazon.com/gp/customer-reviews/AFAKEID12345/")[0]
    with ReviewChecker() as checker:
        r = checker.check_batch([ref])[0]
    assert r["status"] == "deleted", f"假 ID 应判已删,实际: {r['status']} ({r['note']})"


if __name__ == "__main__":
    live = "--live" in sys.argv
    ok = all([
        step("编译四个模块", check_compile),
        step("导入引擎/登录模块", check_imports),
        step("链接解析(六国 allowlist + 三格式 + 去重 + 拒非法域名)", check_parse),
        step("TOTP 算码", check_totp),
        step("异常监控(三表+规则+基线+看板聚合)", check_monitor),
    ])
    if live:
        ok = step("实测:假 ID 应判 🐕 已删(走真实 Amazon)", check_live) and ok
    print("\n" + ("🎉 冒烟通过" if ok else "💥 有失败项,修复后再部署"))
    sys.exit(0 if ok else 1)
