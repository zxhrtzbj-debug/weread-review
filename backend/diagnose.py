#!/usr/bin/env python3
"""诊断 bookmarklist API — 内嵌浏览器登录 + 多方式对比测试"""

import asyncio, json, sys, os
from pathlib import Path

# 在后台启动 server 前先导入修正后的 cookie 域
sys.path.insert(0, str(Path(__file__).parent))
from browser import BrowserSession

COOKIES_FILE = Path("/tmp/weread_cookies.json")


async def login_and_save_cookies():
    """启动浏览器，让用户扫码，保存 cookies"""
    bs = BrowserSession("diag")
    await bs.start()

    # 保存二维码图片
    qr_path = Path("/tmp/weread_qr.png")
    if bs.qrcode_bytes:
        qr_path.write_bytes(bs.qrcode_bytes)
        print(f"\n📱 二维码已保存到: {qr_path}")
        print("   macOS 上可以直接打开: open /tmp/weread_qr.png")
    else:
        print("\n⚠️  无法捕获二维码，尝试自动等待")

    print("⏳ 等待扫码登录（120s 超时）...")
    try:
        cookies = await bs.wait_for_login(timeout=120)
        if cookies:
            # 过滤出 weread/qq.com 的 cookie
            COOKIES_FILE.write_text(json.dumps(cookies, indent=2))
            print(f"✅ 登录成功，已保存 {len(cookies)} 个 cookies 到 {COOKIES_FILE}")
            return cookies
        else:
            print("❌ 登录失败：未获取到 cookies")
            return None
    except TimeoutError:
        print("❌ 扫码超时")
        return None
    finally:
        await bs.close()


