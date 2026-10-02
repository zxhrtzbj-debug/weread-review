import asyncio
import json
import uuid as uuid_lib
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, Response, FileResponse
from pydantic import BaseModel

import time
from session import get_uid, poll_login, extract_all_data
from browser import BrowserSession
from analyzer import generate_report
from llm import LLMConfig
from collections import Counter

app = FastAPI(title="Weread Review")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
INDEX_HTML = FRONTEND_DIR / "index.html"

sessions = {}
browser_sessions: dict[str, BrowserSession] = {}


@app.get("/")
async def serve_index():
    return FileResponse(str(INDEX_HTML))


class CookieInput(BaseModel):
    cookies: dict


# ── Browser-based login (Playwright) ──────────────────────────


@app.post("/api/browser/start")
async def browser_start():
    sid = uuid_lib.uuid4().hex[:12]
    bs = BrowserSession(sid)
    browser_sessions[sid] = bs
    try:
        await bs.start()
        return {
            "sid": sid,
            "qrcode_url": f"/api/browser/qrcode/{sid}",
        }
    except Exception as e:
        browser_sessions.pop(sid, None)
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/browser/qrcode/{sid}")
async def browser_qrcode(sid: str):
    bs = browser_sessions.get(sid)
    if not bs or not bs.qrcode_bytes:
        raise HTTPException(status_code=404, detail="QR not ready")
    return Response(content=bs.qrcode_bytes, media_type="image/png")


@app.get("/api/browser/status/{sid}")
async def browser_status(sid: str):
    async def event_generator():
        bs = browser_sessions.get(sid)
        if not bs:
            yield f"data: {json.dumps({'status': 'error', 'message': 'session not found'})}\n\n"
            return

        try:
            await asyncio.wait_for(bs.login_event.wait(), timeout=120)
            cookies = bs.cookies
            yield f"data: {json.dumps({'status': 'ok', 'cookies': cookies})}\n\n"
        except asyncio.TimeoutError:
            yield f"data: {json.dumps({'status': 'timeout'})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'status': 'error', 'message': str(e)})}\n\n"
        finally:
            browser_sessions.pop(sid, None)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# ── QR-code API login (fallback) ──────────────────────────────


@app.post("/api/auth/qrcode")
async def create_qrcode():
    try:
        uid = await get_uid()
        sessions[uid] = {
            "uid": uid,
            "cookies": {},
            "logged_in": False,
            "data": None,
            "extracting": False,
            "progress": {},
            "deleted_book_ids": [],
            "llm_config": LLMConfig().model_dump(),
            "report": None,
            "analyzing": False,
            "style": "serious",
            "analysis_progress": {},
        }
        return {"uid": uid}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/auth/status/{uid}")
async def auth_status(uid: str):
    async def event_generator():
        session = sessions.get(uid)
        if not session:
            yield f"data: {json.dumps({'status': 'error', 'message': 'session not found'})}\n\n"
            return

        try:
            cookies = await poll_login(uid)
            session["cookies"] = cookies
            session["logged_in"] = True
            yield f"data: {json.dumps({'status': 'ok', 'cookie_keys': list(cookies.keys())})}\n\n"
        except TimeoutError:
            yield f"data: {json.dumps({'status': 'timeout'})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'status': 'error', 'message': str(e)})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.post("/api/auth/cookies/{uid}")
async def set_cookies(uid: str, body: CookieInput):
    session = sessions.get(uid)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    session["cookies"] = body.cookies
    session["logged_in"] = True
    return {"status": "ok", "cookie_keys": list(body.cookies.keys())}


# ── Data extraction (shared) ──────────────────────────────────


FILTER_TIMEOUT = 60  # 等待前端筛选的最长秒数

