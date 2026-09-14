"""monitor —— 链接异常监控包(阶段 1-3:数据模型 + 规则引擎 + 异常看板)。

对应架构文档 doc/06。独立于现有 engine.py(评价检测),共用登录态/Playwright
底层,但业务逻辑分开。模块:
- model.py    数据契约(ProductProfile / SnapshotRecord)
- store.py    SQLite 三表(profiles/snapshots/anomalies)追加式
- address.py  采集接口 + 适配器(阶段 4 换 Playwright 自研解析)
- rules.py    判定引擎(你的核心 Know-how)
- baseline.py 基线管理(首拍设基点 / 确认无误前移)
- pipeline.py 一轮采集→判定→落 anomalies 的链路
- view.py    看板数据聚合与展示整形(**纯逻辑,零框架依赖**)
- board.py    链接监控统一看板(Streamlit 渲染层)

分层约定(2026-09-14 解耦审计):
- 想拿看板数据 → import `monitor.view`(纯逻辑)。
- **别 import `monitor.board`** —— 它会连带加载整个 streamlit 栈;
  只有 Streamlit 界面(app.py)才该 import 它。
- 站点显示映射唯一一份在 `model.DOMAIN_CC` / `model.short_domain`。
"""

from __future__ import annotations

from .store import DEFAULT_DB  # noqa: F401

DB_PATH = DEFAULT_DB
