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

from config import (
    DEFAULT_CONTENT_FILTER,
    LOCAL_PICK_SLOTS,
    MAX_SESSIONS,
    SESSION_TTL,
    UNRECOGNIZED_CATEGORY,
)
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
            # /api/data/scan 的预取结果：确认时间筛选后提取直接复用，
            # 书单接口不用打第二遍。
            "scan_books_raw": None,
            "scan_shelf": None,
            # 审核阶段
            "deleted_book_ids": [],
            "content_filter": _empty_content_filter(),
            # 本地上传文件的自选结果：{bookId: {"slot": "印象最深", "note": "用户写的感悟"}}
            # 这些文件没有书名/分类/评分，默认不进书单，只有被选中才参与评价。
            "local_picks": {},
            # 来源判定的人工改判：{bookId: "weread" | "local"}。
            # 自动判定靠元数据缺失度，冷门书偶尔会被误判成本地文件；
            # 没有这个出口的话，那本书就只能从书单里消失且没法救回来。
            "source_overrides": {},
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
        """剔除已删除书籍 + 按内容筛选裁剪字段，并重算统计。

        本地上传文件的处理在这里：它们默认不进书单（书名常是文件名，
        会让 LLM 对着一串 PDF 名字编造品味），只有在 local_picks 里被
        用户归入某个分类的才进来，并带上用户自己写的介绍感悟。
        全部本地文件仍然一并返回（挂在 localFiles 上）供前端选择面板用。
        """
        data = session.get("data")
        if not data:
            return None

        deleted = self.deleted_ids(session)
        kept = [b for b in data.get("books", []) if b.get("bookId") not in deleted]
        overrides = session.get("source_overrides") or {}

        all_local = [b for b in kept if _is_local(b, overrides)]
        picks = session.get("local_picks") or {}

        books = []
        for b in kept:
            if _is_local(b, overrides):
                pick = picks.get(b.get("bookId"))
                if not pick:
                    continue  # 未被用户选中 → 不进书单
                b = {**b, "userSlot": pick.get("slot", ""),
                     "userNote": (pick.get("note") or "").strip()}
            books.append(b)

        cf = {**_empty_content_filter(), **(session.get("content_filter") or {})}
        books = [_apply_content_filter(b, cf) for b in books]

        # 本地文件连书名都不可靠，让它们被联网补检只会浪费检索配额
        searchable = [b for b in books if not _is_local(b, overrides)]
        return {
            "books": books,
            "stats": _recompute_stats(books, local_total=len(all_local)),
            "localFiles": [_local_file_view(b) for b in all_local],
            "_searchable_book_ids": [b.get("bookId") for b in searchable],
        }

    # ── 来源判定的手工改判 ───────────────────────────
    def set_source_override(self, session: dict, book_id: str, source: str | None) -> dict:
        """人工改判一本书是上架书籍还是本地文件。source=None 表示撤销改判。

        自动判定按"出版物元数据缺失度"打分，冷门书偶尔会被误判成本地文件；
        这个出口让那本书能被放回书单。
        """
        overrides = dict(session.get("source_overrides") or {})
        if source in ("weread", "local"):
            overrides[str(book_id)] = source
        else:
            overrides.pop(str(book_id), None)
        session["source_overrides"] = overrides
        # 改判成上架书籍时，之前为它做的本地分类就作废了
        if source == "weread":
            picks = dict(session.get("local_picks") or {})
            picks.pop(str(book_id), None)
            session["local_picks"] = picks
        return overrides

    # ── 本地上传文件的自选分类 ───────────────────────
    def set_local_picks(self, session: dict, picks: dict) -> dict:
        """保存用户为本地文件做的分类选择。

        picks: {bookId: {"slot": str, "note": str}}。同一个分类只保留最后一个
        提交的书（一个分类选一本），note 留空或键缺失即清除该选择。
        """
        cleaned: dict[str, dict] = {}
        for book_id, pick in (picks or {}).items():
            if not isinstance(pick, dict):
                continue
            slot = (pick.get("slot") or "").strip()
            note = (pick.get("note") or "").strip()
            if not slot and not note:
                continue
            cleaned[str(book_id)] = {"slot": slot, "note": note}
        session["local_picks"] = cleaned
        return cleaned

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


def _is_local(book: dict, overrides: dict | None = None) -> bool:
    """本地上传文件。人工改判优先于自动判定。

    source 缺失时（旧数据/示例数据）一律按上架书籍处理，
    免得一次后端升级把整份旧数据判成本地文件、书单直接清空。
    """
    bid = book.get("bookId")
    if overrides and bid in overrides:
        return overrides[bid] == "local"
    return book.get("source") == "local"


def _local_file_view(book: dict) -> dict:
    """给前端选择面板用的精简视图。"""
    return {
        "bookId": book.get("bookId"),
        "title": book.get("title") or "（无标题）",
        "author": book.get("author") or "",
        "sourceSignals": book.get("sourceSignals") or [],
        "sourceConfidence": book.get("sourceConfidence") or "high",
        # 评分是"这必然是上架书籍"的硬证据：误判进这个面板的书多半带着评分，
        # 前端据此把一眼可辨的误伤挑出来，供用户批量放回书单。
        "myRating": book.get("myRating"),
        "rating": book.get("rating") or 0,
        "totalBookmarks": book.get("totalBookmarks", 0),
        "totalReviews": book.get("totalReviews", 0),
        "totalBookReviews": book.get("totalBookReviews", 0),
    }


def _apply_content_filter(book: dict, cf: dict) -> dict:
    """按内容筛选裁剪单本书。返回副本，不动原数据。"""
    out = dict(book)

    # 只保留"我打过分的书"的划线/想法：打分是一次明确的偏好表态，
    # 没打分的书只保留书单行（书名/分类/社区评分），不再展开内容。
    rated_only = bool(cf.get("ratedOnly")) and not out.get("hasMyRating")

    if cf.get("keepBookmarks") and not rated_only:
        bms = list(out.get("bookmarks") or [])
        cap = cf.get("maxBookmarksPerBook") or 0
        if cap and len(bms) > cap:
            bms = bms[:cap]
        out["bookmarks"] = bms
    else:
        out["bookmarks"] = []

    if cf.get("keepReviews") and not rated_only:
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


def _recompute_stats(books: list[dict], local_total: int = 0) -> dict:
    categories = Counter(
        (b.get("category") or "").strip() or UNRECOGNIZED_CATEGORY
        for b in books if b.get("source") != "local"
    )
    authors = Counter(b.get("author", "") for b in books if b.get("author"))
    rated = [b.get("myRating") for b in books if b.get("myRating") is not None]
    return {
        "totalBooks": len(books),
        "totalBookmarks": sum(b.get("totalBookmarks", 0) for b in books),
        "totalReviews": sum(b.get("totalReviews", 0) for b in books),
        "totalBookReviews": sum(b.get("totalBookReviews", 0) for b in books),
        # 全部本地文件数（含未被选中的）—— 前端要显示"还有 N 本没归类"
        "localFiles": local_total,
        "localPicked": sum(1 for b in books if b.get("userSlot")),
        "uncertainBooks": sum(
            1 for b in books if b.get("sourceConfidence") == "low"
        ),
        "ratedBooks": len(rated),
        "avgMyRating": round(sum(rated) / len(rated), 2) if rated else 0,
        "topCategories": categories.most_common(10),
        "topAuthors": authors.most_common(10),
    }


def deep_copy(data):
    return deepcopy(data)


# 进程内单例。所有 router 共用同一个仓库。
store = SessionStore()
