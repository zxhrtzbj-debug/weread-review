"""极简 HTTP 层：只用标准库，替代 fastapi + uvicorn。

为什么要替掉它们
────────────────
fastapi/uvicorn 装起来是两个包、几十 MB，还必须有虚拟环境；而本项目只需要：
  · 十几条 JSON 路由
  · 两三条 SSE 流式接口
  · 一个静态首页
用一个 ThreadingHTTPServer + 单条后台 asyncio loop 就够，代价是完全不装东西。

线程模型
────────
  HTTP 线程（每个连接一个）  ──读取请求──▶  判定是普通请求还是 SSE
        ▲                                      │
        │                                      ▼
        └──写回小块◀── Queue ──   后台 loop 线程跑业务协程（SSE/后台任务在此）

业务协程必须跑在同一条 loop 上，否则 analysis 的后台 task 与 SSE 读取看到的世界不一致。

handler 返回值约定
──────────────────
  dict / list / str  → JSON 200
  Raw(bytes, ctype)  → 原样返回（二维码 PNG 用）
  SSE(factory)       → text/event-stream，factory(request) 必须产 async generator
  HTTPError 异常     → JSON 错误响应
"""

from __future__ import annotations

import asyncio
import inspect
import json
import queue
import re
import secrets
import threading
import traceback
import types
import typing
from concurrent.futures import Future
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote

from stdmodel import Model

# ── 路由表 ────────────────────────────────────────────

ROUTES: list[tuple[str, re.Pattern, typing.Callable]] = []


class HTTPError(Exception):
    """对应 fastapi.HTTPException(status_code=..., detail=...)。"""

    def __init__(self, status_code: int = 400, detail: str = "", **extra):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass
class Request:
    method: str
    path: str
    params: dict[str, str] = field(default_factory=dict)
    query: dict[str, list[str]] = field(default_factory=dict)
    headers: dict = field(default_factory=dict)
    body: bytes = b""

    def json(self) -> dict:
        if not self.body:
            return {}
        try:
            return json.loads(self.body.decode("utf-8"))
        except Exception:
            raise HTTPError(400, "请求体不是合法 JSON")


@dataclass
class Raw:
    """直接返回原始字节。"""

    content: bytes
    media_type: str = "application/octet-stream"


@dataclass
class SSE:
    """SSE 流。factory(request) 应返回一个 async generator，逐块产出 str。"""

    factory: typing.Callable


class App:
    """替代 fastapi.APIRouter：提供 .get/.post 装饰器与 prefix。"""

    def __init__(self, prefix: str = ""):
        self.prefix = prefix.rstrip("/")

    def _register(self, method: str, path: str):
        full = self.prefix + path
        pattern = re.compile("^" + re.sub(r"{(\w+)}", r"(?P<\1>[^/]+)", full) + "$")

        def deco(fn):
            ROUTES.append((method, pattern, fn))
            return fn

        return deco

    def get(self, path): return self._register("GET", path)
    def post(self, path): return self._register("POST", path)
    def put(self, path): return self._register("PUT", path)
    def delete(self, path): return self._register("DELETE", path)


# ── 后台 loop ─────────────────────────────────────────

_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None


def start_loop() -> asyncio.AbstractEventLoop:
    global _loop, _loop_thread
    if _loop is not None:
        return _loop
    _loop = asyncio.new_event_loop()
    ready = threading.Event()

    def runner():
        asyncio.set_event_loop(_loop)
        _loop.call_soon(ready.set)
        _loop.run_forever()

    _loop_thread = threading.Thread(target=runner, name="app-loop", daemon=True)
    _loop_thread.start()
    ready.wait(5)
    return _loop


def stop_loop() -> None:
    global _loop, _loop_thread
    if _loop is None:
        return
    loop, thread = _loop, _loop_thread
    _loop, _loop_thread = None, None
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=3)


def run_coro(coro, timeout: float | None = None):
    """在后台 loop 上跑协程并同步取结果（HTTP 线程调用）。"""
    fut: Future = asyncio.run_coroutine_threadsafe(coro, start_loop())
    return fut.result(timeout)


# ── 参数绑定 ──────────────────────────────────────────

def _annotation_body(annotation):
    """把 handler 的注解解析成一种 body 形式。

    支持三种：dict（原样给 body）、Model 子类（构造实例）、Model | None（可空）。
    """
    origin = typing.get_origin(annotation)
    if annotation is dict or annotation is inspect.Parameter.empty:
        return "dict", None
    if isinstance(annotation, type) and issubclass(annotation, Model):
        return "model", annotation
    if origin in (types.UnionType, typing.Union):
        args = typing.get_args(annotation)
        for a in args:
            if isinstance(a, type) and issubclass(a, Model):
                return "optional_model", a
        if dict in args:
            return "dict", None
    return None, None


def _bind(fn, req: Request) -> typing.Any:
    kwargs: dict[str, typing.Any] = {}
    sig = inspect.signature(fn)
    # 路由模块普遍用了 `from __future__ import annotations`，此时注解是字符串，
    # 必须解析成真实类型才能判断该往里塞什么
    try:
        hints = typing.get_type_hints(fn)
    except Exception:
        hints = {}
    body_cache: dict | None = None

    for name, p in sig.parameters.items():
        if name in req.params:
            kwargs[name] = unquote(req.params[name])
            continue
        kind, model_cls = None, None
        annotation = hints.get(name, p.annotation)

        # body 参数：除 path 之外的那一个
        if annotation is Request:
            kwargs[name] = req
            continue

        resolved = _annotation_body(annotation)
        if resolved == (None, None):
            continue
        kind, model_cls = resolved

        if body_cache is None:
            try:
                body_cache = req.json()
            except HTTPError:
                if p.default is inspect.Parameter.empty:
                    raise
                kwargs[name] = p.default
                continue

        if kind == "dict":
            kwargs[name] = body_cache or {}
        elif kind == "model":
            kwargs[name] = model_cls(**(body_cache or {}))
        elif kind == "optional_model":
            kwargs[name] = model_cls(**(body_cache or {})) if body_cache else p.default

    return fn(**kwargs)


