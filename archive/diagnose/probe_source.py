"""校准「上架书籍 vs 本地上传文件」判定：这次不猜，让真机数据说话。

为什么需要它
──────────
判定规则（services/weread.py 的 classify_book_source）里，评分是硬证据
（微信读书只给上架书籍打分），书名扩展名是另一条硬证据。剩下的书落在
「详情没取到 / 元数据稀疏」这一档，目前按上架书籍收录并标低置信。

这一档能不能再切一刀，取决于真机数据：真本地文件和低置信上架书在
author / cover / category / bookId 形态上到底差多少。这个脚本就是把
这个分布打出来——先算，再谈改判据。

跑法
────
    cd review_weread
    python3 archive/diagnose/probe_source.py --cookie 'wr_vid=xxx; wr_skey=yyy'
    python3 archive/diagnose/probe_source.py --cookie-file ~/.weread_cookie
    WEREAD_COOKIE='...' python3 archive/diagnose/probe_source.py --max-books 200

cookie 怎么拿：浏览器登录 weread.qq.com → DevTools → Network → 随便一个
weread.qq.com/web/... 请求 → Request Headers → Cookie 整行复制。

输出
────
  · 判定分布（上架高置信 / 上架低置信 / 本地）
  · 被判本地的书里有多少带评分 ——  nonzero 就是规则写错了，不是数据怪
  · errcode 分布：哪些码出现在本该是上架书的东西上
  · 三组书的字段画像对照表（author/cover/category 空值率、bookId 形态）
  · 低置信组里"作者与封面同时为空"的占比 —— 这一刀值不值得切，看这个数
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from services import weread_api as api  # noqa: E402
from services.weread import (  # noqa: E402
    _response_rating,
    classify_book_source,
    extract_my_rating,
)

# 上架书的 bookId 常见形态：24–32 位小写十六进制（书城详情页 URL 里那种）。
# 本地上传文件的 bookId 形态各家说法不一，这里只统计分布，不当判据用。
_HEX_ID_RE = re.compile(r"^[0-9a-f]{24,32}$", re.I)
_DIGIT_ID_RE = re.compile(r"^\d+$")


def parse_cookie(raw: str) -> dict:
    out = {}
    for part in (raw or "").split(";"):
        pair = part.strip()
        if "=" not in pair:
            continue
        k, v = pair.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def book_id_shape(book_id: str) -> str:
    bid = str(book_id or "")
    if _HEX_ID_RE.match(bid):
        return "hex24-32"
    if _DIGIT_ID_RE.match(bid):
        return "纯数字"
    return "其他"


def profile(book: dict, info: dict) -> dict:
    """一本书的字段画像：判据之外还能看见什么。"""
    return {
        "author_empty": not (book.get("author") or info.get("author") or "").strip(),
        "cover_empty": not (book.get("cover") or info.get("cover") or "").strip(),
        "category_empty": not (info.get("category") or "").strip(),
        "intro_empty": not (info.get("intro") or "").strip(),
        "id_shape": book_id_shape(book.get("bookId")),
    }


def summarize(tag: str, rows: list[dict]) -> None:
    if not rows:
        print(f"\n── {tag} ──\n  （0 本）")
        return
    n = len(rows)
    print(f"\n── {tag} ── 共 {n} 本")
    for field in ("author_empty", "cover_empty", "category_empty", "intro_empty"):
        empty = sum(1 for r in rows if r[field])
        print(f"  {field:16s} 空 {empty:5d} 本（{empty / n * 100:5.1f}%）")
    both = sum(1 for r in rows if r["author_empty"] and r["cover_empty"])
    print(f"  作者与封面同时为空    {both:5d} 本（{both / n * 100:5.1f}%）")
    print(f"  bookId 形态分布       {dict(Counter(r['id_shape'] for r in rows))}")


async def probe(cookies: dict, max_books: int, verbose: bool) -> None:
    notebooks, cookies = await api.fetch_notebooks(cookies)
    books_raw = notebooks.get("books", []) or []
    if not books_raw:
        print("笔记本列表为空 —— cookie 多半已失效（换一份再试）")
        return

    total = min(max_books, len(books_raw)) if max_books else len(books_raw)
    print(f"笔记本 {len(books_raw)} 本，本次探测前 {total} 本\n")

    groups: dict[str, list[dict]] = {
        "上架·高置信": [],
        "上架·低置信": [],
        "本地上传": [],
    }
    errcodes: Counter = Counter()
    local_with_rating: list[str] = []
    recovered = 0

    for i, item in enumerate(books_raw[:total]):
        book = item.get("book", item) if isinstance(item, dict) else {}
        bid = book.get("bookId")
        if not bid:
            continue
        title = (book.get("title") or "")[:34]

        info, _, meta = await api.fetch_book_info_meta(dict(cookies), bid)
        reviews_data, _ = await api.fetch_reviews(dict(cookies), bid)
        my_rating, _src = extract_my_rating(reviews_data.get("reviews", []) or [])
        if my_rating is None:
            my_rating = _response_rating(reviews_data)

        if meta.get("recovered"):
            recovered += 1
        if meta.get("errcode") is not None:
            errcodes[meta["errcode"]] += 1

        source, signals, confidence = classify_book_source(
            book, info or {}, my_rating=my_rating
        )
        row = profile(book, info or {})
        row["title"] = title
        row["my_rating"] = my_rating
        row["signals"] = signals

        if source == "local":
            groups["本地上传"].append(row)
            if my_rating is not None or (info or {}).get("newRating"):
                local_with_rating.append(f"{title}（评分 {my_rating}）")
        elif confidence == "low":
            groups["上架·低置信"].append(row)
        else:
            groups["上架·高置信"].append(row)

        if verbose:
            print(f"[{i + 1}] {title}")
            print(f"    {source}/{confidence} ｜ {', '.join(signals)}")
            print(f"    errcode={meta.get('errcode')} attempts={meta.get('attempts')} "
                  f"recovered={meta.get('recovered')}")

    print("\n════ 汇总 ════")
    for tag, rows in groups.items():
        print(f"  {tag:12s} {len(rows):5d} 本")
    print(f"\n  换路径后救回详情的    {recovered} 本"
          f"（这些书原本会因为 errcode 被误判成本地文件）")

    print(f"\n  errcode 分布: {dict(errcodes)}")
    print("  ↑ errcode 只说明『这一路没取到』，不说明『它是本地文件』；")
    print("    看上面『救回』那一栏就知道有多少是可以靠换路径修好的。")

    if local_with_rating:
        print(f"\n  ⚠️ 被判本地却带评分的 {len(local_with_rating)} 本（规则有 bug，不是数据怪）：")
        for t in local_with_rating[:20]:
            print(f"      · {t}")
    else:
        print("\n  ✓ 被判本地的书里没有一本带评分 —— 评分硬信号生效正常。")

    summarize("上架·高置信", groups["上架·高置信"])
    summarize("上架·低置信", groups["上架·低置信"])
    summarize("本地上传", groups["本地上传"])

    low = groups["上架·低置信"]
    loc = groups["本地上传"]
    if low and loc:
        low_both = sum(1 for r in low if r["author_empty"] and r["cover_empty"]) / len(low)
        loc_both = sum(1 for r in loc if r["author_empty"] and r["cover_empty"]) / len(loc)
        print("\n  能不能再切一刀：")
        print(f"    低置信上架书里 作者+封面 同时为空的占 {low_both * 100:.1f}%")
        print(f"    真本地文件里   作者+封面 同时为空的占 {loc_both * 100:.1f}%")
        print("    两者差距够大，这条才能当判据；差不开就维持现状（一律按上架收录）。")


def main() -> None:
    ap = argparse.ArgumentParser(description="校准上架书籍 / 本地上传文件的判定")
    ap.add_argument("--cookie", default="", help="浏览器复制的 Cookie 整行")
    ap.add_argument("--cookie-file", default="", help="存 cookie 的文件路径")
    ap.add_argument("--max-books", type=int, default=0, help="探测几本书，0 = 全部")
    ap.add_argument("--verbose", action="store_true", help="逐本打印判定过程")
    args = ap.parse_args()

    raw = args.cookie
    if not raw and args.cookie_file:
        raw = Path(args.cookie_file).expanduser().read_text(encoding="utf-8")
    if not raw:
        raw = os.environ.get("WEREAD_COOKIE", "")

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