@app.post("/api/data/extract/{uid}")
async def extract_data(uid: str):
    sess = sessions.get(uid)
    if not sess:
        raise HTTPException(status_code=404, detail="session not found")
    if not sess.get("cookies"):
        raise HTTPException(status_code=400, detail="no cookies, login first")
    if sess.get("extracting"):
        raise HTTPException(status_code=409, detail="already extracting")

    sess["extracting"] = True
    sess["data"] = None
    sess["progress"] = {}
    sess["pending_books"] = []
    sess["notebooks_meta"] = None
    sess["_notebooks_sent"] = False
    sess["timeline"] = None
    sess["_timeline_sent"] = False
    sess["extract_filter"] = None
    sess["extract_filter_done"] = False

    async def extract_task():
        try:
            async def progress_callback(phase, current, total, book_title, **kwargs):
                sess["progress"] = {
                    "phase": phase,
                    "current": current,
                    "total": total,
                    "book_title": book_title,
                }
                if "books_meta" in kwargs:
                    sess["notebooks_meta"] = kwargs["books_meta"]
                    sess["_notebooks_sent"] = False
                if "book_data" in kwargs:
                    book_data = kwargs["book_data"]
                    book_data["_index"] = current - 1
                    sess["pending_books"].append(book_data)
                if "timeline_data" in kwargs:
                    sess["timeline"] = kwargs["timeline_data"]
                    sess["_timeline_sent"] = False

            async def get_filter():
                sess["progress"] = {"phase": "filtering", "current": 0, "total": 0,
                                    "book_title": "请选择时间范围筛选书籍"}
                deadline = time.monotonic() + FILTER_TIMEOUT
                while time.monotonic() < deadline:
                    if sess.get("extract_filter"):
                        sess["extract_filter_done"] = True
                        return sess.pop("extract_filter")
                    await asyncio.sleep(0.5)
                return None

            data = await extract_all_data(sess["cookies"], progress_callback,
                                          get_filter=get_filter)
            sess["data"] = data
        except Exception as e:
            sess["progress"] = {"phase": "error", "message": str(e)}
        finally:
            sess["extracting"] = False

    asyncio.create_task(extract_task())
    return {"status": "started"}


@app.post("/api/data/set-filter/{uid}")
async def set_extract_filter(uid: str, body: dict):
    sess = sessions.get(uid)
    if not sess:
        raise HTTPException(status_code=404, detail="session not found")
    if sess.get("extract_filter_done"):
        raise HTTPException(status_code=400, detail="filter already received or timed out")
    sess["extract_filter"] = body
    return {"status": "ok"}


@app.get("/api/data/progress/{uid}")
async def data_progress(uid: str):
    async def event_generator():
        sess = sessions.get(uid)
        if not sess:
            yield f"data: {json.dumps({'status': 'error', 'message': 'session not found'})}\n\n"
            return

        while True:
            progress = sess.get("progress", {})
            data = sess.get("data")
            extracting = sess.get("extracting", False)

            # 1. Timeline event (sent once after shelf/sync scan)
            timeline = sess.get("timeline")
            if timeline and not sess.get("_timeline_sent"):
                sess["_timeline_sent"] = True
                yield f"data: {json.dumps({'status': 'timeline', 'books': timeline})}\n\n"

            # 2. Filtering phase (keep SSE alive while waiting)
            if progress.get("phase") == "filtering":
                yield f"data: {json.dumps({'status': 'filtering', **progress})}\n\n"
                await asyncio.sleep(0.5)
                continue

            # 3. Empty result
            if progress.get("phase") == "empty":
                yield f"data: {json.dumps({'status': 'empty', 'message': progress.get('message', '')})}\n\n"
                return

            # 4. Notebooks meta (one-time, after filtering)
            notebooks_meta = sess.get("notebooks_meta")
            if notebooks_meta and not sess.get("_notebooks_sent"):
                sess["_notebooks_sent"] = True
                yield f"data: {json.dumps({'status': 'notebooks', 'books': notebooks_meta})}\n\n"
                continue

            # 5. Pending books (incremental per-book results)
            pending = sess.get("pending_books", [])
            if pending:
                to_send = list(pending)
                sess["pending_books"] = []
                progress = sess.get("progress", {})
                yield f"data: {json.dumps({'status': 'book_done', 'batch': to_send, 'current': progress.get('current'), 'total': progress.get('total')})}\n\n"

            # 6. Error
            if progress.get("phase") == "error":
                yield f"data: {json.dumps({'status': 'error', 'message': progress.get('message', '')})}\n\n"
                return

            # 7. Complete
            if data:
                book_count = len(data.get("books", []))
                yield f"data: {json.dumps({'status': 'complete', 'bookCount': book_count, 'stats': data.get('stats')})}\n\n"
                return

            # 8. Working progress
            if progress:
                yield f"data: {json.dumps({'status': 'working', **progress})}\n\n"

            if not extracting and not progress:
                yield f"data: {json.dumps({'status': 'waiting'})}\n\n"

            await asyncio.sleep(0.5)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


