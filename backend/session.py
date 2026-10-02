import asyncio
import json
import httpx
from collections import Counter

WEREAD_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Referer": "https://weread.qq.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Content-Type": "application/json",
}

log = lambda *a, **kw: print("[weread]", *a, **kw, flush=True)

ERRCODE_EXPIRED = -2012  # 微信读书登录超时错误码


async def get_uid():
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://weread.qq.com/web/login/getuid",
            headers=WEREAD_HEADERS,
        )
        resp.raise_for_status()
        data = resp.json()
        uid = data.get("uid")
        if not uid:
            raise ValueError(f"failed to get uid: {data}")
        return uid


async def extract_cookies(client):
    cookies = {}
    for cookie in client.cookies.jar:
        domain = cookie.domain or ""
        if "weread" in domain or "qq.com" in domain:
            cookies[cookie.name] = cookie.value
    return cookies


async def refresh_cookies(cookies):
    """发送 HEAD 请求到 weread.qq.com 刷新 cookie 会话。
    参考 Obsidian Weread 插件的 refreshCookie() 机制：
    1. 发 HEAD 到 https://weread.qq.com 触发 Set-Cookie
    2. 从响应头中解析 set-cookie 更新 cookies
    失败时返回原始 cookies 不中断流程。"""
    try:
        headers = build_headers(cookies)
        async with httpx.AsyncClient() as client:
            resp = await client.head(
                "https://weread.qq.com",
                headers=headers,
                follow_redirects=True,
            )
            updated = dict(cookies)
            # 从响应头 Set-Cookie 中解析更新的 cookie
            set_cookie = resp.headers.get("set-cookie")
            if set_cookie:
                for pair in set_cookie.split(";"):
                    pair = pair.strip()
                    if "=" in pair and not pair.lower().startswith("path=") and not pair.lower().startswith("domain=") and not pair.lower().startswith("expires=") and not pair.lower().startswith("max-age=") and not pair.lower().startswith("httponly") and not pair.lower().startswith("secure") and not pair.lower().startswith("samesite"):
                        k, v = pair.split("=", 1)
                        updated[k.strip()] = v.strip()
            log(f"cookie refresh status={resp.status_code} keys={list(updated.keys())}")
            return updated
    except Exception as e:
        log(f"cookie refresh failed: {e}")
        return dict(cookies)


async def poll_login(uid, max_attempts=12):
    async with httpx.AsyncClient() as client:
        for attempt in range(max_attempts):
            try:
                resp = await client.get(
                    f"https://weread.qq.com/web/login/getinfo?uid={uid}",
                    headers=WEREAD_HEADERS,
                    timeout=40,
                )
                data = resp.json()
                cookies = await extract_cookies(client)

                has_vid = "wr_vid" in cookies and cookies["wr_vid"]
                has_name = "wr_name" in cookies and cookies["wr_name"]

                if has_vid or has_name:
                    if "wr_vid" not in cookies:
                        cookies["wr_vid"] = "1"
                    return cookies

                if data.get("succ") == 1 or data.get("result") == 1:
                    return cookies

            except httpx.TimeoutException:
                continue
            except Exception:
                await asyncio.sleep(2)
                continue

            await asyncio.sleep(1)

        raise TimeoutError("扫码超时，请重新尝试")


def build_headers(cookies):
    """构建请求头，包含 Cookie（匹配插件显式传 Cookie 的方式）"""
    h = dict(WEREAD_HEADERS)
    if cookies:
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
        h["Cookie"] = cookie_str
    return h


async def fetch_notebooks(cookies):
    resp, cookies = await request_with_retry(
        "GET",
        "https://weread.qq.com/api/user/notebook",
        cookies,
    )
    resp.raise_for_status()
    data = resp.json()
    log(f"notebook API status={resp.status_code} type={type(data).__name__}")
    # API 可能返回数组 [{book:...}, ...] 或对象 {books: [...]}
    if isinstance(data, list):
        log(f"notebook API returned list of {len(data)} items (not dict)")
        return {"books": data}
    return data