# ── HTTP handler ──────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "weread-review/0.3"

    # BaseHTTPRequestHandler 的默认日志又吵又没用，压掉
    def log_message(self, fmt, *args):  # noqa: D102
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def do_OPTIONS(self):  # noqa: N802
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self): self._handle("GET")      # noqa: N802
    def do_POST(self): self._handle("POST")    # noqa: N802
    def do_PUT(self): self._handle("PUT")      # noqa: N802
    def do_DELETE(self): self._handle("DELETE")  # noqa: N802

    def _match(self, method: str, path: str):
        for m, pattern, fn in ROUTES:
            if m == method and (mo := pattern.match(path)):
                return fn, mo.groupdict()
        return None, {}

    def _handle(self, method: str) -> None:
        path, _, query_string = self.path.partition("?")
        fn, params = self._match(method, path)

        if fn is None:
            self._send_json({"detail": f"no route for {method} {path}"}, status=404)
            return

        if not self._authorized(path, query_string):
            return

        req = Request(
            method=method,
            path=path,
            params=params,
            query=parse_qs(query_string),
            headers=dict(self.headers.items()),
            body=self._read_body(),
        )

        try:
            result = _bind(fn, req)

            if isinstance(result, SSE):
                self._emit_sse(result.factory(req))
                return

            payload = run_coro(result) if asyncio.iscoroutine(result) else result

            # handler 可能先 await 一点东西再返回 SSE，所以检查放在 await 之后
            if isinstance(payload, SSE):
                self._emit_sse(payload.factory(req))
                return

            if isinstance(payload, Raw):
                self._send_raw(payload.content, payload.media_type)
            else:
                self._send_json(payload)

        except HTTPError as e:
            self._send_json({"detail": e.detail}, status=e.status_code)
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001 - 顶层兜底，避免单请求打挂服务
            traceback.print_exc()
            self._send_json({"detail": f"{type(e).__name__}: {e}"}, status=500)

    # ── 访问口令 ──────────────────────────────────────
    # 免鉴权路径：只有页面本身和探活。少了这两个，浏览器直接打开就会 401，
    # 拿到链接的人连页面都打不开。
    _PUBLIC_PATHS = {"/", "/api/ping", "/api/health", "/favicon.ico"}

    def _authorized(self, path: str, query_string: str) -> bool:
        from config import ACCESS_TOKEN

        if not ACCESS_TOKEN or path in self._PUBLIC_PATHS:
            return True

        supplied = self.headers.get("X-Access-Token") or ""
        if not supplied:
            # EventSource 无法自定义请求头，SSE 只能走 query 传令牌
            supplied = (parse_qs(query_string).get("token") or [""])[0]

        # 常数时间比较：避免按字符猜口令
        if secrets.compare_digest(supplied, ACCESS_TOKEN):
            return True

        self._send_json(
            {"detail": "访问口令不正确。链接后加 ?token=你的口令，或设置 X-Access-Token 头。"},
            status=401,
        )
        return False

    # ── 响应写回 ──────────────────────────────────────

    def _send_json(self, payload, status: int = 200) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self._cors()
        self.end_headers()
        try:
            self.wfile.write(raw)
        except BrokenPipeError:
            pass

    def _send_raw(self, content: bytes, media_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", media_type)
        self.send_header("Content-Length", str(len(content)))
        self._cors()
        self.end_headers()
        try:
            self.wfile.write(content)
        except BrokenPipeError:
            pass

    def _emit_sse(self, agen) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self._cors()
        self.end_headers()

        loop = start_loop()
        q: queue.Queue = queue.Queue()

        async def pump():
            try:
                async for chunk in agen:
                    q.put(chunk)
            except Exception:  # noqa: BLE001 - 流里任何异常都要转成 SSE 错误帧
                traceback.print_exc()
                try:
                    q.put(json.dumps({"status": "error", "message": "stream failed"},
                                     ensure_ascii=False))
                except Exception:
                    pass
            finally:
                q.put(None)

        fut = asyncio.run_coroutine_threadsafe(pump(), loop)
        try:
            while True:
                chunk = q.get()
                if chunk is None:
                    break
                if isinstance(chunk, dict):
                    chunk = f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                try:
                    self.wfile.write(chunk.encode("utf-8"))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    break
        except Exception:  # noqa: BLE001
            pass
        finally:
            fut.cancel()


def serve_file(file_path, media_type: str = "text/html; charset=utf-8"):
    """把磁盘上的静态文件包成一个 handler（首页用）。"""
    def handler(request: Request):
        try:
            with open(file_path, "rb") as f:
                content = f.read()
        except OSError:
            raise HTTPError(404, "index.html not found")
        return Raw(content, media_type)

    return handler


# ── 启动 ──────────────────────────────────────────────

def run_server(host: str, port: int, *, on_ready=None) -> None:
    start_loop()
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True

    if on_ready:
        on_ready(host, port)

    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()

    stop_loop()