def _get_active_data(session: dict) -> dict | None:
    """从 session 数据中排除已删除的书籍并重新计算统计。"""
    data = session.get("data")
    if not data:
        return None
    deleted_ids = set(session.get("deleted_book_ids", []))
    if not deleted_ids:
        return data

    filtered_books = [b for b in data.get("books", []) if b.get("bookId") not in deleted_ids]
    categories = Counter(b.get("category", "") for b in filtered_books if b.get("category"))
    authors = Counter(b.get("author", "") for b in filtered_books if b.get("author"))
    stats = {
        "totalBooks": len(filtered_books),
        "totalBookmarks": sum(b.get("totalBookmarks", 0) for b in filtered_books),
        "totalReviews": sum(b.get("totalReviews", 0) for b in filtered_books),
        "totalBookReviews": sum(b.get("totalBookReviews", 0) for b in filtered_books),
        "topCategories": categories.most_common(10),
        "topAuthors": authors.most_common(10),
    }
    return {"books": filtered_books, "stats": stats}


@app.post("/api/data/delete-book/{uid}")
async def delete_book(uid: str, body: dict):
    session_obj = sessions.get(uid)
    if not session_obj:
        raise HTTPException(status_code=404, detail="session not found")
    book_id = body.get("bookId")
    if not book_id:
        raise HTTPException(status_code=400, detail="bookId required")
    if book_id not in session_obj["deleted_book_ids"]:
        session_obj["deleted_book_ids"].append(book_id)
    return {"status": "ok"}


@app.get("/api/data/result/{uid}")
async def get_data_result(uid: str):
    session_obj = sessions.get(uid)
    if not session_obj:
        raise HTTPException(status_code=404, detail="session not found")
    data = _get_active_data(session_obj)
    if not data:
        raise HTTPException(status_code=404, detail="data not ready")
    return data


@app.get("/api/ping")
async def ping():
    return {"status": "ok"}


# ── AI Analysis ────────────────────────────────────────────────

class LLMConfigInput(BaseModel):
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    temperature: float | None = None


class StyleInput(BaseModel):
    style: str


@app.post("/api/analysis/start/{uid}")
async def analysis_start(uid: str):
    session = sessions.get(uid)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    if not session.get("data"):
        raise HTTPException(status_code=400, detail="no data, extract first")
    if session.get("analyzing"):
        raise HTTPException(status_code=409, detail="already analyzing")

    session["analyzing"] = True
    session["analysis_progress"] = {}
    session["report"] = None

    async def analysis_task():
        try:
            llm_config = LLMConfig(**session.get("llm_config", {}))
            style = session.get("style", "serious")

            session["analysis_progress"] = {"phase": "preparing", "message": "正在准备阅读数据..."}
            
            async def progress(msg: str):
                session["analysis_progress"] = {"phase": "analyzing", "message": msg}

            session["analysis_progress"] = {"phase": "analyzing", "message": "正在调用 AI 分析阅读数据..."}
            active_data = _get_active_data(session)
            report = await generate_report(active_data or session["data"], llm_config, style)
            session["report"] = report
            session["analysis_progress"] = {"phase": "done", "message": "分析完成"}
        except Exception as e:
            session["analysis_progress"] = {"phase": "error", "message": str(e)}
        finally:
            session["analyzing"] = False

    asyncio.create_task(analysis_task())
    return {"status": "started"}


@app.get("/api/analysis/progress/{uid}")
async def analysis_progress(uid: str):
    async def event_generator():
        session = sessions.get(uid)
        if not session:
            yield f"data: {json.dumps({'status': 'error', 'message': 'session not found'})}\n\n"
            return

        while True:
            progress = session.get("analysis_progress", {})
            report = session.get("report")
            analyzing = session.get("analyzing", False)

            if report:
                yield f"data: {json.dumps({'status': 'done', 'report': report})}\n\n"
                return

            phase = progress.get("phase", "")
            if phase == "error":
                yield f"data: {json.dumps({'status': 'error', 'message': progress.get('message', '')})}\n\n"
                return

            if progress:
                yield f"data: {json.dumps({'status': 'working', **progress})}\n\n"

            if not analyzing and not progress:
                yield f"data: {json.dumps({'status': 'waiting'})}\n\n"

            await asyncio.sleep(0.5)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/api/analysis/report/{uid}")
