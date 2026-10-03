"""分析路由：AI 评价、报告、联网搜索配置与连通性测试。"""

from __future__ import annotations

import asyncio
import json

from dataclasses import dataclass, field
from stdhttp import App, HTTPError, Raw, SSE
from stdmodel import Model

from services.analyzer import build_reading_profile, estimate_tokens, generate_report
from services.llm import LLMConfig, LLMMessage, create_llm_client
from services.search import PROVIDERS, SearchConfig, build_queries, search
from store import store

router = App(prefix="/api")


@dataclass
class StyleInput(Model):
    style: str


@dataclass
class AnalysisStartInput(Model):
    use_search: bool | None = None


@dataclass
class SearchConfigInput(Model):
    enabled: bool | None = None
    provider: str | None = None
    depth: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    max_results: int | None = None
    fetch_pages: bool | None = None


@dataclass
class SearchTestInput(Model):
    provider: str = "weread"
    api_key: str = ""
    base_url: str = ""
    query: str = "《置身事内》 兰小欢 主要内容"
    uid: str = ""


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


# ── 启动时估算（省 token 用） ───────────────────────────

@router.get("/analysis/estimate/{uid}")
async def estimate_payload(uid: str):
    """按当前删除 + 内容筛选状态，估算真正会送进 LLM 的量。"""
    sess = store.get(uid)
    if not sess:
        raise HTTPError(status_code=404, detail="session not found")
    data = store.active_data(sess)
    if not data:
        raise HTTPError(status_code=404, detail="data not ready")

    text = build_reading_profile(data)
    cf = sess.get("content_filter", {})
    return {
        "chars": len(text),
        "tokens": estimate_tokens(text),
        "books": len(data.get("books", [])),
        "kept": {
            "bookmarks": cf.get("keepBookmarks", True),
            "reviews": cf.get("keepReviews", True),
            "bookReviews": cf.get("keepBookReviews", True),
        },
        "counts": {
            "bookmarks": data["stats"]["totalBookmarks"],
            "reviews": data["stats"]["totalReviews"],
            "bookReviews": data["stats"]["totalBookReviews"],
        },
        "note": "CJK 按 1 token/字、其余按 4 字符/token 粗估，不确定度约 ±30%",
    }


# ── 分析 ───────────────────────────────────────────────

@router.post("/analysis/start/{uid}")
async def analysis_start(uid: str, body: AnalysisStartInput | None = None):
    sess = store.get(uid)
    if not sess:
        raise HTTPError(status_code=404, detail="session not found")
    if not sess.get("data"):
        raise HTTPError(status_code=400, detail="no data, extract first")
    if sess.get("analyzing"):
        raise HTTPError(status_code=409, detail="already analyzing")

    if body and body.use_search is not None:
        store.set_search_config(sess, {"enabled": bool(body.use_search)})

    sess["analyzing"] = True
    sess["analysis_progress"] = {}
    sess["report"] = None
    sess["search_digest"] = None

    async def analysis_task():
        try:
            llm_config = LLMConfig(**sess.get("llm_config", {}))
            style = sess.get("style", "serious")
            search_config = SearchConfig(**sess.get("search_config", {}))
            # weread provider 需要登录态，每次启动分析时现取，不写回 session
            search_config.cookies = sess.get("cookies") or {}

            async def progress(msg: str):
                sess["analysis_progress"] = {"phase": "analyzing", "message": msg}

            await progress("正在准备阅读数据...")

            active_data = store.active_data(sess) or sess["data"]
            report = await generate_report(
                active_data, llm_config, style,
                search_config=search_config, progress=progress,
            )
            sess["report"] = report
            sess["analysis_progress"] = {"phase": "done", "message": "分析完成"}
        except Exception as e:
            sess["analysis_progress"] = {"phase": "error", "message": str(e)}
        finally:
            sess["analyzing"] = False

    asyncio.create_task(analysis_task())
    return {"status": "started"}


@router.get("/analysis/progress/{uid}")
async def analysis_progress(uid: str):
    async def event_generator():
        sess = store.get(uid)
        if not sess:
            yield _sse({"status": "error", "message": "session not found"})
            return

        while True:
            progress = sess.get("analysis_progress", {})
            report = sess.get("report")
            analyzing = sess.get("analyzing", False)

            if report:
                yield _sse({"status": "done", "report": report})
                return

            phase = progress.get("phase", "")
            if phase == "error":
                yield _sse({"status": "error", "message": progress.get("message", "")})
                return

            if progress:
                yield _sse({"status": "working", **progress})

            if not analyzing and not progress:
                yield _sse({"status": "waiting"})

            await asyncio.sleep(0.5)

    return SSE(lambda _req: event_generator())


