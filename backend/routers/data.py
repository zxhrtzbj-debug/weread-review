"""数据路由：提取（SSE 进度）、时间筛选、内容筛选、删除、示例数据。"""

from __future__ import annotations

import asyncio
import json
import time
import traceback

from dataclasses import dataclass, field
from stdhttp import App, HTTPError, Raw, SSE
from stdmodel import Model

from config import FILTER_TIMEOUT, LOCAL_PICK_SLOTS
from services.demo import build_demo_data
from services.weread import extract_all_data, preflight
from store import store

router = App(prefix="/api")


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@dataclass
class ContentFilterInput(Model):
    keepBookmarks: bool | None = None
    keepReviews: bool | None = None
    keepBookReviews: bool | None = None
    maxBookmarksPerBook: int | None = None
    maxReviewsPerBook: int | None = None
    # 只保留"我打过分的书"的划线/想法，其余书只留书单行
    ratedOnly: bool | None = None


@dataclass
class ExtractInput(Model):
    mode: str | None = None  # "full" | "book_reviews"

    def normalized(self) -> str:
        return "book_reviews" if (self.mode or "full") == "book_reviews" else "full"


@dataclass
class LocalPicksInput(Model):
    """本地上传文件的分类选择：{bookId: {"slot": "印象最深", "note": "…"}}。"""
    picks: dict = field(default_factory=dict)


# ── 提取 ───────────────────────────────────────────────

@router.post("/data/preflight/{uid}")
async def data_preflight(uid: str):
    """开跑前的自检：把"点了没反应"变成"明确告诉你卡在哪"。

    只发两个轻量请求，几秒内返回结论，不碰任何书籍数据。
    """
    sess = store.get(uid)
    if not sess:
        raise HTTPError(status_code=404, detail="session not found")
    if not sess.get("cookies"):
        raise HTTPError(status_code=400, detail="no cookies, login first")
    return await preflight(sess["cookies"])


@router.get("/data/status/{uid}")
async def data_status(uid: str):
    """提取状态快照：正在跑吗？跑到哪一步了？前端据此显示"正在提取"。

    SSE 断线时也能靠它确认任务是否还活着，不必盲等。
    """
    sess = store.get(uid)
    if not sess:
        raise HTTPError(status_code=404, detail="session not found")
    progress = sess.get("progress") or {}
    return {
        "extracting": bool(sess.get("extracting")),
        "progress": progress,
        "booksDone": len(sess.get("pending_books") or []),
        "hasData": bool(sess.get("data")),
        "filterDone": bool(sess.get("extract_filter_done")),
        "waitingFilter": progress.get("phase") == "filtering",
        "serverTime": int(time.time()),
    }


@router.post("/data/extract/{uid}")
async def extract_data(uid: str, body: ExtractInput | None = None):
    sess = store.get(uid)
    if not sess:
        raise HTTPError(status_code=404, detail="session not found")
    if not sess.get("cookies"):
        raise HTTPError(status_code=400, detail="no cookies, login first")
    if sess.get("extracting"):
        raise HTTPError(status_code=409, detail="already extracting")

    mode = body.normalized() if body else "full"
    sess["extract_mode"] = mode
    sess["extracting"] = True
    sess["data"] = None
    sess["extract_started_at"] = time.time()
    sess["progress"] = {
        "phase": "starting", "current": 0, "total": 0,
        "book_title": "正在启动提取任务…",
    }
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
                sess["progress"] = {
                    "phase": "filtering", "current": 0, "total": 0,
                    "book_title": "请选择时间范围筛选书籍",
                }
                deadline = time.monotonic() + FILTER_TIMEOUT
                while time.monotonic() < deadline:
                    if sess.get("extract_filter"):
                        sess["extract_filter_done"] = True
                        return sess.pop("extract_filter")
                    await asyncio.sleep(0.5)
                return None

            sess["data"] = await extract_all_data(
                sess["cookies"], progress_callback,
                get_filter=get_filter, mode=mode,
            )
        except Exception as e:
            sess["progress"] = {"phase": "error", "message": str(e)}
            traceback.print_exc()
        finally:
            sess["extracting"] = False

    asyncio.create_task(extract_task())
    return {"status": "started"}


@router.post("/data/set-filter/{uid}")
async def set_extract_filter(uid: str, body: dict):
    sess = store.get(uid)
    if not sess:
        raise HTTPError(status_code=404, detail="session not found")
    if sess.get("extract_filter_done"):
        raise HTTPError(status_code=400, detail="filter already received or timed out")
    sess["extract_filter"] = body
    return {"status": "ok"}


