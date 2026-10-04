"""数据路由：扫描时间线、提取（SSE 进度）、内容筛选、删除、示例数据。"""

from __future__ import annotations

import asyncio
import json
import time
import traceback

from dataclasses import dataclass, field
from stdhttp import App, HTTPError, Raw, SSE
from stdmodel import Model

from config import LOCAL_PICK_SLOTS
from services.demo import build_demo_data
from services.weread import extract_all_data, preflight, scan_books
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
    # 时间范围筛选（前端「时间范围」步骤确认后随提取一起提交）。
    # 三个字段要么全有（按筛选提取），要么全没有（全量提取）。
    startDate: float | None = None
    endDate: float | None = None
    excludedBookIds: list | None = None

    def normalized(self) -> str:
        return "book_reviews" if (self.mode or "full") == "book_reviews" else "full"

    def filter_payload(self) -> dict | None:
        if self.startDate is None or self.endDate is None:
            return None
        return {
            "startDate": self.startDate,
            "endDate": self.endDate,
            "excludedBookIds": self.excludedBookIds or [],
        }


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
        "serverTime": int(time.time()),
    }


@router.post("/data/scan/{uid}")
async def data_scan(uid: str):
    """扫描书单 + 阅读时间线（不逐本抓内容），给「时间范围」步骤提供数据。

    扫描结果（books_raw / shelf_bookmarks）留在会话里，用户确认筛选后
    /data/extract 直接复用，书单接口不用打第二遍。
    """
    sess = store.get(uid)
    if not sess:
        raise HTTPError(status_code=404, detail="session not found")
    if not sess.get("cookies"):
        raise HTTPError(status_code=400, detail="no cookies, login first")
    if sess.get("extracting"):
        raise HTTPError(status_code=409, detail="extracting, try later")
    try:
        books_raw, shelf_bookmarks, timeline, cookies = await scan_books(sess["cookies"])
    except Exception as e:  # noqa: BLE001 - 扫描失败要变成明确的前端提示
        traceback.print_exc()
        raise HTTPError(status_code=502, detail=f"扫描失败: {e}") from e
    sess["cookies"] = cookies  # refresh_cookies 可能轮换过登录态
    sess["scan_books_raw"] = books_raw
    sess["scan_shelf"] = shelf_bookmarks
    return {"books": timeline, "total": len(books_raw)}


@router.post("/data/extract/{uid}")
async def extract_data(uid: str, body: ExtractInput | None = None):
    """开始提取。带时间筛选字段 → 复用 /data/scan 的预取结果按筛选提取；
    不带 → 全量提取。没有任何"等待前端再确认"的中间态。
    """
    sess = store.get(uid)
    if not sess:
        raise HTTPError(status_code=404, detail="session not found")
    if not sess.get("cookies"):
        raise HTTPError(status_code=400, detail="no cookies, login first")
    if sess.get("extracting"):
        raise HTTPError(status_code=409, detail="already extracting")

    mode = body.normalized() if body else "full"
    filter_data = body.filter_payload() if body else None
    prefetched = None
    if filter_data is not None:
        # 筛选面板的数据来自 scan；没有扫描结果就说明前端流程被绕过了
        if sess.get("scan_books_raw") is None:
            raise HTTPError(status_code=400, detail="请先扫描阅读时间线再做时间筛选")
        prefetched = (sess["scan_books_raw"], sess.get("scan_shelf") or {})

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

            sess["data"] = await extract_all_data(
                sess["cookies"], progress_callback,
                mode=mode, prefetched=prefetched, filter_data=filter_data,
            )
        except Exception as e:
            sess["progress"] = {"phase": "error", "message": str(e)}
            traceback.print_exc()
        finally:
            sess["extracting"] = False

    asyncio.create_task(extract_task())
    return {"status": "started"}


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

            # 1. 空结果（时间筛选后一本书都不剩）
            if progress.get("phase") == "empty":
                yield _sse({"status": "empty", "message": progress.get("message", "")})
                return

            # 2. 书单（开始逐本抓取前发一次）
            notebooks_meta = sess.get("notebooks_meta")
            if notebooks_meta and not sess.get("_notebooks_sent"):
                sess["_notebooks_sent"] = True
                yield _sse({"status": "notebooks", "books": notebooks_meta})
                continue

            # 3. 逐本增量下发
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

            # 4. 错误
            if progress.get("phase") == "error":
                yield _sse({"status": "error", "message": progress.get("message", "")})
                return

            # 5. 完成
            if data:
                yield _sse({
                    "status": "complete",
                    "bookCount": len(data.get("books", [])),
                    "stats": data.get("stats"),
                })
                return

            # 6. 进行中：带elapsed 与进度，前端据此显示"正在提取"而不是静默
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
    """人工改判来源：source = "weread" | "local" | null（null 撤销改判）。

    bookId 改判一本，bookIds 批量改判。批量是为了"有评分的书必然是上架书籍"
    这类一眼可辨的误判：几千本里挑出来的一批，一本本点不现实。
    """
    bookId: str = ""
    bookIds: list = field(default_factory=list)
    source: str | None = None


@router.post("/data/source-override/{uid}")
async def set_source_override(uid: str, body: SourceOverrideInput):
    """把书在「上架书籍 / 本地上传文件」之间手动改判，支持批量。

    自动判定按证据强度定案，证据不足时一律按上架书籍收录，
    所以真正需要手工改判的是反方向：把混进书单的本地文件移出去。
    """
    sess = store.get(uid)
    if not sess:
        raise HTTPError(status_code=404, detail="session not found")
    if body.source not in ("weread", "local", None):
        raise HTTPError(status_code=400, detail='source must be "weread", "local" or null')

    ids: list[str] = []
    if body.bookId:
        ids.append(str(body.bookId))
    for raw in body.bookIds or []:
        sid = str(raw)
        if sid and sid not in ids:
            ids.append(sid)
    if not ids:
        raise HTTPError(status_code=400, detail="bookId or bookIds required")

    overrides = {}
    for bid in ids:
        overrides = store.set_source_override(sess, bid, body.source)
    return {"status": "ok", "overrides": overrides, "changed": len(ids)}


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
