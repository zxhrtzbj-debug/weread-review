"""Playwright 无头浏览器扫码登录。

前端 step-2 的时序：
    POST /api/browser/start          启动浏览器、截二维码
    GET  /api/browser/qrcode/{sid}   拿二维码 PNG
    GET  /api/browser/status/{sid}   SSE，登录成功后一次性下发 cookies

已从旧版删除：
    _parse_cookies / KEY_COOKIES / wait_for_login
    —— 只有归档的 diagnose 脚本在用，主流程走 login_event + status SSE。
"""

from __future__ import annotations

import asyncio

# playwright 是可选依赖：只在真正扫码登录时才需要。
# 不装也能跑 demo 预设与全流程 UI 联调。
LOGIN_URL = "https://weread.qq.com/#login"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


class BrowserSession:
    def __init__(self, sid: str):
        self.sid = sid
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.cookies: dict = {}
        self.login_event = asyncio.Event()
        self.qrcode_bytes: bytes | None = None

    async def start(self):
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            raise RuntimeError(
                "未安装 playwright。扫码登录需要它：pip install playwright && "
                "playwright install chromium。若只想用示例数据跑通流程，"
                "点前端的「一键体验」即可，无需安装。"
            ) from e
        try:
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(
                headless=True, args=["--no-sandbox"]
            )
            self.context = await self.browser.new_context(
                user_agent=USER_AGENT, locale="zh-CN"
            )
            self.page = await self.context.new_page()
            self.page.on("response", self._on_response)

            await self.page.goto(LOGIN_URL, wait_until="domcontentloaded")
            await asyncio.sleep(3)
            await self._capture_qrcode()
        except BaseException:
            # 半途失败时浏览器可能已经起来了。异常继续往上抛给路由，
            # 但先把已经占用的资源放掉，否则每次失败漏一个 Chromium 进程。
            await self.close()
            raise

    def _on_response(self, response):
        url = response.url
        if "/web/user" in url and "userVid=" in url:
            asyncio.ensure_future(self._extract_cookies())
        if "/api/auth/getLoginInfo" in url and "uid=" in url:
            if response.status == 200:
                asyncio.ensure_future(self._extract_cookies())

    async def _extract_cookies(self):
        all_cookies = {}
        for c in await self.context.cookies():
            domain = c.get("domain", "")
            if "weread" in domain or "qq.com" in domain:
                all_cookies[c["name"]] = c["value"]
        if all_cookies.get("wr_vid") or all_cookies.get("wr_name"):
            self.cookies = all_cookies
            if not self.login_event.is_set():
                self.login_event.set()

    async def _capture_qrcode(self):
        for sel in ("canvas", ".qrcode", "#qrcode", "[class*=qrcode]", "[class*=QR]"):
            try:
                el = await self.page.wait_for_selector(sel, timeout=2000)
                if el:
                    self.qrcode_bytes = await el.screenshot()
                    return
            except Exception:
                continue
        try:
            dialog = await self.page.wait_for_selector(
                "[class*=login], [class*=Login], [class*=auth]", timeout=3000
            )
            self.qrcode_bytes = await dialog.screenshot()
        except Exception:
            self.qrcode_bytes = await self.page.screenshot()

    async def close(self):
        try:
            if self.page:
                await self.page.close()
            if self.context:
                await self.context.close()
            if self.browser:
                await self.browser.close()
            if self.playwright:
                await self.playwright.stop()
        except Exception:
            pass