@router.get("/analysis/report/{uid}")
async def get_analysis_report(uid: str):
    sess = store.get(uid)
    if not sess:
        raise HTTPError(status_code=404, detail="session not found")
    report = sess.get("report")
    if not report:
        raise HTTPError(status_code=404, detail="report not ready")
    return report


@router.post("/analysis/style/{uid}")
async def set_analysis_style(uid: str, body: StyleInput):
    sess = store.get(uid)
    if not sess:
        raise HTTPError(status_code=404, detail="session not found")
    if body.style not in ("serious", "sarcastic"):
        raise HTTPError(status_code=400, detail="style must be 'serious' or 'sarcastic'")
    sess["style"] = body.style
    return {"status": "ok", "style": body.style}


# ── 联网搜索 ───────────────────────────────────────────

@router.get("/search/providers")
async def search_providers():
    """给前端下拉框用的 provider 元信息。"""
    return {"providers": PROVIDERS}


@router.get("/search/config/{uid}")
async def get_search_config(uid: str):
    sess = store.get(uid)
    if not sess:
        raise HTTPError(status_code=404, detail="session not found")
    return SearchConfig(**sess.get("search_config", {})).public()


@router.post("/search/config/{uid}")
async def set_search_config(uid: str, body: SearchConfigInput):
    sess = store.get(uid)
    if not sess:
        raise HTTPError(status_code=404, detail="session not found")
    patch = body.model_dump(exclude_none=True)
    cfg = store.set_search_config(sess, patch)
    return {"status": "ok", "search_config": {**cfg, "api_key": ""}}


@router.post("/search/test")
async def test_search(body: SearchTestInput):
    """试一次检索，确认 provider 配得通。

    weread / mock 需要指定 uid（从会话里取 cookie 与书），其余用 query 直搜。
    """
    cfg = SearchConfig(
        enabled=True,
        provider=body.provider,
        api_key=body.api_key,
        base_url=body.base_url,
        max_results=3,
    )

    book = None
    if body.provider == "weread":
        sess = store.get(body.uid) if body.uid else None
        if not sess:
            raise HTTPError(
                status_code=400,
                detail="微信读书站内检索需要登录态：请先扫码登录，或改用其他 provider",
            )
        cfg.cookies = sess.get("cookies") or {}
        data = store.active_data(sess) or {}
        book = next((b for b in data.get("books", [])), None)
        if not book:
            raise HTTPError(status_code=400, detail="还没有数据，先提取或用一键体验")
    elif body.provider == "mock":
        from services.demo import build_demo_data
        books = build_demo_data()["books"]
        book = next((b for b in books if b.get("bookId") == "demo007"), books[0])

    try:
        results = await search(body.query, cfg, book=book)
    except Exception as e:
        raise HTTPError(status_code=502, detail=f"检索失败: {e}")

    if not results:
        hint = {
            "weread": "这本书在站内没有简介也没有他人书评。",
            "mock": "离线示例库里没有这本书。",
        }.get(body.provider, "检查 provider 配置；DuckDuckGo/Bing 会被风控，"
                            "稳定方案是 Tavily / Brave / SearXNG。")
        raise HTTPError(status_code=502, detail="没有拿到任何结果。" + hint)

    return {
        "status": "ok",
        "provider": results[0].provider or cfg.provider,
        "query": body.query,
        "book": (book or {}).get("title", ""),
        "results": [r.model_dump() for r in results],
    }


@router.post("/search/preview/{uid}")
async def preview_search_queries(uid: str, body: dict):
    """预览某本书会发哪些检索式（不真的发请求）。"""
    sess = store.get(uid)
    if not sess:
        raise HTTPError(status_code=404, detail="session not found")
    data = store.active_data(sess) or {}
    book_id = body.get("bookId")
    book = next((b for b in data.get("books", []) if b.get("bookId") == book_id), None)
    if not book:
        raise HTTPError(status_code=404, detail="book not found")
    return {"title": book.get("title"), "queries": build_queries(book)}