async def request_with_retry(method, url, cookies, **kwargs):
    """发送请求并在遇到 -2012（cookie 过期）时自动刷新 cookie 重试一次。"""
    headers = build_headers(cookies)
    kwargs.setdefault("timeout", httpx.Timeout(30.0, connect=15.0))
    async with httpx.AsyncClient() as client:
        resp = await client.request(method, url, headers=headers, **kwargs)
        data = {}
        try:
            data = resp.json()
        except Exception:
            pass
        # 检查 cookie 过期错误码，刷新后重试一次
        if isinstance(data, dict) and data.get("errcode") == ERRCODE_EXPIRED:
            log(f"cookie expired (errcode=-2012), refreshing and retrying: {url[:80]}")
            new_cookies = await refresh_cookies(cookies)
            cookies.clear()
            cookies.update(new_cookies)
            headers = build_headers(cookies)
            resp = await client.request(method, url, headers=headers, **kwargs)
    return resp, cookies


BOOKMARKLIST_URLS = [
    "https://weread.qq.com/web/book/bookmarklist?bookId={book_id}&synckey=0",
    "https://weread.qq.com/api/book/bookmarklist?bookId={book_id}&synckey=0",
    "https://i.weread.qq.com/book/bookmarklist?bookId={book_id}&synckey=0",
]

REVIEW_LIST_URLS = [
    "https://weread.qq.com/web/review/list?bookId={book_id}&listType=11&mine=1&synckey=0",
    "https://weread.qq.com/api/review/list?bookId={book_id}&listType=11&mine=1&synckey=0",
    "https://i.weread.qq.com/review/list?bookId={book_id}&listType=11&mine=1&synckey=0",
]

SHELF_SYNC_URL = "https://weread.qq.com/web/shelf/sync?userVid={user_vid}&synckey=0&lectureSynckey=0"


async def fetch_shelf_sync(cookies):
    """调用 shelf/sync 获取所有书籍的划线数据（Obsidian 插件标记的'获取书籍划线'API）。
    返回 per-book bookmark 字典 {bookId: {updated: [...], chapters: [...]}}"""
    cookies = await refresh_cookies(cookies)
    user_vid = cookies.get("wr_vid", "")
    if not user_vid:
        log("shelf/sync: no wr_vid in cookies, skipping")
        return {}

    url = SHELF_SYNC_URL.format(user_vid=user_vid)
    resp, cookies = await request_with_retry("GET", url, cookies)
    if resp.status_code != 200:
        log(f"shelf/sync: status={resp.status_code}")
        return {}

    try:
        data = resp.json()
    except Exception:
        log(f"shelf/sync: not JSON")
        return {}

    if not isinstance(data, dict):
        log(f"shelf/sync: response not dict, type={type(data).__name__}")
        return {}

    log(f"shelf/sync: top-level keys={list(data.keys())}")

    # shelf/sync 返回格式未知，尝试多种路径提取每本书的划线数据
    per_book = {}

    # 尝试 books 数组（每个 entry 带 updated）
    for candidate_key in ("books", "recentBooks", "finishReadBooks", "data"):
        entries = data.get(candidate_key, [])
        if isinstance(entries, list):
            for entry in entries:
                bid = entry.get("bookId") or entry.get("book_id") or entry.get("id")
                updated = entry.get("updated") or entry.get("bookmarks") or entry.get("highlights") or entry.get("items")
                chapters = entry.get("chapters")
                if bid:
                    if bid not in per_book:
                        per_book[bid] = {"updated": [], "chapters": [], "_entry": None}
                    if isinstance(updated, list):
                        per_book[bid]["updated"].extend(updated)
                    if isinstance(chapters, list):
                        per_book[bid]["chapters"].extend(chapters)
                    if per_book[bid]["_entry"] is None:
                        per_book[bid]["_entry"] = entry

    # 尝试扁平结构：顶层有 updated（某本书的划线，但不知道是哪本）
    if not per_book:
        updated = data.get("updated")
        chapters = data.get("chapters")
        if isinstance(updated, list):
            log(f"shelf/sync: flat 'updated' list with {len(updated)} items (unkeyed by bookId)")
            # 无法按 bookId 索引，标记为 raw 后面再分发
            return {"_raw": {"updated": updated, "chapters": chapters if isinstance(chapters, list) else []}}

    log(f"shelf/sync: extracted bookmarks for {len(per_book)} books")
    for bid, bm in per_book.items():
        log(f"  {bid}: {len(bm['updated'])} highlights, {len(bm['chapters'])} chapters")
        if bm["updated"]:
            log(f"    sample markText={repr(bm['updated'][0].get('markText','')[:60])}")

    return per_book


