# 微信读书 · AI 阅读评价

把微信读书里的**划线 / 想法 / 书评**提取出来，交给大模型生成一份阅读评价报告。

**后端零第三方依赖** —— HTTP 层、数据模型、HTTP client 全是标准库实现。
不建虚拟环境、不 `pip install`、磁盘零写入。`git clone` 完就能跑。

```bash
git clone <本仓库> && cd review_weread
python3 backend/main.py
```

浏览器会自动打开 `http://127.0.0.1:8777/`。没装 Python？下面有各平台一键脚本。

---

## 目录

- [在线体验](#在线体验)
- [快速开始](#快速开始)
- [它做了什么](#它做了什么)
- [在线部署](#在线部署)
- [安全须知](#安全须知-先看这条)
- [配置项](#配置项)
- [接口一览](#接口一览)
- [架构](#架构)
- [开发](#开发)

---

## 在线体验

想先看看效果再决定要不要部署：**第 2 步点「🧪 一键体验」**，
不需要登录、不需要任何 API Key，灌入示例书单直接跑通后面 4 步。

> 真实数据需要你自己的微信读书登录态 + 一个 LLM API Key（DeepSeek、
> 通义千问、OpenAI 等任意 OpenAI 兼容接口都行，自己填）。

---

## 快速开始

需要 **Python 3.10 或更高版本**，仅此而已。

### 双击启动（推荐）

| 系统 | 操作 |
|---|---|
| macOS | 双击 `start.command` |
| Windows | 双击 `start.bat` |
| Linux / WSL | `chmod +x start.sh && ./start.sh` |

脚本会自己找可用的 Python、起服务、开浏览器。
**关掉终端窗口 = 进程彻底结束，不留任何文件。**

### 命令行启动

```bash
python3 backend/main.py              # 默认自动开浏览器
python3 backend/main.py --no-browser # 不开标签页
```

### Windows 用户注意

首次运行如果提示找不到 Python，去 [python.org](https://www.python.org/downloads/) 安装，
**安装时务必勾选 "Add Python to PATH"**。

### 可选：扫码登录

只有「扫码登录微信读书」需要 Playwright。不想装也能用——手动粘贴 cookie
或直接用「🧪 一键体验」都行。

```bash
pip install -r backend/requirements-login.txt
playwright install chromium
```

---

## 它做了什么

### 1. 提取过程可观测

点「开始提取」之后，任何一步卡住都能定位到具体环节：

| 位置 | 显示内容 |
|---|---|
| 自检面板 | 逐条列出网络连通 / 登录态检查、HTTP 结果与各自耗时 |
| 进度条 | 起步阶段走**跑马灯**条纹（总量未知 ≠ 卡死），拿到总量后转确定进度 |
| 进度条下方 | 已用时秒表，每秒跳动 |
| 阶段文案 | 后端真实阶段：`正在刷新登录态…` / `正在获取笔记本列表…` / `正在扫描书架…` |
| 终端 | `[weread] extract: start mode=…` 及逐本结果；异常时打印完整 traceback |

自检不通过会**直接不发起抓取**，并提示是登录态过期还是被限流。

### 2. 只提取书评（请求数约减半）

第 3 步多了一个提取范围选择：

- **全量**：划线 + 想法/笔记 + 书评（默认）
- **🔥 只提取书评**：只抓书评 + 书籍信息（书名/作者/封面/分类/评分/简介），跳过划线

可行性依据是接口本就彼此独立，不需要新造流程：

```
书评     review/list?bookId=…&listType=11&mine=1     ← fetch_reviews
划线     shelf/sync（预取） + bookmarklist（兜底）    ← fetch_bookmarks
书籍信息  book/info?bookId=…                          ← fetch_book_info
章节标题  chapterInfos（仅用于标注划线所在章节）        ← fetch_chapters
```

逐本抓取时会并发请求这四个接口，只取书评即**直接不请求** `fetch_bookmarks`
与 `fetch_chapters`，每本从 4 次降到 2 次。
12 本书的实测：`50 → 26` 次请求（**-48%**），书评条数与书籍信息完全一致。

时间线仍然保留（来自 `shelf/sync` 的 `sort` 字段，是"最近阅读时间"的唯一来源）。

### 3. 内容分类筛选

第 4 步顶部的筛选栏决定「哪些内容真的会送进 AI」：

- 三个勾选项：划线 / 想法·笔记 / 书评
- 每本书的条数上限（填 0 或留空 = 不限）
- 四个一键预设，**「只保留书评」最省 token**
- 实时显示预估 token 数与相对全量的节省比例

实现要点：session 里的 `data` 永远是原始全量，筛选在 `store.active_data()`
里现场投影，所以可以随时来回切换而不会丢数据。

token 估算口径：CJK 按 1 token/字、其余按 4 字符/token，不确定度约 ±30%，
只用来比较不同方案的量级，不做计费依据。

### 4. 联网补检冷门书籍

第 5 步打开「🌐 联网补检冷门书籍」。跑的是**固定五道工序**，全程不让 LLM 参与决策：

```
① 选书      本地打分：每本书算"本地信息缺失度"（无简介 +2 / 无评分 +2 / 无笔记 +1），
            排序后卡 min_score 与 max_books 两道闸
② 出检索式  纯模板：《书名》 作者 主要内容/核心观点 · 《书名》 作者 豆瓣评分/适合谁读
③ 检索      调你选的那个 provider，每本最多 N 条
④ 排序      按 URL 去重，有摘要的优先，其次摘要长的优先
⑤ 编译      压成紧凑文本块，总长封顶 SEARCH_DIGEST_CHARS（默认 6000 字）
```

运行时只有一次 LLM 调用：最后那份评价报告。
这样做的好处是开销可预测——不会因为模型"觉得需要多查几本"就把 token 烧掉几倍。

「补检深度」预设：

| 预设 | 最多几本 | 触发门槛 | 每本书几条检索式 |
|---|---|---|---|
| 精简 | 3 | 缺失度 ≥ 3 | 1 |
| 标准（默认） | 5 | ≥ 2 | 2 |
| 完整 | 8 | ≥ 1 | 2 |

检索 provider：

| id | 说明 |
|---|---|
| `weread` | **默认**。复用登录态取站内量化指标 + 他人书评（官方简介数据里已有，不再重复返回） |
| `tavily` / `brave` / `serper` | 付费 API，稳定，适合当主力（Tavily 1000 次/月、Brave 2000 次/月免费） |
| `searxng` | 自建元搜索，免 Key 无限额 |
| `duckduckgo` / `bing` | 免 Key 抓取，实测会被 202 风控 / 结果不相关，仅兜底 |
| `jina` | 搜索 + 正文提取，现需 Key |
| `mock` | 离线假数据，仅供一键体验验证流程 |

> 实测记录（2026-10）：DuckDuckGo 前 2 条查询正常、第 3 条起全部 202；
> Bing 能通但结果与查询不相关；Jina 免 Key 已下线返回 401。
> 无 Key 且已登录时 `weread` 是最稳的一档，有 Key 就直接上 Tavily/Brave。

---

## 在线部署

### 方式一：一键部署到 Render

仓库里有 `render.yaml`，Render 会自动识别：

1. 打开 Render，New → Blueprint
2. 填入本仓库地址
3. 在 Environment 里加一个 Secret：`ACCESS_TOKEN` = 你的随机口令
4. 部署完成，拿到链接

生成随机口令：

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(24))"
```

部署完把链接连口令一起发给别人：`https://你的域名/?token=<口令>`
前端会记住口令，不用重复输入。

> 免费额度提醒：free 实例 15 分钟无请求就休眠，冷启动约 30–60 秒。介意请升 starter。

### 方式二：Docker

```bash
docker build -t weread-review .
docker run -d -p 8777:8777 \
  -e WEREREAD_DEPLOY=1 \
  -e ACCESS_TOKEN=你的口令 \
  --name weread-review \
  weread-review
```

### 方式三：自己的服务器

```bash
git clone <本仓库> && cd review_weread
ACCESS_TOKEN=你的口令 WEREREAD_DEPLOY=1 PORT=8777 python3 backend/main.py
```

前面挂 nginx / Caddy 套个 HTTPS 即可。

---

## 安全须知（先看这条）

**这个项目会处理你的微信读书登录 cookie 和 LLM API Key。**

- 全部只在**内存**里，磁盘零写入，进程退出即消失
- 本地运行时只监听 `127.0.0.1`，只有你自己能访问——这是最安全的形态
- **公网部署必须设 `ACCESS_TOKEN`**，否则任何拿到链接的人都能用你的实例
  消耗你的额度，并把它当作跳板去请求微信读书

另外这是一个**个人项目、非官方工具**，与腾讯/微信读书无任何关联。
自动化调用微信读书接口可能违反其用户协议，请自行评估使用范围，建议仅用于个人数据整理。

详见 [SECURITY.md](SECURITY.md)。

---

## 配置项

全部走环境变量，都有合理的默认值，本地什么都不用设。

| 变量 | 默认 | 说明 |
|---|---|---|
| `PORT` | `8777` | 监听端口。部署时由平台注入 |
| `HOST` | `127.0.0.1` | 监听地址。`WEREREAD_DEPLOY=1` 时默认 `0.0.0.0` |
| `WEREREAD_DEPLOY` | — | 设为 `1` 时自动绑 `0.0.0.0` 并关闭自动开浏览器 |
| `ACCESS_TOKEN` | 空 | 访问口令。空 = 不鉴权（**仅限本机**） |
| `SESSION_TTL` | `7200` | 会话空闲回收秒数 |
| `MAX_SESSIONS` | `200` | 会话数上限，超出按最久未使用丢弃 |

LLM 的 Base URL / API Key / Model 不走环境变量——在前端第 1 步填，
只存在当前会话的内存里。

---

## 接口一览

```
GET    /                          首页
GET    /api/ping                  后端存活
GET    /api/health                探活 + 部署自检（免鉴权）
POST   /api/auth/session          建会话（返回 uid）
POST   /api/auth/cookies/{uid}    写入登录 cookie
POST   /api/browser/start         启动无头浏览器扫码
GET    /api/browser/qrcode/{sid}  二维码 PNG
GET    /api/browser/status/{sid}  SSE：登录结果
POST   /api/data/preflight/{uid}  提取前自检（连通性 + 登录态）
GET    /api/data/status/{uid}     提取状态快照
POST   /api/data/extract/{uid}    开始提取（body: {mode: "full"|"book_reviews"}）
GET    /api/data/progress/{uid}   SSE：working → timeline → filtering → notebooks → book_done → complete
POST   /api/data/set-filter/{uid} 提交时间/书籍筛选
POST   /api/data/content-filter/{uid}  提交内容分类筛选
POST   /api/data/delete-book/{uid}     删除
POST   /api/data/restore-book/{uid}    撤销删除
GET    /api/data/result/{uid}     取生效数据
POST   /api/demo/load/{uid}       载入示例数据
GET    /api/analysis/estimate/{uid}  token 估算
POST   /api/analysis/style/{uid} 切换报告风格
POST   /api/analysis/start/{uid}  开始分析（body: use_search）
GET    /api/analysis/progress/{uid}   SSE
GET    /api/analysis/report/{uid}     取报告
GET    /api/search/providers      provider 元信息
GET/POST /api/search/config/{uid}     检索配置
POST   /api/search/test           连通性测试
POST   /api/search/preview/{uid}  预览补检结果（不调用 LLM）
GET/POST /api/llm/config/{uid}
POST   /api/llm/test              连通性测试
```

---

## 架构

```
backend/
  main.py           应用装配 + 静态页 + 启动入口（唯一入口文件）
  config.py         常量与部署配置（端口/绑定/口令/会话 TTL）
  store.py          SessionStore：会话状态 + 删除/内容筛选的投影逻辑 + 回收
  routers/
    auth.py         扫码登录、会话建立
    data.py         提取（SSE）、时间筛选、内容筛选、删除、示例数据
    analysis.py     AI 评价、报告、联网补检配置与测试
    llm.py          LLM 配置与连通性测试
  services/
    weread_api.py   微信读书 HTTP 接口层（只换 JSON，不做业务）
    weread.py       提取编排：并发、review 解析、章节映射、统计
    browser.py      Playwright 扫码登录
    llm.py          OpenAI 兼容客户端
    analyzer.py     prompt 组装、token 估算、报告生成与解析
    search.py       联网补检：固定五道工序 + 多 provider
    demo.py         示例数据预设
  stdhttp.py        极简 HTTP 层（ThreadingHTTPServer + 单条后台 loop，支持 SSE）
  stdmodel.py       数据模型基类（dataclass + model_dump）
  stdfetch.py       HTTP client（urllib + 线程池，含 gzip/多 Set-Cookie 处理）
frontend/index.html 单文件前端（6 步向导）
smoke_test.py       23 项冒烟测试，标准库即可运行
archive/diagnose/   归档的一次性调试脚本
```

**分层约定：`routers` 只做参数校验和 SSE 转发，业务逻辑一律在 `services`。**

### 为什么自己写 HTTP 层

本项目原本用 fastapi + uvicorn + httpx。为了它们要引入 pip → 虚拟环境 →
几十 MB 安装目录，运行前后目录不一样，违背了"关掉窗口就不留痕迹"的目标。
这里只需要十几条 JSON 路由、两三条 SSE 流、一个静态首页，
一个 `ThreadingHTTPServer` + 单条后台 asyncio loop 就够。

代价是 handler 返回值有约定：`dict` → JSON 200，`Raw(bytes, ctype)` → 原样返回，
`SSE(factory)` → text/event-stream，抛 `HTTPError` → JSON 错误响应。

---

## 开发

```bash
python3 smoke_test.py           # 23 项冒烟测试，应输出「全部通过 ✓」
python3 -m compileall -q backend/
```

CI 会在 Python 3.10 / 3.12 / 3.13 上跑测试，外加 JS 语法检查和 Docker 构建。

改动前请读 [CONTRIBUTING.md](CONTRIBUTING.md)，尤其是两条硬约定：
**后端保持零第三方依赖**、**`session["data"]` 绝不原地修改**。

---

## 归档说明

`archive/diagnose/` 下三个脚本是当初排查 bookmarklist API 失败原因时写的，
问题早已定位（改用 `shelf/sync` 预取），且与主流程无引用关系，故移出 backend。

---

## 许可

[MIT](LICENSE) © review_weread contributors

个人项目，与腾讯/微信读书无任何关联。
