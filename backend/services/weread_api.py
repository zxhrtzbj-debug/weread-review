"""微信读书 HTTP 接口层。

只负责"把一个 bookId 换成一段 JSON"，不做任何业务拼装：
    refresh_cookies   续一波登录态（best-effort）
    fetch_notebooks   笔记本（有笔记的书列表）
    fetch_shelf_sync  书架同步（一次拿回全部书的划线 + sort 时间戳）
    fetch_bookmarks   单书划线（shelf/sync 未命中时回落 bookmarklist）
    fetch_reviews     单书想法(type=1) / 书评(type=4)
    fetch_reviews_social  他人书评（联网补检的社群评价来源）
    fetch_book_info   单书详情（分类、评分、简介）
    fetch_chapters    章节标题映射

所有 fetch_* 统一返回 (data, cookies)：cookies 可能在过程中被续期过，
调用方应当用返回的那份继续往下传。编排逻辑在 services/weread.py。

已从旧 session.py 删除的部分：
    fetch_reviews_raw / fetch_bookmarks_raw / fetch_chapters_raw
    —— 只服务于 /api/debug/* 调试端点，端点已一并移除
    get_uid / poll_login / extract_cookies
    —— 二维码 HTTP 登录回路，前端实际只用 Playwright 浏览器登录

HTTP client 用标准库实现（stdfetch），不引第三方依赖。
"""

from __future__ import annotations

import asyncio

from config import REQUEST_TIMEOUT
from stdfetch import Resp, arequest

WEREAD_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": "https://weread.qq.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Content-Type": "application/json",
}

ERRCODE_EXPIRED = -2012  # 微信读书登录态过期


def log(*a, **kw):
    print("[weread]", *a, **kw, flush=True)


def build_headers(cookies: dict) -> dict:
    """构建请求头，显式带 Cookie（与 Obsidian Weread 插件一致）。"""
    h = dict(WEREAD_HEADERS)
    if cookies:
        h["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    return h


# ── Cookie 刷新 ────────────────────────────────────────

_COOKIE_ATTRS = (
    "path=", "domain=", "expires=", "max-age=",
    "httponly", "secure", "samesite",
)


async def refresh_cookies(cookies: dict) -> dict:
    """发 HEAD 到 weread.qq.com 触发 Set-Cookie，续一波登录态。

    失败时原样返回，绝不中断主流程——这是个 best-effort 优化。
    """
    try:
        resp = await arequest(
            "HEAD", "https://weread.qq.com",
            headers=build_headers(cookies), timeout=15.0,
        )
        updated = dict(cookies)
        # Set-Cookie 可能出现多次：Message.get() 只给第一条，得全收
        for set_cookie in resp.get_all("set-cookie"):
            for pair in set_cookie.split(";"):
                pair = pair.strip()
                low = pair.lower()
                if "=" not in pair or low.startswith(_COOKIE_ATTRS):
                    continue
                k, v = pair.split("=", 1)
                updated[k.strip()] = v.strip()
        log(f"cookie refresh status={resp.status_code} keys={len(updated)}")
        return updated
    except Exception as e:
        log(f"cookie refresh failed: {e}")
        return dict(cookies)


async def request_with_retry(
    method: str, url: str, cookies: dict, **kwargs
) -> tuple[Resp, dict]:
    """发请求，遇到 errcode=-2012（登录过期）刷新 cookie 后重试一次。"""
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)
    resp = await arequest(method, url, headers=build_headers(cookies), **kwargs)

    try:
        data = resp.json()
    except Exception:
        data = {}

    if isinstance(data, dict) and data.get("errcode") == ERRCODE_EXPIRED:
        log(f"cookie expired (errcode=-2012), refreshing and retrying: {url[:80]}")
        new_cookies = await refresh_cookies(cookies)
        cookies.clear()
        cookies.update(new_cookies)
        resp = await arequest(method, url, headers=build_headers(cookies), **kwargs)

    return resp, cookies


# ── 接口 ──────────────────────────────────────────────

BOOKMARKLIST_URLS = [
    "https://weread.qq.com/web/book/bookmarklist?bookId={book_id}&synckey=0",
    "https://weread.qq.com/api/book/bookmarklist?bookId={book_id}&synckey=0",
    "https://i.weread.qq.com/book/bookmarklist?bookId={book_id}&synckey=0",
]

