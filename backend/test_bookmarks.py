#!/usr/bin/env python3
"""诊断 bookmarklist API 获取失败的原因。
用法：
  1. 先获取有效 cookie（通过前端扫码登录），保存到文件
  2. python test_bookmarks.py <cookies_json_path> [book_id]
"""

import asyncio
import json
import sys
import httpx

WEREAD_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Referer": "https://weread.qq.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Content-Type": "application/json",
}

BOOKMARKLIST_URLS = [
    "https://i.weread.qq.com/book/bookmarklist?bookId={}&synckey=0",
    "https://weread.qq.com/api/book/bookmarklist?bookId={}&synckey=0",
    "https://weread.qq.com/web/book/bookmarklist?bookId={}&synckey=0",
]


def build_headers(cookies):
    h = dict(WEREAD_HEADERS)
    if cookies:
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
        h["Cookie"] = cookie_str
    return h


async def test_httpx(cookies, book_id):
    """测试 httpx 的 TLS 指纹是否被拦截"""
    print("\n=== TEST: httpx bookmarklist ===")
    for url_tpl in BOOKMARKLIST_URLS:
        url = url_tpl.format(book_id)
        headers = build_headers(cookies)
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=headers)
            print(f"  URL: {url}")
            print(f"  Status: {resp.status_code}")
            raw = resp.text[:500]
            print(f"  Response preview: {raw[:200]}")
            try:
                data = resp.json()
                if isinstance(data, dict):
                    updated = data.get("updated")
                    print(f"  keys={list(data.keys())}")
                    print(f"  'updated' type={type(updated).__name__}, len={len(updated) if isinstance(updated, list) else 'N/A'}")
                    if updated:
                        print(f"  SAMPLE: {json.dumps(updated[0], ensure_ascii=False)[:300]}")
                return True, data
            except Exception as e:
                print(f"  NOT JSON: {e}")
    return False, None


async def test_playwright_pwctx(cookies, book_id):
    """测试共享 PW context 方式（与 session.py 中的 fetch_bookmarks_via_pwctx 一致）"""
    print("\n=== TEST: playwright via pw_ctx (page.request.get) ===")
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("  playwright not installed")
        return False, None

    p = await async_playwright().start()
    browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
    context = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        locale="zh-CN",
    )
    pw_cookies = []
    for k, v in cookies.items():
        for domain in (".weread.qq.com", ".qq.com", "i.weread.qq.com"):
            pw_cookies.append({"name": k, "value": v, "domain": domain, "path": "/"})
    await context.add_cookies(pw_cookies)

    page = await context.new_page()
    await page.goto("https://weread.qq.com/web/shelf", wait_until="domcontentloaded", timeout=30000)
    # warm up i.weread.qq.com domain
    try:
        await page.goto("https://i.weread.qq.com", wait_until="domcontentloaded", timeout=10000)
    except Exception:
        pass

    url = f"https://i.weread.qq.com/book/bookmarklist?bookId={book_id}&synckey=0"
    resp = await page.request.get(url, headers={
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
        'Referer': 'https://weread.qq.com/web/shelf',
    })
    print(f"  Status: {resp.status}")
    raw_body = await resp.text()
    print(f"  Response preview: {raw_body[:300]}")
    try:
        data = resp.json()
        updated = data.get("updated")
        print(f"  keys={list(data.keys())}")
        print(f"  'updated' type={type(updated).__name__}, len={len(updated) if isinstance(updated, list) else 'N/A'}")
        if updated:
            print(f"  SAMPLE: {json.dumps(updated[0], ensure_ascii=False)[:300]}")
        await page.close()
        await browser.close()
        await p.stop()
        return True, data
    except Exception as e:
        print(f"  JSON parse error: {e}")
        print(f"  Raw body: {raw_body[:500]}")
        await page.close()
        await browser.close()
        await p.stop()
        return False, None


async def test_playwright_fallback(cookies, book_id):
    """测试 page.evaluate() fetch 方式（与 fetch_bookmarks_via_playwright 一致）"""
    print("\n=== TEST: playwright via page.evaluate() fetch ===")
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("  playwright not installed")
        return False, None

    p = await async_playwright().start()
    browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
    context = await browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        locale="zh-CN",
    )
    pw_cookies = []
    for k, v in cookies.items():
        for domain in (".weread.qq.com", ".qq.com", "i.weread.qq.com"):
            pw_cookies.append({"name": k, "value": v, "domain": domain, "path": "/"})
    await context.add_cookies(pw_cookies)
    page = await context.new_page()
    await page.goto("https://weread.qq.com/web/shelf", wait_until="domcontentloaded", timeout=15000)
    # warm up i.weread.qq.com domain
    try:
        await page.goto("https://i.weread.qq.com", wait_until="domcontentloaded", timeout=10000)
    except Exception:
        pass

    url = f"https://i.weread.qq.com/book/bookmarklist?bookId={book_id}&synckey=0"
    result = await page.evaluate("""
        async (url) => {
            const resp = await fetch(url, {
                credentials: 'include',
                headers: {
                    'Accept': 'application/json, text/plain, */*',
                    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
                    'Referer': 'https://weread.qq.com/web/shelf',
                }
            });
            const body = await resp.text();
            return { status: resp.status, body: body };
        }
    """, url)
    print(f"  Status: {result['status']}")
    print(f"  Response preview: {result['body'][:300]}")
    try:
        data = json.loads(result['body'])
        updated = data.get("updated")
        print(f"  keys={list(data.keys())}")
        print(f"  'updated' type={type(updated).__name__}, len={len(updated) if isinstance(updated, list) else 'N/A'}")
        if updated:
            print(f"  SAMPLE: {json.dumps(updated[0], ensure_ascii=False)[:300]}")
        await page.close()
        await browser.close()
        await p.stop()
        return True, data
    except Exception as e:
        print(f"  JSON parse error: {e}")
        await page.close()
        await browser.close()
        await p.stop()
        return False, None


