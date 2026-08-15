# UI 优化与扩展设计文档

> 日期：2026-08-15
> 目的：优化当前 UI 体验，为未来产品链接检测功能留空间

---

## 一、优化内容总结

### 1.1 视觉优化

**状态徽章系统**（[app.py:37-53](app.py:37-53)）
- 登录状态从纯文字升级为色块徽章
- 三种状态：🟢 在线(绿)、⚪ 离线(灰)、🟡 过期(黄)
- 统一圆角 12px，视觉上与 metric 卡片呼应

**Metric 卡片增强**（[app.py:37-53](app.py:37-53)）
- 添加 hover 阴影过渡效果
- label 颜色统一为 #6B7280（中灰）
- 卡片圆角 10px，border 1px solid #E5E7EB
- 百分比占比作为 delta 显示，数据更直观

**全局样式优化**（[app.py:37-53](app.py:37-53)）
- 表格圆角 8px，overflow:hidden 避免内容溢出
- 容器统一圆角 10px
- 色彩体系保持 Amazon 橙(#FF9900)主题

### 1.2 交互优化

**登录状态可视化**（[app.py:252-259](app.py:252-259)）
- 按钮类型动态：全部在线 → primary(橙)，部分在线 → secondary(灰)
- 状态数 `{online}/{total}` 一目了然

**进度显示优化**（[app.py:298-309](app.py:298-309)）
- 进度文本增加域名简称（如 `com`、`in`）
- 显示当前检测项状态 + 标题预览（20 字符）
- 格式：`[3/20] com · R1XXX → ✅ 正常 · Great product…`

**CSV 导出优化**（[app.py:378-381](app.py:378-381)）
- 文件名包含时间戳 + 条数：`amreview_20260815_1200_20条.csv`
- 自动 UTF-8-BOM 编码，Excel 直接打开不乱码

**数据洞察增强**（[app.py:337-357](app.py:337-357)）
- metric 卡片显示百分比占比
- 多站点时自动显示分站统计（折叠面板）
- 各站存活率对比，快速识别问题站点

### 1.3 数据优化

**空值处理**（[app.py:361-374](app.py:361-374)）
- 空字段统一显示 `—`（破折号），而非空白
- 首次检测的"上次"列显示 `—`，更简洁

**状态变化标记**（[app.py:361-374](app.py:361-374)）
- 状态变化时显示 ⚠️ 符号
- 格式：`🐕 已删 · 2026-08-14 ⚠️`

---

## 二、扩展预留（已设计但未启用）

### 2.1 产品链接检测

**引擎层预留**（[engine.py:52-75](engine.py:52-75)）

```python
@dataclass
class ProductRef:
    raw: str
    asin: str
    domain: str
    url: str

检测字段：
- title: 产品标题
- bullet_points: BP 列表
- price: 价格(含货币)
- deal_tag: Deal 标签(Lightning Deal 等)
- sold_by: 卖家名称
- rating: 评分(4.5)
- review_count: 评价数
- top_review: 首页评价预览
- main_image: 主图 URL
- availability: 库存状态
```

**方法接口**（[engine.py:287-311](engine.py:287-311)）

```python
def check_products(self, product_refs: list[ProductRef], 
                   delay: tuple = (3, 5),
                   on_result=None) -> list[dict]:
    """批量检测产品链接,提取字段"""
    pass
```

**数据库表预留**（[app.py:49-64](app.py:49-64)）

```sql
CREATE TABLE IF NOT EXISTS product_history (
    asin TEXT, domain TEXT, url TEXT, status TEXT,
    title TEXT, price TEXT, currency TEXT, deal_tag TEXT,
    sold_by TEXT, rating REAL, review_count INTEGER,
    main_image TEXT, availability TEXT,
    note TEXT, checked_at TEXT
)
```

**UI 切换器预留**（[app.py:274-288](app.py:274-288)）

```python
check_type = st.radio(
    "检测类型",
    options=["评价链接", "产品链接(ASIN)"],
    horizontal=True
)
```

### 2.2 AI 复核功能

**按钮入口预留**（[app.py:389-392](app.py:389-392)）

```python
if any(r["status"] == "unknown" for r in results):
    if st.button("🤖 AI 复核未知项"):
        # 调用 GLM-4V 分析截图
        pass
```

调用时机：检测结果包含 ❓ 未知项时显示按钮

功能：
- 读取 `screenshots/` 下的截图
- 调用 GLM-4V API 分析页面状态
- 返回建议判定：存活/已删/被拦截/其他

### 2.3 批量任务/队列模式

**按钮预留**（[app.py:285-288](app.py:285-288)）

```python
if btn_col2.button("📋 加入队列", help="添加到后台检测队列"):
    st.info("队列功能开发中，将支持大批量任务后台运行")
```

设计思路：
- 当前是实时检测（阻塞式，20 条约 60-100 秒）
- 队列模式：提交任务 → 后台运行 → 完成后通知
- 适合 100+ 条的大批量检测

技术方案：
- SQLite 任务表：`tasks(id, status, input, result, created_at)`
- 单独进程循环轮询 `pending` 任务
- 前端轮询任务状态，完成后显示结果

### 2.4 快速开始引导

**折叠面板预留**（[app.py:318-327](app.app.py:318-327)）

```python
with st.expander("ℹ️ 快速开始", expanded=False):
    st.markdown("""
    1. **首次使用**：配置站点登录态
    2. **粘贴链接**：每行一条，六国混贴
    3. **查看结果**：自动保存历史，可导出 CSV
    4. **AI 复核**：未知项可启用截图分析
    """)
```

### 2.5 状态筛选器

**多选框预留**（[app.py:375-377](app.py:375-377)）

```python
filter_status = st.multiselect(
    "筛选状态",
    options=list(STATUS_LABEL.values()),
    default=list(STATUS_LABEL.values())
)
filtered_table = [row for row in table if row["状态"] in filter_status]
```

---

## 三、组件使用规范（不手搓原则）

### 3.1 已使用的 Streamlit 公共组件

- `st.container(border=True)` — 卡片容器
- `st.metric(label, value, delta)` — 统计数值
- `@st.dialog(title, width)` — 弹窗
- `st.columns([ratio1, ratio2])` — 响应式布局
- `st.text_area()` — 多行输入
- `st.button()` — 按钮
- `st.progress()` — 进度条
- `st.dataframe()` — 表格
- `st.expander()` — 折叠面板
- `st.download_button()` — 文件下载
- `st.image()` — 图片展示
- `st.markdown()` — 富文本（仅用于徽章等轻量样式）

### 3.2 样式定制原则

**允许的样式定制**：
- 全局 CSS 变量（颜色、圆角、间距）
- Streamlit 组件的微调（hover 效果、边框）
- 状态徽章等小型 inline 元素

**禁止的样式定制**：
- 手写完整的自定义组件（如自己实现表格、弹窗）
- 破坏 Streamlit 原生组件的核心交互
- 大量 JavaScript 注入

---

## 四、代码位置索引

| 功能 | 文件 | 行号 |
|------|------|------|
| 全局样式（徽章/metric） | app.py | 37-53 |
| 登录弹窗徽章显示 | app.py | 178-192 |
| 顶栏登录按钮动态 | app.py | 252-259 |
| 热度条格式优化 | app.py | 261-269 |
| 检测输入区（预留产品检测） | app.py | 272-295 |
| 进度显示优化 | app.py | 298-309 |
| 分站统计卡片 | app.py | 337-357 |
| 表格数据处理 | app.py | 361-378 |
| CSV 导出优化 | app.py | 380-383 |
| AI 复核按钮预留 | app.py | 389-392 |
| 产品检测类型预留 | engine.py | 52-75 |
| 产品检测方法预留 | engine.py | 287-311 |
| 数据库表预留 | app.py | 49-64 |

---

## 五、未来开发优先级

### P0（高优先级，用户明确需求）

1. **产品链接检测** — 用户已明确需求：检查标题/BP/价格/Deal Tag/Sold by/评分/评价数/首页评价/图片
2. **多站点对比** — 当前热度条已支持，需加强分站对比维度

### P1（中优先级，提升体验）

3. **AI 复核未知项** — GLM-4V 截图分析，降低人工复核成本
4. **状态筛选器** — 大批量检测时快速定位问题项

### P2（低优先级，规模化需求）

5. **批量任务/队列** — 100+ 条检测时避免阻塞
6. **快速开始引导** — 首次使用时的新手引导

---

## 六、设计哲学

1. **优先使用公共组件** — Streamlit 组件已覆盖 90% 需求，不手搓
2. **预留而不实现** — 扩展接口提前设计，注释清晰，随时可启用
3. **渐进式增强** — 先把核心功能做稳，再逐步增加高级功能
4. **保持克制** — 不为了炫技而加功能，每个功能都对应真实需求
5. **数据驱动** — 热度条、分站统计等基于历史数据的洞察优先

---

## 七、技术债务（已规避）

- ❌ 没有手写复杂自定义组件（维护成本高）
- ❌ 没有引入前端框架（React/Vue），保持 Streamlit 纯净
- ❌ 没有过度设计数据库（SQLite 够用，暂不上 PostgreSQL）
- ❌ 没有过早优化性能（20 条/次场景下无需并发/异步）

---

## 八、待确认事项

1. **产品检测的字段优先级** — 标题/价格/评分是必需，其他字段哪些最重要?
2. **AI 复核的成本** — GLM-4V API 调用频率和预算限制
3. **队列模式的触发阈值** — 多少条以上才需要后台运行?
