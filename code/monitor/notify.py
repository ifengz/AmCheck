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


def _push_webhook(cfg: dict, text: str) -> tuple[bool, str]:
    """群机器人 webhook(自定义机器人,支持钉钉/企微加签)。"""
    url = cfg["webhook"]
    if not url:
        return False, "未配置 webhook"
    if cfg["secret"] and "dingtalk" in url:
        ts = str(round(time.time() * 1000))
        url += f"&timestamp={ts}&sign={_sign(cfg['secret'], int(ts))}"
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


def _push_app(cfg: dict, text: str) -> tuple[bool, str]:
    """企业内部应用机器人单聊推送,收件人取 cfg["user_ids"]。"""
    if not cfg["client_id"] or not cfg["client_secret"]:
        return False, "未配置应用 Client ID / Secret"
    if not cfg["user_ids"]:
        return False, "未配置收件人 userid"
    try:
        tok = _access_token(cfg["client_id"], cfg["client_secret"])
    except Exception as e:
        return False, f"取 accessToken 失败: {e.__class__.__name__}: {e}"
    body = json.dumps({
        "robotCode": cfg["robot_code"] or cfg["client_id"],
        "userIds": cfg["user_ids"],
        "msgKey": "sampleText",
        "msgParam": json.dumps({"content": text}, ensure_ascii=False),
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
    """推一条文本;按配置选群机器人或企业应用。返回 (成功?, 说明)。"""
    cfg = get_config(db_path)
    if cfg["mode"] == "app":
        return _push_app(cfg, text)
    return _push_webhook(cfg, text)


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
    """把本轮新异常(静默期外)汇总推一条;返回实际推送条数。"""
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
    lines = []
    seen = set()
    for a in fresh:
        key = (a["asin"], a["domain"], a["metric"])
        if key in seen:
            continue  # 同指标重复异常只推一次
        seen.add(key)
        if now - _last_push(db_path, *key) < cfg["mute_h"] * 3600:
            continue  # 静默期内,不重复打扰
        metric = METRIC_LABELS.get(a["metric"], a["metric"])
        detail = " → ".join(x for x in (a.get("old_value"), a.get("new_value"))
                            if x) or a.get("detail", "")
        lines.append((key, f"{'🚨' if a['severity'] == 'critical' else '⚠'} "
                      f"[{metric}] {a['asin']}({a['domain']}) {detail}"))
    if not lines:
        return 0
    texts = [t for _, t in lines]
    head = (f"AmCheck 监控告警:新增 {len(texts)} 条异常\n"
            f"时间 {datetime.now():%F %T}\n" + "\n".join(texts[:10]) +
            ("\n…" if len(texts) > 10 else ""))
    ok, _ = push_text(db_path, head)
    if ok:
        for key, _t in lines[:10]:
            _mark_pushed(db_path, *key)
    return len(lines) if ok else 0