async def get_analysis_report(uid: str):
    session = sessions.get(uid)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    report = session.get("report")
    if not report:
        raise HTTPException(status_code=404, detail="report not ready")
    return report


@app.post("/api/analysis/style/{uid}")
async def set_analysis_style(uid: str, body: StyleInput):
    session = sessions.get(uid)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    if body.style not in ("serious", "sarcastic"):
        raise HTTPException(status_code=400, detail="style must be 'serious' or 'sarcastic'")
    session["style"] = body.style
    return {"status": "ok", "style": body.style}


@app.get("/api/llm/config/{uid}")
async def get_llm_config(uid: str):
    session = sessions.get(uid)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    cfg = dict(session.get("llm_config", {}))
    cfg.pop("api_key", None)
    return cfg


@app.post("/api/llm/config/{uid}")
async def set_llm_config(uid: str, body: LLMConfigInput):
    session = sessions.get(uid)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    cfg = session.get("llm_config", {})
    if body.base_url:
        cfg["base_url"] = body.base_url
    if body.api_key:
        cfg["api_key"] = body.api_key
    if body.model:
        cfg["model"] = body.model
    if body.temperature is not None:
        cfg["temperature"] = body.temperature
    session["llm_config"] = cfg
    return {"status": "ok"}


class TestLLMInput(BaseModel):
    base_url: str = ""
    api_key: str = ""
    model: str = ""


@app.post("/api/llm/test")
async def test_llm_connection(body: TestLLMInput):
    """Test LLM connection with a simple chat completion."""
    cfg = LLMConfig(
        base_url=body.base_url or "https://api.openai.com/v1",
        api_key=body.api_key or "",
        model=body.model or "gpt-4o-mini",
    )
    if not cfg.api_key:
        raise HTTPException(status_code=400, detail="API Key is required")
    try:
        from llm import create_llm_client, LLMMessage
        client = create_llm_client(cfg)
        resp = await client.chat(
            system="You are a helpful assistant.",
            messages=[LLMMessage(role="user", content="Respond with exactly: OK")],
        )
        return {"status": "ok", "response": resp}
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Connection failed: {str(e)}")


# ── Debug endpoints to inspect raw WeRead API responses ──────────


@app.post("/api/debug/reviews/{uid}")
async def debug_reviews(uid: str, body: dict):
    """测试 review API 返回的原始数据"""
    session = sessions.get(uid)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    cookies = session.get("cookies")
    if not cookies:
        raise HTTPException(status_code=400, detail="no cookies")

    book_id = body.get("bookId", "")
    if not book_id:
        raise HTTPException(status_code=400, detail="bookId required")

    from session import fetch_reviews_raw
    data, raw_text = await fetch_reviews_raw(cookies, book_id)
    return {"raw_response": raw_text, "parsed": data}


@app.post("/api/debug/bookmarks/{uid}")
async def debug_bookmarks(uid: str, body: dict):
    """测试 bookmarklist API 返回的原始数据"""
    session = sessions.get(uid)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    cookies = session.get("cookies")
    if not cookies:
        raise HTTPException(status_code=400, detail="no cookies")

    book_id = body.get("bookId", "")
    if not book_id:
        raise HTTPException(status_code=400, detail="bookId required")

    from session import fetch_bookmarks_raw
    data, raw_text = await fetch_bookmarks_raw(cookies, book_id)
    return {"raw_response": raw_text, "parsed": data}


@app.post("/api/debug/chapters/{uid}")
async def debug_chapters(uid: str, body: dict):
    """测试 chapter API 返回的原始数据"""
    session = sessions.get(uid)
    if not session:
        raise HTTPException(status_code=404, detail="session not found")
    cookies = session.get("cookies")
    if not cookies:
        raise HTTPException(status_code=400, detail="no cookies")

    book_id = body.get("bookId", "")
    if not book_id:
        raise HTTPException(status_code=400, detail="bookId required")

    from session import fetch_chapters_raw
    data, raw_text = await fetch_chapters_raw(cookies, book_id)
    return {"raw_response": raw_text, "parsed": data}
