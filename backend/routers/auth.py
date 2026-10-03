"""登录路由：Playwright 扫码 + 会话建立。"""

from __future__ import annotations

import asyncio
import json
import uuid as uuid_lib

from dataclasses import dataclass, field
from stdhttp import App, HTTPError, Raw, SSE
from stdmodel import Model

from services.browser import BrowserSession
from store import store

router = App(prefix="/api")

# 浏览器登录会话（sid → BrowserSession），登录完成即出队
browser_sessions: dict[str, BrowserSession] = {}


@dataclass
class CookieInput(Model):
    cookies: dict


@router.post("/auth/session")
async def create_session():
    """建立会话。uid 只作为本地会话键，不再走微信登录接口换取。"""
    session = store.create()
    return {"uid": session["uid"]}


@router.post("/auth/cookies/{uid}")
async def set_cookies(uid: str, body: CookieInput):
    session = store.get(uid)
    if not session:
        raise HTTPError(status_code=404, detail="session not found")
    session["cookies"] = body.cookies
    session["logged_in"] = True
    return {"status": "ok", "cookie_keys": list(body.cookies.keys())}


@router.post("/browser/start")
async def browser_start():
    sid = uuid_lib.uuid4().hex[:12]
    bs = BrowserSession(sid)
    browser_sessions[sid] = bs
    try:
        await bs.start()
        return {"sid": sid, "qrcode_url": f"/api/browser/qrcode/{sid}"}
    except Exception as e:
        # 启动中途失败时 playwright 可能已经起来了，同样要关，
        # 否则每次失败泄漏一个 Chromium
        browser_sessions.pop(sid, None)
        try:
            await bs.close()
        except Exception:
            pass
        raise HTTPError(status_code=502, detail=str(e))


@router.get("/browser/qrcode/{sid}")
async def browser_qrcode(sid: str):
    bs = browser_sessions.get(sid)
    if not bs or not bs.qrcode_bytes:
        raise HTTPError(status_code=404, detail="QR not ready")
    return Raw(bs.qrcode_bytes, "image/png")


@router.get("/browser/status/{sid}")
async def browser_status(sid: str):
    async def event_generator():
        bs = browser_sessions.get(sid)
        if not bs:
            yield _sse({"status": "error", "message": "session not found"})
            return
        try:
            # 先立刻发一帧：EventSource 建连后到第一帧之间，网关可能因为
            # 「连接空闲」判定超时而掐断——而 login_event 最长 120 秒后才置位。
            # 这一帧让前端马上进入"等待扫码"状态，也让连接立刻有流量。
            yield _sse({"status": "waiting"})

            # 边等边发心跳。多数反代对 SSE 有 idle timeout（常见 30~60 秒），
            # 静默等待必然被切；心跳把连接的"活跃度"维持住。
            deadline = 120.0
            waited = 0.0
            heartbeat = 15.0
            while waited < deadline:
                try:
                    await asyncio.wait_for(
                        bs.login_event.wait(), timeout=min(heartbeat, deadline - waited)
                    )
                    break
                except asyncio.TimeoutError:
                    waited += heartbeat
                    if waited < deadline:
                        yield ": ping\n\n"  # 注释帧：EventSource 会忽略

            if bs.login_event.is_set():
                yield _sse({"status": "ok", "cookies": bs.cookies})
            else:
                yield _sse({"status": "timeout"})
        except Exception as e:
            yield _sse({"status": "error", "message": str(e)})
        finally:
            # 出队前必须关掉浏览器：不关的话每次登录漏一个 Chromium 进程，
            # 公网实例开着几轮就 OOM 了。
            browser_sessions.pop(sid, None)
            try:
                await bs.close()
            except Exception:
                pass

    return SSE(lambda _req: event_generator())


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
