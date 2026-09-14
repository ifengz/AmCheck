"""chatbot —— 钉钉机器人聊天(Stream 模式)。

用 Stream 长连接接收@机器人的消息,无需公网回调地址、不用开端口
(实测可复用监控通知同一个企业应用凭据)。

支持的指令:
    帮助 / help          → 用法
    状态 / 概况           → 监控总数、异常数、站点分布
    列表 [国家码]         → 当前有异常的 ASIN 列表(如:列表 US)
    查 <ASIN>            → 该 ASIN 的最新快照 + 未确认异常
    <其他任意话>          → 交给 AI,带当前监控概况作上下文回答

启动:与 monitor_cli 共用凭据;.venv/bin/python -m monitor.chatbot
"""

from __future__ import annotations

import asyncio
import json
import threading

from . import store
from .model import DOMAIN_CC

HELP = ("AmCheck 监控助手,可用指令:\n"
        "· 状态 — 监控概况(总数/异常数)\n"
        "· 列表 [国家码] — 有异常的 ASIN,如「列表 US」\n"
        "· 查 <ASIN> — 某商品最新状态,如「查 B081RKPTM4」\n"
        "· 其他问题直接用大白话问,比如「美国站最近怎么了」")


def _overview(db_path) -> str:
    """监控概况:总数、启用数、未确认异常数、按站点分布。"""
    o = store.monitor_overview(db_path)
    by_cc = ", ".join(
        f"{DOMAIN_CC.get(d, d.replace('amazon.', '').upper())} {c}"
        for d, c in o["anomalies_by_domain"]) or "无"
    return (f"监控 {o['total']} 条(启用 {o['enabled']})· "
            f"未确认异常 {o['unconfirmed']} 条\n"
            f"异常分布:{by_cc}")


def _list_anomalies(db_path, cc: str = "") -> str:
    """当前有未确认异常的 ASIN 列表,可按国家码过滤。"""
    rows = store.unconfirmed_anomaly_rows(db_path)
    from .rules import METRIC_LABELS
    out = []
    for r in rows:
        asin, domain, metric = r["asin"], r["domain"], r["metric"]
        old, new, model = r["old_value"], r["new_value"], r["model_number"]
        site = DOMAIN_CC.get(domain, domain.replace("amazon.", "").upper())
        if cc and site != cc.upper():
            continue
        label = METRIC_LABELS.get(metric, metric)
        name = f" {model}" if model else ""
        out.append(f"· [{site}] {asin}{name} {label}: {old or '无'} → {new or '无'}")
    if not out:
        return f"当前{' ' + cc.upper() if cc else ''}没有未确认异常 ✅"
    head = f"未确认异常 {len(out)} 条" + (f"(仅 {cc.upper()})" if cc else "") + ":\n"
    return head + "\n".join(out[:15]) + ("\n…" if len(out) > 15 else "")


def _check_asin(db_path, asin: str) -> str:
    """某个 ASIN 的最新快照 + 未确认异常。"""
    asin = asin.strip().upper()
    profs = store.profiles_by_asin(db_path, asin)
    if not profs:
        return f"没找到 {asin} 的监控记录。先在监控页「添加监控」粘贴它的链接。"
    row = profs[0]
    domain, title, model, enabled = (row["domain"], row["title"],
                                     row["model_number"], row["monitor_enabled"])
    site = DOMAIN_CC.get(domain, domain.replace("amazon.", "").upper())
    snap = store.latest_snapshot(db_path, asin, domain) or {}
    lines = [f"{asin} {model or ''} ({site}) {'监控中' if enabled else '已停用'}",
             f"标题:{(title or snap.get('title') or '—')[:40]}"]
    if snap:
        lines.append(
            f"最新快照 {snap.get('checked_at', '')}\n"
            f"价格 {snap.get('price') or '—'} · 评分 {snap.get('rating') or '—'} · "
            f"评价数 {snap.get('review_count') or '—'} · "
            f"状态 {snap.get('status') or '—'}")
    anoms = [(a["metric"], a["old_value"], a["new_value"])
             for a in store.unconfirmed_anomalies(db_path)
             if a["asin"] == asin][:8]
    if anoms:
        from .rules import METRIC_LABELS
        lines.append("未确认异常:")
        lines += [f"· {METRIC_LABELS.get(m, m)}: {o or '无'} → {n or '无'}"
                  for m, o, n in anoms]
    else:
        lines.append("无未确认异常 ✅")
    return "\n".join(lines)


