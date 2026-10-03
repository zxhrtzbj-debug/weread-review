"""阅读数据 → LLM 报告。

三个对外函数：
    build_reading_profile(data, search_digest)  把结构化数据压成 prompt 文本
    estimate_tokens(text)                       粗估 token 消耗（用于前端省 token 提示）
    generate_report(data, llm_config, style, search_config)  端到端生成报告
"""

from __future__ import annotations

import json
import re

from services.llm import LLMConfig, LLMMessage, create_llm_client
from services.search import SearchConfig, enrich_books

REPORT_SCHEMA = """{
  "overall_score": <0-10>,
  "summary": "<2-3段总评>",
  "dimensions": [
    {
      "name": "<维度名>",
      "score": <0-10>,
      "comment": "<评语>",
      "evidence": "<具体例证>"
    }
  ],
  "strengths": ["<优点1>", "<优点2>"],
  "weaknesses": ["<不足1>", "<不足2>"],
  "book_picks": {
    "top": [{"title": "<书名>", "reason": "<推荐理由>"}],
    "flop": [{"title": "<书名>", "reason": "<吐槽理由>"}]
  },
  "recommendations": ["<建议1>", "<建议2>"],
  "one_liner": "<一句话锐评>"
}"""

_DIMENSION_GUIDE = """评价维度说明：
- 阅读广度：涉猎分类和作者的多样性
- 阅读深度：选择的书籍难度、长度、评分偏好
- 思考质量：划线笔记和想法的独立见解程度
- 阅读活跃度：阅读频率和持续性
- 品味独特性：选书口味的小众程度"""

# 空字段说明：这一条是防 LLM 编造的关键。
# 用户可能主动关掉了某类内容（省 token），此时数据里根本没有那一节；
# 不说清楚的话，模型会把"没给"读成"用户没有"，然后对着空气长篇分析。
_DATA_NOTES = """数据口径说明（务必遵守）：
- 只会出现本次纳入的内容类型。某一节缺失 = 该类型本次未纳入，
  不等于用户没有这类内容，更不得据此推断用户的阅读习惯。
- 「我的评价」是用户自己给书的打分（0-5 星），「社区评分」是微信读书全站均分（0-10）。
  两者不一致是正常的，恰恰是分析品味差异的切入点。
- 没有评分标记为「未评价」，不要写成"打了 0 分"。
- 本地上传文件（PDF/扫描件等）单独列出，书名可能就是文件名，不要按书名臆测其内容。"""

SERIOUS_SYSTEM = """你是专业的阅读分析顾问，擅长根据用户的微信读书数据，分析其阅读习惯和品味。请基于提供的数据，输出客观、有洞察力的分析报告。

请严格按照以下 JSON 格式输出（只输出 JSON，不要 markdown 代码块标记）：

{report_schema}

{dimension_guide}

{data_notes}"""

SARCASTIC_SYSTEM = """你是一名嘴毒心善的读书吐槽大师，擅长用犀利幽默的方式点评别人的微信读书数据。风格参考：半佛仙人、罗永浩、毒舌电影。

请注意：吐槽要犀利但留有余地，毒舌中带着关爱，让被点评的人笑着接受。

请严格按照以下 JSON 格式输出（只输出 JSON，不要 markdown 代码块标记）：

{report_schema}

{dimension_guide}

{data_notes}"""


# ── Token 估算 ─────────────────────────────────────────

_CJK_RE = re.compile(r"[　-〿㐀-䶿一-鿿豈-﫿＀-￯]")


def estimate_tokens(text: str) -> int:
    """粗估 token 数。

    口径：CJK 字符按 1 token/字（cl100k/o200k 对中文大致 0.6–1.2 token/字，
    取上界偏保守），非 CJK 按 4 字符/token。整体不确定度约 ±30%，
    用途是"比较不同筛选方案的量级"，不是计费依据。
    """
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    other = len(text) - cjk
    return int(cjk * 1.0 + other / 4)


# ── Prompt 组装 ────────────────────────────────────────

def _star_str(rating: float | None) -> str:
    """0-5 星 → ★★★☆☆ 形式的文本。None → 未评价。"""
    if rating is None:
        return "未评价"
    full = int(rating)
    half = "½" if (rating - full) >= 0.5 else ""
    return "★" * full + half + "☆" * max(0, 5 - full - (1 if half else 0))