@router.get("/data/progress/{uid}")
async def data_progress(uid: str):
    async def event_generator():
        sess = store.get(uid)
        if not sess:
            yield _sse({"status": "error", "message": "session not found"})
            return

        # 计时起点：优先用提取任务的真实开始时间，退化到流开始时间
        started_at = float(sess.get("extract_started_at") or time.time())

        while True:
            progress = sess.get("progress", {})
            data = sess.get("data")
            extracting = sess.get("extracting", False)

            # 1. 时间线（书架扫描后发一次）
            timeline = sess.get("timeline")
            if timeline and not sess.get("_timeline_sent"):
                sess["_timeline_sent"] = True
                yield _sse({"status": "timeline", "books": timeline})

            # 2. 挂起等前端筛选，保持 SSE 存活
            if progress.get("phase") == "filtering":
                yield _sse({"status": "filtering", **progress})
                await asyncio.sleep(0.5)
                continue

            # 3. 空结果
            if progress.get("phase") == "empty":
                yield _sse({"status": "empty", "message": progress.get("message", "")})
                return

            # 4. 书单（筛选后发一次）
            notebooks_meta = sess.get("notebooks_meta")
            if notebooks_meta and not sess.get("_notebooks_sent"):
                sess["_notebooks_sent"] = True
                yield _sse({"status": "notebooks", "books": notebooks_meta})
                continue

            # 5. 逐本增量下发
            pending = sess.get("pending_books", [])
            if pending:
                to_send = list(pending)
                sess["pending_books"] = []
                yield _sse({
                    "status": "book_done",
                    "batch": to_send,
                    "current": progress.get("current"),
                    "total": progress.get("total"),
                })

            # 6. 错误
            if progress.get("phase") == "error":
                yield _sse({"status": "error", "message": progress.get("message", "")})
                return

            # 7. 完成
            if data:
                yield _sse({
                    "status": "complete",
                    "bookCount": len(data.get("books", [])),
                    "stats": data.get("stats"),
                })
                return

            # 8. 进行中：带elapsed 与进度，前端据此显示"正在提取"而不是静默
            if progress:
                yield _sse({
                    "status": "working",
                    "extracting": extracting,
                    "elapsed": int(time.time() - started_at),
                    **progress,
                })

            if not extracting and not progress:
                yield _sse({"status": "waiting"})

            await asyncio.sleep(0.5)

    return SSE(lambda _req: event_generator())


# ── 审核：删书 + 内容筛选 ───────────────────────────────

@router.post("/data/delete-book/{uid}")
async def delete_book(uid: str, body: dict):
    sess = store.get(uid)
    if not sess:
        raise HTTPError(status_code=404, detail="session not found")
    book_id = body.get("bookId")
    if not book_id:
        raise HTTPError(status_code=400, detail="bookId required")
    if book_id not in sess["deleted_book_ids"]:
        sess["deleted_book_ids"].append(book_id)
    return {"status": "ok"}


@router.post("/data/restore-book/{uid}")
async def restore_book(uid: str, body: dict):
    sess = store.get(uid)
    if not sess:
        raise HTTPError(status_code=404, detail="session not found")
    book_id = body.get("bookId")
    if book_id in sess["deleted_book_ids"]:
        sess["deleted_book_ids"].remove(book_id)
    return {"status": "ok"}


@dataclass
class SourceOverrideInput(Model):
    """人工改判来源：source = "weread" | "local" | null（null 撤销改判）。"""
    bookId: str = ""
    source: str | None = None


@router.post("/data/source-override/{uid}")
async def set_source_override(uid: str, body: SourceOverrideInput):
    """把某本书在「上架书籍 / 本地上传文件」之间手动改判。

    自动判定按元数据缺失度打分，冷门书偶尔会被误判成本地文件而从书单消失。
    这个端点就是那个出口：改回 "weread" 即可放回书单。
    """
    sess = store.get(uid)
    if not sess:
        raise HTTPError(status_code=404, detail="session not found")
    if not body.bookId:
        raise HTTPError(status_code=400, detail="bookId required")
    if body.source not in ("weread", "local", None):
        raise HTTPError(status_code=400, detail='source must be "weread", "local" or null')
    overrides = store.set_source_override(sess, body.bookId, body.source)
    return {"status": "ok", "overrides": overrides}


@router.get("/data/local-picks/{uid}")
async def get_local_picks(uid: str):
    """当前会话里被判定为「本地上传文件」的书 + 已做的分类选择。"""
    sess = store.get(uid)
    if not sess:
        raise HTTPError(status_code=404, detail="session not found")
    data = store.active_data(sess)
    return {
        "slots": list(LOCAL_PICK_SLOTS),
        "files": (data or {}).get("localFiles", []),
        "picks": sess.get("local_picks") or {},
        "overrides": sess.get("source_overrides") or {},
    }


@router.post("/data/local-picks/{uid}")
async def set_local_picks(uid: str, body: LocalPicksInput):
    """保存用户对本地上传文件的分类选择。

    这些文件没有书名/分类/评分，默认不进书单；只有被归入某个分类的
    才会参与评价，并带上用户写的介绍感悟。
    """
    sess = store.get(uid)
    if not sess:
        raise HTTPError(status_code=404, detail="session not found")
    picks = store.set_local_picks(sess, body.picks)
    return {"status": "ok", "picks": picks}


@router.post("/data/content-filter/{uid}")
async def set_content_filter(uid: str, body: ContentFilterInput):
    """设置内容筛选：决定送进 LLM 的到底是划线、想法还是书评。"""
    sess = store.get(uid)
    if not sess:
        raise HTTPError(status_code=404, detail="session not found")
    patch = body.model_dump(exclude_none=True)
    cf = store.set_content_filter(sess, patch)
    return {"status": "ok", "content_filter": cf}


@router.get("/data/result/{uid}")
async def get_data_result(uid: str):
    sess = store.get(uid)
    if not sess:
        raise HTTPError(status_code=404, detail="session not found")
    data = store.active_data(sess)
    if not data:
        raise HTTPError(status_code=404, detail="data not ready")
    return data


# ── 示例数据预设 ───────────────────────────────────────

@router.post("/demo/load/{uid}")
async def load_demo(uid: str):
    """灌入示例数据，跳过微信登录直接跑通后续流程。"""
    sess = store.get(uid)
    if not sess:
        raise HTTPError(status_code=404, detail="session not found")
    sess["data"] = build_demo_data()
    sess["deleted_book_ids"] = []
    sess["logged_in"] = True
    data = store.active_data(sess)
    return {"status": "ok", "stats": data["stats"]}
