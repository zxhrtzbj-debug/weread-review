# 更新日志

本项目遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [未发布]

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