def build_reading_profile(data: dict, search_digest: str = "") -> str:
    """将提取的微信读书数据压缩为 LLM 友好的概要文本。

    三条硬规则（都源于实测的 LLM 跑偏）：
      1. 空的一律不写。某类内容被用户关掉后，每本书都是 0 条；
         把"想法: 0 条"写满全篇，模型会开始分析"为什么有书评却没想法"。
         改为顶部声明一次口径，正文里干脆不出现这一项。
      2. 书单只列上架书籍。本地上传文件单独一节，且前面放用户自己写的介绍。
      3. 摘录小节内容为空就整节省略，不留空标题。
    """
    stats = data.get("stats", {})
    books = data.get("books", [])

    # 本次实际纳入了哪些内容类型（按"是否有任何一本书留下了该类内容"判定）
    has_bm = any(b.get("bookmarks") for b in books)
    has_rv = any(b.get("reviews") for b in books)
    has_br = any(b.get("bookReviews") for b in books)

    lines = ["=== 阅读概况 ==="]
    lines.append(f"有笔记的书籍总数: {stats.get('totalBooks', 0)}")
    # 未纳入的类型连总数都不写：给了总数却一条内容都没有，
    # 模型会开始推断"这 29 条想法去哪了"。
    if has_bm:
        lines.append(f"划线/高亮总数: {stats.get('totalBookmarks', 0)}")
    if has_rv:
        lines.append(f"想法/笔记总数: {stats.get('totalReviews', 0)}")
    if has_br:
        lines.append(f"书评总数: {stats.get('totalBookReviews', 0)}")
    rated = stats.get("ratedBooks", 0)
    if rated:
        lines.append(
            f"我打过分的书: {rated} 本，平均 {stats.get('avgMyRating', 0)} / 5 星"
        )
    else:
        lines.append("我打过分的书: 0 本（本次数据中没有任何自评分数）")

    local_total = stats.get("localFiles", 0)
    local_picked = stats.get("localPicked", 0)
    if local_total:
        tail = f"，其中 {local_picked} 本由用户手动选入（见文末）" if local_picked else "，均未选入书单"
        lines.append(f"本地上传文件: 共 {local_total} 本{tail}")

    lines.append("\n=== 本次纳入的内容类型 ===")
    lines.append(f"  划线: {'纳入' if has_bm else '未纳入'}")
    lines.append(f"  想法/笔记: {'纳入' if has_rv else '未纳入'}")
    lines.append(f"  书评: {'纳入' if has_br else '未纳入'}")
    lines.append("  未纳入的类型表示本次未提供该类内容，不代表用户没有，不要据此推断。")

    top_cats = stats.get("topCategories", [])
    if top_cats:
        lines.append("\n=== 分类分布 (Top 10) ===")
        for name, count in top_cats:
            lines.append(f"  {name}: {count}本")

    top_authors = stats.get("topAuthors", [])
    if top_authors:
        lines.append("\n=== 作者频率 (Top 10) ===")
        for name, count in top_authors:
            lines.append(f"  {name}: {count}本")

    # ── 书单：只列上架书籍 ──
    shelf_books = [b for b in books if b.get("source") != "local"]
    local_books = [b for b in books if b.get("source") == "local"]

    lines.append("\n=== 全部书籍列表 ===")
    if not shelf_books:
        lines.append("  （本次没有上架书籍）")
    for b in shelf_books:
        lines.append(f"  {_book_list_line(b)}")

    if search_digest:
        lines.append("\n=== 冷门书籍联网补检资料 ===")
        lines.append(search_digest)

    # ── 详细样本 ──
    sampled = _sample_books(books)
    sampled_shelf = [(b, why) for b, why in sampled if b.get("source") != "local"]
    sampled_local = [(b, why) for b, why in sampled if b.get("source") == "local"]

    if sampled_shelf:
        lines.append(f"\n=== 详细样本 (精选 {len(sampled_shelf)} 本) ===")
        for b, why in sampled_shelf:
            lines.append(_render_book_detail(b, why, has_bm, has_rv, has_br))

    # 本地文件单独一节：用户既然手动选了，就一定想让它被评价，
    # 而且它们是"用户自己挑出来的"，本身就是强信号。
    if local_books:
        lines.append(f"\n=== 用户自选的本地上传文件 ({len(local_books)} 本) ===")
        lines.append("  这些是用户自己上传的文件，书名可能就是文件名；"
                     "用户为每本写了分类与说明，说明即用户本人的态度。")
        for b in local_books:
            why = next((w for bb, w in sampled_local if bb is b), "")
            lines.append(_render_book_detail(b, why, has_bm, has_rv, has_br))

    return "\n".join(lines)


def _book_list_line(b: dict) -> str:
    """书单信息行：社区评分 + 我的评价 + 书名/作者/分类。"""
    rating = b.get("rating", 0) or 0
    my = b.get("myRating")
    rate_txt = f"{rating:.1f}" if rating else "暂无"
    return (
        f"[社区 {rate_txt}｜我的评价 {_star_str(my)}] "
        f"《{b.get('title', '')}》 - {b.get('author') or '?'}"
        f"  [{b.get('category') or '?'}]"
    )


