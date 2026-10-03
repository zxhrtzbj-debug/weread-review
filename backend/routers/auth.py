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
        browser_sessions.pop(sid, None)
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
            await asyncio.wait_for(bs.login_event.wait(), timeout=120)
            yield _sse({"status": "ok", "cookies": bs.cookies})
        except asyncio.TimeoutError:
            yield _sse({"status": "timeout"})
        except Exception as e:
            yield _sse({"status": "error", "message": str(e)})
        finally:
            browser_sessions.pop(sid, None)

    return SSE(lambda _req: event_generator())


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
