"""ai —— 告警摘要(可选):把规则判出的变化列表交给 LLM 生成一句人话。

定位:纯增强,不是依赖。没配 key / 调用失败 / 超时都静默降级(返回空串),
推送照常发,只是少了那行解读。

配置读取优先级:settings 表(UI「定时与通知」里配的)> 环境变量兜底。
    AMCHECK_AI_KEY / ai_api_key      没有 key 整体跳过 AI 环节
    AMCHECK_AI_BASE / ai_base_url    默认 https://api.deepseek.com
    AMCHECK_AI_MODEL / ai_model      默认 deepseek-chat

解读规范(prompt)三层,从高到低:
    单链接:profiles.ai_prompt(该 ASIN 专属)
    国家:  settings ai_prompt_<CC>(如 ai_prompt_US)
    全局:  settings ai_prompt
都没有则用 _SYSTEM 默认。
"""

from __future__ import annotations

import json
import os
import urllib.request

from . import store

_TIMEOUT = 40          # 秒;思考型模型耗时长,超时就放弃摘要,不拖慢推送
_MAX_TOKENS = 3000     # 思考型模型(reasoning)会先烧掉大量 token 再写正文,
                       # 给太小会出现 finish_reason=length 且 content 为空

_BASE_RULES = ("你是亚马逊运营助手。根据给定商品的监控变化列表,输出一句中文结论。"
               "硬性格式:单行纯文本,40字以内,不加序号、不换行、不用markdown、"
               "不用emoji、不重复罗列原始数据。")

_DEFAULT_SYSTEM = (_BASE_RULES + "内容上直接给判断,像老练运营扫一眼后的结论。")


def get_ai_config(db_path) -> dict:
    """AI 接入配置:settings 优先,环境变量兜底。"""
    return {
        "key": store.get_setting(db_path, "ai_api_key") or
               os.environ.get("AMCHECK_AI_KEY", ""),
        "base": store.get_setting(db_path, "ai_base_url") or
                os.environ.get("AMCHECK_AI_BASE", "https://api.deepseek.com"),
        "model": store.get_setting(db_path, "ai_model") or
                 os.environ.get("AMCHECK_AI_MODEL", "deepseek-chat"),
    }


def _post(cfg: dict, system: str, user: str) -> str:
    body = json.dumps({
        "model": cfg["model"],
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "max_tokens": _MAX_TOKENS, "temperature": 0.3}).encode()
    req = urllib.request.Request(
        f"{cfg['base'].rstrip('/')}/v1/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {cfg['key']}"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            d = json.loads(r.read())
        out = (d["choices"][0]["message"].get("content") or "").strip()
    except Exception:
        return ""      # 任何失败都静默降级
    # 兜底:模型偶尔仍输出多行,压成单行并截断,免得撑爆推送
    out = " ".join(x.strip() for x in out.splitlines() if x.strip())
    return out[:80]


def resolve_prompt(db_path, asin: str, domain: str, cc: str) -> str:
    """解读规范:单链接 > 国家 > 全局 > 默认。

    用户自定义规范是"附加要求",必须与基础约束(一句话/40字内/不逐条复述)
    拼在一起下发,否则用户写"严格点名"这种短指令时,模型会当成输出模板照抄。
    """
    custom = ""
    try:
        if domain:
            custom = store.profile_ai_prompt(db_path, asin, domain).strip()
    except Exception:
        pass
    if not custom and cc:
        custom = store.get_setting(db_path, f"ai_prompt_{cc}").strip()
    if not custom:
        custom = store.get_setting(db_path, "ai_prompt").strip()
    if not custom:
        return _DEFAULT_SYSTEM
    return (f"{_BASE_RULES}\n补充要求(优先遵守,但仍受上面格式约束):{custom}")


def _post(cfg: dict, system: str, user: str) -> str:
    body = json.dumps({
        "model": cfg["model"],
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "max_tokens": _MAX_TOKENS, "temperature": 0.3}).encode()
    req = urllib.request.Request(
        f"{cfg['base'].rstrip('/')}/v1/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {cfg['key']}"})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            d = json.loads(r.read())
        return (d["choices"][0]["message"].get("content") or "").strip()
    except Exception:
        return ""      # 任何失败都静默降级


def summarize_changes(db_path, asin: str, name: str, cc: str, domain: str = "",
                      changes: list[tuple[str, str, str]] | None = None) -> str:
    """changes: [(指标, 旧值, 新值)];返回一句摘要,失败返回空串。"""
    changes = changes or []
    if not changes:
        return ""
    cfg = get_ai_config(db_path)
    if not cfg["key"]:
        return ""
    lines = "\n".join(f"- {m}: {o or '无'} → {n or '无'}"
                      for m, o, n in changes)
    ident = f"{asin}{' ' + name if name else ''}({cc})"
    user = f"商品 {ident} 监控到以下变化:\n{lines}"
    return _post(cfg, resolve_prompt(db_path, asin, domain, cc), user)
