"""微信读书数据提取编排层。

职责：并发抓书 → 解析 review 类型 → 拼装章节标题 → 生成统计。
HTTP 细节全在 services/weread_api.py，这里不出现任何 URL。

extract_all_data 的时序（与前端 step-3 的 SSE 状态机严格对应）：
    notebook  → shelf/sync  →  timeline 事件
                             → 挂起等前端筛选（get_filter，最多 FILTER_TIMEOUT 秒）
                             →  notebooks 事件（筛选后的书单）
                             →  逐本 book_done 事件
                             →  返回 {books, stats}
"""

from __future__ import annotations

import asyncio
import time
from collections import Counter

from config import BOOK_CONCURRENCY
from services import weread_api as api
from stdfetch import arequest


# ── 时间线 ─────────────────────────────────────────────

def extract_timeline(shelf_bookmarks: dict, books_raw: list) -> list[dict]:
    """从 shelf/sync 的 sort 字段取每本书的真实最近阅读时间，降序排列。"""
    timeline = []
    for item in books_raw:
        b = _unwrap(item)
        book_id = b.get("bookId")
        if not book_id:
            continue
        last_reading_time = None
        entry = shelf_bookmarks.get(book_id) if isinstance(shelf_bookmarks, dict) else None
        if isinstance(entry, dict):
            raw = entry.get("_entry")
            if isinstance(raw, dict):
                sort_val = raw.get("sort")
                if isinstance(sort_val, (int, float)):
                    last_reading_time = int(sort_val)
        if last_reading_time is None:
            continue
        timeline.append({
            "bookId": book_id,
            "title": b.get("title", ""),
            "author": b.get("author", ""),
            "lastReadingTime": last_reading_time,
        })
    timeline.sort(key=lambda x: x["lastReadingTime"], reverse=True)
    return timeline


def _unwrap(item) -> dict:
    """笔记本条目可能是 {book: {...}} 或直接是 {...}。"""
    if isinstance(item, dict) and "book" in item:
        return item["book"]
    return item if isinstance(item, dict) else {}


# ── review 解析 ────────────────────────────────────────

_ABSTRACT_FIELDS = (
    "abstract", "excerpt", "quoteText", "sourceText",
    "refText", "highlightText", "originalText", "markText",
)


def _review_field(r: dict, field: str, default=None):
    """review 字段可能嵌套在 r['review'] 里，也可能平铺在 r 上。"""
    inner = r.get("review")
    if isinstance(inner, dict) and field in inner:
        return inner.get(field)
    if field in r:
        return r.get(field)
    return default


def _review_type(r: dict):
    val = _review_field(r, "type")
    if isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, str):
        try:
            return int(val)
        except ValueError:
            return None
    return None


def _extract_abstract(r: dict) -> str:
    for field in _ABSTRACT_FIELDS:
        val = _review_field(r, field)
        if val:
            return val
    return ""


def _chapter_title(review: dict, chapters_map: dict) -> str:
    uid = review.get("chapterUid")
    if uid and uid in chapters_map:
        return chapters_map[uid]
    return review.get("chapterTitle") or review.get("chapterName") or ""


def parse_book_detail(
    book: dict, info: dict, bookmarks_data: dict, reviews_data: dict, chapters_map: dict
) -> dict:
    """把四个接口的原始响应拼成一本书的结构化记录。"""
    chapters_map = dict(chapters_map)
    for ch in bookmarks_data.get("chapters", []):
        uid, title = ch.get("chapterUid"), ch.get("title")
        if uid and title and uid not in chapters_map:
            chapters_map[uid] = title

    bookmarks = bookmarks_data.get("updated", [])
    all_reviews = reviews_data.get("reviews", [])

    type1 = [r for r in all_reviews if _review_type(r) == 1]   # 想法/笔记
    type4 = [r for r in all_reviews if _review_type(r) == 4]   # 书评
    unknown = [r for r in all_reviews if _review_type(r) not in (1, 4)]
    if unknown:
        sample = unknown[0]
        api.log(
            f"  WARNING: {len(unknown)} reviews with unknown type, "
            f"sample keys={list(sample.keys()) if isinstance(sample, dict) else type(sample).__name__}"
        )

    reviews = [
        {
            "content": _review_field(r, "content"),
            "htmlContent": _review_field(r, "htmlContent") or "",
            "abstract": _extract_abstract(r),
            "range": _review_field(r, "range", ""),
            "chapterUid": _review_field(r, "chapterUid"),
            "chapterTitle": _chapter_title(r, chapters_map),
            "createTime": _review_field(r, "createTime"),
            "type": 1,
        }
        for r in type1
    ]

    book_reviews = [
        {
            "content": _review_field(r, "content"),
            "htmlContent": _review_field(r, "htmlContent") or "",
            "createTime": _review_field(r, "createTime"),
            "type": 4,
        }
        for r in type4
    ]

    # 把想法按 range 挂回对应的划线，这样划线和批注能对上
    range_to_review = {}
    for r in type1:
        rng = _review_field(r, "range", "")
        if rng:
            range_to_review[rng] = (
                _review_field(r, "htmlContent") or _review_field(r, "content") or ""
            )

    highlights = []
    for bm in bookmarks:
        entry = dict(bm)
        rng = entry.get("range", "")
        if rng in range_to_review:
            entry["reviewContent"] = range_to_review[rng]
        highlights.append(entry)

    return {
        "bookId": book.get("bookId"),
        "title": book.get("title"),
        "author": book.get("author"),
        "cover": book.get("cover"),
        "category": info.get("category"),
        "rating": (info.get("newRating", 0) or 0) / 1000,
        "intro": info.get("intro", ""),
        "totalBookmarks": len(highlights),
        "totalReviews": len(reviews),
        "totalBookReviews": len(book_reviews),
        "bookmarks": highlights,
        "reviews": reviews,
        "bookReviews": book_reviews,
    }


