"""notify —— 变化通知(钉钉机器人)。

配置优先级:settings 表(UI 里配的)> 环境变量(AMCHECK_NOTIFY_WEBHOOK /
AMCHECK_NOTIFY_SECRET,兼容旧 CLI 用法)。UI 的「定时与通知」弹窗写 settings。

去重:同一 (asin, domain, metric) 在静默期(默认 12h)内只推一次。
用 notify_state 表记"上次推送时间",不能用 anomalies.checked_at 判断——
那会把静默期内新产生的其它异常也误杀。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus

from . import store
from .rules import METRIC_LABELS

# 域名 → 国家码(与 ui.py 的 DOMAIN_CC 保持一致;monitor 包不反向依赖 UI)
DOMAIN_CC = {"amazon.com": "US", "amazon.co.uk": "UK", "amazon.de": "DE",
             "amazon.co.jp": "JP", "amazon.com.au": "AU", "amazon.in": "IN",
             "amazon.com.mx": "MX", "amazon.com.br": "BR", "amazon.es": "ES",
             "amazon.it": "IT", "amazon.fr": "FR", "amazon.ca": "CA"}


def _sign(secret: str, ts_ms: int) -> str:
    string = f"{ts_ms}\n{secret}"
    digest = hmac.new(secret.encode(), string.encode(), hashlib.sha256).digest()
    return quote_plus(base64.b64encode(digest).decode())


def get_config(db_path) -> dict:
    """读通知配置:DB 优先,环境变量兜底;AMCHECK_NOTIFY_DISABLE=1 全局静音。

    mode="group" 用群机器人 webhook;mode="app" 用企业内部应用机器人单聊。
    """
    if os.environ.get("AMCHECK_NOTIFY_DISABLE") == "1":
        return {"mode": "", "webhook": "", "secret": "", "mute_h": 12.0,
                "client_id": "", "client_secret": "", "robot_code": "",
                "user_ids": []}
    mode = store.get_setting(db_path, "notify_mode", "group") or "group"
    url = store.get_setting(db_path, "notify_webhook") or \
        os.environ.get("AMCHECK_NOTIFY_WEBHOOK", "")
    secret = store.get_setting(db_path, "notify_secret") or \
        os.environ.get("AMCHECK_NOTIFY_SECRET", "")
    client_id = store.get_setting(db_path, "notify_client_id") or \
        os.environ.get("AMCHECK_DING_CLIENT_ID", "")
    client_secret = store.get_setting(db_path, "notify_client_secret") or \
        os.environ.get("AMCHECK_DING_CLIENT_SECRET", "")
    robot_code = store.get_setting(db_path, "notify_robot_code") or client_id
    raw_ids = store.get_setting(db_path, "notify_user_ids") or \
        os.environ.get("AMCHECK_DING_USER_IDS", "")
    user_ids = [x.strip() for x in raw_ids.replace("，", ",").split(",")
                if x.strip()]
    try:
        mute = float(store.get_setting(db_path, "notify_mute_h") or
                     os.environ.get("AMCHECK_NOTIFY_UPTIME_H", "12") or 12)
    except ValueError:
        mute = 12.0
    return {"mode": mode.strip(),
            "webhook": url.strip(), "secret": secret.strip(), "mute_h": mute,
            "client_id": client_id.strip(), "client_secret": client_secret.strip(),
            "robot_code": robot_code.strip(), "user_ids": user_ids}


_TOKEN_CACHE = {"token": "", "exp": 0.0}


def _access_token(client_id: str, client_secret: str) -> str:
    """企业内部应用 accessToken,缓存到过期前 5 分钟。"""
    now = time.time()
    if _TOKEN_CACHE["token"] and _TOKEN_CACHE["exp"] > now:
        return _TOKEN_CACHE["token"]
    body = json.dumps({"appKey": client_id,
                       "appSecret": client_secret}).encode()
    req = urllib.request.Request(
        "https://api.dingtalk.com/v1.0/oauth2/accessToken", data=body,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        d = json.loads(r.read())
    tok = d.get("accessToken", "")
    if not tok:
        raise RuntimeError(str(d)[:120])
    expire = int(d.get("expireIn", 7200) or 7200)
    _TOKEN_CACHE.update(token=tok, exp=now + max(expire - 300, 60))
    return tok


def _push_webhook(cfg: dict, text: str, title: str = "",
                  markdown: bool = False) -> tuple[bool, str]:
    """群机器人 webhook(自定义机器人,支持钉钉/企微加签)。"""
    url = cfg["webhook"]
    if not url:
        return False, "未配置 webhook"
    if cfg["secret"] and "dingtalk" in url:
        ts = str(round(time.time() * 1000))
        url += f"&timestamp={ts}&sign={_sign(cfg['secret'], int(ts))}"
    if markdown:
        body = json.dumps({"msgtype": "markdown",
                           "markdown": {"title": title or "AmCheck 通知",
                                        "text": text}}).encode()
    else:
        body = json.dumps({"msgtype": "text",
                           "text": {"content": text}}).encode()
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read().decode(errors="replace")
        ok = r.status == 200
        try:
            errcode = json.loads(raw).get("errcode", 0)
        except Exception:
            errcode = 0
        if ok and errcode == 0:
            return True, "已推送"
        return False, f"机器人拒绝: {raw[:120]}"
    except Exception as e:
        return False, f"{e.__class__.__name__}: {e}"


def _push_app(cfg: dict, text: str, title: str = "",
              markdown: bool = False) -> tuple[bool, str]:
    """企业内部应用机器人单聊推送,收件人取 cfg["user_ids"]。"""
    if not cfg["client_id"] or not cfg["client_secret"]:
        return False, "未配置应用 Client ID / Secret"
    if not cfg["user_ids"]:
        return False, "未配置收件人 userid"
    try:
        tok = _access_token(cfg["client_id"], cfg["client_secret"])
    except Exception as e:
        return False, f"取 accessToken 失败: {e.__class__.__name__}: {e}"
    if markdown:
        msg_key = "sampleMarkdown"
        msg_param = {"title": title or "AmCheck 通知", "text": text}
    else:
        msg_key = "sampleText"
        msg_param = {"content": text}
    body = json.dumps({
        "robotCode": cfg["robot_code"] or cfg["client_id"],
        "userIds": cfg["user_ids"],
        "msgKey": msg_key,
        "msgParam": json.dumps(msg_param, ensure_ascii=False),
    }, ensure_ascii=False).encode()
    req = urllib.request.Request(
        "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend",
        data=body,
        headers={"Content-Type": "application/json",
                 "x-acs-dingtalk-access-token": tok})
    status = 0
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw, status = r.read().decode(errors="replace"), r.status
    except Exception as e:
        try:
            raw, status = e.read().decode(errors="replace"), getattr(e, "code", 0)
        except Exception:
            return False, f"{e.__class__.__name__}: {e}"
    try:
        d = json.loads(raw)
    except Exception:
        d = {}
    if status == 200 and not d.get("code"):
        return True, "已推送"
    return False, f"机器人拒绝: {raw[:140]}"


def push_text(db_path, text: str) -> tuple[bool, str]:
    """推一条纯文本;按配置选群机器人或企业应用。返回 (成功?, 说明)。"""
    cfg = get_config(db_path)
    if cfg["mode"] == "app":
        return _push_app(cfg, text)
    return _push_webhook(cfg, text)


def push_markdown(db_path, title: str, md: str) -> tuple[bool, str]:
    """推一条 markdown(支持加粗);按配置选群机器人或企业应用。"""
    cfg = get_config(db_path)
    if cfg["mode"] == "app":
        return _push_app(cfg, md, title=title, markdown=True)
    return _push_webhook(cfg, md, title=title, markdown=True)


def _last_push(db_path, asin: str, domain: str, metric: str) -> float:
    import sqlite3
    with store._connect(db_path) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS notify_state (
            asin TEXT, domain TEXT, metric TEXT, pushed_at REAL,
            PRIMARY KEY (asin, domain, metric))""")
        row = conn.execute(
            "SELECT pushed_at FROM notify_state "
            "WHERE asin=? AND domain=? AND metric=?",
            (asin, domain, metric)).fetchone()
    return row[0] if row else 0.0


