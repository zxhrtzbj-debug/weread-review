import json
from llm import LLMClient, LLMMessage, create_llm_client, LLMConfig

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

SERIOUS_SYSTEM = """你是专业的阅读分析顾问，擅长根据用户的微信读书数据，分析其阅读习惯和品味。请基于提供的数据，输出客观、有洞察力的分析报告。

请严格按照以下 JSON 格式输出（只输出 JSON，不要 markdown 代码块标记）：

{report_schema}

评价维度说明：
- 阅读广度：涉猎分类和作者的多样性
- 阅读深度：选择的书籍难度、长度、评分偏好
- 思考质量：划线笔记和想法的独立见解程度
- 阅读活跃度：阅读频率和持续性
- 品味独特性：选书口味的小众程度"""

SARCASTIC_SYSTEM = """你是一名嘴毒心善的读书吐槽大师，擅长用犀利幽默的方式点评别人的微信读书数据。风格参考：半佛仙人、罗永浩、毒舌电影。

请注意：吐槽要犀利但留有余地，毒舌中带着关爱，让被点评的人笑着接受。

请严格按照以下 JSON 格式输出（只输出 JSON，不要 markdown 代码块标记）：

{report_schema}

评价维度说明：
- 阅读广度：涉猎分类和作者的多样性
- 阅读深度：选择的书籍难度、长度、评分偏好
- 思考质量：划线笔记和想法的独立见解程度
- 阅读活跃度：阅读频率和持续性
- 品味独特性：选书口味的小众程度"""


def build_reading_profile(data: dict) -> str:
    """将提取的微信读书数据压缩为 LLM 友好的概要文本。"""
    stats = data.get("stats", {})
    books = data.get("books", [])

    lines = []
    lines.append(f"=== 阅读概况 ===")
    lines.append(f"有笔记的书籍总数: {stats.get('totalBooks', 0)}")
    lines.append(f"划线/高亮总数: {stats.get('totalBookmarks', 0)}")
    lines.append(f"想法/笔记总数: {stats.get('totalReviews', 0)}")
    lines.append(f"书评总数: {stats.get('totalBookReviews', 0)}")

    top_cats = stats.get("topCategories", [])
    if top_cats:
        lines.append(f"\n=== 分类分布 (Top 10) ===")
        for name, count in top_cats:
            lines.append(f"  {name}: {count}本")

    top_authors = stats.get("topAuthors", [])
    if top_authors:
        lines.append(f"\n=== 作者频率 (Top 10) ===")
        for name, count in top_authors:
            lines.append(f"  {name}: {count}本")

    lines.append(f"\n=== 全部书籍列表 ===")
    for b in books:
        lines.append(f"  [{b.get('rating', 0):.1f}] 《{b['title']}》 - {b.get('author', '?')}  [{b.get('category', '?')}]")

    sampled = _sample_books(books)
    if sampled:
        lines.append(f"\n=== 详细样本 (精选 {len(sampled)} 本) ===")
        for b in sampled:
            lines.append(f"\n--- 《{b['title']}》 ---")
            lines.append(f"  作者: {b.get('author', '?')}")
            lines.append(f"  分类: {b.get('category', '?')}")
            lines.append(f"  评分: {b.get('rating', 0)}")
            lines.append(f"  简介: {b.get('intro', '无')[:120]}")
            lines.append(f"  划线数: {b.get('totalBookmarks', 0)}")
            lines.append(f"  想法数: {b.get('totalReviews', 0)}")

            bms = b.get("bookmarks", [])
            if bms:
                lines.append(f"  划线摘录 (最多5条):")
                for m in bms[:5]:
                    text = m.get("markText", "")
                    chapter = m.get("chapterTitle", "")
                    if text:
                        lines.append(f"    [{chapter}] 「{text[:80]}」")

            rvs = b.get("reviews", [])
            if rvs:
                lines.append(f"  想法摘录 (最多3条):")
                for r in rvs[:3]:
                    content = r.get("content", "")
                    if content:
                        lines.append(f"    「{content[:100]}」")

    return "\n".join(lines)


def _sample_books(books: list) -> list:
    """从书籍列表中采样最具代表性的样本。"""
    if not books:
        return []

    valid = [b for b in books if b.get("title")]
    if len(valid) <= 10:
        return valid

    sampled = {}
    # 评分最高/最低各 2 本
    sorted_by_rating = sorted(valid, key=lambda b: b.get("rating", 0) or 0)
    for b in sorted_by_rating[:2]:
        sampled[b["bookId"]] = b
    for b in sorted_by_rating[-2:]:
        sampled[b["bookId"]] = b

    # 划线最多 2 本
    sorted_by_bm = sorted(valid, key=lambda b: b.get("totalBookmarks", 0) or 0, reverse=True)
    for b in sorted_by_bm[:2]:
        sampled[b["bookId"]] = b

    # 想法最多 2 本
    sorted_by_rv = sorted(valid, key=lambda b: b.get("totalReviews", 0) or 0, reverse=True)
    for b in sorted_by_rv[:2]:
        sampled[b["bookId"]] = b

    # 覆盖不同分类
    seen_cats = set()
    for b in valid:
        cat = b.get("category", "")
        if cat and cat not in seen_cats and b["bookId"] not in sampled:
            sampled[b["bookId"]] = b
            seen_cats.add(cat)

    return list(sampled.values())


async def generate_report(
    data: dict,
    llm_config: LLMConfig,
    style: str = "serious",
) -> dict:
    """生成阅读评价报告。"""
    profile = build_reading_profile(data)

    if style == "sarcastic":
        system = SARCASTIC_SYSTEM.format(report_schema=REPORT_SCHEMA)
    else:
        system = SERIOUS_SYSTEM.format(report_schema=REPORT_SCHEMA)

    user_msg = LLMMessage(role="user", content=f"请分析以下微信读书用户的阅读数据：\n\n{profile}")

    client = create_llm_client(llm_config)
    raw = await client.chat(system=system, messages=[user_msg])

    report = _parse_report(raw)
    report["_style"] = style
    return report


def _parse_report(raw: str) -> dict:
    """从 LLM 输出中提取 JSON。"""
    raw = raw.strip()
    # 移除 ```json ... ``` 包裹
    if raw.startswith("```"):
        lines = raw.splitlines()
        if lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines)

    # 找到第一个 { 和最后一个 }
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        raw = raw[start : end + 1]

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"error": "parse_failed", "raw": raw[:500]}
