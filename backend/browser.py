import asyncio
import re
from playwright.async_api import async_playwright

LOGIN_URL = "https://weread.qq.com/#login"
SHELF_URL = "https://weread.qq.com/web/shelf"
# 提取所有 cookie，某些 API 需要额外 cookie（如 pgv_pvi、pgv_si 等）
# 但仍然保留对这 3 个核心 cookie 的判断用于确认登录状态
KEY_COOKIES = {"wr_vid", "wr_name", "wr_skey"}


def _parse_cookies(cookie_str: str) -> dict:
    cookies = {}
    for pair in cookie_str.split(";"):
        pair = pair.strip()
        if "=" not in pair:
            continue
        k, v = pair.split("=", 1)
        cookies[k.strip()] = v.strip()
    return cookies


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
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(
            headless=True, args=["--no-sandbox"]
        )
        self.context = await self.browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            locale="zh-CN",
        )
        self.page = await self.context.new_page()

        self.page.on("response", self._on_response)

        await self.page.goto(LOGIN_URL, wait_until="domcontentloaded")
        await asyncio.sleep(3)
        await self._capture_qrcode()

    def _on_response(self, response):
        url = response.url

        if "/web/user" in url and "userVid=" in url:
            asyncio.ensure_future(self._extract_cookies())

        if "/api/auth/getLoginInfo" in url and "uid=" in url:
            if response.status == 200:
                asyncio.ensure_future(self._extract_cookies())

    async def _extract_cookies(self):
        ctx_cookies = await self.context.cookies()
        all_cookies = {}
        for c in ctx_cookies:
            domain = c.get("domain", "")
            if "weread" in domain or "qq.com" in domain:
                all_cookies[c["name"]] = c["value"]
        # 确认核心登录 cookie 存在
        has_login = bool(all_cookies.get("wr_vid") or all_cookies.get("wr_name"))
        if has_login:
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
            login_dialog = await self.page.wait_for_selector(
                "[class*=login], [class*=Login], [class*=auth]", timeout=3000
            )
            self.qrcode_bytes = await login_dialog.screenshot()
        except Exception:
            self.qrcode_bytes = await self.page.screenshot()

    async def wait_for_login(self, timeout=120):
        try:
            await asyncio.wait_for(self.login_event.wait(), timeout=timeout)

            if "wr_vid" not in self.cookies:
                await self.page.goto(SHELF_URL, wait_until="domcontentloaded")
                await asyncio.sleep(2)
                await self._extract_cookies()

            return self.cookies
        except asyncio.TimeoutError:
            raise TimeoutError("登录超时")
        finally:
            await self.close()

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
