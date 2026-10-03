#!/usr/bin/env python3
"""诊断 bookmarklist API — HTTP API 登录（无需 headless 浏览器）+ 多方式测试"""

import asyncio, json, sys, webbrowser
from pathlib import Path
import httpx

WEREAD_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Referer": "https://weread.qq.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Content-Type": "application/json",
}

COOKIES_FILE = Path("/tmp/weread_cookies.json")


async def login_via_api():
    """使用 HTTP API 登录（无需 headless 浏览器）"""
    async with httpx.AsyncClient() as client:
        # 1. 获取 UID
        resp = await client.post(
            "https://weread.qq.com/web/login/getuid",
            headers=WEREAD_HEADERS,
        )
        resp.raise_for_status()
        data = resp.json()
        uid = data.get("uid")
        if not uid:
            print(f"❌ 获取 UID 失败: {data}")
            return None

        # 2. 二维码 URL
        qr_url = f"https://weread.qq.com/web/login/qrcode?uid={uid}"
        print(f"\n📱 请用微信扫码登录:")
        print(f"   {qr_url}")
        print(f"   已自动打开浏览器...")
        webbrowser.open(qr_url)

        # 3. 轮询登录状态
        print("⏳ 等待扫码登录（60s 超时）...")
        for attempt in range(60):
            await asyncio.sleep(1)
            try:
                resp = await client.get(
                    f"https://weread.qq.com/web/login/getinfo?uid={uid}",
                    headers=WEREAD_HEADERS,
                    timeout=40,
                )
                data = resp.json()
                # 提取 cookies
                cookies = {}
                for cookie in client.cookies.jar:
                    domain = cookie.domain or ""
                    if "weread" in domain or "qq.com" in domain:
                        cookies[cookie.name] = cookie.value

                has_vid = "wr_vid" in cookies and cookies["wr_vid"]
                has_name = "wr_name" in cookies and cookies["wr_name"]
                if has_vid or has_name:
                    if "wr_vid" not in cookies:
                        cookies["wr_vid"] = "1"
                    print(f"✅ 登录成功! cookies={list(cookies.keys())}")
                    return cookies

                if data.get("succ") == 1 or data.get("result") == 1:
                    print(f"✅ 登录成功 (succ=1)! cookies={list(cookies.keys())}")
                    return cookies

            except httpx.TimeoutException:
                continue
            except Exception as e:
                print(f"  ⚠️  轮询出错: {e}")
                await asyncio.sleep(2)

        print("❌ 扫码超时")
        return None