def _ai_answer(db_path, question: str) -> str:
    """自由问答:带监控概况作上下文,让 AI 用大白话答。"""
    from . import ai
    cfg = ai.get_ai_config(db_path)
    if not cfg["key"]:
        return "没配 AI key,只能回答固定指令。\n\n" + HELP
    ctx = _overview(db_path) + "\n\n" + _list_anomalies(db_path)
    system = ("你是亚马逊运营助手,通过钉钉回答用户关于商品监控的问题。"
              "用大白话、结论先行,不超过 120 字,不用 markdown 语法。"
              "只依据下面提供的实时数据回答,数据里没有的就说不知道。")
    user = f"实时监控数据:\n{ctx}\n\n用户问:{question}"
    return ai._post(cfg, system, user) or "AI 没返回内容,稍后再试。"


def handle_command(db_path, text: str) -> str:
    """把一条消息文本转成回复文本(不依赖钉钉,便于本地测试)。"""
    t = (text or "").strip()
    if not t:
        return HELP
    low = t.lower()
    if low in ("帮助", "help", "?", "？", "菜单"):
        return HELP
    if t.startswith(("状态", "概况", "summary")):
        return _overview(db_path)
    if t.startswith(("列表", "list")):
        parts = t.split()
        cc = parts[1] if len(parts) > 1 else ""
        return _list_anomalies(db_path, cc)
    if t.startswith(("查 ", "查:", "check")):
        return _check_asin(db_path, t.replace("查", "", 1).strip(" :："))
    # 纯 ASIN 直接查
    if len(t) == 10 and t.upper().startswith("B0"):
        return _check_asin(db_path, t)
    return _ai_answer(db_path, t)


def run(db_path) -> None:
    """启动 Stream 长连接,阻塞运行。凭据取通知配置里的企业应用。"""
    import dingtalk_stream
    from .notify import get_config
    cfg = get_config(db_path)
    if not (cfg["client_id"] and cfg["client_secret"]):
        raise SystemExit("未配置企业应用 Client ID/Secret,无法启动聊天机器人")

    class Handler(dingtalk_stream.ChatbotHandler):
        async def process(self, callback):
            msg = dingtalk_stream.ChatbotMessage.from_dict(callback.data)
            text = (msg.text.content if msg.text else "") or ""
            try:
                reply = handle_command(db_path, text)
            except Exception as e:
                reply = f"处理失败:{e.__class__.__name__}: {e}"
            self.reply_text(reply, msg)
            return dingtalk_stream.AckMessage.STATUS_OK, "OK"

    client = dingtalk_stream.DingTalkStreamClient(
        dingtalk_stream.Credential(cfg["client_id"], cfg["client_secret"]))
    client.register_callback_handler(
        dingtalk_stream.ChatbotMessage.TOPIC, Handler())
    client.start_forever()


def start_in_thread(db_path) -> threading.Thread:
    """后台线程启动(给 UI/主进程顺带拉起用);连不上不阻塞主流程。"""
    def _run():
        try:
            run(db_path)
        except Exception:
            pass
    th = threading.Thread(target=_run, daemon=True)
    th.start()
    return th


if __name__ == "__main__":
    from pathlib import Path
    import os
    db = Path(os.environ.get("AMCHECK_DB",
                             Path(__file__).resolve().parent.parent / "monitor.db"))
    print(f"启动钉钉聊天机器人(Stream 模式),库:{db}")
    run(db)