# ── 筛选 ───────────────────────────────────────────────

def apply_time_filter(books_raw: list, timeline: list, filter_data: dict) -> list:
    """按时间区间 + 排除列表过滤笔记本条目。"""
    start_ts = filter_data.get("startDate")
    end_ts = filter_data.get("endDate")
    excluded = set(filter_data.get("excludedBookIds", []))

    timeline_map = {t["bookId"]: t for t in timeline}
    kept = []
    for item in books_raw:
        b = _unwrap(item)
        book_id = b.get("bookId")
        if not book_id or book_id in excluded:
            continue
        tl = timeline_map.get(book_id)
        if not tl:
            continue
        lrt = tl.get("lastReadingTime", 0)
        if start_ts and lrt < start_ts:
            continue
        if end_ts and lrt > end_ts:
            continue
        kept.append(item)
    return kept


def compute_stats(books: list[dict]) -> dict:
    categories = Counter(b.get("category", "") for b in books if b.get("category"))
    authors = Counter(b.get("author", "") for b in books if b.get("author"))
    return {
        "totalBooks": len(books),
        "totalBookmarks": sum(b.get("totalBookmarks", 0) for b in books),
        "totalReviews": sum(b.get("totalReviews", 0) for b in books),
        "totalBookReviews": sum(b.get("totalBookReviews", 0) for b in books),
        "topCategories": categories.most_common(10),
        "topAuthors": authors.most_common(10),
    }


# ── 主流程 ─────────────────────────────────────────────

async def preflight(cookies: dict) -> dict:
    """开跑前的连通性自检。

    目的：把「点了没反应」变成「明确告诉你卡在哪一步」。
    只发两个轻量请求（HEAD 探活 + notebook 列表），几秒内就能判定登录态是否可用。
    """
    checks: list[dict] = []
    t0 = time.monotonic()

    async def run(name: str, coro):
        start = time.monotonic()
        try:
            ok, detail = await coro
            checks.append({
                "name": name, "ok": ok, "detail": detail,
                "ms": int((time.monotonic() - start) * 1000),
            })
            return ok
        except Exception as e:  # noqa: BLE001 - 自检本身绝不能抛
            checks.append({
                "name": name, "ok": False, "detail": f"{type(e).__name__}: {e}",
                "ms": int((time.monotonic() - start) * 1000),
            })
            return False

    async def probe_network():
        resp = await arequest(
            "HEAD", "https://weread.qq.com",
            headers=api.build_headers(cookies), timeout=15.0,
        )
        return resp.status_code < 500, f"weread.qq.com HTTP {resp.status_code}"

    async def probe_notebook():
        resp, _ = await api.request_with_retry(
            "GET", "https://weread.qq.com/api/user/notebook", cookies
        )
        if resp.status_code != 200:
            return False, f"notebook 接口 HTTP {resp.status_code}"
        data = resp.json()
        if isinstance(data, dict) and data.get("errcode") == api.ERRCODE_EXPIRED:
            return False, "登录态已过期（errcode=-2012），请重新扫码登录"
        books = data.get("books") if isinstance(data, dict) else None
        n = len(books) if isinstance(books, list) else 0
        return True, f"笔记本接口可用，{n} 本书有笔记记录"

    await run("网络连通（weread.qq.com）", probe_network())
    auth_ok = await run("登录态（notebook 接口）", probe_notebook())

    return {
        "ok": auth_ok,
        "checks": checks,
        "elapsedMs": int((time.monotonic() - t0) * 1000),
        "hint": "" if auth_ok else "请回到第 2 步重新扫码登录；若登录正常，多半是微信读书限流，稍后重试即可。",
    }


