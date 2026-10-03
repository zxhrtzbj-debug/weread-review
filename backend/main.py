"""Weread Review —— 应用入口。

启动方式：
    python3 backend/main.py          # 推荐，start.command 双击走的也是这一句

零第三方依赖：HTTP 层、数据模型、HTTP client 全部是标准库实现
（stdhttp / stdmodel / stdfetch），所以不建虚拟环境、不装包、
不在项目目录里留下任何运行产物。

本文件只做装配：注册路由 + 起服务，业务在 services/，路由在 routers/。
"""

import sys

# 连 __pycache__ 都不要留：运行前后目录应当完全一致
sys.dont_write_bytecode = True

from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import threading  # noqa: E402
import webbrowser  # noqa: E402

from config import (  # noqa: E402
    ACCESS_TOKEN,
    INDEX_HTML,
    SERVER_HOST,
    SERVER_PORT,
    find_free_port,
)
from stdhttp import HTTPError, App, Raw, Request, run_server  # noqa: E402
from store import store  # noqa: E402

# 导入即注册路由
import routers.analysis  # noqa: F401,E402
import routers.auth  # noqa: F401,E402
import routers.data  # noqa: F401,E402
import routers.llm  # noqa: F401,E402

app = App()


@app.get("/")
def serve_index(request: Request):
    try:
        content = INDEX_HTML.read_bytes()
    except OSError:
        raise HTTPError(404, "index.html not found")
    return Raw(content, "text/html; charset=utf-8")


@app.get("/api/ping")
def ping(request: Request):
    """前端靠它判断后端在不在。"""
    return {"status": "ok", "service": "weread-review", "deps": "stdlib-only"}


@app.get("/api/health")
def health(request: Request):
    """平台探活 + 部署自检。

    免鉴权：不带这个接口，Render/Fly 的健康检查会一直把实例判成不健康。
    """
    return {
        "status": "ok",
        "version": "0.4.0",
        "auth_required": bool(ACCESS_TOKEN),
        "store": store.stats(),
    }


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    open_browser = "--no-browser" not in argv and not ACCESS_TOKEN

    port = find_free_port(SERVER_PORT)
    # 容器/反代后面拿到的 Host 才是真实地址，浏览器打开的链接要用它
    url = f"http://{SERVER_HOST}:{port}/"
    if SERVER_HOST == "0.0.0.0":
        url = f"http://127.0.0.1:{port}/"

    def on_ready(host: str, actual_port: int) -> None:
        print(f"→ 已启动：{url}")
        print(f"→ 监听 {host}:{actual_port}；关闭本窗口即完全退出，不留任何文件")
        if ACCESS_TOKEN:
            print("→ 已启用访问口令：所有 /api 请求需带 ?token=...")
        if open_browser:
            threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    run_server(SERVER_HOST, port, on_ready=on_ready)


if __name__ == "__main__":
    main()