async def fetch_bookmarks_via_httpx(cookies, book_id):
    """用 httpx 获取划线，多 URL 重试。"""
    cookies = await refresh_cookies(cookies)
    for url_tpl in BOOKMARKLIST_URLS:
        url = url_tpl.format(book_id=book_id)
        resp, cookies = await request_with_retry("GET", url, cookies)
        if resp.status_code != 200:
            log(f"  httpx bookmarklist: status={resp.status_code} url={url[:80]}")
            continue
        try:
            data = resp.json()
        except Exception:
            log(f"  httpx bookmarklist: not JSON, url={url[:80]}")
            continue
        if isinstance(data, dict):
            if isinstance(data.get("updated"), list):
                log(f"  httpx bookmarklist: bookId={book_id} url={url[:80]} updated={len(data['updated'])}")
                return data, cookies
            # bookmarklist 可能返回 {} 空对象
            if not data:
                log(f"  httpx bookmarklist: empty dict {{}}, url={url[:80]}")
                continue
            log(f"  httpx bookmarklist: no 'updated' list, url={url[:80]}, keys={list(data.keys())}")
    return None, cookies


async def fetch_bookmarks(cookies, book_id, shelf_bookmarks=None):
    """获取书籍划线。
    优先级链：shelf/sync 预取数据 → httpx bookmarklist 兜底"""
    # 1) 从 shelf/sync 预取数据中提取
    if shelf_bookmarks:
        if book_id in shelf_bookmarks:
            data = shelf_bookmarks[book_id]
            updated = data.get("updated", [])
            log(f"  shelf/sync: bookId={book_id} updated={len(updated)}")
            if updated:
                log(f"  shelf/sync: markText={repr(updated[0].get('markText','')[:60])}")
            return data, cookies
        # 如果 shelf/sync 返回了_raw，尝试分发
        if "_raw" in shelf_bookmarks:
            pass  # fall through to httpx

    # 2) httpx bookmarklist 兜底
    try:
        data, cookies = await asyncio.wait_for(
            fetch_bookmarks_via_httpx(cookies, book_id),
            timeout=30,
        )
        if data and isinstance(data.get("updated"), list):
            return data, cookies
    except asyncio.TimeoutError:
        log(f"  httpx bookmarklist timeout for bookId={book_id}")

    log(f"bookmarklist EMPTY for bookId={book_id}")
    return {"updated": [], "chapters": []}, cookies


async def fetch_reviews(cookies, book_id):
    data = None
    for url_tpl in REVIEW_LIST_URLS:
        url = url_tpl.format(book_id=book_id)
        log(f"review trying: {url}")
        resp, cookies = await request_with_retry("GET", url, cookies)
        if resp.status_code != 200:
            log(f"  status={resp.status_code}")
            continue
        try:
            data = resp.json()
        except Exception:
            log(f"  not JSON")
            continue
        if isinstance(data, dict) and data.get("reviews") is not None:
            log(f"  SUCCESS: got {len(data['reviews'])} reviews")
            break
        log(f"  no 'reviews' field in response, keys={list(data.keys()) if isinstance(data, dict) else 'N/A'}")

    if data is None:
        log(f"review ALL URLS FAILED for bookId={book_id}")
        return {"reviews": []}, cookies

    reviews = data.get("reviews", [])
    log(f"review API OK bookId={book_id} reviews={len(reviews)}")
    if reviews:
        sample = reviews[0]
        log(f"review sample top-level keys={list(sample.keys()) if isinstance(sample, dict) else type(sample).__name__}")
        inner = sample.get("review", {})
        log(f"  nested review keys={list(inner.keys()) if isinstance(inner, dict) else type(inner).__name__}")
        if isinstance(inner, dict):
            log(f"  type={inner.get('type')!r} (type={type(inner.get('type')).__name__})")
            log(f"  abstract={repr(inner.get('abstract','')[:80])}")
            log(f"  content={repr(inner.get('content','')[:80])}")
            alt_fields = ["excerpt", "quoteText", "sourceText", "refText",
                          "highlightText", "originalText", "markText"]
            for f in alt_fields:
                if inner.get(f):
                    log(f"  alt field '{f}'={repr(inner.get(f,'')[:80])}")
    else:
        log(f"reviews EMPTY - response had keys {list(data.keys())}")
    return data, cookies


