# 微信读书账号 AI 评价系统 — 设计文档

## 1. 项目定位

通过微信读书 Web 端 API，获取用户阅读数据（书架、划线、笔记），为后续 AI 分析提供数据基础。

### 当前阶段（Phase 1）
- 扫码登录（后端代理 WeRead 认证）
- 全量数据提取（书架、划线、笔记、书籍信息）
- 数据在前端展示并保存在 IndexedDB

### 后续阶段（Phase 2）
- AI 分析用户阅读画像
- 生成评价报告（毒舌/正经双风格）

## 2. 架构

```
┌──────────────────┐      HTTP/SSE       ┌──────────────────┐      HTTP        ┌──────────────┐
│  前端             │ ◄────────────────► │  后端 FastAPI    │ ◄─────────────► │  WeRead API  │
│  index.html      │                    │  backend/        │                 │  i.weread    │
│  (无框架 Vanilla) │                    │  main.py         │                 │  .qq.com     │
└──────────────────┘                    │  session.py      │                 └──────────────┘
                                        └──────────────────┘
```

### 关键设计决策
- **后端代理登录**：不依赖无头浏览器，直接 HTTP 调用 WeRead 的 `getuid`/`getinfo` 接口处理扫码登录。后端拦截 Set-Cookie 头获取认证凭据。
- **SSE 推送**：登录等待和数据提取进度均使用 Server-Sent Events，前端实时显示状态。
- **前端无框架**：MVP 阶段使用单页面 Vanilla JS + Dexie.js（IndexedDB 封装）。

## 3. 后端设计

### 目录结构
```
backend/
├── main.py            # FastAPI 应用、路由
├── session.py         # WeRead 登录 + 数据提取逻辑
└── requirements.txt   # 依赖
```

### API 端点

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/auth/qrcode` | POST | 获取 UID，返回 QR 图片 URL |
| `/api/auth/qrcode-img/{uid}` | GET | 代理返回二维码图片（PNG） |
| `/api/auth/status/{uid}` | GET | SSE：等待扫码完成，捕获 Cookie |
| `/api/data/extract/{uid}` | POST | 启动全量数据提取（异步） |
| `/api/data/status/{uid}` | GET | 提取状态查询 |
| `/api/data/result/{uid}` | GET | 获取提取结果 JSON |

### 数据模型

```json
{
  "user": {
    "uid": "",
    "cookies": {}
  },
  "books": [
    {
      "bookId": "",
      "title": "",
      "author": "",
      "cover": "",
      "category": "",
      "rating": 0.0,
      "intro": "",
      "chapterCount": 0,
      "totalBookmarks": 0,
      "totalReviews": 0,
      "bookmarks": [
        { "chapterTitle": "", "markText": "", "createTime": 0, "style": 0 }
      ],
      "reviews": [
        { "content": "", "createTime": 0, "type": 1 }
      ]
    }
  ],
  "stats": {
    "totalBooks": 0,
    "totalWithNotes": 0,
    "totalBookmarks": 0,
    "totalReviews": 0,
    "topCategories": [],
    "topAuthors": []
  }
}
```

## 4. 扫码登录流程

```
Frontend                Backend                    WeRead
   │                       │                         │
   │  POST /api/auth/qrcode │                         │
   │──────────────────────►│  POST /web/login/getuid  │
   │                       │─────────────────────────►│
   │  {uid, qrcode_url}    │◄─────────────────────────│
   │◄──────────────────────│       {uid: "xxx"}       │
   │                       │                         │
   │  <img qrcode_url>     │                         │
   │  (用户用微信扫描)      │                         │
   │                       │                         │
   │  GET /api/auth/status  │                         │
   │  (SSE) ──────────────►│  GET /web/login/getinfo  │
   │                       │  (long poll, ~30s)       │
   │                       │  ... 用户扫码确认 ...     │
   │                       │◄─────────────────────────│
   │                       │  Set-Cookie: wr_*        │
   │  event: login_ok      │                         │
   │◄──────────────────────│                         │
```

## 5. 数据提取流程

```
1. GET /user/notebooks     → 获取有笔记的书籍列表
2. 对每本书并发执行（最大5并发）:
   a. GET /book/info        → 书籍详情
   b. GET /book/bookmarklist → 划线列表
   c. GET /review/list      → 笔记列表
3. 聚合数据 → 前端
```

## 6. 项目文件清单

```
review_weread/
├── docs/superpowers/specs/2026-05-14-weread-review-design.md
├── backend/
│   ├── main.py
│   ├── session.py
│   └── requirements.txt
└── frontend/
    └── index.html
```
