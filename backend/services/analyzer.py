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

SERIOUS_SYSTEM = """你是专业的阅读分析顾问，擅长根据用户的微信读书数据，分析其阅读习惯和品味。请基于提供的数据，输出客观、有洞察力的分析报告。

请严格按照以下 JSON 格式输出（只输出 JSON，不要 markdown 代码块标记）：

{report_schema}

{dimension_guide}"""

SARCASTIC_SYSTEM = """你是一名嘴毒心善的读书吐槽大师，擅长用犀利幽默的方式点评别人的微信读书数据。风格参考：半佛仙人、罗永浩、毒舌电影。

请注意：吐槽要犀利但留有余地，毒舌中带着关爱，让被点评的人笑着接受。

请严格按照以下 JSON 格式输出（只输出 JSON，不要 markdown 代码块标记）：

{report_schema}

{dimension_guide}"""


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

def build_reading_profile(data: dict, search_digest: str = "") -> str:
    """将提取的微信读书数据压缩为 LLM 友好的概要文本。"""
    stats = data.get("stats", {})
    books = data.get("books", [])

    lines = ["=== 阅读概况 ==="]
    lines.append(f"有笔记的书籍总数: {stats.get('totalBooks', 0)}")
    lines.append(f"划线/高亮总数: {stats.get('totalBookmarks', 0)}")
    lines.append(f"想法/笔记总数: {stats.get('totalReviews', 0)}")
    lines.append(f"书评总数: {stats.get('totalBookReviews', 0)}")

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

    lines.append("\n=== 全部书籍列表 ===")
    for b in books:
        lines.append(
            f"  [{b.get('rating', 0):.1f}] 《{b['title']}》 - {b.get('author', '?')}"
            f"  [{b.get('category', '?')}]"
        )

    if search_digest:
        lines.append("\n=== 冷门书籍联网补检资料 ===")
        lines.append(search_digest)

    sampled = _sample_books(books)
    if sampled:
        lines.append(f"\n=== 详细样本 (精选 {len(sampled)} 本) ===")
        for b in sampled:
            lines.append(f"\n--- 《{b['title']}》 ---")
            lines.append(f"  作者: {b.get('author', '?')}")
            lines.append(f"  分类: {b.get('category', '?')}")
            lines.append(f"  评分: {b.get('rating', 0)}")
            lines.append(f"  简介: {b.get('intro', '无')[:120]}")
            lines.append(
                f"  划线数: {b.get('totalBookmarks', 0)}"
                f"  想法数: {b.get('totalReviews', 0)}"
                f"  书评数: {b.get('totalBookReviews', 0)}"
            )

            bms = b.get("bookmarks", [])
            if bms:
                lines.append("  划线摘录 (最多5条):")
                for m in bms[:5]:
                    text = m.get("markText", "")
                    if text:
                        ch = m.get("chapterTitle", "")
                        extra = m.get("reviewContent", "")
                        lines.append(f"    [{ch}] 「{text[:80]}」")
                        if extra:
                            lines.append(f"      ↳ 想法: {extra[:80]}")

            rvs = b.get("reviews", [])
            if rvs:
                lines.append("  想法摘录 (最多3条):")
                for r in rvs[:3]:
                    content = r.get("content", "")
                    if content:
                        lines.append(f"    「{content[:100]}」")

            brs = b.get("bookReviews", [])
            if brs:
                lines.append("  书评摘录 (最多2条):")
                for r in brs[:2]:
                    content = r.get("content", "")
                    if content:
                        lines.append(f"    「{content[:150]}」")

    return "\n".join(lines)


def _sample_books(books: list) -> list:
    """从书籍列表中采样最具代表性的样本。"""
    valid = [b for b in books if b.get("title")]
    if len(valid) <= 10:
        return valid

    sampled: dict[str, dict] = {}

    def _pick(sorted_list, n):
        for b in sorted_list[:n]:
            sampled.setdefault(b["bookId"], b)

    _pick(sorted(valid, key=lambda b: b.get("rating", 0) or 0), 2)          # 最低分
    _pick(sorted(valid, key=lambda b: b.get("rating", 0) or 0, reverse=True), 2)  # 最高分
    _pick(sorted(valid, key=lambda b: b.get("totalBookmarks", 0) or 0, reverse=True), 2)
    _pick(sorted(valid, key=lambda b: b.get("totalReviews", 0) or 0, reverse=True), 2)

    # 覆盖不同分类
    seen_cats = set()
    for b in valid:
        cat = b.get("category", "")
        if cat and cat not in seen_cats and b["bookId"] not in sampled:
            sampled[b["bookId"]] = b
            seen_cats.add(cat)

    return list(sampled.values())


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
        report_schema=REPORT_SCHEMA, dimension_guide=_DIMENSION_GUIDE
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
