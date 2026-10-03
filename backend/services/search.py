"""联网搜索：给 AI 补上"这本书到底是讲什么的、谁在读、口碑如何"。

为什么需要它
────────────
微信读书接口只给书名/作者/分类/评分和自我简介，冷门书这些数据几乎是空的。
让 LLM 凭空评价一本它没见过的书，结果只能是编。所以在分析前先联网补一层资料。

选型（参考 GitHub 上 Agent 搜索生态的主流做法）
──────────────────────────────────────────────
  weread      默认。复用已登录的微信读书会话取官方简介 + 其他读者书评。
              不抓取、不限流、不需要任何 Key，社群评价质量最高
  duckduckgo  免 Key 抓取。实测连续请求会被 202 风控拦截，只适合小批量
  bing        免 Key 抓取。同样不稳定，作为 DDG 的互备
  searxng     自建元搜索，聚合 Google/Bing/Brave/DDG，无调用限额（推荐自建）
  tavily      专为 Agent 设计，直接返回可消费的结构化摘要，1000 次/月免费
  brave       Claude Code WebSearch 用的就是它，2000 次/月免费，独立索引
  serper      Google SERP 结构化代理，性价比高
  jina        s.jina.ai，搜索 + 正文提取一步到位（现需 Key）
  mock        离线假数据，仅供「一键体验」验证流程，不联网

统一约定：所有 provider 都归一成 SearchResult(title, url, snippet, provider)，
上层不关心底下是谁。加新 provider 只需写一个 `_search_xxx(query, cfg)` 并
在 `_DISPATCH` 里登记。

实测记录（2026-10，本机网络）：
  DuckDuckGo 前 2 条查询正常，第 3 条起全部 202 风控；
  Bing 能通但返回的 SERP 与查询不相关；
  Jina 免 Key 额度已下线，返回 401。
  → 因此默认走 weread，抓取类只作补充，付费 API 才是稳定解。
"""

from __future__ import annotations

import asyncio
import base64
import re
from dataclasses import dataclass, field
from urllib.parse import quote, unquote

from config import (
    SEARCH_DIGEST_CHARS,
    SEARCH_MAX_BOOKS,
    SEARCH_MAX_QUERIES,
    SEARCH_SNIPPET_CHARS,
    SEARCH_TIMEOUT,
)
from stdfetch import arequest
from stdmodel import Model


@dataclass
class SearchConfig(Model):
    enabled: bool = False
    provider: str = "weread"
    api_key: str = ""
    base_url: str = ""
    max_results: int = 5
    fetch_pages: bool = False
    # 补检深度预设：直接决定选几本、发几条检索式，不需要运行时再想想
    depth: str = "standard"
    # 仅 weread provider 用：登录态 cookie，由路由在启动分析时注入
    cookies: dict = field(default_factory=dict)

    def public(self) -> dict:
        """给前端的配置（永远不带 api_key / cookies）。"""
        d = self.model_dump()
        d.pop("api_key", None)
        d.pop("cookies", None)
        d["api_key_set"] = bool(self.api_key)
        return d


@dataclass
class SearchResult(Model):
    title: str = ""
    url: str = ""
    snippet: str = ""
    provider: str = ""


PROVIDERS = [
    {"id": "weread", "label": "微信读书站内（默认，免 Key 免抓取）", "needs_key": False,
     "needs_base_url": False,
     "hint": "复用你的登录态取官方简介 + 其他读者书评，最稳最快；仅限书架内的书"},
    {"id": "tavily", "label": "Tavily（Agent 专用，推荐配 Key）", "needs_key": True,
     "needs_base_url": False, "hint": "app.tavily.com 申请，1000 次/月免费"},
    {"id": "brave", "label": "Brave Search API", "needs_key": True,
     "needs_base_url": False, "hint": "2000 次/月免费，独立索引"},
    {"id": "serper", "label": "Serper（Google SERP）", "needs_key": True,
     "needs_base_url": False, "hint": "serper.dev，2500 次/月免费"},
    {"id": "searxng", "label": "SearXNG（自建，免 Key 无限额）", "needs_key": False,
     "needs_base_url": True, "hint": "base_url 填 http://localhost:8080"},
    {"id": "jina", "label": "Jina Reader（搜索+抓正文）", "needs_key": True,
     "needs_base_url": False, "hint": "现已需要 Key，jina.ai 申请"},
    {"id": "duckduckgo", "label": "DuckDuckGo（免 Key，易被限流）", "needs_key": False,
     "needs_base_url": False, "hint": "连续请求会返回 202 风控，仅适合小批量"},
    {"id": "bing", "label": "Bing 网页（免 Key，相关性不稳）", "needs_key": False,
     "needs_base_url": False, "hint": "能通但结果有时与查询无关，作兜底"},
    {"id": "mock", "label": "离线假数据（仅一键体验用）", "needs_key": False,
     "needs_base_url": False, "hint": "不联网，用于在没有登录态时验证流程跑通"},
]