def _render_book_detail(
    b: dict, why: str, has_bm: bool, has_rv: bool, has_br: bool
) -> str:
    """单本书的详情块。空的小节整块省略，不留空标题。"""
    out = [f"\n--- 《{b.get('title', '')}》 ---"]
    if why:
        out.append(f"  入选理由: {why}")
    if b.get("userSlot"):
        out.append(f"  用户分类: {b['userSlot']}（本地上传文件）")
    if b.get("userNote"):
        out.append(f"  用户对这本书的介绍与感悟: {b['userNote']}")

    author = b.get("author") or "?"
    category = b.get("category") or "?"
    out.append(f"  作者: {author} ｜ 分类: {category}")

    rating = b.get("rating", 0) or 0
    my = b.get("myRating")
    community_txt = f"{rating:.1f} / 10" if rating else "暂无"
    score_line = f"  社区评分: {community_txt} ｜ 我的评价: {_star_str(my)}"
    if my is not None and rating:
        gap = my * 2 - rating  # 都折算到 0-10 再比
        score_line += f"（{'高于' if gap > 0 else '低于'}社区评分 {abs(gap):.1f}）"
    out.append(score_line)

    intro = (b.get("intro") or "").strip()
    if intro:
        out.append(f"  简介: {intro[:120]}")

    # 只有非零的才写。写满"想法 0 条 ｜ 书评 0 条"会让模型去分析
    # "为什么这本书书评多却没划线"，而真相只是用户没留下这类内容。
    counts = []
    if has_bm and b.get("totalBookmarks"):
        counts.append(f"划线 {b['totalBookmarks']} 条")
    if has_rv and b.get("totalReviews"):
        counts.append(f"想法 {b['totalReviews']} 条")
    if has_br and b.get("totalBookReviews"):
        counts.append(f"书评 {b['totalBookReviews']} 条")
    if counts:
        out.append("  " + " ｜ ".join(counts))

    if has_bm and b.get("bookmarks"):
        out.append("  划线摘录 (最多5条):")
        for m in b["bookmarks"][:5]:
            text = (m.get("markText") or "").strip()
            if not text:
                continue
            ch = m.get("chapterTitle", "")
            out.append(f"    [{ch}] 「{text[:80]}」")
            extra = (m.get("reviewContent") or "").strip()
            if extra:
                out.append(f"      ↳ 想法: {extra[:80]}")

    if has_rv and b.get("reviews"):
        out.append("  想法摘录 (最多3条):")
        for r in b["reviews"][:3]:
            content = (r.get("content") or "").strip()
            if content:
                out.append(f"    「{content[:100]}」")

    if has_br and b.get("bookReviews"):
        # 带评分的那条在前：它同时说明"打了多少分"和"为什么"
        brs = [r for r in b["bookReviews"] if (r.get("content") or "").strip()]
        brs = sorted(
            brs, key=lambda r: (r.get("star") is None, -(r.get("star") or 0))
        )[:2]
        if brs:
            out.append("  书评摘录:")
            for r in brs:
                star = r.get("star")
                tag = f"[{_star_str(star)}] " if star is not None else "[未评分] "
                out.append(f"    {tag}「{r['content'].strip()[:150]}」")

    return "\n".join(out)


# ── 抽样 ───────────────────────────────────────────────

# 每个分类要几本、以及总量上限。总量上限是省 token 的闸门：
# 分类越多越全，但样本从 8 本涨到 30 本会把省下的 token 全吃回去。
_SAMPLE_PER_BUCKET = 2
_SAMPLE_LIMIT = 16


def _rating_gap(b: dict) -> float | None:
    """我的评价与社区评分的差值，都折算到 0-10 再比。

    两者都有值才算得出"倒挂"；任何一个缺失都返回 None（不参与倒挂分组）。
    """
    my = b.get("myRating")
    community = b.get("rating", 0) or 0
    if my is None or not community:
        return None
    return my * 2 - community