async def fetch_book_info(cookies, book_id):
    resp, cookies = await request_with_retry(
        "GET",
        f"https://weread.qq.com/web/book/info?bookId={book_id}",
        cookies,
    )
    if resp.status_code != 200:
        log(f"bookinfo API status={resp.status_code} bookId={book_id}")
        return {}, cookies
    return resp.json(), cookies


async def fetch_chapters(cookies, book_id):
    """获取书籍章节信息（Obsidian 插件使用 POST /web/book/chapterInfos）。"""
    resp, cookies = await request_with_retry(
        "POST",
        f"https://weread.qq.com/web/book/chapterInfos",
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
            chapters_map[ch.get("chapterUid")] = ch.get("title")
        log(f"chapter API OK bookId={book_id} chapters={len(chapters_map)}")
    return chapters_map, cookies


# ── Raw fetch functions for debug API ──────────────────────────


async def fetch_reviews_raw(cookies, book_id):
    """返回 review API 的原始响应文本（用于调试）。"""
    headers = build_headers(cookies)
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://weread.qq.com/web/review/list?bookId={book_id}&listType=11&mine=1&synckey=0",
            headers=headers,
        )
        raw = resp.text
        data = {}
        try:
            data = resp.json()
        except Exception:
            pass
        return data, raw[:2000]


async def fetch_bookmarks_raw(cookies, book_id):
    """返回 bookmarklist API 的原始响应文本（用于调试），多 URL 重试。"""
    headers = build_headers(cookies)
    for url_tpl in BOOKMARKLIST_URLS:
        url = url_tpl.format(book_id=book_id)
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=headers)
            raw = resp.text
            data = {}
            try:
                data = resp.json()
            except Exception:
                pass
            if isinstance(data, dict) and isinstance(data.get("updated"), list):
                return data, raw[:2000]
    return {}, ""


async def fetch_chapters_raw(cookies, book_id):
    """返回 chapter API 的原始响应文本（用于调试）。"""
    headers = build_headers(cookies)
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"https://weread.qq.com/web/book/chapterInfos",
            headers=headers,
            content=bytes(f'{{"bookIds":["{book_id}"]}}', "utf-8"),
        )
        raw = resp.text
        data = {}
        try:
            data = resp.json()
        except Exception:
            pass
        return data, raw[:2000]


def extract_timeline(shelf_bookmarks, books_raw):
    """从 shelf/sync 数据中提取每本书的 sort 时间（真实最近阅读时间）。
    返回按 lastReadingTime 降序排列的列表。"""
    timeline = []
    for item in books_raw:
        b = item.get("book") if isinstance(item, dict) and "book" in item else item
        book_id = b.get("bookId")
        if not book_id:
            continue
        title = b.get("title", "")
        author = b.get("author", "")
        last_reading_time = None
        if isinstance(shelf_bookmarks, dict):
            entry = shelf_bookmarks.get(book_id)
            if isinstance(entry, dict):
                raw = entry.get("_entry")
                if isinstance(raw, dict):
                    sort_val = raw.get("sort")
                    if isinstance(sort_val, (int, float)):
                        last_reading_time = int(sort_val)
        if last_reading_time is not None:
            timeline.append({
                "bookId": book_id,
                "title": title,
                "author": author,
                "lastReadingTime": last_reading_time,
            })
    timeline.sort(key=lambda x: x["lastReadingTime"], reverse=True)
    return timeline


