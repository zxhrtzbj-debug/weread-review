"""用真实登录态校准两件事：用户评价字段、本地上传文件判定。

为什么需要它
────────────
「我的评分」和「本地文件判定」这两条规则是从接口文档与字段缺失度推出来的，
没有真机数据校准过。判定规则写在 services/weread.py 里（extract_my_rating /
classify_book_source），改判据之前先跑这个脚本看真实字段。

跑法
────
    cd review_weread
    python3 archive/diagnose/probe_rating.py --cookie 'wr_vid=xxx; wr_skey=yyy'
    python3 archive/diagnose/probe_rating.py --cookie-file ~/.weread_cookie
    WEREAD_COOKIE='...' python3 archive/diagnose/probe_rating.py

cookie 怎么拿：浏览器登录 weread.qq.com → DevTools → Network → 随便一个
weread.qq.com/web/... 请求 → Request Headers → Cookie 整行复制。

输出
────
每本书三行：评分命中情况 / review 原始键 / 来源判定与证据。
末尾给汇总：多少本取到评分、来源判定分布、拿到的全部 star 取值集合。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from services import weread_api as api  # noqa: E402
from services.weread import (  # noqa: E402
    _deep_field,
    _normalize_star,
    _response_rating,
    _review_type,
    classify_book_source,
    extract_my_rating,
    parse_book_detail,
)


def parse_cookie(raw: str) -> dict:
    out = {}
    for part in (raw or "").split(";"):
        pair = part.strip()
        if "=" not in pair:
            continue
        k, v = pair.split("=", 1)
        out[k.strip()] = v.strip()
    return out


async def probe(cookies: dict, max_books: int, verbose: bool) -> None:
    notebooks, cookies = await api.fetch_notebooks(cookies)
    books_raw = notebooks.get("books", []) or []
    if not books_raw:
        print("笔记本列表为空 —— cookie 多半已失效（换一份再试）")
        return

    print(f"笔记本 {len(books_raw)} 本，本次探测前 {max_books} 本\n")
    stars_seen: set = set()
    rated = 0
    sources: dict[str, int] = {}
    keys_seen: set = set()

    for i, item in enumerate(books_raw[:max_books]):
        book = item.get("book", item) if isinstance(item, dict) else {}
        bid = book.get("bookId")
        title = (book.get("title") or "")[:34]
        if not bid:
            continue

        info, _ = await api.fetch_book_info(dict(cookies), bid)
        reviews_data, _ = await api.fetch_reviews(dict(cookies), bid)
        all_reviews = reviews_data.get("reviews", []) or []

        if my_rating is None:
            my_rating, src = _response_rating(reviews_data), "response"
        source, signals, confidence = classify_book_source(
            book, info or {}, my_rating=my_rating
        )
        sources[f"{source}/{confidence}"] = sources.get(f"{source}/{confidence}", 0) + 1
        if my_rating is not None:
            rated += 1
            stars_seen.add(my_rating)

        print(f"[{i + 1}] {title}")
        print(f"    评分: {my_rating}（来源 {src or '无'}）｜社区评分: {(info or {}).get('newRating')}")
        print(f"    来源判定: {source}（置信 {confidence}）｜依据: {', '.join(signals) or '无'}")
        if verbose:
            print(f"    notebook 条目键: {sorted(book.keys())}")

        for r in all_reviews[:3]:
            if not isinstance(r, dict):
                continue
            keys_seen.update(r.keys())
            inner = r.get("review")
            if isinstance(inner, dict):
                keys_seen.update("review." + k for k in inner.keys())
                deep = inner.get("review")
                if isinstance(deep, dict):
                    keys_seen.update("review.review." + k for k in deep.keys())
            raw_star = _deep_field(r, "star")
            if raw_star is not None:
                stars_seen.add(("raw", raw_star))
            print(
                f"      · type={_review_type(r)} star_raw={raw_star!r} "
                f"→归一 {_normalize_star(raw_star)} "
                f"chapterName={(_deep_field(r, 'chapterName') or '')[:12]!r} "
                f"isFinish={_deep_field(r, 'isFinish')!r}"
            )
        if not all_reviews:
            print("      · 无 review")

        if verbose:
            print(f"      info keys: {sorted((info or {}).keys())}")
        print()

    print("──── 汇总 ────")
    print(f"探测 {min(max_books, len(books_raw))} 本，取到评分 {rated} 本")
    print(f"来源判定分布: {sources}")
    print(f"star 取值集合（已归一）: {sorted({s for s in stars_seen if not isinstance(s, tuple)})}")
    raw_vals = sorted({s[1] for s in stars_seen if isinstance(s, tuple)},
                      key=lambda x: (isinstance(x, str), x))
    print(f"star 原始取值集合: {raw_vals}")
    print(f"review 出现过的键: {sorted(keys_seen)}")
    print(
        "\n判据校准提示：\n"
        "  · 若 star 原始值全是 20/40/60/80/100 → 百分制，_normalize_star 已覆盖\n"
        "  · 若出现 -1 → 无评分，已按 None 处理\n"
        "  · 若 rated=0 而其实你打过分 → 检查上面 printed 的原始键里评分叫什么，"
        "把它加进 _RESPONSE_RATING_FIELDS 或 _deep_field 的候选路径"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="探测微信读书评分字段与本地文件判定")
    ap.add_argument("--cookie", default="", help="浏览器复制的 Cookie 整行")
    ap.add_argument("--cookie-file", default="", help="存 cookie 的文件路径")
    ap.add_argument("--max-books", type=int, default=5, help="探测几本书，默认 5")
    ap.add_argument("--verbose", action="store_true", help="打印 book/info 的全部键")
    args = ap.parse_args()

    raw = args.cookie
    if not raw and args.cookie_file:
        raw = Path(args.cookie_file).expanduser().read_text(encoding="utf-8")
    if not raw:
        raw = __import__("os").environ.get("WEREAD_COOKIE", "")

    cookies = parse_cookie(raw)
    if not cookies:
        print("没有 cookie。用 --cookie 'wr_vid=...; wr_skey=...' 传入，或设 WEREAD_COOKIE。")
        print(json.dumps({"hint": "浏览器 DevTools → Network → weread.qq.com 请求头里的 Cookie"},
                         ensure_ascii=False))
        sys.exit(1)
    if "wr_vid" not in cookies:
        print(f"警告：cookie 里没有 wr_vid，只有 {list(cookies)[:6]} —— 可能没登录")

    asyncio.run(probe(cookies, args.max_books, args.verbose))


if __name__ == "__main__":
    main()