# 抓取类 provider：连续请求会被风控，需要节流 + 空结果退避重试
_RATE_LIMITED = {"duckduckgo", "bing", "jina"}

_TAGS_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _clean(text: str, limit: int | None = None) -> str:
    text = _WS_RE.sub(" ", _TAGS_RE.sub("", text or "")).strip()
    return text[:limit] if limit and len(text) > limit else text


def _ua() -> dict:
    return {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }


# ── 各 provider ────────────────────────────────────────

_DDG_LINK_RE = re.compile(
    r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S
)
_DDG_SNIP_RE = re.compile(r'class="result__snippet"[^>]*>(.*?)</a>', re.S)
_DDG_UDdg_RE = re.compile(r"uddg=([^&]+)")


def _search_duckduckgo_sync(html: str, provider: str) -> list[SearchResult]:
    out = []
    links = _DDG_LINK_RE.findall(html)
    snips = _DDG_SNIP_RE.findall(html)
    for i, (href, title) in enumerate(links):
        m = _DDG_UDdg_RE.search(href)
        url = unquote(m.group(1)) if m else href
        snippet = _clean(snips[i], SEARCH_SNIPPET_CHARS) if i < len(snips) else ""
        out.append(SearchResult(
            title=_clean(title, 120), url=url, snippet=snippet, provider=provider
        ))
    return out


async def _search_duckduckgo(query: str, cfg: SearchConfig) -> list[SearchResult]:
    """POST html.duckduckgo.com/html/，失败回落 lite 端点。"""
    for url in ("https://html.duckduckgo.com/html/", "https://lite.duckduckgo.com/lite/"):
        try:
            resp = await arequest(
                "POST", url, data={"q": query, "kl": "cn-zh"},
                headers=_ua(), timeout=SEARCH_TIMEOUT,
            )
            if resp.status_code != 200:
                continue
            results = _search_duckduckgo_sync(resp.text, "duckduckgo")
            if results:
                return results[: cfg.max_results]
        except Exception:
            continue
    return []


_BING_BLOCK_RE = re.compile(r'<li class="b_algo".*?</li>', re.S)
_BING_H2_RE = re.compile(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S)
_BING_P_RE = re.compile(r"<p[^>]*>(.*?)</p>", re.S)
_BING_REDIR_RE = re.compile(r"[?&]u=a1([A-Za-z0-9+/=_-]+)")


def _bing_real_url(href: str) -> str:
    """Bing 的跳转链接把真实地址 base64 塞在 u=a1... 里。"""
    m = _BING_REDIR_RE.search(href)
    if not m:
        return href
    try:
        return base64.urlsafe_b64decode(m.group(1) + "==").decode("utf-8", "ignore")
    except Exception:
        return href


async def _search_bing(query: str, cfg: SearchConfig) -> list[SearchResult]:
    """抓取 bing.com SERP。能通，但实测相关性不稳，只作兜底。"""
    resp = await arequest(
        "GET", "https://www.bing.com/search",
        params={"q": query, "setlang": "zh-CN", "mkt": "zh-CN"},
        headers=_ua(), timeout=SEARCH_TIMEOUT,
    )
    if resp.status_code != 200:
        return []
    html = resp.text

    out = []
    for blk in _BING_BLOCK_RE.findall(html):
        h2 = _BING_H2_RE.search(blk)
        if not h2:
            continue
        cap = _BING_P_RE.search(blk)
        out.append(SearchResult(
            title=_clean(h2.group(2), 120),
            url=_bing_real_url(h2.group(1)),
            snippet=_clean(cap.group(1), SEARCH_SNIPPET_CHARS) if cap else "",
            provider="bing",
        ))
        if len(out) >= cfg.max_results:
            break
    return out