async def extract_all_data(
    cookies: dict, progress_callback=None, *, get_filter=None, mode: str = "full"
):
    """mode="full"抓全部；mode="book_reviews" 只抓书评 + 书籍信息。

    快慢的差别来自接口本身：划线走 shelf/sync 预取 + bookmarklist 兜底，
    书评走 review/list，两者是独立端点。所以只取书评时可以直接不碰划线接口，
    顺带省掉只为划线服务的章节标题映射。
    """
    book_reviews_only = mode == "book_reviews"

    async def emit(phase, current, total, title="", **kw):
        if progress_callback:
            await progress_callback(phase, current, total, title, **kw)

    api.log(f"extract: start mode={mode}（刷新登录态）")
    await emit("phase", 0, 0, "正在刷新登录态…")
    cookies = await api.refresh_cookies(cookies)

    api.log("extract: 拉取笔记本列表")
    await emit("phase", 0, 0, "正在获取笔记本列表…")
    notebooks_data, cookies = await api.fetch_notebooks(cookies)
    books_raw = notebooks_data.get("books", [])
    api.log(f"extract: notebook 返回 {len(books_raw)} 本书")

    if book_reviews_only:
        # 只取书评时不需要划线数据；但时间线仍来自 shelf/sync 的 sort 字段，
        # 所以这一请求保留（它是唯一带"最近阅读时间"的来源），只是不再逐本解析划线。
        api.log("extract: 仅书评模式，仍取阅读时间线（不解析划线内容）")
        await emit("phase", 0, len(books_raw), "正在获取阅读时间线…")
        shelf_bookmarks, cookies = await api.fetch_shelf_sync(cookies)
    else:
        api.log("extract: 拉取书架（划线 + 阅读时间）")
        await emit("phase", 0, len(books_raw), "正在扫描书架与阅读时间…")
        shelf_bookmarks, cookies = await api.fetch_shelf_sync(cookies)

    timeline = extract_timeline(shelf_bookmarks, books_raw)
    if progress_callback and timeline:
        await progress_callback(
            "timeline", 0, len(books_raw), "", timeline_data=timeline
        )

    if get_filter:
        filter_data = await get_filter()
        if filter_data:
            books_raw = apply_time_filter(books_raw, timeline, filter_data)
            if not books_raw:
                if progress_callback:
                    await progress_callback(
                        "empty", 0, 0, "", message="所选时间段内没有阅读记录"
                    )
                return {"books": [], "stats": {}}
            api.log(f"filtered to {len(books_raw)} books")

    if progress_callback:
        await progress_callback(
            "notebooks", 0, len(books_raw), "",
            books_meta=[
                {
                    "bookId": b.get("bookId"),
                    "title": b.get("title"),
                    "author": b.get("author"),
                    "cover": b.get("cover"),
                }
                for b in (_unwrap(i) for i in books_raw)
            ],
        )

    api.log(f"extract: 开始逐本抓取，共 {len(books_raw)} 本，"
            f"并发 {BOOK_CONCURRENCY}，模式 {mode}")
    semaphore = asyncio.Semaphore(BOOK_CONCURRENCY)

    async def process_book(index: int, item) -> dict | None:
        book = _unwrap(item)
        book_id = book.get("bookId")
        if not book_id:
            return None
        async with semaphore:
            try:
                if book_reviews_only:
                    # 书评 + 书籍信息：两个独立请求，跳过划线与章节标题
                    (info, _), (reviews_data, _) = await asyncio.gather(
                        api.fetch_book_info(dict(cookies), book_id),
                        api.fetch_reviews(dict(cookies), book_id),
                    )
                    bookmarks_data, chapters_map = {}, {}
                else:
                    (info, _), (bookmarks_data, _), (reviews_data, _), (chapters_map, _) = (
                        await asyncio.gather(
                            api.fetch_book_info(dict(cookies), book_id),
                            api.fetch_bookmarks(dict(cookies), book_id,
                                                shelf_bookmarks=shelf_bookmarks),
                            api.fetch_reviews(dict(cookies), book_id),
                            api.fetch_chapters(dict(cookies), book_id),
                        )
                    )
            except Exception as e:
                api.log(f"book {index} {book.get('title', '')} failed: {e}")
                return None

        result = parse_book_detail(
            book, info or {}, bookmarks_data or {}, reviews_data or {}, chapters_map or {}
        )
        api.log(
            f"book {index} {book.get('title', '')[:20]} "
            f"highlights={result['totalBookmarks']} "
            f"reviews={result['totalReviews']} "
            f"bookReviews={result['totalBookReviews']}"
        )
        if progress_callback:
            await progress_callback(
                "book_done", index + 1, len(books_raw), book.get("title", ""),
                book_data=result,
            )
        return result

    results = await asyncio.gather(
        *[process_book(i, item) for i, item in enumerate(books_raw)]
    )
    books = [b for b in results if b is not None]
    api.log(f"extract: 完成，成功 {len(books)}/{len(books_raw)} 本")
    return {"books": books, "stats": compute_stats(books)}