async def test_bookmarklist(cookies, book_id):
    """测试 bookmarklist 所有三种 URL + 两种 PW 方式"""
    print(f"\n{'='*70}")
    print(f"📖 诊断书籍: bookId={book_id}")
    print(f"{'='*70}")

    BOOKMARKLIST_URLS = [
        ("i.weread.qq.com", "https://i.weread.qq.com/book/bookmarklist?bookId={}&synckey=0"),
        ("weread.qq.com/api", "https://weread.qq.com/api/book/bookmarklist?bookId={}&synckey=0"),
        ("weread.qq.com/web", "https://weread.qq.com/web/book/bookmarklist?bookId={}&synckey=0"),
    ]

    # ── 1. httpx direct ──
    print("\n--- [1] httpx bookmarklist ---")
    for label, url_tpl in BOOKMARKLIST_URLS:
        url = url_tpl.format(book_id)
        cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
        headers = dict(WEREAD_HEADERS)
        headers["Cookie"] = cookie_str
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.get(url, headers=headers, follow_redirects=True)
                print(f"  {label}: status={resp.status_code} len={len(resp.text)}")
                if resp.status_code == 200:
                    try:
                        data = resp.json()
                        if isinstance(data, dict):
                            updated = data.get("updated")
                            chapters = data.get("chapters")
                            print(f"    keys={list(data.keys())}")
                            print(f"    updated: type={type(updated).__name__}, len={len(updated) if isinstance(updated, list) else 'N/A'}")
                            if isinstance(updated, list) and updated:
                                print(f"    sample: markText={repr(updated[0].get('markText','')[:80])}")
                            if chapters:
                                print(f"    chapters: {len(chapters)}")
                        else:
                            print(f"    type={type(data).__name__}, preview={str(data)[:200]}")
                    except Exception as e:
                        print(f"    not JSON: {e}, raw={resp.text[:300]}")
                else:
                    print(f"    body={resp.text[:300]}")
        except Exception as e:
            print(f"  {label}: EXCEPTION={e}")

    # ── 2. PW via page.request.get ──
    print("\n--- [2] PW page.request.get ---")
    try:
        from playwright.async_api import async_playwright
        p = await async_playwright().start()
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            locale="zh-CN",
        )
        # 注入 cookies，包括 i.weread.qq.com
        pw_cookies = []
        for k, v in cookies.items():
            for domain in ("weread.qq.com", "i.weread.qq.com", ".weread.qq.com", ".i.weread.qq.com", ".qq.com"):
                pw_cookies.append({"name": k, "value": v, "domain": domain, "path": "/"})
        await context.add_cookies(pw_cookies)
        page = await context.new_page()
        await page.goto("https://weread.qq.com/web/shelf", wait_until="domcontentloaded", timeout=30000)

        for label, url_tpl in BOOKMARKLIST_URLS:
            url = url_tpl.format(book_id)
            try:
                resp = await page.request.get(url, headers={
                    'Accept': 'application/json, text/plain, */*',
                    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
                    'Referer': 'https://weread.qq.com/web/shelf',
                })
                raw = await resp.text()
                print(f"  {label}: status={resp.status} len={len(raw)}")
                if resp.status == 200:
                    try:
                        data = resp.json()
                        if isinstance(data, dict):
                            updated = data.get("updated")
                            print(f"    keys={list(data.keys())}")
                            print(f"    updated: type={type(updated).__name__}, len={len(updated) if isinstance(updated, list) else 'N/A'}")
                            if isinstance(updated, list) and updated:
                                print(f"    sample: {repr(updated[0].get('markText','')[:80])}")
                    except Exception as e:
                        print(f"    not JSON: {e} raw={raw[:300]}")
                else:
                    print(f"    body={raw[:300]}")
            except Exception as e:
                print(f"  {label}: EXCEPTION={e}")

        await page.close()
        await browser.close()
        await p.stop()
    except ImportError:
        print("  playwright not installed, skipping")
    except Exception as e:
        print(f"  PW EXCEPTION: {e}")

    # ── 3. PW via page.evaluate() JS fetch (原有 fallback) ──
    print("\n--- [3] PW page.evaluate() JS fetch (原 fallback) ---")
    try:
        from playwright.async_api import async_playwright
        p = await async_playwright().start()
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            locale="zh-CN",
        )
        pw_cookies = []
        for k, v in cookies.items():
            for domain in ("weread.qq.com", "i.weread.qq.com", ".weread.qq.com", ".i.weread.qq.com", ".qq.com"):
                pw_cookies.append({"name": k, "value": v, "domain": domain, "path": "/"})
        await context.add_cookies(pw_cookies)
        page = await context.new_page()
        await page.goto("https://weread.qq.com/web/shelf", wait_until="domcontentloaded", timeout=30000)

        for label, url_tpl in BOOKMARKLIST_URLS:
            url = url_tpl.format(book_id)
            try:
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
                print(f"  {label}: status={result['status']} len={len(result['body'])}")
                if result['status'] == 200:
                    try:
                        data = json.loads(result["body"])
                        if isinstance(data, dict):
                            updated = data.get("updated")
                            print(f"    keys={list(data.keys())}")
                            print(f"    updated: type={type(updated).__name__}, len={len(updated) if isinstance(updated, list) else 'N/A'}")
                            if isinstance(updated, list) and updated:
                                print(f"    sample: {repr(updated[0].get('markText','')[:80])}")
                    except Exception as e:
                        print(f"    not JSON: {e} raw={result['body'][:300]}")
                else:
                    print(f"    body={result['body'][:300]}")
            except Exception as e:
                print(f"  {label}: EXCEPTION={e}")

        await page.close()
        await browser.close()
        await p.stop()
    except ImportError:
        print("  playwright not installed, skipping")
    except Exception as e:
        print(f"  PW EXCEPTION: {e}")

    # ── 4. review API 类型分布 ──
    print("\n--- [4] review API type distribution ---")
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
    headers = dict(WEREAD_HEADERS)
    headers["Cookie"] = cookie_str
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://weread.qq.com/web/review/list?bookId={book_id}&listType=11&mine=1&synckey=0",
                headers=headers,
            )
            if resp.status_code == 200:
                data = resp.json()
                reviews = data.get("reviews", [])
                type_dist = {}
                for r in reviews:
                    inner = r.get("review", r)
                    t = inner.get("type", "unknown")
                    type_dist[t] = type_dist.get(t, 0) + 1
                print(f"  total reviews: {len(reviews)}")
                print(f"  type distribution: {type_dist}")
                for r in reviews:
                    inner = r.get("review", r)
                    t = inner.get("type")
                    if t == 5:
                        print(f"  ✅ type=5 纯划线: abstract={repr(inner.get('abstract','')[:100])}")
            else:
                print(f"  status={resp.status_code} body={resp.text[:200]}")
    except Exception as e:
        print(f"  EXCEPTION: {e}")


async def main():
    book_id = sys.argv[1] if len(sys.argv) > 1 else None

    # 尝试加载已有 cookies
    if COOKIES_FILE.exists():
        cookies = json.loads(COOKIES_FILE.read_text())
        print(f"已加载 {len(cookies)} 个 cookies 来自 {COOKIES_FILE}")
        if book_id:
            await test_bookmarklist(cookies, book_id)
        else:
            # 先获取笔记本列表，选第一本书
            async with httpx.AsyncClient() as client:
                cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
                headers = dict(WEREAD_HEADERS)
                headers["Cookie"] = cookie_str
                resp = await client.get("https://weread.qq.com/api/user/notebook", headers=headers)
                data = resp.json()
                books_raw = data.get("books", data if isinstance(data, list) else [])
                if books_raw:
                    item = books_raw[0]
                    book = item.get("book", item) if isinstance(item, dict) else item
                    book_id = book.get("bookId")
                    print(f"📚 使用第一本书: {book.get('title','?')} (bookId={book_id})")
            if book_id:
                await test_bookmarklist(cookies, book_id)
        return

    # 重新登录
    print("需要重新登录获取 cookies")
    print("="*60)
    cookies = await login_via_api()
    if cookies:
        COOKIES_FILE.write_text(json.dumps(cookies, indent=2))
        print(f"✅ cookies 已保存到 {COOKIES_FILE}")
        if book_id:
            await test_bookmarklist(cookies, book_id)
    else:
        print("❌ 登录失败")


if __name__ == "__main__":
    asyncio.run(main())