async def test_reviews(cookies, book_id):
    """测试 review API"""
    print("\n=== TEST: httpx reviews ===")
    headers = build_headers(cookies)
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://weread.qq.com/web/review/list?bookId={book_id}&listType=11&mine=1&synckey=0",
            headers=headers,
        )
        print(f"  Status: {resp.status_code}")
        try:
            data = resp.json()
            reviews = data.get("reviews")
            print(f"  keys={list(data.keys())}")
            print(f"  reviews count: {len(reviews) if isinstance(reviews, list) else 'N/A'}")
            if reviews:
                sample = reviews[0]
                print(f"  SAMPLE top keys: {list(sample.keys())}")
                inner = sample.get("review", {})
                print(f"  nested review keys: {list(inner.keys())}")
                print(f"  type: {inner.get('type')!r}")
                print(f"  abstract: {repr(inner.get('abstract', '')[:100])}")
                print(f"  range: {inner.get('range')!r}")
            return data
        except Exception as e:
            print(f"  NOT JSON: {e}")
            print(f"  Raw: {resp.text[:200]}")
            return None


async def test_notebooks(cookies):
    """测试笔记本列表 API"""
    print("\n=== TEST: httpx notebooks ===")
    headers = build_headers(cookies)
    async with httpx.AsyncClient() as client:
        resp = await client.get("https://weread.qq.com/api/user/notebook", headers=headers)
        print(f"  Status: {resp.status_code}")
        try:
            data = resp.json()
            if isinstance(data, dict):
                books = data.get("books", [])
            elif isinstance(data, list):
                books = data
            else:
                books = []
            print(f"  total books: {len(books)}")
            return books
        except Exception as e:
            print(f"  NOT JSON: {e}")
            return []


async def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return

    cookies_path = sys.argv[1]
    with open(cookies_path) as f:
        cookies = json.load(f)
    print(f"Loaded {len(cookies)} cookies from {cookies_path}")
    print(f"Cookie keys: {list(cookies.keys())}")

    # 先获取笔记本列表，找一个有想法的书
    books = await test_notebooks(cookies)
    if not books:
        print("ERROR: notebook API returned no books - check cookies")
        return

    # 如果传了 book_id 就用，否则用第一本书
    if len(sys.argv) >= 3:
        book_id = sys.argv[2]
        books_to_test = [(book_id, "指定书籍")]
    else:
        # 找一本有 reviewCount 的书
        books_to_test = []
        for b in books:
            bk = b.get("book", b) if isinstance(b, dict) else b
            bid = bk.get("bookId", "")
            title = bk.get("title", "")
            rc = bk.get("reviewCount", 0)
            nc = bk.get("noteCount", 0)
            books_to_test.append((bid, title))
            if rc > 0 and nc > 0:
                print(f"\nSelected first book with reviews: {title} (bookId={bid}, reviewCount={rc}, noteCount={nc})")
                break
        else:
            bid, title = books_to_test[0]
            print(f"\nNo book with reviews found, using first: {title}")

    book_id, title = books_to_test[0]
    print(f"\n{'='*60}")
    print(f"Testing book: {title} (bookId={book_id})")
    print(f"{'='*60}")

    # Test httpx bookmarklist
    httpx_ok, httpx_data = await test_httpx(cookies, book_id)

    # Test playwright pwctx
    pwctx_ok, pwctx_data = await test_playwright_pwctx(cookies, book_id)

    # Test playwright fallback
    pwfb_ok, pwfb_data = await test_playwright_fallback(cookies, book_id)

    # Test reviews
    reviews_data = await test_reviews(cookies, book_id)

    print(f"\n{'='*60}")
    print("SUMMARY:")
    print(f"  httpx bookmarklist:          {'OK' if httpx_ok and httpx_data.get('updated') else 'FAILED/EMPTY'}")
    print(f"  PW ctx bookmarklist:         {'OK' if pwctx_ok and pwctx_data.get('updated') else 'FAILED/EMPTY'}")
    print(f"  PW fallback bookmarklist:    {'OK' if pwfb_ok and pwfb_data.get('updated') else 'FAILED/EMPTY'}")
    print(f"{'='*60}")


if __name__ == "__main__":
    asyncio.run(main())