async def extract_all_data(cookies, progress_callback=None, *,
                           get_filter=None):
    cookies = await refresh_cookies(cookies)

    notebooks_data = await fetch_notebooks(cookies)
    books_raw = notebooks_data.get("books", [])
    log(f"total books: {len(books_raw)}")

    # ── shelf/sync 预取所有书籍划线数据 ───
    shelf_bookmarks = await fetch_shelf_sync(cookies)

    # ── 提取 timeline（书架 sort 时间 = 真实最近阅读时间）───
    timeline = extract_timeline(shelf_bookmarks, books_raw)
    if progress_callback and timeline:
        await progress_callback("timeline", 0, len(books_raw), "",
                                timeline_data=timeline)

    # ── 等待前端筛选（带超时）───
    filter_data = None
    if get_filter:
        filter_data = await get_filter()

    # ── 应用筛选 ──
    if filter_data:
        start_ts = filter_data.get("startDate")
        end_ts = filter_data.get("endDate")
        excluded_ids = set(filter_data.get("excludedBookIds", []))

        timeline_map = {t["bookId"]: t for t in timeline}
        filtered_books = []
        for item in books_raw:
            b = item.get("book") if isinstance(item, dict) and "book" in item else item
            book_id = b.get("bookId")
            if not book_id or book_id in excluded_ids:
                continue
            tl = timeline_map.get(book_id)
            if not tl:
                continue
            lrt = tl.get("lastReadingTime", 0)
            if start_ts and lrt < start_ts:
                continue
            if end_ts and lrt > end_ts:
                continue
            filtered_books.append(item)

        if not filtered_books:
            if progress_callback:
                await progress_callback("empty", 0, 0, "",
                                        message="所选时间段内没有阅读记录")
            return {"books": [], "stats": {}}
        books_raw = filtered_books
        log(f"filtered to {len(books_raw)} books")

    # ── 发送书籍列表（筛选后）───
    basic_books = []
    for item in books_raw:
        if isinstance(item, dict) and "book" in item:
            b = item["book"]
        else:
            b = item
        basic_books.append({
            "bookId": b.get("bookId"),
            "title": b.get("title"),
            "author": b.get("author"),
            "cover": b.get("cover"),
        })
    log(f"basic books extracted: {len(basic_books)}")
    if progress_callback:
        await progress_callback("notebooks", 0, len(books_raw), "",
                                books_meta=basic_books)

    # ── 逐书获取详情 ──
    semaphore = asyncio.Semaphore(5)

    async def process_book(index, item):
        if isinstance(item, dict) and "book" in item:
            book = item["book"]
        else:
            book = item
        book_id = book.get("bookId")
        if not book_id:
            log(f"book {index}: no bookId, skipping item keys={list(item.keys()) if isinstance(item, dict) else type(item).__name__}")
            return None

        async with semaphore:
            try:
                (info, _), (bookmarks_data, _), (reviews_data, _), (chapters_map_from_api, _) = await asyncio.gather(
                    fetch_book_info(dict(cookies), book_id),
                    fetch_bookmarks(dict(cookies), book_id, shelf_bookmarks=shelf_bookmarks),
                    fetch_reviews(dict(cookies), book_id),
                    fetch_chapters(dict(cookies), book_id),
                )
            except Exception as e:
                log(f"book {index} {book.get('title','')} failed: {e}")
                return None

            chapters_map = dict(chapters_map_from_api)
            for ch in bookmarks_data.get("chapters", []):
                uid = ch.get("chapterUid")
                title = ch.get("title")
                if uid and title and uid not in chapters_map:
                    chapters_map[uid] = title

            bookmarks = bookmarks_data.get("updated", [])
            all_reviews = reviews_data.get("reviews", [])
            log(f"book {index} {book.get('title','')[:20]} total_reviews={len(all_reviews)}")

            def get_review_field(r, field, default=None):
                inner = r.get("review")
                if isinstance(inner, dict):
                    if field in inner:
                        return inner.get(field)
                if field in r:
                    return r.get(field)
                return default

            def parse_review_type(r):
                val = get_review_field(r, "type")
                if isinstance(val, int):
                    return val
                if isinstance(val, str):
                    try:
                        return int(val)
                    except (ValueError, TypeError):
                        pass
                return None

            def extract_abstract(r):
                for field in ("abstract", "excerpt", "quoteText", "sourceText",
                              "refText", "highlightText", "originalText", "markText"):
                    val = get_review_field(r, field)
                    if val:
                        return val
                return ""

            type1_reviews = [r for r in all_reviews if parse_review_type(r) == 1]
            type4_reviews = [r for r in all_reviews if parse_review_type(r) == 4]
            unknown_reviews = [r for r in all_reviews if parse_review_type(r) not in (1, 4)]
            if unknown_reviews:
                sample = unknown_reviews[0]
                log(f"  WARNING: {len(unknown_reviews)} reviews with unknown type, sample top keys={list(sample.keys()) if isinstance(sample, dict) else type(sample).__name__}")
                inner = sample.get("review", {})
                if isinstance(inner, dict):
                    log(f"  sample review.type value={inner.get('type')!r} type={type(inner.get('type')).__name__}")
                else:
                    log(f"  sample direct type value={sample.get('type')!r} type={type(sample.get('type')).__name__}")
                log(f"  abstract fields found: abstract={sample.get('abstract')!r}")
                if isinstance(inner, dict):
                    log(f"  nested abstract={inner.get('abstract')!r}")

            log(f"  type1={len(type1_reviews)} type4={len(type4_reviews)}")

            def get_chapter_title(review):
                uid = review.get("chapterUid")
                if uid and uid in chapters_map:
                    return chapters_map[uid]
                return review.get("chapterTitle") or review.get("chapterName") or ""

            parsed_reviews = [
                {
                    "content": get_review_field(r, "content"),
                    "htmlContent": get_review_field(r, "htmlContent") or "",
                    "abstract": extract_abstract(r),
                    "range": get_review_field(r, "range", ""),
                    "chapterUid": get_review_field(r, "chapterUid"),
                    "chapterTitle": get_chapter_title(r),
                    "createTime": get_review_field(r, "createTime"),
                    "type": 1,
                }
                for r in type1_reviews
            ]

            parsed_book_reviews = [
                {
                    "content": get_review_field(r, "content"),
                    "htmlContent": get_review_field(r, "htmlContent") or "",
                    "createTime": get_review_field(r, "createTime"),
                    "type": 4,
                }
                for r in type4_reviews
            ]

            range_to_review = {}
            for r in type1_reviews:
                rng = get_review_field(r, "range", "")
                if rng:
                    rc = get_review_field(r, "htmlContent") or get_review_field(r, "content") or ""
                    range_to_review[rng] = rc

            all_highlights = []
            for bm in bookmarks:
                entry = dict(bm)
                rng = entry.get("range", "")
                if rng in range_to_review:
                    entry["reviewContent"] = range_to_review[rng]
                all_highlights.append(entry)
            total_highlights = len(all_highlights)
            log(f"  result: highlights={total_highlights} reviews={len(parsed_reviews)} bookReviews={len(parsed_book_reviews)}")

            result = {
                "bookId": book_id,
                "title": book.get("title"),
                "author": book.get("author"),
                "cover": book.get("cover"),
                "category": info.get("category"),
                "rating": (info.get("newRating", 0) or 0) / 1000,
                "intro": info.get("intro", ""),
                "totalBookmarks": total_highlights,
                "totalReviews": len(parsed_reviews),
                "totalBookReviews": len(parsed_book_reviews),
                "bookmarks": all_highlights,
                "reviews": parsed_reviews,
                "bookReviews": parsed_book_reviews,
            }

            if progress_callback:
                await progress_callback(
                    "book_done", index + 1, len(books_raw), book.get("title", ""),
                    book_data=result,
                )
            return result

    tasks = [process_book(i, item) for i, item in enumerate(books_raw)]
    results = await asyncio.gather(*tasks)
    books = [b for b in results if b is not None]

    categories = Counter(b.get("category", "") for b in books if b.get("category"))
    authors = Counter(b.get("author", "") for b in books if b.get("author"))

    stats = {
        "totalBooks": len(books),
        "totalBookmarks": sum(b["totalBookmarks"] for b in books),
        "totalReviews": sum(b["totalReviews"] for b in books),
        "totalBookReviews": sum(b["totalBookReviews"] for b in books),
        "topCategories": categories.most_common(10),
        "topAuthors": authors.most_common(10),
    }

    return {"books": books, "stats": stats}