def _rhash(item: dict) -> str:
    """单条差评的身份哈希:标题+正文,忽略采集顺序/星级微调。"""
    import hashlib
    raw = f"{item.get('title', '')}\n{item.get('text', '')}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _reviews_pushed(db_path, asin: str, domain: str,
                    hashes: list[str]) -> set[str]:
    """查这批差评哈希里哪些已经推送过。"""
    if not hashes:
        return set()
    with store._connect(db_path) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS review_push_state (
            asin TEXT, domain TEXT, rhash TEXT, pushed_at REAL,
            PRIMARY KEY (asin, domain, rhash))""")
        marks = ",".join("?" * len(hashes))
        rows = conn.execute(
            f"SELECT rhash FROM review_push_state "
            f"WHERE asin=? AND domain=? AND rhash IN ({marks})",
            [asin, domain] + hashes).fetchall()
    return {r[0] for r in rows}


def _mark_reviews_pushed(db_path, asin: str, domain: str,
                         hashes: list[str]) -> None:
    if not hashes:
        return
    import time as _t
    with store._connect(db_path) as conn:
        conn.executemany(
            """INSERT INTO review_push_state (asin, domain, rhash, pushed_at)
               VALUES (?,?,?,?) ON CONFLICT(asin,domain,rhash) DO NOTHING""",
            [(asin, domain, h, _t.time()) for h in hashes])


def _parse_bad_items(detail_raw) -> list[dict]:
    """anomalies.detail 里的差评条目:JSON(新)或纯文本(旧)都兼容。"""
    import json
    if not detail_raw:
        return []
    try:
        items = json.loads(detail_raw)
        if isinstance(items, list):
            return [x for x in items if isinstance(x, dict)]
    except (ValueError, TypeError):
        pass
    return []


def _mark_pushed(db_path, asin: str, domain: str, metric: str) -> None:
    with store._connect(db_path) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS notify_state (
            asin TEXT, domain TEXT, metric TEXT, pushed_at REAL,
            PRIMARY KEY (asin, domain, metric))""")
        conn.execute(
            "INSERT INTO notify_state (asin, domain, metric, pushed_at) "
            "VALUES (?,?,?,?) ON CONFLICT(asin,domain,metric) "
            "DO UPDATE SET pushed_at=excluded.pushed_at",
            (asin, domain, metric, time.time()))