WEREAD_BOOK_URL = "https://weread.qq.com/web/reader/{book_id}"

# 他人书评：把 mine=1 改成 mine=0 即可拿到全站书评（提取流程里已在用同一端点）
WEREAD_SOCIAL_REVIEWS = (
    "https://weread.qq.com/web/review/list"
    "?bookId={book_id}&listType=11&mine=0&synckey=0"
)

_MOCK_LIBRARY = {
    "规模": (
        "杰弗里·韦斯特用「规模法则」解释生命体、城市与公司的生长与衰亡："
        "代谢率随体重的 3/4 次幂变化，城市的基础设施呈次线性、创新呈超线性。"
        "受众为对复杂性科学、城市研究、商业战略感兴趣的读者；"
        "口碑两极——跨学科野心受好评，推演严谨性被部分专业读者质疑。"
    ),
    "禅与摩托车维修艺术": (
        "波西格以一次摩托车旅行为线索，讨论「良质」（Quality）这一无法定义却可感知的价值，"
        "融合哲学随笔与公路叙事。核心受众是对哲学、技术与自我探索有兴趣的读者；"
        "被视为 20 世纪最具影响力的哲理小说之一，也有人觉得后半段说理过于冗长。"
    ),
}


async def _search_weread(query: str, cfg: SearchConfig, book: dict | None) -> list[SearchResult]:
    """用已登录的微信读书会话拿官方简介 + 其他读者书评。

    books_already_have = 现有数据里每本书都带 intro，这里再返回一遍等于重复烧 token。
    所以只补 data 里没有的那部分：站内量化指标 + 他人书评（真正的社群评价）。
    """
    book = book or {}
    book_id = book.get("bookId") or ""
    if not cfg.cookies or not book_id or book_id.startswith("demo"):
        return []

    from services import weread_api as api

    title = book.get("title") or ""
    out: list[SearchResult] = []
    page_url = WEREAD_BOOK_URL.format(book_id=book_id)

    info, _ = await api.fetch_book_info(dict(cfg.cookies), book_id)
    extra_bits = []
    for key, label in (("category", "分类"), ("newRating", "站内评分"),
                       ("ratingCount", "评分人数"), ("readCount", "阅读人数")):
        val = (info or {}).get(key)
        if val:
            extra_bits.append(f"{label}={val}")
    if extra_bits:
        out.append(SearchResult(
            title=f"微信读书站内数据：{title}",
            url=page_url,
            snippet="；".join(extra_bits),
            provider="weread",
        ))

    reviews_data, _ = await api.fetch_reviews_social(dict(cfg.cookies), book_id)
    for r in (reviews_data or {}).get("reviews", [])[: cfg.max_results]:
        inner = r.get("review") if isinstance(r.get("review"), dict) else r
        content = _clean(
            inner.get("content") or inner.get("htmlContent") or inner.get("abstract") or "",
            SEARCH_SNIPPET_CHARS,
        )
        if not content:
            continue
        out.append(SearchResult(
            title=f"读者书评：{title}",
            url=WEREAD_BOOK_URL.format(book_id=book_id),
            snippet=content,
            provider="weread",
        ))

    return out


async def _search_mock(query: str, cfg: SearchConfig, book: dict | None) -> list[SearchResult]:
    """离线假数据。只为「一键体验」在没有登录态时验证流程，生产上不该命中。"""
    title = (book or {}).get("title") or ""
    text = _MOCK_LIBRARY.get(title)
    if not text:
        return []
    return [SearchResult(
        title=f"[离线示例] {title} 的内容/受众/口碑",
        url="",
        snippet=text,
        provider="mock",
    )]