def _sample_books(books: list) -> list[tuple[dict, str]]:
    """从书籍列表中采样最具代表性的样本，返回 [(书, 入选理由)]。

    关键改动是**顺延**：以前用 setdefault 占位，一旦"评分最高的两本"恰好
    也是"划线最多的两本"，后面那个分类就白拿一次名额，实际样本数少于预期。
    现在每个分类在各自排序里跳过已被占用的书继续往后取 —— 评分最高两本被
    占用了，就取第 3、第 4 高。

    新增的两组"倒挂"样本专门用来暴露品味差异：
      我评高/社区低 —— 用户偏爱但大众不买账
      我评低/社区高 —— 大众追捧但用户不以为然
    这两组的划线与想法态度往往最能说明问题。
    """
    valid = [b for b in books if b.get("title")]
    if len(valid) <= 10:
        return [(b, "") for b in valid]

    taken: dict[str, str] = {}
    out: list[tuple[dict, str]] = []

    def take(sorted_list, n: int, reason: str) -> None:
        added = 0
        for b in sorted_list:
            if added >= n:
                break
            bid = b.get("bookId")
            if bid in taken:
                continue  # 已被前面的分类占用 → 顺延到下一本
            taken[bid] = reason
            out.append((b, reason))
            added += 1
            if len(out) >= _SAMPLE_LIMIT:
                return

    # 用户手动选入的本地文件必须进样本：他既然挑了，就是想让它们被评价
    for b in valid:
        if b.get("userSlot") and b.get("bookId") not in taken:
            taken[b["bookId"]] = "用户手动选入的本地文件"
            out.append((b, "用户手动选入的本地文件"))

    def by_rating(desc: bool):
        return sorted(valid, key=lambda b: (b.get("rating", 0) or 0), reverse=desc)

    def by_my_rating(desc: bool):
        rated = [b for b in valid if b.get("myRating") is not None]
        return sorted(rated, key=lambda b: b.get("myRating") or 0, reverse=desc)

    take(by_rating(True), _SAMPLE_PER_BUCKET, "社区评分最高")
    take(by_rating(False), _SAMPLE_PER_BUCKET, "社区评分最低")
    take(by_my_rating(True), _SAMPLE_PER_BUCKET, "我的评价最高")
    take(by_my_rating(False), _SAMPLE_PER_BUCKET, "我的评价最低")

    # 倒挂两组：我评高/社区低、我评低/社区高
    gapped = [(b, _rating_gap(b)) for b in valid]
    gapped = [(b, g) for b, g in gapped if g is not None]
    take(
        [b for b, _ in sorted(gapped, key=lambda x: -x[1])],
        _SAMPLE_PER_BUCKET, "我的评价高于社区评分",
    )
    take(
        [b for b, _ in sorted(gapped, key=lambda x: x[1])],
        _SAMPLE_PER_BUCKET, "我的评价低于社区评分",
    )

    take(
        sorted(valid, key=lambda b: b.get("totalBookmarks", 0) or 0, reverse=True),
        _SAMPLE_PER_BUCKET, "划线最多",
    )
    take(
        sorted(valid, key=lambda b: b.get("totalReviews", 0) or 0, reverse=True),
        _SAMPLE_PER_BUCKET, "想法最多",
    )

    # 覆盖不同分类（每类 1 本）
    seen_cats: set[str] = set()
    for b in valid:
        if len(out) >= _SAMPLE_LIMIT:
            break
        cat = b.get("category", "")
        if cat and cat not in seen_cats and b.get("bookId") not in taken:
            seen_cats.add(cat)
            taken[b["bookId"]] = "分类覆盖"
            out.append((b, "分类覆盖"))

    return out


# ── 报告生成 ───────────────────────────────────────────

async def generate_report(
    data: dict,
    llm_config: LLMConfig,
    style: str = "serious",
    search_config: SearchConfig | None = None,
    progress=None,
) -> dict:
    """生成阅读评价报告。search_config.enabled 为真时先做联网补检。"""
    client = create_llm_client(llm_config)

    digest = ""
    hits: list[dict] = []
    if search_config and search_config.enabled:
        # 五道工序全在本地完成，不调用 LLM
        digest, hits = await enrich_books(
            data.get("books", []), search_config, progress=progress
        )

    profile = build_reading_profile(data, digest)

    template = SARCASTIC_SYSTEM if style == "sarcastic" else SERIOUS_SYSTEM
    system = template.format(
        report_schema=REPORT_SCHEMA,
        dimension_guide=_DIMENSION_GUIDE,
        data_notes=_DATA_NOTES,
    )

    if progress:
        await progress(
            f"正在调用 AI 分析（约 {estimate_tokens(profile)} tokens）..."
        )

    raw = await client.chat(
        system=system,
        messages=[LLMMessage(
            role="user",
            content=f"请分析以下微信读书用户的阅读数据：\n\n{profile}",
        )],
    )

    report = _parse_report(raw)
    report["_style"] = style
    if hits:
        report["_search_hits"] = hits
    return report


def _parse_report(raw: str) -> dict:
    """从 LLM 输出中提取 JSON。"""
    raw = raw.strip()
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines)

    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        raw = raw[start:end + 1]

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"error": "parse_failed", "raw": raw[:500]}
