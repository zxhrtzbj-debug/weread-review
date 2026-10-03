"""httpx 的最小替代：只用标准库。

为什么自己写
──────────
本项目原本依赖 httpx 做外网请求。为了它要引入 pip → venv → 几十 MB 安装目录，
运行前运行后不一样，违背了"关掉窗口就不留痕迹"的目标。
这里只实现项目真正用到的能力：GET/POST/HEAD、query 参数、form/json/raw body、
超时、重定向跟随、headers 与 set-cookie 读取。约 100 行，换来零依赖。

用法对齐 httpx 的那部分 API（status_code / text / json() / raise_for_status），
上层几乎不用改。

同步调用走线程执行器，避免阻塞 asyncio loop。
"""

from __future__ import annotations

import asyncio
import gzip
import json as _json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

DEFAULT_TIMEOUT = 30.0

# 页面抓取 + LLM 请求都可能并发出现，给一个共用线程池，避免每次请求都建线程
_EXECUTOR = ThreadPoolExecutor(max_workers=16, thread_name_prefix="fetch")

_GZIP_MAGIC = b"\x1f\x8b"

# 某些 Python 构建（最典型是 python.org 官方 macOS 安装包，且没跑过
# /Applications/Python 3.13/Install Certificates.command）自带的 OpenSSL
# 指向一个并不存在的证书束，create_default_context() 会加载到 0 个根证书，
# 于是任何 HTTPS 请求都报 CERTIFICATE_VERIFY_FAILED。这里记下几个系统级
# 证书束，仅在那种「确实一个证书都没有」的情况下回退，绝不关闭校验。
_CA_BUNDLE_CANDIDATES = (
    "/etc/ssl/cert.pem",
    "/etc/ssl/certs/ca-certificates.crt",
    "/usr/local/etc/openssl/cert.pem",
    "/opt/homebrew/etc/openssl/cert.pem",
)

_SSL_CTX: ssl.SSLContext | None = None


class FetchError(Exception):
    """对应 httpx.HTTPStatusError 的最小形态。"""

    def __init__(self, status: int, url: str = "", body: str = ""):
        super().__init__(f"HTTP {status} for {url}")
        self.status = status
        self.url = url
        self.body = body[:500]


class Resp:
    """对应 httpx.Response 的最小形态。"""

    __slots__ = ("status_code", "headers", "url", "_body")

    def __init__(self, status_code: int, body: bytes, headers, url: str = ""):
        self.status_code = status_code
        self.headers = headers          # email.message.Message：getName 大小写不敏感
        self.url = url
        self._body = body

    @property
    def content(self) -> bytes:
        return self._body

    @property
    def text(self) -> str:
        return self._body.decode("utf-8", "replace")

    def json(self):
        return _json.loads(self._body.decode("utf-8", "replace"))

    def raise_for_status(self):
        if self.status_code >= 400:
            raise FetchError(self.status_code, self.url, self.text)

    def get_all(self, name: str) -> list[str]:
        """同名 header 的全部取值（set-cookie 会重复出现）。"""
        return self.headers.get_all(name) or []


def _merge_query(url: str, params: dict | None) -> str:
    if not params:
        return url
    clean = {k: v for k, v in params.items() if v is not None}
    if not clean:
        return url
    sep = "&" if "?" in url else "?"
    return url + sep + urllib.parse.urlencode(clean)


def _decompress(raw: bytes) -> bytes:
    # 有些站点不认 Accept-Encoding 也会回 gzip，这里兜一层
    if raw[:2] == _GZIP_MAGIC:
        try:
            return gzip.decompress(raw)
        except Exception:
            return raw
    return raw


def _try_ca_bundle(path: str) -> ssl.SSLContext | None:
    """用指定证书束建 context，建不出来或里面一个证书都没有则返回 None。"""
    try:
        ctx = ssl.create_default_context(cafile=path)
    except Exception:
        return None
    return ctx if ctx.cert_store_stats().get("x509") else None


def ssl_context() -> ssl.SSLContext:
    """全局共用的 TLS context，带根证书缺失兜底。

    正常环境（默认 context 已加载根证书）走原路径，零副作用；
    只有在一个根证书都拿不到的构建上才回退到系统证书束。
    """
    global _SSL_CTX
    if _SSL_CTX is not None:
        return _SSL_CTX

    ctx = ssl.create_default_context()
    if not ctx.cert_store_stats().get("x509"):
        for path in _CA_BUNDLE_CANDIDATES:
            if not os.path.exists(path):
                continue
            fallback = _try_ca_bundle(path)
            if fallback is not None:
                ctx = fallback
                break
        else:
            # 系统证书束也没有（例如 Windows），退到 certifi；装了才用
            try:
                import certifi

                fallback = _try_ca_bundle(certifi.where())
            except Exception:
                fallback = None
            if fallback is not None:
                ctx = fallback

    _SSL_CTX = ctx
    return _SSL_CTX


def request(
    method: str,
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    json_body: dict | None = None,
    data: dict | None = None,
    content: bytes | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> Resp:
    """发一次请求。4xx/5xx 不抛异常，照 httpx 的习惯把响应体带回来。"""
    url = _merge_query(url, params)
    hdrs = dict(headers or {})
    body = None

    if json_body is not None:
        body = _json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    elif data is not None:
        body = urllib.parse.urlencode(data).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
    elif content is not None:
        body = content

    req = urllib.request.Request(url, data=body, headers=hdrs, method=method.upper())
    ctx = ssl_context()

    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return Resp(r.status, _decompress(r.read()), r.headers, url)
    except urllib.error.HTTPError as e:
        # HTTPError 本身就是个 response，正文照样要
        return Resp(e.code, _decompress(e.read() or b""), e.headers, url)


async def arequest(*args, **kwargs) -> Resp:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_EXECUTOR, lambda: request(*args, **kwargs))
