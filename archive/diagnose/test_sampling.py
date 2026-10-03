"""抽样机制的对照验证：顺延、倒挂两组、上限。

跑法：cd backend && python3 ../archive/diagnose/test_sampling.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from services.analyzer import _sample_books  # noqa: E402


def mk(i, rating, my, bm, rv, cat):
    return {
        "bookId": f"b{i}", "title": f"书{i}", "rating": rating,
        "myRating": my, "category": cat, "totalBookmarks": bm,
        "totalReviews": rv, "bookmarks": [], "reviews": [],
        "bookReviews": [], "source": "weread", "hasMyRating": my is not None,
    }


def show(title, books):
    s = _sample_books(books)
    print(f"\n=== {title}（样本 {len(s)} 本）===")
    for b, why in s:
        print(f"  {b['bookId']:>4} 社区{b['rating']:5.1f} 我的{str(b['myRating']):>4} "
              f"划线{b['totalBookmarks']:>4} 想法{b['totalReviews']:>3}  <- {why}")
    return s


# 场景 1：评分最高的两本同时是划线最多的两本 → 后者必须顺延到 3、4 名
books = [mk(0, 9.8, 5, 900, 1, "A"), mk(1, 9.7, 1, 800, 2, "A")]
for i in range(2, 20):
    books.append(mk(i, 9.0 - i * 0.4, None if i % 5 == 0 else 5 - (i % 5), 50 + i, i, f"C{i % 3}"))
s1 = show("顺延：评分最高 == 划线最多", books)

reasons = [w for _, w in s1]
assert "划线最多" in reasons, "划线分类没拿到名额"

# 顺延的定义：被前面分类占用的书不能再出现，"划线最多"拿到的是剩余里的前两名
taken_before: set[str] = set()
for b, w in s1:
    if w == "划线最多":
        break
    taken_before.add(b["bookId"])
bm_picks = [b["bookId"] for b, w in s1 if w == "划线最多"]
expect = [b["bookId"] for b in sorted(
    (x for x in books if x["bookId"] not in taken_before),
    key=lambda x: -x["totalBookmarks"],
)][:2]
assert bm_picks == expect, f"划线最多应顺延到 {expect}，实际 {bm_picks}"
assert not ({"b0", "b1"} & set(bm_picks)), "顺延失效：又选回了已被占用的书"
print(f"  → 划线最多顺延到 {bm_picks}（b0/b1 已被评分分类占用）")

rv_picks = [b["bookId"] for b, w in s1 if w == "想法最多"]
assert rv_picks, "想法分类没拿到名额"
print(f"  → 想法最多: {rv_picks}")

# 场景 2：倒挂两组都要凑齐
books2 = []
for i in range(14):
    # 前 7 本：我评高 / 社区低；后 7 本：我评低 / 社区高
    if i < 7:
        books2.append(mk(i, 6.0 + i * 0.1, 5.0, 10, 10, "X"))
    else:
        books2.append(mk(i, 9.0, 1.0, 10, 10, "Y"))
s2 = show("倒挂：我评高/社区低 vs 我评低/社区高", books2)

hi = [b["bookId"] for b, w in s2 if w == "我的评价高于社区评分"]
lo = [b["bookId"] for b, w in s2 if w == "我的评价低于社区评分"]
assert len(hi) == 2, f"我评高组应 2 本，实际 {hi}"
assert len(lo) == 2, f"我评低组应 2 本，实际 {lo}"
print(f"  → 我评高组 {hi}（应来自 b0-b6）, 我评低组 {lo}（应来自 b7-b13）")
assert all(b.startswith(("b0", "b1", "b2", "b3", "b4", "b5", "b6")) for b in hi)
assert all(int(b[1:]) >= 7 for b in lo)

# 场景 3：总量上限
assert len(s1) <= 16 and len(s2) <= 16, "样本超出上限"

print("\n全部断言通过")
