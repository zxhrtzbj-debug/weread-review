"""LLM 配置路由。"""

from __future__ import annotations

import socket
import ssl
import urllib.error
from dataclasses import dataclass, field

from stdhttp import App, HTTPError, Raw, SSE
from stdmodel import Model

from services.llm import CHAT_TIMEOUT, LLMConfig, LLMMessage, create_llm_client
from store import store

router = App(prefix="/api/llm")


@dataclass
class LLMConfigInput(Model):
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    temperature: float | None = None


@dataclass
class TestLLMInput(Model):
    base_url: str = ""
    api_key: str = ""
    model: str = ""


@router.get("/config/{uid}")
async def get_llm_config(uid: str):
    sess = store.get(uid)
    if not sess:
        raise HTTPError(status_code=404, detail="session not found")
    cfg = dict(sess.get("llm_config", {}))
    cfg.pop("api_key", None)
    return cfg


@router.post("/config/{uid}")
async def set_llm_config(uid: str, body: LLMConfigInput):
    sess = store.get(uid)
    if not sess:
        raise HTTPError(status_code=404, detail="session not found")
    cfg = store.set_llm_config(sess, body.model_dump(exclude_none=True))
    return {"status": "ok", "config": {**cfg, "api_key": ""}}


def _describe_error(e: Exception) -> str:
    """把底层异常翻译成一句知道下一步该干嘛的话。"""
    msg = str(e)

    if isinstance(e, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in msg:
        return (
            "TLS 证书校验失败：本机 Python 找不到根证书。"
            "请执行一次 /Applications/Python 3.13/Install Certificates.command，然后重启本服务。"
        )
    if isinstance(e, socket.gaierror) or "Name or service not known" in msg:
        return f"域名解析失败，请检查 API Base URL 是否填写正确（{msg}）"
    if isinstance(e, TimeoutError) or "timed out" in msg.lower():
        return f"请求超时（上限 {CHAT_TIMEOUT:.0f}s），请检查网络连接或代理设置（{msg}）"
    if isinstance(e, urllib.error.URLError) and "Connection refused" in msg:
        return f"连接被拒绝，请确认 API Base URL 的域名与端口（{msg}）"

    status = getattr(e, "status", None)
    if isinstance(status, int):
        if status == 401:
            return "API Key 无效或已被吊销（HTTP 401）"
        if status == 403:
            return "没有访问该模型的权限，请检查 API Key 的授权范围（HTTP 403）"
        if status == 404:
            return "接口地址不存在（HTTP 404），请检查 API Base URL，注意通常需要以 /v1 结尾"
        if status == 429:
            return "请求被限流（HTTP 429），请稍后重试或检查账户配额"
        if status >= 500:
            return f"服务端错误（HTTP {status}），请稍后重试"

    return f"连接失败：{msg}"


@router.post("/test")
async def test_llm_connection(body: TestLLMInput):
    """发一条最小 chat completion 验证连通性。"""
    cfg = LLMConfig(
        base_url=body.base_url or "https://api.openai.com/v1",
        api_key=body.api_key or "",
        model=body.model or "gpt-4o-mini",
    )
    if not cfg.api_key:
        raise HTTPError(status_code=400, detail="API Key is required")
    try:
        client = create_llm_client(cfg)
        resp = await client.chat(
            system="You are a helpful assistant.",
            messages=[LLMMessage(role="user", content="Respond with exactly: OK")],
        )
        return {"status": "ok", "response": resp}
    except Exception as e:
        raise HTTPError(status_code=502, detail=_describe_error(e))