async def run_diagnostics(cookies, book_id=None):
    """运行三种方式对比测试"""
    if not book_id:
        # 从笔记本列表中选第一本书
        from session import fetch_notebooks
        data = await fetch_notebooks(cookies)
        books_raw = data.get("books", [])
        if not books_raw:
            print("❌ 笔记本为空")
            return
        item = books_raw[0]
        book = item.get("book", item) if isinstance(item, dict) else item
        book_id = book.get("bookId")
        print(f"\n📚 未指定 book_id，使用第一本书: {book.get('title','?')} (bookId={book_id})")

    print(f"\n{'='*70}")
    print(f"📖 诊断书籍: bookId={book_id}")
    print(f"{'='*70}")

    # 1. httpx bookmarklist
    from session import fetch_bookmarks_via_httpx, refresh_cookies
    print("\n--- [1/3] httpx bookmarklist ---")
    httpx_ok = False
    try:
        cookies_fresh = await refresh_cookies(dict(cookies))
        httpx_data, _ = await fetch_bookmarks_via_httpx(cookies_fresh, book_id)
        if httpx_data and isinstance(httpx_data.get("updated"), list):
            n = len(httpx_data["updated"])
            print(f"  状态: {'✅ OK' if n > 0 else '⚠️ EMPTY'} (updated={n})")
            httpx_ok = n > 0
            if httpx_data.get("chapters"):
                print(f"  chapters: {len(httpx_data['chapters'])}")
        else:
            print(f"  状态: ❌ FAILED (返回 None 或结构错误)")
    except Exception as e:
        print(f"  状态: ❌ EXCEPTION: {e}")

    # 2. PW ctx via page.request.get (test 文件的新方式)
    print("\n--- [2/3] PW ctx via page.request.get ---")
    pwctx_ok = False
    try:
        from playwright.async_api import async_playwright
        p = await async_playwright().start()
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            locale="zh-CN",
        )
        # 关键修复：加上 i.weread.qq.com
        pw_cookies = []
        for k, v in cookies.items():
            for domain in (".weread.qq.com", ".qq.com", "i.weread.qq.com", ".i.weread.qq.com"):
                pw_cookies.append({"name": k, "value": v, "domain": domain, "path": "/"})
        await context.add_cookies(pw_cookies)
        page = await context.new_page()
        await page.goto("https://weread.qq.com/web/shelf", wait_until="domcontentloaded", timeout=30000)
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
        print(f"  URL: {url}")
        print(f"  Status: {resp.status}")
        raw = await resp.text()
        print(f"  Body preview: {raw[:300]}")
        try:
            data = resp.json()
            updated = data.get("updated")
            n = len(updated) if isinstance(updated, list) else 0
            print(f"  状态: {'✅ OK' if n > 0 else '⚠️ EMPTY'} (updated={n})")
            pwctx_ok = n > 0
            if isinstance(data, dict):
                print(f"  keys: {list(data.keys())}")
            if updated and len(updated) > 0:
                print(f"  SAMPLE: {json.dumps(updated[0], ensure_ascii=False)[:200]}")
        except Exception as e:
            print(f"  JSON 解析失败: {e}")
            print(f"  Raw: {raw[:500]}")
        await page.close()
        await browser.close()
        await p.stop()
    except Exception as e:
        print(f"  状态: ❌ EXCEPTION: {e}")

    # 3. PW 原有 fallback: page.evaluate() + JS fetch
    print("\n--- [3/3] PW ctx via page.evaluate() JS fetch (原方式) ---")
    pwfb_ok = False
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
            for domain in (".weread.qq.com", ".qq.com", "i.weread.qq.com", ".i.weread.qq.com"):
                pw_cookies.append({"name": k, "value": v, "domain": domain, "path": "/"})
        await context.add_cookies(pw_cookies)
        page = await context.new_page()
        await page.goto("https://weread.qq.com/web/shelf", wait_until="domcontentloaded", timeout=15000)
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
        print(f"  URL: {url}")
        print(f"  Status: {result['status']}")
        print(f"  Body preview: {result['body'][:300]}")
        try:
            data = json.loads(result["body"])
            updated = data.get("updated")
            n = len(updated) if isinstance(updated, list) else 0
            print(f"  状态: {'✅ OK' if n > 0 else '⚠️ EMPTY'} (updated={n})")
            pwfb_ok = n > 0
            if isinstance(data, dict):
                print(f"  keys: {list(data.keys())}")
            if updated and len(updated) > 0:
                print(f"  SAMPLE: {json.dumps(updated[0], ensure_ascii=False)[:200]}")
        except Exception as e:
            print(f"  JSON 解析失败: {e}")
            print(f"  Raw: {result['body'][:500]}")
        await page.close()
        await browser.close()
        await p.stop()
    except Exception as e:
        print(f"  状态: ❌ EXCEPTION: {e}")

    # 4. 额外测试: review API 看能不能作为 reference
    print("\n--- [extra] review API (type=5 纯划线) ---")
    try:
        from session import fetch_reviews, request_with_retry
        reviews_data, _ = await fetch_reviews(dict(cookies), book_id)
        if reviews_data:
            reviews = reviews_data.get("reviews", [])
            types = {}
            for r in reviews:
                inner = r.get("review", {})
                t = inner.get("type", r.get("type"))
                types[t] = types.get(t, 0) + 1
            print(f"  reviews: {len(reviews)}, 类型分布: {types}")
            for r in reviews:
                inner = r.get("review", {})
                t = inner.get("type", r.get("type"))
                if t == 5:
                    print(f"  ✅ 发现 type=5 纯划线! abstract={repr(inner.get('abstract','')[:100])}")
        else:
            print(f"  reviews: None")
    except Exception as e:
        print(f"  ❌ {e}")

    print(f"\n{'='*70}")
    print("📊 诊断总结:")
    print(f"  [1/3] httpx bookmarklist:     {'✅ OK' if httpx_ok else '❌ FAIL'}")
    print(f"  [2/3] PW page.request.get:    {'✅ OK' if pwctx_ok else '❌ FAIL'}")
    print(f"  [3/3] PW evaluate() JS fetch: {'✅ OK' if pwfb_ok else '❌ FAIL'}")
    print(f"{'='*70}")

    if pwctx_ok:
        print("\n💡 修复方案: 将 session.py 中的 fetch_bookmarks_via_pwctx")
        print("   改为使用 page.request.get() 并加上 i.weread.qq.com cookie 域")
    elif pwfb_ok:
        print("\n💡 修复方案: 将 session.py 中的 cookie 域加上 i.weread.qq.com")
    else:
        print("\n💡 全部失败，可能需要进一步分析 WeRead API 是否变更")


async def main():
    if COOKIES_FILE.exists():
        ans = input(f"发现已有 cookies 文件 {COOKIES_FILE}，直接使用？(Y/n): ").strip().lower()
        if ans != "n":
            cookies = json.loads(COOKIES_FILE.read_text())
            print(f"已加载 {len(cookies)} 个 cookies")
            book_id = sys.argv[1] if len(sys.argv) > 1 else None
            await run_diagnostics(cookies, book_id)
            return

    # 重新登录
    cookies = await login_and_save_cookies()
    if not cookies:
        print("❌ 登录失败，无法继续诊断")
        return

    book_id = sys.argv[1] if len(sys.argv) > 1 else None
    await run_diagnostics(cookies, book_id)


if __name__ == "__main__":
    asyncio.run(main())