async def _search_searxng(query: str, cfg: SearchConfig) -> list[SearchResult]:
    base = (cfg.base_url or "http://localhost:8080").rstrip("/")
    resp = await arequest(
        "GET", f"{base}/search",
        params={"q": query, "format": "json", "safesearch": 0, "language": "zh-CN"},
        headers=_ua(), timeout=SEARCH_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    return [
        SearchResult(
            title=_clean(r.get("title", ""), 120),
            url=r.get("url", ""),
            snippet=_clean(r.get("content", ""), SEARCH_SNIPPET_CHARS),
            provider="searxng",
        )
        for r in data.get("results", [])[: cfg.max_results]
    ]


async def _search_tavily(query: str, cfg: SearchConfig) -> list[SearchResult]:
    resp = await arequest(
        "POST", "https://api.tavily.com/search",
        json_body={
            "api_key": cfg.api_key,
            "query": query,
            "max_results": cfg.max_results,
            "search_depth": "basic",
        },
        timeout=SEARCH_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    return [
        SearchResult(
            title=_clean(r.get("title", ""), 120),
            url=r.get("url", ""),
            snippet=_clean(r.get("content", ""), SEARCH_SNIPPET_CHARS),
            provider="tavily",
        )
        for r in data.get("results", [])
    ]


async def _search_brave(query: str, cfg: SearchConfig) -> list[SearchResult]:
    resp = await arequest(
        "GET", "https://api.search.brave.com/res/v1/web/search",
        params={"q": query, "count": cfg.max_results, "search_lang": "zh-hans"},
        headers={**_ua(), "X-Subscription-Token": cfg.api_key,
                 "Accept": "application/json"},
        timeout=SEARCH_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    return [
        SearchResult(
            title=_clean(r.get("title", ""), 120),
            url=r.get("url", ""),
            snippet=_clean(r.get("description", ""), SEARCH_SNIPPET_CHARS),
            provider="brave",
        )
        for r in data.get("web", {}).get("results", [])
    ]


async def _search_serper(query: str, cfg: SearchConfig) -> list[SearchResult]:
    resp = await arequest(
        "POST", "https://google.serper.dev/search",
        json_body={"q": query, "num": cfg.max_results, "hl": "zh-cn"},
        headers={**_ua(), "X-API-KEY": cfg.api_key,
                 "Content-Type": "application/json"},
        timeout=SEARCH_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    return [
        SearchResult(
            title=_clean(r.get("title", ""), 120),
            url=r.get("link", ""),
            snippet=_clean(r.get("snippet", ""), SEARCH_SNIPPET_CHARS),
            provider="serper",
        )
        for r in data.get("organic", [])
    ]


async def _search_jina(query: str, cfg: SearchConfig) -> list[SearchResult]:
    """s.jina.ai：搜完顺手把正文抓了，一步到位。返回整段 Markdown 作为单条结果。"""
    headers = {**_ua(), "Accept": "text/markdown"}
    if cfg.api_key:
        headers["Authorization"] = f"Bearer {cfg.api_key}"
    resp = await arequest(
        "GET", f"https://s.jina.ai/{quote(query)}",
        headers=headers, timeout=SEARCH_TIMEOUT * 2,
    )
    resp.raise_for_status()
    return [SearchResult(
        title=f"Jina 检索：{query}",
        url=f"https://s.jina.ai/{quote(query)}",
        snippet=_clean(resp.text, SEARCH_SNIPPET_CHARS * 6),
        provider="jina",
    )]


# 需要 book（而非 query）的 provider：按书取站内资料
_BOOK_SCOPED = {"weread", "mock"}

_DISPATCH = {
    "weread": _search_weread,
    "mock": _search_mock,
    "duckduckgo": _search_duckduckgo,
    "bing": _search_bing,
    "searxng": _search_searxng,
    "tavily": _search_tavily,
    "brave": _search_brave,
    "serper": _search_serper,
    "jina": _search_jina,
}

# 免 Key provider 拿不到结果时的互备顺序（只在同一轮里退让一次）
_FALLBACK_CHAIN = {
    "weread": [],            # 站内拿不到就是真没有，退给抓取也没意义
    "mock": [],
    "duckduckgo": ["bing"],
    "bing": ["duckduckgo"],
    "searxng": [],
    "tavily": [],
    "brave": [],
    "serper": [],
    "jina": ["duckduckgo"],
}


# 抓取类 provider 连续请求会被风控：不加间隔的话连发 7 条只有第 1 条有结果。
_MIN_INTERVAL = 1.5
_last_call = [0.0]
_throttle_lock = asyncio.Lock()


async def _throttle(provider: str) -> None:
    if provider not in _RATE_LIMITED:
        return
    async with _throttle_lock:
        loop = asyncio.get_event_loop()
        wait = _MIN_INTERVAL - (loop.time() - _last_call[0])
        if wait > 0:
            await asyncio.sleep(wait)
        _last_call[0] = loop.time()


async def _once(query: str, cfg: SearchConfig, book: dict | None, provider: str) -> list[SearchResult]:
    fn = _DISPATCH.get(provider, _search_duckduckgo)
    await _throttle(provider)
    if provider in _BOOK_SCOPED:
        return await fn(query, cfg, book)
    return await fn(query, cfg)


async def search(query: str, cfg: SearchConfig, book: dict | None = None) -> list[SearchResult]:
    """单条检索。

    抓取类 provider 遇到空结果会退避重试一次（风控的典型表现是返回空而不是报错），
    仍为空则沿 _FALLBACK_CHAIN 换一个 provider 再试一次。
    任何环节炸了都返回空列表，绝不打断主流程。
    """
    chain = [cfg.provider, *_FALLBACK_CHAIN.get(cfg.provider, [])]
    for provider in chain:
        try:
            results = await _once(query, cfg, book, provider)
            if results:
                return results
            if provider in _RATE_LIMITED:
                await asyncio.sleep(_MIN_INTERVAL * 2)
                results = await _once(query, cfg, book, provider)
                if results:
                    return results
        except Exception as e:
            print(f"[search] {provider} failed for {query!r}: {e}", flush=True)
    return []


async def fetch_page(url: str, api_key: str = "") -> str:
    """r.jina.ai 抓正文转 Markdown。可选，失败返回空串。"""
    headers = {"Accept": "text/markdown"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        resp = await arequest(
            "GET", f"https://r.jina.ai/{url}", headers=headers,
            timeout=SEARCH_TIMEOUT * 2,
        )
        if resp.status_code == 200:
            return _clean(resp.text, 2500)
    except Exception as e:
        print(f"[search] fetch_page failed {url}: {e}", flush=True)
    return ""


# ── 书目补检 ───────────────────────────────────────────

SEARCH_PRESETS = {
    "lean":     {"label": "精简", "max_books": 3, "min_score": 3, "queries": 1},
    "standard": {"label": "标准", "max_books": 5, "min_score": 2, "queries": 2},
    "full":     {"label": "完整", "max_books": SEARCH_MAX_BOOKS, "min_score": 1,
                 "queries": SEARCH_MAX_QUERIES},
}


def build_queries(book: dict, n: int = SEARCH_MAX_QUERIES) -> list[str]:
    """工序二：出检索式。纯模板，不经过 LLM。

    "主要内容 / 受众 / 社群评价"三个诉求各一条，由预设决定发几条。
    """
    title = (book.get("title") or "").strip()
    author = (book.get("author") or "").strip()
    who = f"{author} " if author else ""
    pool = [
        f"《{title}》 {who}主要内容 讲的是什么 核心观点",
        f"《{title}》 {who}豆瓣 评分 读者评价 适合什么人读",
    ]
    return pool[:n]



def _obscurity_score(book: dict) -> int:
    """工序一用的打分：书的"本地信息缺失度"，分越高越该拿去联网补。

    刻意收紧：宁可漏检，也不要把大半个书架都送去检索（慢、贵、还容易踩风控）。
    """
    score = 0
    intro = (book.get("intro") or "").strip()
    if len(intro) < 10:
        score += 2          # 连简介都没有
    if not book.get("rating"):
        score += 2          # 微信读书没评分，通常是冷门书
    if not book.get("totalBookmarks") and not book.get("totalReviews"):
        score += 1          # 自己也没留下笔记，本地无信息可补
    return score


def select_candidates(books: list[dict], preset: dict) -> list[dict]:
    """工序一：选书。按信息缺失度排序，卡 min_score 与 max_books 两道闸。"""
    scored = [(_obscurity_score(b), b) for b in books]
    scored = [item for item in scored if item[0] >= preset["min_score"]]
    scored.sort(key=lambda x: -x[0])
    return [b for _, b in scored[: preset["max_books"]]]


def compile_entry(book: dict, results: list[SearchResult]) -> str:
    """工序五：把一本书的结果压成紧凑文本块。"""
    lines = [f"--- 《{book.get('title', '')}》 / {book.get('author', '?')} ---"]
    for i, r in enumerate(results, 1):
        lines.append(f"[{i}] {r.title}")
        if r.url:
            lines.append(f"    {r.url}")
        if r.snippet:
            lines.append(f"    {r.snippet}")
    return "\n".join(lines)


def rank_results(results: list[SearchResult], limit: int) -> list[SearchResult]:
    """工序四：去重 + 排序。

    有摘要的排前面（空摘要进 prompt 只是白占 token），其次按摘要长度降序。
    """
    seen, uniq = set(), []
    for r in results:
        key = r.url or f"{r.title}::{r.snippet[:40]}"
        if key in seen:
            continue
        seen.add(key)
        uniq.append(r)
    uniq.sort(key=lambda r: (-len(r.snippet or ""), -len(r.title or "")))
    return [r for r in uniq if r.snippet][:limit] or uniq[:limit]


DIGEST_HEADER = (
    "以下是联网检索到的外部资料，用于补足你可能不熟悉的书籍。"
    "请优先采信这些资料判断其内容、受众与口碑，但不要虚构资料中不存在的信息。"
    "若资料与你的记忆冲突，以资料为准。\n"
)


async def enrich_books(
    books: list[dict],
    cfg: SearchConfig,
    *,
    progress=None,
) -> tuple[str, list[dict]]:
    """补检编排：选书 → 出检索式 → 检索 → 排序 → 编译摘要。五道工序全是本地逻辑。

    这里刻意没有任何 LLM 调用。整条链路里 LLM 只出场一次：最后那次生成评价。
    "哪些书要搜、发什么检索式、结果怎么挑"全部由 SEARCH_PRESETS 与本地打分定死，
    既省 token，也让每次运行的开销可预测。
    """
    if not cfg.enabled or not books:
        return "", []

    preset = SEARCH_PRESETS.get(cfg.depth, SEARCH_PRESETS["standard"])
    candidates = select_candidates(books, preset)
    if not candidates:
        if progress:
            await progress("联网补检：本地资料已足够，跳过外部检索")
        return "", []

    if progress:
        await progress(
            f"联网补检：{len(candidates)} 本书"
            f"（预设 {preset['label']}，provider={cfg.provider}）"
        )

    digest_parts: list[str] = []
    hits: list[dict] = []

    for b in candidates:
        title = b.get("title", "")
        results: list[SearchResult] = []
        queries: list[str] = []

        if cfg.provider in _BOOK_SCOPED:
            # 站内 / 离线：按书直取，不需要检索式
            if progress:
                await progress(f"补检：《{title}》")
            results = await search("", cfg, book=b)
        else:
            queries = build_queries(b, preset["queries"])
            for q in queries:
                if progress:
                    await progress(f"检索：《{title}》")
                results.extend(await search(q, cfg, book=b))

        uniq = rank_results(results, cfg.max_results)
        if not uniq:
            continue

        if cfg.fetch_pages and uniq[0].url:
            page = await fetch_page(uniq[0].url, cfg.api_key)
            if page:
                uniq[0].snippet = (uniq[0].snippet + " " + page)[: SEARCH_SNIPPET_CHARS * 8]

        hits.append({
            "bookId": b.get("bookId"),
            "title": title,
            "author": b.get("author", "?"),
            "infoGap": _obscurity_score(b),
            "queries": queries,
            "provider": uniq[0].provider or cfg.provider,
            "results": [r.model_dump() for r in uniq[: cfg.max_results]],
        })
        digest_parts.append(compile_entry(b, uniq[: cfg.max_results]))

    if not digest_parts:
        return "", []

    digest = "\n\n".join(digest_parts)
    return DIGEST_HEADER + digest[:SEARCH_DIGEST_CHARS], hits
