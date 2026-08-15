# GitHub 调研:Amazon 自动化仓库(2026-08-15)

> 调研方法:`gh` CLI 仓库搜索(星数无关,要求近 5 个月内有提交)+ 代码级搜索(`edgex/guard`、`storage_state`、`curl_cffi` 等解法特征词)+ 逐个读 README/源码。
> 结论先行:**免费世界不存在"穿越"登录墙/验证码的方案**;真实公司的生产工具用的正是我们已采用的架构(持久化档案 + 人工登一次 + 限速 + 失效重登)。付费增量只有打码服务和代理池。

---

## 一、有价值仓库清单

### ⭐ 强相关(值得精读)

| 仓库 | 活跃 | 价值 |
|------|------|------|
| **[codingintheusa0402/spigen-gcx-automation](https://github.com/codingintheusa0402/spigen-gcx-automation)** | 2026-08-14 | **Spigen(真实大卖)客服团队内部工具**:Playwright + 持久化 Chrome 档案 + Seller Central 登录 + 评价监控,含 `/gp/customer-reviews/` 永久链接的批量抓取。与我们场景最接近,详见第二节拆解 |
| **[alexdlaird/amazon-orders](https://github.com/alexdlaird/amazon-orders)** | 2026-08-12(171★) | Python 买家账号自动化库:密码登录 + **TOTP 密钥自动生成 OTP**(`AMAZON_OTP_SECRET_KEY`)+ 可选接付费打码(`pip install amazon-orders[capsolver]`)过 WAF。TOTP 技巧是唯一值得抄的免费增量 |
| **[philipmulcahy/azad](https://github.com/philipmulcahy/azad)** | 2026-08-13(324★) | Chrome 扩展方案:跑在用户自己已登录的浏览器里,完全绕开服务端登录问题。适合个人用,不适合我们"服务器统一登录态"的需求 |

### 有参考价值(局部借鉴)

| 仓库 | 活跃 | 价值 |
|------|------|------|
| **[NanmiCoder/MediaCrawler](https://github.com/NanmiCoder/MediaCrawler)** | 2026-08-14(62k★) | 多平台(小红书/抖音/快手/B站/微博/贴吧/知乎)爬虫,登录墙处理集大成者。三种登录方式:扫码(终端渲染二维码+轮询 cookie 变化)、手机号、**Cookie 粘贴**;`tools/slider_util.py` 用 OpenCV 模板匹配自动解滑块;代理池 provider 抽象。逐项评估见 1.5 节 |
| [LXE123/LXE_AGENT](https://github.com/LXE123/LXE_AGENT) | 近期 | 代码里处理了 edgex guard,但只是**识别被拦**(HTTP 403/429/503 + "robot check" 文案标记)并把拦截原因写进诊断报告——优雅降级,不硬闯。启示:被拦项要留诊断信息 |
| [HRN-Projects/amazon-captcha-solver](https://github.com/HRN-Projects/amazon-captcha-solver) | 2026-06(94★) | TensorFlow CNN 识别 Amazon 老式图片验证码(Flask API)。只能解歪扭字母图,解不了滑块/拼图 |
| [gopkg-dev/amazoncaptcha](https://github.com/gopkg-dev/amazoncaptcha) | 2026-06(24★) | 同上,Go 标准库实现,无深度学习依赖。说明老式图片码确实是可解的 |
| [akaszynski/keepa](https://github.com/akaszynski/keepa) | 2026-08(311★) | Keepa 官方 API 的 Python 封装。背景:doc/03 已论证 Keepa 不覆盖单条评价,不适用 |

### 商业引流仓库(只当行情参考,不是解法)

| 仓库 | 说明 |
|------|------|
| [oxylabs/amazon-scraper](https://github.com/oxylabs/amazon-scraper)(3288★)及 [how-to-handle-amazon-captcha](https://github.com/oxylabs/how-to-handle-amazon-captcha)(1664★) | 付费 Scraper API 广告:住宅代理池 + 云端农场过验证码,按请求收费 |
| [Thordata/how-to-bypass-amazon-captcha-when-scraping](https://github.com/Thordata/how-to-bypass-amazon-captcha-when-scraping) | 同类,2026 版教程引流 |
| [ScrapingBee/amazon-review-scraper](https://github.com/ScrapingBee/amazon-review-scraper)(182★)、omkarcloud、scrapapi、data-scrape 系列 | 全是 API 引流壳,无真实代码 |
| [capsolver/capsolver-browser-extension](https://github.com/capsolver/capsolver-browser-extension)(94★) | 付费打码的浏览器扩展形态,约 $1~3/千次 |

### ⚠️ 警示类别(搜索时大量出现,全部跳过)

- **"下载 zip 使用"型**(如 amazon-reviews-scraper-with-advanced-filters):README 只有下载链接无源码——挂软件套路
- **登录页克隆练习项目**(`amazon-login` 关键词下近 5 个月提交的全是)
- **NLP 分析型**(`amazon review` 搜索结果 90%):拿现成数据集做情感分析,与爬取无关

### 1.5 爬虫通用层头部项目(2026-08-15 补充,非 Amazon 专属)

| 层 | 项目 | 状态 | 与本项目关系 |
|------|------|------|------|
| 浏览器指纹伪装 | [daijro/camoufox](https://github.com/daijro/camoufox)(11.1k★)、[patchright](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright)(4.1k★) | 均当日有提交,活跃 | **备胎已备好**:真 Chrome headless 失效时按 doc/04 预案升级 |
| 验证码识别 | [sml2h3/ddddocr](https://github.com/sml2h3/ddddocr)(14.6k★) | 当日有提交 | 一库三类码(图片 OCR/滑块缺口/点选目标),比单点 CNN solver 通用;留作后备,遇码再引入 |
| IP 代理池 | [jhao104/proxy_pool](https://github.com/jhao104/proxy_pool)(23.6k★) | 活跃 | 单站 >500 条/日才用,已预留接口 |
| 框架/规模 | [scrapy](https://github.com/scrapy/scrapy)(63.8k★)、[crawlee-python](https://github.com/apify/crawlee-python)(9.4k★) | 均当日有提交 | 为"百万级页面"设计,20 条/批用不上,过度设计 |
| 监控型爬虫 | [dgtlmoon/changedetection.io](https://github.com/dgtlmoon/changedetection.io)(33.1k★) | 活跃 | 思路同我们(检测变化而非爬全量),但面向公开页,不处理登录墙 |

### 1.6 MediaCrawler 逐项评估(2026-08-15 补充)


| MediaCrawler 的解法 | 对 Amazon 适用性 |
|------|------|
| **扫码登录**:截取页面二维码 → 终端渲染 → 轮询 cookie(`web_session` 变化)/UI 元素双信号判定登录成功 | ❌ **不适用**:Amazon 网页登录没有"App 扫码"入口,这是小红书/抖音特有的登录形态。我们用"账号密码自动填 + OTP 网页中继"是 Amazon 下的对应物 |
| **Cookie 粘贴登录**(`login_by_cookies`):从任一已登录浏览器复制 cookie 字符串,解析后 `add_cookies` 写入上下文 | ✅ **直接可搬**:作为第三条登录通道——懂 F12 的人在自己电脑登 Amazon,复制 cookie 粘贴到网页,服务器写入档案(`at-main`/`x-main`/`session-id` 等 + `.amazon.<域名>` domain),30 行左右。比截图推流快,适合管理员自救 |
| **OpenCV 滑块自动识别**(`slider_util.py`):模板匹配找缺口位置 + 拖动 | ⚠️ **留作后备**:实测 Amazon 评价页的墙是 edgex guard(真浏览器可过),极少弹滑块;真遇到再上,且 cv2 依赖较重,不值得预先引入 |
| 代理池 provider 抽象(极述/快代理/豌豆等) | 结构漂亮,单站 >500 条/日才用,doc/04 已预留接口 |
| `AbstractLogin` 每平台一个适配器、config 驱动 | 对单站点小项目是过度设计,不采纳 |


---

## 二、Spigen GCX 深度拆解(重点学习对象)

> 文件:`Scrapers/SC_Review_Scraper/scrape_sc_reviews.py`(1277 行,async Playwright)
> 场景:每天登录 Seller Central 抓全站点品牌评价 + 用 `/gp/customer-reviews/<ID>/` 永久链接批量取评价图片。**与我们同款 URL、同款登录墙问题,生产环境日级使用。**

### 2.1 架构与我们逐项对照

| Spigen 做法 | AmReview 现状 | 评价 |
|------|------|------|
| 专用持久化档案 `~/.chrome-scraper-profile`,登录一次长期复用 | `~/.amreview/profile/<域名>/` | ✅ 一致 |
| 人工在可见浏览器完成登录+OTP,后台模式给 300s 倒计时等登完 | 网页截图推流引导登录 | 同思路,他们更原始 |
| 每次启动逐门户检查会话(URL 含 `/ap/`、`signin`、**`mfa`** 即失效),有效则跳过登录 | 检测 `at-main`/`x-main` cookie | 他们的 URL 标记法多了 `mfa`,值得并入 |
| 抓取中途掉登录 → **暂停等人工重登(120s)→ 重试同一页,不丢进度** | 标 🍪 让用户整批重跑 | ✅ 值得抄 |
| 反检测分 LOW/MEDIUM/HIGH 三档结构化配置 | 固定随机 3~5s | 结构值得借鉴 |
| 默认有头浏览器,headless 需先完全退出 Chrome(档案锁) | headless 为主 | 我们服务器场景不同,保持 |

### 2.2 优秀技术点(按含金量排序)

**① 页内批量 fetch 替代逐页导航(含金量最高)**
批量访问评价永久链接时,他们**不逐条 `page.goto`**,而是在已登录页面的 JS 上下文里并发 `fetch(url, {credentials:'include'})`,带完整真实请求头(`Sec-Fetch-Dest/Mode/Site`、`Referer`、`Upgrade-Insecure-Requests`),每条错开随机抖动(0~600ms),用 `DOMParser` 解析返回 HTML 提取 `data-hook` 字段。
- 效果:同档安全性下速度快一个量级;请求形态是"已登录页面发出的 XHR",比连续导航更像真人
- 对我们:20 条链接检测可以从"逐条开页面(2~3 分钟)"变成"一个热页面批量 fetch(约 30 秒)",404/`data-hook=review`/登录页标记都能从 `resp.status` 和返回 HTML 判断

**② 检测规避配置档案化**
```python
_PROFILES = {"MEDIUM": {"nav_delay": (2.0,5.0), "read_delay": (1.0,2.5),
             "batch_delay": (2.0,4.5), "fetch_jitter": (0,600),
             "batch_min": 15, "batch_max": 22, "scroll": True}, ...}
```
延迟不是拍脑袋散落各处,而是按用途命名(导航/阅读/批次/抖动)集中成档,LOW/MEDIUM/HIGH 一键切换。我们目前只有一种 3~5s 延迟,可以照此结构化。

**③ 中途会话失效的"暂停-等待-原位重试"**
登录重定向 → 打印提示 → 等 120s(交互式等回车)→ **重试同一页且不前进**。不会因为掉登录丢掉整批进度。

**④ 懒渲染内容就绪守卫**
提取前 `wait_for_function`:等所有评价卡片的正文文本非空才提取,防止拿到骨架屏/旧 DOM 槽位数据;发现"不同 ID 但作者+标题雷同"的脏数据会整页重载再提取,仍脏则丢弃该行不污染结果。

**⑤ 滚动拟人化**
随机 60~180px 步进、60~120ms 间隔滚到底,8 秒硬上限保证不挂死。配合 `read_delay` 模拟阅读。

**⑥ 数据自校验**
从评价链接里反推域名,不信自己分配的变量("marketplace 切换可能静默失败");每页先验证页头显示的目标市场,不对就中止而不是抓错数据。

**⑦ 增量落盘**
每抓完一页立即 append 进 CSV,中途崩溃不丢已抓数据。

**⑧ CDP 教训**
他们从"attach 固定 CDP 端口 9222"迁移回 `launch_persistent_context(channel="chrome")`,注释写明:生产版 Chrome 拒绝 Playwright 在 `connect_over_cdp` 时发的 `Browser.setDownloadBehavior` 调用,且固定端口会和其他 CDP 工具冲突。印证我们直接用 `launch_persistent_context` 是对的。

### 2.3 不适用/暂不采纳

- **EU 多国共享一个会话串行切换**:Seller Central 特有(我们按域名分档案已天然隔离)
- **Google Sheets 上传、Monday.com 同步、GAS 集成**:他们的业务分发层,与我们无关
- **Seller Central 的 `kat-*` Web Component 选择器**:SC 后台专有,买家侧还是 `data-hook` 体系

---

## 三、对 AmReview 的改进清单(按收益排序)

1. **引擎升级为页内批量 fetch**(来自 Spigen ②.2-①):每域名先开一个已登录热页,批量 `fetch` 评价 permalink,`resp.status` + 返回 HTML 走同一套状态机。速度约 5 倍,代码量约 +40 行
2. **TOTP 支持**(来自 amazon-orders):小号开验证器 App 两步验证,`accounts.json` 存 TOTP 密钥,`pyotp` 自动算 OTP——引导登录彻底无人化(除非弹图片码)
2b. **Cookie 粘贴登录**(来自 MediaCrawler):网页登录面板加一个"粘贴 cookie"通道,管理员从自己浏览器复制后一键写入服务器档案
3. **登录失效原位重试**(Spigen ②.2-③):检测中途遇 🍪 暂停提示重登,恢复后续跑而不是整批重来
4. **登录判定并入 `mfa` URL 标记**(Spigen 会话检查)
5. **被拦项留诊断信息**(LXE_AGENT):🤖/❓ 结果里带上 HTTP 状态与拦截标记,便于事后分析
6. **延迟配置档案化**(Spigen ②.2-②):LOW/MEDIUM/HIGH 三档,现在用 MEDIUM 即可
