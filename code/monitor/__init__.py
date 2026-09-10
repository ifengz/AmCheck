"""monitor —— 链接异常监控包(阶段 1-3:数据模型 + 规则引擎 + 异常看板)。

对应架构文档 doc/06。独立于现有 engine.py(评价检测),共用登录态/Playwright
底层,但业务逻辑分开。模块:
- model.py    数据契约(ProductProfile / SnapshotRecord)
- store.py    SQLite 三表(profiles/snapshots/anomalies)追加式
- address.py  采集接口 + 适配器(阶段 4 换 Playwright 自研解析)
- rules.py    判定引擎(你的核心 Know-how)
- baseline.py 基线管理(首拍设基点 / 确认无误前移)
- pipeline.py 一轮采集→判定→落 anomalies 的链路
- board.py    Streamlit 异常优先看板
"""

from __future__ import annotations

from .store import DEFAULT_DB  # noqa: F401

DB_PATH = DEFAULT_DB