# 用户在一本书上可以写很多条想法，评分那一条未必落在默认的第一页里。
# 所以先要 count=100，拿不到再退回默认分页的旧写法。
REVIEW_LIST_URLS = [
    "https://weread.qq.com/web/review/list"
    "?bookId={book_id}&listType=11&mine=1&synckey=0&count=100",
    "https://weread.qq.com/web/review/list?bookId={book_id}&listType=11&mine=1&synckey=0",
    "https://weread.qq.com/api/review/list?bookId={book_id}&listType=11&mine=1&synckey=0",
    "https://i.weread.qq.com/review/list?bookId={book_id}&listType=11&mine=1&synckey=0",
]

# mine=0 → 全站书评（他人），联网补检的社群评价来源
SOCIAL_REVIEW_URLS = [
    "https://weread.qq.com/web/review/list?bookId={book_id}&listType=11&mine=0&synckey=0",
    "https://weread.qq.com/api/review/list?bookId={book_id}&listType=11&mine=0&synckey=0",
]

SHELF_SYNC_URL = (
    "https://weread.qq.com/web/shelf/sync"
    "?userVid={user_vid}&synckey=0&lectureSynckey=0"
)


async def fetch_notebooks(cookies: dict) -> tuple[dict, dict]:
    resp, cookies = await request_with_retry(
        "GET", "https://weread.qq.com/api/user/notebook", cookies
    )
    resp.raise_for_status()
    data = resp.json()
    # 新版 API 有时直接返回数组 [{book: ...}, ...]
    if isinstance(data, list):
        log(f"notebook API returned list of {len(data)} items (not dict)")
        data = {"books": data}
    return data, cookies


async def fetch_shelf_sync(cookies: dict) -> tuple[dict, dict]:
    """shelf/sync：一次拿回全部书籍的划线，且带 sort（最近阅读时间）。"""
    cookies = await refresh_cookies(cookies)
    user_vid = cookies.get("wr_vid", "")
    if not user_vid:
        log("shelf/sync: no wr_vid in cookies, skipping")
        return {}, cookies

    resp, cookies = await request_with_retry(
        "GET", SHELF_SYNC_URL.format(user_vid=user_vid), cookies
    )
    if resp.status_code != 200:
        log(f"shelf/sync: status={resp.status_code}")
        return {}, cookies

    try:
        data = resp.json()
    except Exception:
        log("shelf/sync: not JSON")
        return {}, cookies
    if not isinstance(data, dict):
        log(f"shelf/sync: response not dict, type={type(data).__name__}")
        return {}, cookies

    log(f"shelf/sync: top-level keys={list(data.keys())}")

    per_book: dict[str, dict] = {}
    for candidate_key in ("books", "recentBooks", "finishReadBooks", "data"):
        entries = data.get(candidate_key, [])
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            bid = entry.get("bookId") or entry.get("book_id") or entry.get("id")
            if not bid:
                continue
            updated = (
                entry.get("updated") or entry.get("bookmarks")
                or entry.get("highlights") or entry.get("items")
            )
            chapters = entry.get("chapters")
            slot = per_book.setdefault(
                bid, {"updated": [], "chapters": [], "_entry": None}
            )
            if isinstance(updated, list):
                slot["updated"].extend(updated)
            if isinstance(chapters, list):
                slot["chapters"].extend(chapters)
            if slot["_entry"] is None:
                slot["_entry"] = entry

    # 扁平结构：无法按 bookId 索引，原样交回编排层处理
    if not per_book:
        updated = data.get("updated")
        if isinstance(updated, list):
            log(f"shelf/sync: flat 'updated' list with {len(updated)} items (unkeyed)")
            chapters = data.get("chapters")
            return {
                "_raw": {
                    "updated": updated,
                    "chapters": chapters if isinstance(chapters, list) else [],
                }
            }, cookies

    log(f"shelf/sync: extracted bookmarks for {len(per_book)} books")
    return per_book, cookies


