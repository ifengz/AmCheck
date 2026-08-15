# AmReview · Amazon 单条评价链接批量检测

粘贴评价 permalink,逐条判定 **✅存活(星级/标题/作者/日期)/ 🐕已删(变狗)/ 🤖被拦 / 🍪登录失效 / ❓未知**,结果导出 CSV,SQLite 留存历史可对比"上次还在/这次没了"。方案依据 `doc/04-终版方案`。

## 架构一句话

**浏览器和 Amazon 登录态全部在服务器端。** 任何人用任何设备(Win/Mac/Linux/手机)打开网页粘贴链接即可检测,自己电脑上什么都不装、不受任何影响。迷你项目:同时在线 ≤3 人,**不加锁、不上队列**,同一域名同时只开一个登录会话即可。

## 文件

| 文件 | 作用 |
|------|------|
| `app.py` | Streamlit 网页:检测输入/结果 + **网页端引导登录面板** |
| `engine.py` | 检测引擎:URL 规范化 + 状态机判定 + 字段提取,可独立 CLI 使用 |
| `weblogin.py` | 网页引导登录:服务器端浏览器 + 登录页截图推流 + 验证码中继 |
| `login.py` | 命令行引导登录(本机调试/SSH X11 用,网页登录的替代品) |
| `accounts.json` | 各站点小号凭据(复制 `accounts.json.example` 填写;网页上会展示账号/密码/二维码) |
| `history.db` | 检测历史(自动创建) |
| `screenshots/` | 已删/未知/被拦项截图证据(自动创建) |

## 网页端引导登录(核心设计)

登录一次,长期保存(存服务器 `~/.amreview/profile/<域名>/`,通常数周才失效一次)。打开网站首页 → 展开「🍪 引导登录」:

1. 选站点,页面**直接显示该站小号账号、密码和二维码**(凭据来自服务器 `code/accounts.json`,谁打开网站谁就能登;未配置则手动填面板里的账号/密码输入框,效果相同)
2. 点「🚀 开始登录」→ 服务器浏览器自动:打开登录页 → 填账号 → 填密码 → 提交,页面同步显示实时截图
3. 若停在 OTP / 图片验证码页 → 在输入框填码,点「提交验证码」中继到服务器浏览器。**配了 TOTP 密钥则此步全自动**:小号在 Amazon「登录与安全 → 两步验证」选「验证器 App」,二维码下方点「无法扫描二维码?」会显示一串字母密钥,填进 `accounts.json` 的 `totp_secret`(或网页上的 TOTP 输入框),OTP 由服务器自己算自己填
4. 检测到 `at-main`/`x-main` cookie 自动保存,提示 ✅ 完成;之后所有检测静默复用

检测结果出现 🍪 登录失效时,页面会点名哪些站点需要重新登录,展开面板重复上述步骤即可。

> 内网站点,凭据对打开网站的人可见——按需配置;不配 accounts.json 也可用 ①③ 手动步骤登录。

## 本机使用

```bash
cd code
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/streamlit run app.py
# 或不用网页,直接命令行检测
.venv/bin/python engine.py "https://www.amazon.com/gp/customer-reviews/RXXXX/"
```

## 宝塔面板部署(内网)

1. **Python 项目管理器**(宝塔软件商店,Python 3.10+),上传 `code/` 目录,添加项目:框架 Streamlit、启动文件 `app.py`、端口 `8765`
2. 项目虚拟环境里执行一次:`playwright install chromium`;有条件再 `apt install google-chrome-stable`(Chrome 穿 Amazon 守卫已实测,Chromium 是兜底)
3. 复制 `accounts.json.example` 为 `accounts.json`,填入各站点小号凭据
4. **Nginx 反代**:网站 → 添加站点 → 反向代理到 `http://127.0.0.1:8765`;若进度条不动,反代配置加 WebSocket 支持:
   ```nginx
   proxy_http_version 1.1;
   proxy_set_header Upgrade $http_upgrade;
   proxy_set_header Connection "upgrade";
   ```
5. 登录态:**直接在网页上引导登录**(见上节),无需 SSH;本机 `login.py` + rsync 档案目录是备选
6. 开机自启;`history.db`、`accounts.json`、`screenshots/` 都在项目目录内,随目录保留

## 限速与账号安全

默认 20 条/批、每条间隔随机 3~5 秒,强度低于人工浏览。多人共用同一份登录态和服务器 IP,限速是全局的——总量控制在**每站点每天 ~500 条**以内;超了再加代理轮换(引擎已按域名分档案,接口预留)。

## 判定逻辑速查

```
/ap/signin → 🍪登录失效(匿名热身复访一次后仍弹才判)
"Server Busy" → 热身首页后重试(≤3次)
/edgex/guard、captcha → 退避 8/20/40s 重试(≤3次)→ 🤖被拦截
HTTP 404 / "Page Not Found" / "Looking for something?" / /dogs/ → 🐕已删
div[data-hook="review"] → ✅存活,提取星级/标题/作者/日期/VP
其他 → ❓未知(自动截图)
```