def notify_new_anomalies(db_path, round_anomaly_count: int,
                         hours: float = 2.0) -> int:
    """把本轮新异常(静默期外)按 ASIN 分组汇总推一条;返回实际推送条数。

    消息结构(以 ASIN 为单位,国家码后缀):
        AmCheck 监控告警:2 个 ASIN · 3 条变化
        时间 …

        B0XXX Solimo Dish Drainer…(US)
        1. 🚨 上下架: 在售 → 不可售
        2. ⚠ 价格: EUR 29.99 → EUR 19.99
    """
    if not round_anomaly_count:
        return 0
    cfg = get_config(db_path)
    if cfg["mode"] == "app":
        configured = bool(cfg["client_id"] and cfg["client_secret"]
                          and cfg["user_ids"])
    else:
        configured = bool(cfg["webhook"])
    if not configured:
        return 0
    import sqlite3
    with store._connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        fresh = [dict(r) for r in conn.execute(
            """SELECT * FROM anomalies
               WHERE confirmed = 0
                 AND checked_at >= datetime('now', 'localtime', ?)
               ORDER BY CASE severity
                 WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END,
                 id DESC""", (f"-{hours} hours",)).fetchall()]
    now = time.time()
    seen = set()
    groups = {}  # (asin, domain) -> [anomaly];组内保持严重级排序
    new_review_hashes = {}  # (asin, domain) -> [rhash] 本次将推送的新差评
    for a in fresh:
        key = (a["asin"], a["domain"], a["metric"])
        if key in seen:
            continue  # 同指标重复异常只推一次
        seen.add(key)
        if a["metric"] == "home_reviews":
            # 差评按"每条正文哈希"去重:只推没推过的新差评,不受静默期约束。
            # 全推过 → 跳过整条,绝不重复发旧差评。
            items = _parse_bad_items(a.get("detail"))
            hashes = [_rhash(x) for x in items]
            done = _reviews_pushed(db_path, a["asin"], a["domain"], hashes)
            fresh_items = [x for x, h in zip(items, hashes) if h not in done]
            fresh_hashes = [h for x, h in zip(items, hashes) if h not in done]
            if not fresh_items:
                continue
            a["_new_items"] = fresh_items
            new_review_hashes[(a["asin"], a["domain"])] = fresh_hashes
        elif now - _last_push(db_path, *key) < cfg["mute_h"] * 3600:
            continue  # 其他指标:静默期内不重复打扰
        groups.setdefault((a["asin"], a["domain"]), []).append(a)
    if not groups:
        return 0

    # ASIN 辨识名:只用页面上抓到的 Model Number(型号);抓不到就不显示名字。
    # 顺带取商品页 url,推送里 ASIN 做成可点链接
    names, urls = {}, {}
    with store._connect(db_path) as conn:
        for (asin, domain) in groups:
            row = conn.execute(
                "SELECT model_number, url FROM profiles "
                "WHERE asin=? AND domain=?", (asin, domain)).fetchone()
            names[(asin, domain)] = (row[0] or "").strip() if row else ""
            urls[(asin, domain)] = (row[1] or "").strip() if row else ""

    def fmt_item(a) -> str:
        metric = METRIC_LABELS.get(a["metric"], a["metric"])
        old, new = a.get("old_value"), a.get("new_value")
        if a["metric"] == "buybox":
            # 推送统一格式:BuyBox变化:原 X → 新 Y(空的一侧显示"无")
            metric = "BuyBox变化"
            detail = f"原 {old or '无'} → 新 {new or '无'}"
        else:
            detail = " → ".join(x for x in (old, new) if x) \
                or a.get("detail", "")
        # 严重级用小号几何符号(• 严重 / ▸ 中度 / ◦ 轻微),不用彩色 emoji
        flag = {"critical": "•", "warning": "▸", "info": "◦"}.get(
            a["severity"], "◦")
        line = f"{flag} {metric}: {detail}"
        # 差评告警:只挂本次新推的差评正文(引用块内再缩进一层)
        if a["metric"] == "home_reviews":
            quoted = "\n>\n".join(
                f"> > {x['star']}★ " + " —— ".join(
                    y for y in (x.get("title", ""), x.get("text", "")) if y)
                for x in a.get("_new_items") or [])
            if quoted:
                line += f"\n{quoted}"
        return line

    MAX_GROUPS = 10
    ordered = sorted(groups.items(),
                     key=lambda kv: min(0 if i["severity"] == "critical"
                                        else 1 for i in kv[1]))

    # 变体族聚合:同一种子的子体变化并进一个块,消息以「族」为单位
    families: dict[tuple, dict] = {}
    for (asin, domain), items in ordered:
        seed = store.family_seed_of(db_path, asin, domain)
        fam = families.setdefault((seed, domain),
                                  {"seed": seed, "domain": domain, "members": []})
        fam["members"].append((asin, items))
    fam_order = sorted(
        families.values(),
        key=lambda f: min(0 if i["severity"] == "critical" else 1
                          for _a, its in f["members"] for i in its))

    # 钉钉 markdown 单个 \n 不换行,行间一律用空行分隔
    blocks = []
    for fam in fam_order[:MAX_GROUPS]:
        seed, domain = fam["seed"], fam["domain"]
        cc = DOMAIN_CC.get(domain, domain.replace("amazon.", "").upper())
        multi = len(fam["members"]) > 1
        seed_name = names.get((seed, domain), "")
        seed_url = urls.get((seed, domain)) or f"https://www.{domain}/dp/{seed}"
        lines = []
        if multi:
            others = [a for a, _ in fam["members"] if a != seed]
            head_txt = (f"> {cc} [**{seed}**]({seed_url})"
                        f"{' ' + seed_name if seed_name else ''} 变体族 · "
                        f"共 {len(fam['members'])} 个有变化")
            if others:
                head_txt += f" (含 {len(others)} 个子体)"
            lines.append(head_txt)
        # 主商品排最前,子体按 ASIN 排序;成员之间空一行分隔,
        # 免得变化行串到上一个成员名下(钉钉里引用块之间用独立行断开)
        members = sorted(fam["members"], key=lambda x: (x[0] != seed, x[0]))
        all_chg = []
        for asin, items in members:
            name = names.get((asin, domain), "")
            url = urls.get((asin, domain)) or f"https://www.{domain}/dp/{asin}"
            if not multi:
                lines.append(f"> {cc} [**{asin}**]({url})"
                             f"{' ' + name if name else ''}")
            else:
                # 多成员时族头已署名种子,子体逐个标;主商品不再重复标题
                tag = f"{cc} " if asin == seed else "子体 "
                lines.append(f"> {tag}[**{asin}**]({url})"
                             f"{' ' + name if name else ''}")
            for a in items:
                m = "BuyBox变化" if a["metric"] == "buybox" else METRIC_LABELS.get(
                    a["metric"], a["metric"])
                all_chg.append((f"{asin[:6]}… {m}" if multi else m,
                                a.get("old_value") or "", a.get("new_value") or ""))
            lines += [f"> {fmt_item(a)}" for a in items]
        # AI 解读(可选):配了 key 才调,失败静默跳过;
        # 规范按 单链接 > 国家 > 全局 三层解析。整族一次调用,结论更整体
        # (用种子 ASIN 的身份调用:它是这族的代表,型号/规范都挂在它身上)
        from . import ai
        if ai.get_ai_config(db_path)["key"]:
            s = ai.summarize_changes(db_path, seed, seed_name, cc, domain, all_chg)
            if s:
                lines.insert(1, f"> 解读: {s}")
        blocks.append("\n>\n".join(lines))
    text = (f"{datetime.now():%F %T}\n\n" + "\n\n".join(blocks))
    if len(ordered) > MAX_GROUPS:
        text += f"\n\n…还有 {len(ordered) - MAX_GROUPS} 个 ASIN"
    ok, _ = push_markdown(db_path, "AmCheck 监控告警", text)
    if ok:
        for items in groups.values():
            for a in items:
                _mark_pushed(db_path, a["asin"], a["domain"], a["metric"])
        # 记本次推送过的差评哈希,下次同一条差评不再重复推
        for (asin, domain), hashes in new_review_hashes.items():
            _mark_reviews_pushed(db_path, asin, domain, hashes)
    return sum(len(v) for v in groups.values()) if ok else 0