async def fetch_bookmarks_via_list(
    cookies: dict, book_id: str
) -> tuple[dict | None, dict]:
    """bookmarklist 兜底：多域名重试。"""
    for url_tpl in BOOKMARKLIST_URLS:
        url = url_tpl.format(book_id=book_id)
        resp, cookies = await request_with_retry("GET", url, cookies)
        if resp.status_code != 200:
            log(f"  bookmarklist: status={resp.status_code} url={url[:80]}")
            continue
        try:
            data = resp.json()
        except Exception:
            log(f"  bookmarklist: not JSON, url={url[:80]}")
            continue
        if not isinstance(data, dict):
            continue
        if isinstance(data.get("updated"), list):
            log(f"  bookmarklist: bookId={book_id} updated={len(data['updated'])}")
            return data, cookies
        if not data:
            log(f"  bookmarklist: empty dict, url={url[:80]}")
            continue
        log(f"  bookmarklist: no 'updated' list, keys={list(data.keys())}")
    return None, cookies


async def fetch_bookmarks(
    cookies: dict, book_id: str, shelf_bookmarks: dict | None = None
) -> tuple[dict, dict]:
    """获取单书划线。优先级：shelf/sync 预取 → bookmarklist 兜底。"""
    if shelf_bookmarks and book_id in shelf_bookmarks:
        data = shelf_bookmarks[book_id]
        updated = data.get("updated", [])
        log(f"  shelf/sync: bookId={book_id} updated={len(updated)}")
        return data, cookies

    try:
        data, cookies = await asyncio.wait_for(
            fetch_bookmarks_via_list(dict(cookies), book_id), timeout=30
        )
        if data and isinstance(data.get("updated"), list):
            return data, cookies
    except asyncio.TimeoutError:
        log(f"  bookmarklist timeout for bookId={book_id}")

    log(f"bookmarklist EMPTY for bookId={book_id}")
    return {"updated": [], "chapters": []}, cookies


async def fetch_reviews(cookies: dict, book_id: str) -> tuple[dict, dict]:
    """自己的想法(type=1) 与书评(type=4)，同一端点按 type 区分。"""
    data: dict | None = None
    for url_tpl in REVIEW_LIST_URLS:
        url = url_tpl.format(book_id=book_id)
        resp, cookies = await request_with_retry("GET", url, cookies)
        if resp.status_code != 200:
            log(f"  review status={resp.status_code}")
            continue
        try:
            candidate = resp.json()
        except Exception:
            log("  review not JSON")
            continue
        if isinstance(candidate, dict) and candidate.get("reviews") is not None:
            data = candidate
            log(f"  review OK: {len(candidate['reviews'])} items")
            break
        log("  review response has no 'reviews' field")

    if data is None:
        log(f"review ALL URLS FAILED for bookId={book_id}")
        return {"reviews": []}, cookies
    return data, cookies


async def fetch_reviews_social(cookies: dict, book_id: str) -> tuple[dict, dict]:
    """他人书评（全站）。联网补检的社群评价来源，拿不到就返回空。"""
    for url_tpl in SOCIAL_REVIEW_URLS:
        url = url_tpl.format(book_id=book_id)
        resp, cookies = await request_with_retry("GET", url, cookies)
        if resp.status_code != 200:
            log(f"  social review status={resp.status_code}")
            continue
        try:
            data = resp.json()
        except Exception:
            continue
        if isinstance(data, dict) and data.get("reviews") is not None:
            log(f"  social review OK: {len(data['reviews'])} items")
            return data, cookies
    return {"reviews": []}, cookies


async def fetch_book_info(cookies: dict, book_id: str) -> tuple[dict, dict]:
    resp, cookies = await request_with_retry(
        "GET", f"https://weread.qq.com/web/book/info?bookId={book_id}", cookies
    )
    if resp.status_code != 200:
        log(f"bookinfo API status={resp.status_code} bookId={book_id}")
        return {}, cookies
    return resp.json(), cookies


async def fetch_chapters(cookies: dict, book_id: str) -> tuple[dict, dict]:
    """章节标题映射：POST /web/book/chapterInfos。"""
    resp, cookies = await request_with_retry(
        "POST",
        "https://weread.qq.com/web/book/chapterInfos",
        cookies,
        content=bytes(f'{{"bookIds":["{book_id}"]}}', "utf-8"),
    )
    if resp.status_code != 200:
        log(f"chapter API status={resp.status_code} bookId={book_id}")
        return {}, cookies

    data = resp.json()
    chapters_map = {}
    if isinstance(data, dict):
        for ch in data.get("data", []):
            if isinstance(ch, dict):
                chapters_map[ch.get("chapterUid")] = ch.get("title")
        log(f"chapter API OK bookId={book_id} chapters={len(chapters_map)}")
    return chapters_map, cookies
