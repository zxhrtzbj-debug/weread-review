# 贡献指南

感谢你愿意动手。这个项目不大，但有几条约定能让改动保持在正确的路上。

## 开发环境

**没有虚拟环境，没有 pip install。** 后端 HTTP 层、数据模型、HTTP client
全部是标准库实现（`stdhttp.py` / `stdmodel.py` / `stdfetch.py`），
替代了 fastapi + uvicorn + httpx。所以：

```bash
python3 backend/main.py          # 就能跑
python3 smoke_test.py           # 跑测试
```

如果你的改动需要 `import requests`，先停下来重新想想有没有标准库的做法——
引入第三方依赖会让「clone 下来就能跑」这个属性消失，而那是本项目最核心的便利性。

唯一允许的可选依赖是 `playwright`，且只有「扫码登录」路径用得到
（`backend/requirements-login.txt`）。不装也能用手动粘贴 cookie 或 demo 数据。

## 架构约定

```
routers/   只做参数校验和 SSE 转发，不写业务逻辑
services/  全部业务逻辑
```

判断新代码该放哪：能在 router 里放三行以内就放，放不下就进 service。

`session["data"]` **永远是原始全量数据，绝不原地修改**。
删除书籍和内容筛选都在 `store.active_data()` 里现场投影，
所以用户可以随时来回切换筛选而不会丢数据。想改这条约定请先开 issue 讨论。

## 提交前必跑

```bash
python3 smoke_test.py           # 应输出「全部通过 ✓」
python3 -m compileall -q backend/
```

改了 `frontend/index.html` 的话，额外确认内嵌 JS 能解析：

```bash
node -e "
const fs=require('fs');
const m=fs.readFileSync('frontend/index.html','utf8').match(/<script[^>]*>([\s\S]*?)<\/script>/);
new Function(m[1]); console.log('✓ JS 语法通过');
"
```

## 提交信息

沿用 Conventional Commits：

```
feat: 新增书评导出为 Markdown
fix: 端口被占用时页面永远打不到服务
docs: 补充 provider 对比表
chore: 归档 diagnose 脚本
```

## PR 流程

1. 从 `main` 切分支：`git checkout -b 你的分支名`
2. 改动 + 跑通冒烟测试
3. 提 PR，CI 会自动跑三个 Python 版本 + JS 语法 + Docker 构建

## 加依赖的判断标准

只有同时满足这三条才允许加：

1. 标准库真的做不到（不是"不方便"，是"做不到"）
2. 装它需要虚拟环境或污染全局环境
3. 它带来的能力对核心流程（提取 → 筛选 → 评价）有实质必要性

三条里有任何一条不满足，就是不该加。
