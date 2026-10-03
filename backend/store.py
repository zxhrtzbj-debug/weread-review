"""会话状态仓库。

原先这些状态是 main.py 里一个裸 dict（sessions = {}），
删除书籍、内容筛选、LLM 配置、搜索配置全靠 setdefault 现补字段，
改一处要翻三个地方。这里收成一个 SessionStore：

    SessionStore
      ├─ create()           新会话（微信登录或 demo 预设共用）
      ├─ get()
      ├─ active_data(uid)   应用「删除书籍 + 内容筛选」后的有效数据
      └─ build_payload()    交给 LLM 之前的最终数据

核心约定：session["data"] 永远是原始全量数据，绝不原地修改。
删除和内容筛选都在 active_data() 里现场投影，因此可以随时来回切换。
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import Counter
from copy import deepcopy

from config import DEFAULT_CONTENT_FILTER, MAX_SESSIONS, SESSION_TTL
from services.llm import LLMConfig


def _empty_content_filter() -> dict:
    return dict(DEFAULT_CONTENT_FILTER)


class SessionStore:
    """内存会话表。

    本地单机用时这层越简单越好，所以原本只有个裸 dict。上公网后多了两件事：

    · 空闲回收：session 里有 cookies、有整本书的划线数据，全部常驻内存。
      没有回收的话，实例跑上几天内存就只增不减，最终被 OOM 掉。
    · 容量上限：同理，防止有人循环调 /api/auth/session 把内存打满。
    """

    def __init__(self) -> None:
        self._sessions: dict[str, dict] = {}
        self._touched: dict[str, float] = {}
        self._lock = threading.Lock()

    # ── 生命周期 ──────────────────────────────────────
    def create(self, uid: str | None = None) -> dict:
        self._evict()
        uid = uid or uuid.uuid4().hex[:12]
        session = {
            "uid": uid,
            "cookies": {},
            "logged_in": False,
            # 数据提取
            "data": None,
            "extracting": False,
            "progress": {},
            "pending_books": [],
            "notebooks_meta": None,
            "_notebooks_sent": False,
            "timeline": None,
            "_timeline_sent": False,
            "extract_filter": None,
            "extract_filter_done": False,
            # 审核阶段
            "deleted_book_ids": [],
            "content_filter": _empty_content_filter(),
            # 分析阶段
            "llm_config": LLMConfig().model_dump(),
            "search_config": {
                "enabled": False,
                "provider": "weread",
                "api_key": "",
                "base_url": "",
                "max_results": 5,
                "fetch_pages": False,
                "depth": "standard",
            },
            "style": "serious",
            "report": None,
            "analyzing": False,
            "analysis_progress": {},
            "search_digest": None,
        }
        with self._lock:
            self._sessions[uid] = session
            self._touched[uid] = time.monotonic()
        return session

    def get(self, uid: str) -> dict | None:
        with self._lock:
            session = self._sessions.get(uid)
            if session is not None:
                self._touched[uid] = time.monotonic()
            return session

    def drop(self, uid: str) -> None:
        with self._lock:
            self._sessions.pop(uid, None)
            self._touched.pop(uid, None)

    # ── 回收 ──────────────────────────────────────────
    def _evict(self) -> None:
        """回收空闲超时的会话；超上限时按最久未使用丢弃。

        正在提取/分析中的会话不回收：它们占着 CPU 和网络，
        提前杀掉只会让用户看到"session not found"这种没法理解的报错。
        """
        now = time.monotonic()
        with self._lock:
            for uid in [
                u for u, t in self._touched.items()
                if now - t > SESSION_TTL
                and not self._sessions[u].get("extracting")
                and not self._sessions[u].get("analyzing")
            ]:
                self._sessions.pop(uid, None)
                self._touched.pop(uid, None)

            if len(self._sessions) > MAX_SESSIONS:
                excess = len(self._sessions) - MAX_SESSIONS
                for uid in sorted(self._touched, key=self._touched.get)[:excess]:
                    self._sessions.pop(uid, None)
                    self._touched.pop(uid, None)

    def stats(self) -> dict:
        """给 /api/health 看：实例上有几个会话、占用多少。"""
        self._evict()
        with self._lock:
            return {
                "sessions": len(self._sessions),
                "ttl_seconds": SESSION_TTL,
                "max_sessions": MAX_SESSIONS,
            }

    # ── 有效数据投影 ──────────────────────────────────
    def deleted_ids(self, session: dict) -> set[str]:
        return set(session.get("deleted_book_ids", []))

    def active_data(self, session: dict) -> dict | None:
        """剔除已删除书籍 + 按内容筛选裁剪字段，并重算统计。"""
        data = session.get("data")
        if not data:
            return None

        deleted = self.deleted_ids(session)
        books = [b for b in data.get("books", []) if b.get("bookId") not in deleted]

        cf = {**_empty_content_filter(), **(session.get("content_filter") or {})}
        books = [_apply_content_filter(b, cf) for b in books]
        return {"books": books, "stats": _recompute_stats(books)}

    # ── 便捷读写 ──────────────────────────────────────
    def set_content_filter(self, session: dict, patch: dict) -> dict:
        cf = {**_empty_content_filter(), **(session.get("content_filter") or {})}
        cf.update({k: v for k, v in patch.items() if k in cf})
        # 条数上限不可能是负数
        for key in ("maxBookmarksPerBook", "maxReviewsPerBook"):
            try:
                cf[key] = max(0, int(cf[key] or 0))
            except (TypeError, ValueError):
                cf[key] = 0
        session["content_filter"] = cf
        return cf

    def set_llm_config(self, session: dict, patch: dict) -> dict:
        cfg = dict(session.get("llm_config") or {})
        cfg.update({k: v for k, v in patch.items() if v not in (None, "")})
        session["llm_config"] = cfg
        return cfg

    def set_search_config(self, session: dict, patch: dict) -> dict:
        cfg = dict(session.get("search_config") or {})
        cfg.update({k: v for k, v in patch.items() if v is not None})
        session["search_config"] = cfg
        return cfg


def _apply_content_filter(book: dict, cf: dict) -> dict:
    """按内容筛选裁剪单本书。返回副本，不动原数据。"""
    out = dict(book)

    if cf.get("keepBookmarks"):
        bms = list(out.get("bookmarks") or [])
        cap = cf.get("maxBookmarksPerBook") or 0
        if cap and len(bms) > cap:
            bms = bms[:cap]
        out["bookmarks"] = bms
    else:
        out["bookmarks"] = []

    if cf.get("keepReviews"):
        rvs = list(out.get("reviews") or [])
        cap = cf.get("maxReviewsPerBook") or 0
        if cap and len(rvs) > cap:
            rvs = rvs[:cap]
        out["reviews"] = rvs
    else:
        out["reviews"] = []

    if cf.get("keepBookReviews"):
        out["bookReviews"] = list(out.get("bookReviews") or [])
    else:
        out["bookReviews"] = []

    # 统计口径跟裁剪后的实际内容一致，避免前端显示 300 条、实际送进去 0 条
    out["totalBookmarks"] = len(out["bookmarks"])
    out["totalReviews"] = len(out["reviews"])
    out["totalBookReviews"] = len(out["bookReviews"])
    return out


def _recompute_stats(books: list[dict]) -> dict:
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


def deep_copy(data):
    return deepcopy(data)


# 进程内单例。所有 router 共用同一个仓库。
store = SessionStore()
