# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [未发布]

### 新增

- **区分本地上传文件与上架书籍**（`services/weread.py: classify_book_source`）
  本地上传的文件名常常不是书名（扫描件、论文 PDF），混进书单会让 LLM
  对着一串文件名编造阅读品味。判据三条：书名带电子书扩展名 → 本地；
  `book/info` 无返回 / 带 errcode / 无书名 → 本地；出版物元数据
  （ISBN、评分、出版社、分类…）加权命中不足 → 本地。判定依据回传前端。
  本地文件默认**不进书单**、**不参与联网补检**。
- **本地文件的手动选入**：第 4 步新增面板，可给文件挑分类
  （印象最深 / 最想推荐 / 启发性最大 / 平时最有用，支持自定义新增）
  并写一段介绍感悟；这段文字放在该书的划线与想法**前面**。
  端点 `GET|POST /api/data/local-picks/{uid}`。
- **来源判定的人工改判**：`POST /api/data/source-override/{uid}`。
  自动判定按元数据缺失度打分，冷门书偶有误差，改判是那个出口。
- **用户评价（我给这本书打的分）**：从 `review/list` 的 `star` 字段提取，
  兼容三种嵌套结构与两种口径（0-5 星 / 20-100 百分制），-1 视为无评分。
  一本书可写多条书评但评分只有一次 —— 取带 `star` 的那条。
  书单信息行、抽样、内容筛选三处都用得上。
- **抽样顺延 + 倒挂两组**：每个分类在各自排序里跳过已被占用的书继续往后取，
  不再出现"评分最高的两本恰好也是划线最多的两本，后一个分类白拿名额"。
  新增"我的评价高于社区评分"与"低于社区评分"各 2 本，用于暴露品味差异。
  样本上限 16 本。
- **内容筛选新增 `ratedOnly`**：只保留我打过分的书的划线/想法，
  其余书只留书单行。打分是一次明确的偏好表态，比按条数截断更有信息量。

### 修复

- **空信息不再扰乱 LLM**：某类内容被关掉后，原本每本书都会写"想法 0 条"，
  模型会对着满篇 0 长篇分析"为什么书评多却没想法"。
  现在顶部声明一次口径，正文里未纳入的类型连总数都不出现，
  摘录小节为空则整节省略。系统提示也加了同口径说明。
- `review` 字段取用改为三层嵌套查找（`r` / `r.review` / `r.review.review`）；
  原来只查两层，公开点评那类结构的 `content` 与 `star` 会整条读不到。
- `review/list` 请求加 `count=100`：默认分页下评分那一条可能落在第二页之外。
- `cf-rated-only` 复选框漏绑 `change` 事件（jsdom 真实点击才暴露出来），
  勾了不发请求。

### 测试

- `archive/diagnose/test_sampling.py`：抽样顺延与倒挂两组的断言。
- `archive/diagnose/jsdom_local_picks.js`：jsdom 真实点击验证本地文件面板。
- `archive/diagnose/probe_rating.py`：用真实 cookie 校准评分字段与来源判定。
- `frontend_static_check.js`：内嵌 JS 语法 + DOM id 引用检查（已进 CI）。

## [0.4.0] - 2026-10-04

### 新增

- **公网部署支持**：监听地址与端口改从环境变量读取（`HOST` / `PORT`），
  设 `WEREREAD_DEPLOY=1` 时默认绑 `0.0.0.0`。
- **访问口令鉴权**：设 `ACCESS_TOKEN` 后所有 `/api` 请求需带口令
  （`?token=` 或 `X-Access-Token` 头）。前端首次打开会提示输入并记住。
  SSE 接口走 query 传参，因为 `EventSource` 不能自定义请求头。
- `GET /api/health` 探活接口，供 Render / Fly 等平台做健康检查。
- **会话回收**：空闲超 `SESSION_TTL`（默认 2 小时）自动释放，
  会话数超 `MAX_SESSIONS`（默认 200）按最久未使用丢弃。
  公网实例不回收会在几天内被 OOM 掉。
- 跨平台启动脚本：`start.sh`（Linux/WSL）、`start.bat`（Windows），
  与原有 macOS `start.command` 行为一致。
- `Dockerfile` + `render.yaml`，支持一键部署。
- `smoke_test.py`：23 项冒烟测试，标准库即可运行，CI 与本地共用。
- GitHub 工程化：CI（三版本 + JS 语法 + Docker）、dependabot、
  issue/PR 模板、CONTRIBUTING、SECURITY、CODE_OF_CONDUCT。

### 修复

- 端口由平台通过 `PORT` 指定时，不再静默顺延——直接报错说明端口被占。
- 启动时若配置了 `ACCESS_TOKEN`，不再自动打开浏览器（打开也无意义）。

### 安全性

- 历史提交中不含任何真实 cookie、API Key 或本机路径。
  `session` 数据全部在内存，进程退出即消失。
