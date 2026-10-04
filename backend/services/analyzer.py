"""阅读数据 → LLM 报告。

两阶段流水线（本文件对外暴露的东西）：
    build_traces_view(data)                  阶段一的输入：用户自己留下的文字
    build_reading_profile(data, ...)         阶段二的输入：书目结构 + 阶段一画像
    estimate_prompt_sizes(data)              两阶段各自的 token 粗估
    generate_report(data, llm_config, ...)   端到端：阶段一 → 阶段二 → 报告

为什么要分两轮
──────────────
一轮到顶时，模型会把注意力交给最长的那一节（书单），而不是信息密度最高的那一节
（用户自己写的划线、想法、书评）。实测结果是：报告通篇在清点"你读了多少本、
集中在哪一类、建议平衡一下其他领域"，几乎不提用户到底在想什么。

所以先把痕迹单独喂一轮，得到"写下这些文字的人是什么样的一个人"（知识画像），
再让第二轮拿着画像去和书目结构交叉验证。画像同时回填进最终报告，
读者能直接看到"AI 是从我的哪些痕迹里读出这些的"。

为什么背景陈述写得这么长
────────────────────────
早先试过用标签压缩（"未纳入≠没有""抽样≠全量"这种短句）。事实证明多数模型
读不懂标签，会拿书单的本数除以样本的划线数，得出"深读沉淀不足"这类结论。
所以改为完整的数据说明书：每一节覆盖多少书、抽样规则是什么、条数与摘录
条数分别是什么口径。

陈述一律用肯定式。写"不要做 X"只会让模型把 X 当成任务清单里的一个条目——
实测过"不要建议平衡其他领域"，模型转头就把这句话做成了报告小标题。
正确做法是把事实讲清楚（清单是配额抽样、真实分布看全量统计那一节），
模型自己就会用对的那份数据。
"""

from __future__ import annotations

import json
import re

from services.llm import LLMConfig, LLMMessage, create_llm_client
from services.search import SearchConfig, enrich_books


# ── 抽样与预算参数 ──────────────────────────────────────
#
# 书单配额：参考分类（书最少的那个分类）全保留，其余分类最多取同样多本。
# 下限 5 是为了避免"参考分类只有 1~2 本"时把书单压得几乎没有信息量。
_LIST_MIN_QUOTA = 5

# 阶段一痕迹语料：痕迹最多的若干本书，且总量受字符预算封顶。
_TRACE_BOOK_CAP = 14
_TRACE_CHAR_BUDGET = 18000
_TRACE_BM_PER_BOOK = 15
_TRACE_BM_CHARS = 180
_TRACE_RV_PER_BOOK = 6
_TRACE_RV_CHARS = 250
_TRACE_BR_PER_BOOK = 2
_TRACE_BR_CHARS = 300

# 阶段二详细样本里，每本书展示的摘录上限（与阶段一不同，这里刻意少给，
# 因为痕迹已经在阶段一读过了，第二轮的重点是书目 × 痕迹的交叉验证）。
_SAMPLE_BM_PER_BOOK = 5
_SAMPLE_BM_CHARS = 80
_SAMPLE_RV_PER_BOOK = 3
_SAMPLE_RV_CHARS = 100
_SAMPLE_BR_PER_BOOK = 2
_SAMPLE_BR_CHARS = 150

# 每个分类要几本、以及样本总量上限。总量上限是省 token 的闸门。
_SAMPLE_PER_BUCKET = 2
_SAMPLE_LIMIT = 16


# ── 报告 Schema ────────────────────────────────────────

REPORT_SCHEMA = """{
  "overall_score": <0-10>,
  "summary": "<2-3段总评>",
  "knowledge_profile": {
    "headline": "<一句话：这是一个什么样的读者>",
    "axes": [{"name": "<维度名>", "line": "<一句话结论>"}],
    "signature_quotes": [{"book": "<书名>", "text": "<用户留下的原文>", "note": "<为什么这句有代表性>"}],
    "blind_spots": ["<痕迹里看不到的角度或证据缺口>"]
  },
  "profile_note": "<第二阶段对知识画像的补充或修正，2-3句；没有补充就写空字符串>",
  "dimensions": [
    {
      "name": "<维度名>",
      "score": <0-10>,
      "comment": "<评语>",
      "evidence": "<具体例证，尽量指到书名与一句原文>"
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

# 阶段一：知识画像。轴是固定的六个，避免模型每次换一套维度、无法比较。
KNOWLEDGE_AXES = [
    "关注领域",
    "思维方式",
    "价值取向",
    "情绪与动机",
    "摘录与表达习惯",
    "知识迁移",
]

KNOWLEDGE_SCHEMA = """{
  "headline": "<一句话：写下这些文字的人是什么样的一个读者（20-40字）>",
  "trace_overview": "<客观描述本次看到的痕迹规模与分布：多少条划线/想法/书评，集中在哪几本书，给出具体数字>",
  "axes": [
    {
      "name": "<维度名，固定为：关注领域 / 思维方式 / 价值取向 / 情绪与动机 / 摘录与表达习惯 / 知识迁移>",
      "reading": "<3-5句：只基于痕迹的具体判断>",
      "signals": ["<可直接观察到的信号1>", "<信号2>"],
      "evidence": [{"book": "<书名>", "text": "<引用的原文片段，30-60字>"}]
    }
  ],
  "themes": [{"theme": "<反复出现的主题>", "books": ["<书名>"], "note": "<说明>"}],
  "signature_quotes": [{"book": "<书名>", "text": "<最能代表这个人的一句原文>", "note": "<为什么这句有代表性>"}],
  "blind_spots": ["<痕迹里看不到的角度，或本次数据的证据缺口>"]
}"""


_DIMENSION_GUIDE = """评价维度说明（每一项都要落在具体的书与具体的痕迹上）：
- 阅读广度：读过的书彼此之间跨度有多大——题材、立场、年代、作者背景
- 阅读深度：在一本书上停留的密度，以及他追问到的层次
- 思考质量：想法与批注里，他自己的判断占多少、依据是什么
- 阅读活跃度：痕迹在时间上铺开的密度与持续性
- 品味独特性：他的选择与大众评分之间的落差，以及他自己说得出理由的部分"""


# ── 数据说明书（两阶段共用）────────────────────────────
#
# 这一段是防跑偏的核心。写成完整陈述而不是标签：实测多数模型读不懂
# "未纳入≠没有"这类压缩标注，会照旧拿书单本数除以样本划线数去推"沉淀不足"。
_SYSTEM_BRIEF = """════ 数据说明书（开始分析前，请先把这一节读完）════

你眼前的数据来自「微信读书 · AI 阅读评价」。用户在网页上授权登录自己的微信读书
账号，工具导出他的书架与阅读痕迹，再按下面的规则整理成若干小节。每一节覆盖
多少书、书下面的文字是怎么挑出来的，这一节都会说明白。

一、这份数据由哪几部分组成

· 「阅读概况」「分类分布」「作者频率」：本次纳入的全部书籍的统计数字，
  覆盖每一本被纳入的书。要谈数量、比例、集中度，用这三节。
· 「书籍清单」：书目清单，每行只有书目信息（分类、社区评分、我的评价），
  按第二节的规则抽样。
· 「详细样本」：少量书带正文摘录（划线原文、划线旁的批注、想法、书评），
  按第三节的规则挑出，每本都标了入选理由。
· 「第一阶段 · 知识画像」：同一份数据先跑过一轮痕迹分析得到的中间结论，
  你可以直接沿用。

二、「书籍清单」是配额抽样后的结果

为了减少长清单占用注意力，清单按平台分类做了配额抽样：
  · 书最少的那个分类（参考分类）全部保留；
  · 其余每个分类最多保留同样多的本数（且不低于 {list_min_quota} 本），
    优先取用户自己打过分的书，份额不足或超出时按
    评分最高 → 评分最低 → 评分次高 → 评分次低 从两端交替补齐或截取。
清单里各分类的本数由这条规则决定；用户真实的分类分布写在「分类分布」那一节，
那是全量统计，谈分类占比时以它为准。

三、「详细样本」是怎么挑出来的

样本按下列顺序各取若干本，同一本书被前面的组占用就顺延到下一本，总量上限
{sample_limit} 本：
  用户手动选入的本地上传文件 → 社区评分最高 / 最低 → 我的评价最高 / 最低 →
  我的评价高于社区评分 / 低于社区评分（这两组最能看出个人口味与大众口味的
  落差）→ 划线最多 → 想法最多 → 补上尚未覆盖的分类。
每本下面标注的「划线 N 条 / 以下摘录 M 条」里，N 是这本书在本次数据里的划线
总条数，M 是这一节实际印出的条数，受每本 {bm_cap} 条、每条 {bm_chars} 字的
展示上限截断。样本之外还有哪些书，看「书籍清单」。

四、划线 / 想法 / 书评是三种各自独立的内容

一本书只要留下其中任意一种就进入这份数据：它可能只因为打过分或写过书评而
出现在书单里，一条划线也没有。划线只是三种内容中的一种，用户导出时可以选择
只带其中几类（见「本次纳入的内容类型」），也可以在界面上删掉某本书、把自己
上传的本地文件加进来。每本书下面有什么就谈什么：这一节里有想法就谈想法，
有划线就谈划线。

五、你的任务

这份数据里信息密度是这样排的：
  1. 用户自己写下的文字：想法、书评、划线旁的批注
  2. 他选择留下的别人的句子：划线
  3. 他自己打的分
  4. 书的元数据与统计数字
你的任务是还原「这是一个什么样的读者」：他被什么击中、他自己怎么想、他的
判断落在什么样的依据上。每个判断都配上具体的书名和一句具体的原文，
让读者能顺着你的引用回到自己写下的那句话。"""


# ── 阶段一提示词 ───────────────────────────────────────

_TRACES_TASK = """════ 你这一轮的输入：用户自己留下的文字 ════

下面这一节是本次数据里用户写下的内容：划线（他选择留下的别人的句子）、
划线旁的批注、想法（他自己的句子）、书评（他对整本书的评价）。
这是整份数据里唯一能直接反映"这个人怎么想"的部分，请逐条认真读。

几点说明：
· 每本书开头写的「划线 N 条 / 以下摘录 M 条」里，N 是这本书留下的划线总数，
  M 是本次展示的条数，M 受展示上限截断。
· 划线是他被什么击中，想法是他自己怎么想。两者分开看，也放在一起看——
  想法常常就是某条划线的批注。
· 这里按痕迹多少排序列出前若干本书，是抽样。
· 这一轮只产出对这个人的理解。

本轮只回答一个问题：**写下这些文字的人，是什么样的一个人。**

输出纯 JSON 文本（不加 markdown 代码块标记）：

{knowledge_schema}

axes 固定为这六个维度，按下列顺序各输出一项：{axes}。
某一维度痕迹不足以下判断时，reading 写「痕迹不足」，并说明缺的是什么。
evidence 里的 text 必须是原文引用，与原文逐字一致。"""

TRACES_SYSTEM = """你是阅读痕迹分析师。你的任务是读一个人留下的文字，还原出这个人
的知识图像。判断必须能追到具体的一句原文。

{system_brief}

{traces_task}"""


# ── 阶段二提示词 ───────────────────────────────────────

_SECOND_PASS_TASK = """════ 这是第二轮，第一轮已经做过了什么 ════

本次分析分两轮。第一轮已经把用户的划线 / 想法 / 书评单独通读了一遍，
产出了一份「知识画像」，原文附在下面数据的「第一阶段 · 知识画像」小节里。

你这一轮要做的是：把画像和书目结构交叉验证——他选了哪些书、打了多少分、
与社区评分差多少、在哪些书上留下了痕迹、哪些书读完一个字没留——
得出这个人作为读者的整体评价。

· 画像里已有的结论直接沿用，你的增量在于"书目 × 痕迹"的交叉验证。
· 如果发现书目数据与画像矛盾，指出来，并说明哪一边证据更硬。
· 最终 JSON 的 knowledge_profile 字段请把画像压缩后带回：headline 保留原样，
  axes 每个维度压成一句话，signature_quotes 保留 3-5 条最有代表性的原文引用。"""

SERIOUS_SYSTEM = """你是专业的阅读分析顾问，擅长根据用户的微信读书数据，分析其阅读习惯和品味。请基于提供的数据，输出客观、有洞察力的分析报告。

{system_brief}

{second_pass}

请严格按照以下 JSON 格式输出纯 JSON 文本（不加 markdown 代码块标记）：

{report_schema}

{dimension_guide}"""

SARCASTIC_SYSTEM = """你是一名嘴毒心善的读书吐槽大师，擅长用犀利幽默的方式点评别人的微信读书数据。风格参考：半佛仙人、罗永浩、毒舌电影。

请注意：吐槽要犀利但留有余地，毒舌中带着关爱，让被点评的人笑着接受。
吐槽的对象是这个人的阅读行为本身；引用他写下的原话来抖包袱，被点评的人能一眼
认出那是自己写的句子。

{system_brief}

{second_pass}

请严格按照以下 JSON 格式输出纯 JSON 文本（不加 markdown 代码块标记）：

{report_schema}

{dimension_guide}"""


def _render_brief() -> str:
    """渲染数据说明书（把抽样参数填进去）。"""
    return _SYSTEM_BRIEF.format(
        list_min_quota=_LIST_MIN_QUOTA,
        sample_limit=_SAMPLE_LIMIT,
        bm_cap=_SAMPLE_BM_PER_BOOK,
        bm_chars=_SAMPLE_BM_CHARS,
    )


# ── Token 估算 ─────────────────────────────────────────

_CJK_RE = re.compile(r"[　-〿㐀-䶿一-鿿豈-﫿＀-￯]")


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


def _content_flags(books: list) -> tuple[bool, bool, bool]:
    """本次实际纳入了哪些内容类型（按"是否有任何一本书留下了该类内容"判定）。"""
    return (
        any(b.get("bookmarks") for b in books),
        any(b.get("reviews") for b in books),
        any(b.get("bookReviews") for b in books),
    )


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


# ── 书单配额抽样 ───────────────────────────────────────

def _my_rating_key(b: dict) -> tuple:
    """「有我的评分」这一池的排序键：我的评分主序，社区评分破并列。"""
    return (b.get("myRating") or 0, b.get("rating", 0) or 0)


def _community_key(b: dict) -> tuple:
    """「只有社区评分」这一池的排序键。"""
    return (b.get("rating", 0) or 0,)


def _pick_alternating(pool: list, key, need: int) -> list:
    """从两端交替取：最高、最低、次高、次低……

    pool 里不足 need 本时全部返回（没有的份额不硬凑）。
    """
    if need <= 0 or not pool:
        return []
    ordered = sorted(pool, key=key, reverse=True)
    if need >= len(ordered):
        return list(ordered)

    picked: list = []
    lo, hi = 0, len(ordered) - 1
    while len(picked) < need and lo <= hi:
        picked.append(ordered[lo])
        lo += 1
        if len(picked) < need and lo <= hi:
            picked.append(ordered[hi])
            hi -= 1
    return picked


def _quota_pick(books: list, quota: int) -> list:
    """一个分类内按配额取书。

    两个池子是分开的，各自用自己拥有的那种分数排序：
      · 有我的评分的书      → 用「我的评分」排（同分时社区评分破并列）
      · 没有我的评分的书    → 只有社区评分，就用「社区评分」排

    三种情形：
      · 有评价的书 数量 == 配额 → 直接取这些
      · 有评价的书 数量 <  配额 → 全部取走，差额从"没我的评分"的书里交替补齐
      · 有评价的书 数量 >  配额 → 在"有我的评分"的书里交替截取
    """
    rated = [b for b in books if b.get("myRating") is not None]
    plain = [b for b in books if b.get("myRating") is None]

    if not rated:
        return _pick_alternating(plain, _community_key, quota)
    if len(rated) == quota:
        return list(rated)
    if len(rated) > quota:
        return _pick_alternating(rated, _my_rating_key, quota)

    picked = list(rated)
    picked += _pick_alternating(plain, _community_key, quota - len(rated))
    return picked


def sample_shelf_list(books: list) -> tuple[list, dict]:
    """书单的分类配额抽样，返回 (抽出的书, 抽样元信息)。

    元信息会原样写进 prompt —— 让模型知道清单是配额抽样、知道每个分类
    最多几本，它才不会拿清单里各分类的本数去推阅读偏好。
    """
    if not books:
        return [], {"quota": 0, "ref_category": "", "ref_count": 0,
                    "categories": 0, "kept": 0, "total": 0}

    groups: dict[str, list] = {}
    for b in books:
        groups.setdefault(b.get("category") or "未分类", []).append(b)

    if len(groups) <= 1:
        return list(books), {
            "quota": len(books), "ref_category": next(iter(groups), ""),
            "ref_count": len(books), "categories": len(groups),
            "kept": len(books), "total": len(books),
        }

    ref_cat = min(groups, key=lambda c: len(groups[c]))
    quota = max(len(groups[ref_cat]), _LIST_MIN_QUOTA)

    kept: list = []
    for cat, items in groups.items():
        if cat == ref_cat:
            kept.extend(items)          # 参考分类：全部保留
        else:
            kept.extend(_quota_pick(items, quota))

    # 抽样后仍按原始顺序呈现，不按分类聚堆（聚堆会强化"分类"这个无关维度）
    order = {id(b): i for i, b in enumerate(books)}
    kept.sort(key=lambda b: order.get(id(b), 0))

    return kept, {
        "quota": quota,
        "ref_category": ref_cat,
        "ref_count": len(groups[ref_cat]),
        "categories": len(groups),
        "kept": len(kept),
        "total": len(books),
    }


def _render_list_note(info: dict) -> str:
    """书单抽样的口径声明（紧跟在清单标题后面）。"""
    if not info or not info.get("categories"):
        return ""
    if info["categories"] <= 1:
        return ""
    return (
        f"  抽样说明：本次共 {info['categories']} 个分类；"
        f"参考分类「{info['ref_category']}」{info['ref_count']} 本全部保留，"
        f"其余每个分类最多取 {info['quota']} 本"
        f"（优先取用户自己打过分的书，不足或超出时按 评分最高→最低→次高→次低 交替取）。\n"
        f"  清单共 {info['kept']} 本（本次纳入的书共 {info['total']} 本）。\n"
        f"  清单里各分类的本数由抽样规则决定；用户真实的分类分布见上方"
        f"「分类分布」（全量统计）。"
    )


# ── 阶段一：痕迹视图 ───────────────────────────────────

def _trace_weight(b: dict, has_bm: bool, has_rv: bool, has_br: bool) -> int:
    """一本书的痕迹量（只数本次纳入的类型）。"""
    n = 0
    if has_bm:
        n += len(b.get("bookmarks") or [])
    if has_rv:
        n += len(b.get("reviews") or [])
    if has_br:
        n += len(b.get("bookReviews") or [])
    return n


def _truncate(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def build_traces_view(data: dict) -> str:
    """把用户自己留下的文字铺开成一份语料，供阶段一阅读。

    与阶段二的「详细样本」区别：
      · 这里的摘录上限高得多（划线 15 条 / 每条 180 字 vs 5 条 / 80 字），
        因为这一轮的全部任务就是读这些文字；
      · 这里按痕迹量排序取前面的书，而不是取评分极端——极端评分是为了暴露
        品味差异，但那会带进一堆没留下几个字的书，稀释痕迹。
    """
    books = data.get("books", [])
    has_bm, has_rv, has_br = _content_flags(books)
    if not (has_bm or has_rv or has_br):
        return ""

    ranked = sorted(
        (b for b in books if b.get("title")),
        key=lambda b: _trace_weight(b, has_bm, has_rv, has_br),
        reverse=True,
    )

    stats = data.get("stats", {})
    totals = []
    if has_bm:
        totals.append(f"划线 {stats.get('totalBookmarks', 0)} 条")
    if has_rv:
        totals.append(f"想法 {stats.get('totalReviews', 0)} 条")
    if has_br:
        totals.append(f"书评 {stats.get('totalBookReviews', 0)} 条")

    lines = [
        "=== 用户留下的文字（划线 / 想法 / 书评）===",
        f"  本次全量统计：纳入的书 {stats.get('totalBooks', 0) or len(books)} 本，"
        + ("；".join(totals) or "本次没有勾选任何内容类型"),
        f"  下面按痕迹多少排序，最多列出 {_TRACE_BOOK_CAP} 本、"
        f"总字数上限 {_TRACE_CHAR_BUDGET} 字。",
    ]
    used = 0
    listed = 0
    for b in ranked:
        if listed >= _TRACE_BOOK_CAP or used >= _TRACE_CHAR_BUDGET:
            break
        block = _render_traces_block(b, has_bm, has_rv, has_br)
        if not block:
            continue
        lines.append(block)
        used += len(block)
        listed += 1

    if listed == 0:
        return ""
    lines.append(
        f"\n（以上 {listed} 本；本次纳入的书共 {len(books)} 本）"
    )
    return "\n".join(lines)


def _render_traces_block(
    b: dict, has_bm: bool, has_rv: bool, has_br: bool
) -> str:
    """一本书的痕迹块。条数与实际摘录条数分开写，避免"摘录少=划线少"的误读。"""
    out = [f"\n--- 《{b.get('title', '')}》 ---"]
    out.append(
        f"  作者: {b.get('author') or '?'} ｜ 分类: {b.get('category') or '?'}"
        f" ｜ 社区评分: {(b.get('rating', 0) or 0) or '暂无'}"
        f" ｜ 我的评价: {_star_str(b.get('myRating'))}"
    )
    if b.get("userNote"):
        out.append(f"  用户对这本书的介绍与感悟: {_truncate(b['userNote'], 200)}")

    wrote = False

    if has_bm:
        marks = [m for m in (b.get("bookmarks") or [])
                 if (m.get("markText") or "").strip()]
        if marks:
            wrote = True
            shown = min(len(marks), _TRACE_BM_PER_BOOK)
            out.append(
                f"  划线共 {len(marks)} 条，以下摘录 {shown} 条"
                f"（本次展示上限 {_TRACE_BM_PER_BOOK} 条、每条 {_TRACE_BM_CHARS} 字）："
            )
            for i, m in enumerate(marks[:_TRACE_BM_PER_BOOK], 1):
                ch = m.get("chapterTitle", "")
                head = f"[{ch}] " if ch else ""
                out.append(
                    f"    {i}. {head}「{_truncate(m.get('markText'), _TRACE_BM_CHARS)}」"
                )
                note = (m.get("reviewContent") or "").strip()
                if note:
                    out.append(
                        f"       ↳ 这条划线旁的批注: "
                        f"{_truncate(note, _TRACE_RV_CHARS)}"
                    )

    if has_rv:
        rvs = [r for r in (b.get("reviews") or []) if (r.get("content") or "").strip()]
        if rvs:
            wrote = True
            shown = min(len(rvs), _TRACE_RV_PER_BOOK)
            out.append(
                f"  想法共 {len(rvs)} 条，以下摘录 {shown} 条"
                f"（上限 {_TRACE_RV_PER_BOOK} 条、每条 {_TRACE_RV_CHARS} 字）："
            )
            for i, r in enumerate(rvs[:_TRACE_RV_PER_BOOK], 1):
                out.append(
                    f"    {i}. 「{_truncate(r.get('content'), _TRACE_RV_CHARS)}」"
                )

    if has_br:
        brs = [r for r in (b.get("bookReviews") or []) if (r.get("content") or "").strip()]
        brs = sorted(brs, key=lambda r: (r.get("star") is None, -(r.get("star") or 0)))
        if brs:
            wrote = True
            shown = min(len(brs), _TRACE_BR_PER_BOOK)
            out.append(
                f"  书评共 {len(brs)} 条，以下摘录 {shown} 条"
                f"（上限 {_TRACE_BR_PER_BOOK} 条、每条 {_TRACE_BR_CHARS} 字）："
            )
            for r in brs[:_TRACE_BR_PER_BOOK]:
                star = r.get("star")
                tag = f"[我的评分 {_star_str(star)}] " if star is not None else "[未评分] "
                out.append(f"    {tag}「{_truncate(r.get('content'), _TRACE_BR_CHARS)}」")

    if not wrote:
        return ""
    return "\n".join(out)


# ── 阶段二：书目视图 ───────────────────────────────────

def build_reading_profile(
    data: dict, search_digest: str = "", knowledge: dict | None = None
) -> str:
    """书目结构 + 阶段一画像 → 阶段二的 prompt 正文。

    三条规则（都源于实测的 LLM 跑偏）：
      1. 空的一律不写。某类内容被用户关掉后，每本书都是 0 条；
         把"想法: 0 条"写满全篇，模型会开始分析"为什么有书评却没想法"。
         改为顶部声明一次口径，正文里干脆不出现这一项。
      2. 书单只列上架书籍，且做分类配额抽样（否则长书单会吃掉全部注意力）。
      3. 摘录小节内容为空就整节省略，不留空标题。

    口径声明一律肯定式：写清楚"这份数字是什么、真实分布在哪一节"，
    而不是写"不要把这份数字当成真实分布"。
    """
    stats = data.get("stats", {})
    books = data.get("books", [])
    has_bm, has_rv, has_br = _content_flags(books)

    lines = ["=== 阅读概况（全量统计，覆盖本次纳入的全部书籍）==="]
    lines.append(f"本次纳入的书籍总数: {stats.get('totalBooks', 0)}")
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
    lines.append(
        "  本次导出哪几类由用户在界面上勾选；某一类未纳入时，"
        "全篇都不会出现这一类内容。"
    )

    top_cats = stats.get("topCategories", [])
    if top_cats:
        lines.append("\n=== 分类分布 (Top 10，全量统计) ===")
        for name, count in top_cats:
            lines.append(f"  {name}: {count}本")

    top_authors = stats.get("topAuthors", [])
    if top_authors:
        lines.append("\n=== 作者频率 (Top 10，全量统计) ===")
        for name, count in top_authors:
            lines.append(f"  {name}: {count}本")

    # ── 知识画像：第一轮分析的结果 ──
    if knowledge:
        lines.append("\n=== 第一阶段 · 知识画像（已完成的痕迹分析）===")
        lines.append(_render_knowledge(knowledge))
    elif has_bm or has_rv or has_br:
        lines.append("\n=== 第一阶段 · 知识画像 ===")
        lines.append("  （本次阶段一未产出画像，请依据下面的书单与详细样本分析。）")

    # ── 书单：配额抽样后只列上架书籍 ──
    shelf_books = [b for b in books if b.get("source") != "local"]
    local_books = [b for b in books if b.get("source") == "local"]

    kept, list_info = sample_shelf_list(shelf_books)
    lines.append("\n=== 书籍清单（分类配额抽样）===")
    note = _render_list_note(list_info)
    if note:
        lines.append(note)
    if not kept:
        lines.append("  （本次没有上架书籍）")
    for b in kept:
        lines.append(f"  {_book_list_line(b)}")

    if search_digest:
        lines.append("\n=== 冷门书籍联网补检资料 ===")
        lines.append(search_digest)

    # ── 详细样本 ──
    sampled = _sample_books(books)
    sampled_shelf = [(b, why) for b, why in sampled if b.get("source") != "local"]
    sampled_local = [(b, why) for b, why in sampled if b.get("source") == "local"]

    if sampled_shelf:
        total_books = stats.get("totalBooks", 0) or len(books)
        lines.append(
            f"\n=== 详细样本 (从本次 {total_books} 本纳入的书里挑选 "
            f"{len(sampled_shelf)} 本；每本最多 {_SAMPLE_BM_PER_BOOK} 条划线、"
            f"每条 {_SAMPLE_BM_CHARS} 字；条数为各书自己的总数) ==="
        )
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


def _render_knowledge(k: dict) -> str:
    """把阶段一的 JSON 画像铺成可读文本，注入阶段二。

    铺平而不是直接塞 JSON：JSON 里的字段名本身就是一种"标签"，
    模型对标签的理解远不如对一段连贯中文的理解可靠。
    """
    out = []
    headline = (k.get("headline") or "").strip()
    if headline:
        out.append(f"  一句话：{headline}")

    overview = (k.get("trace_overview") or "").strip()
    if overview:
        out.append(f"  痕迹概览：{overview}")

    axes = k.get("axes") or []
    if axes:
        out.append("  分维度判断：")
        for a in axes:
            name = (a.get("name") or "?").strip()
            reading = (a.get("reading") or "").strip()
            out.append(f"    · {name}：{reading}")
            signals = a.get("signals") or []
            if signals:
                out.append(f"      可观察信号：{'；'.join(str(s) for s in signals)}")
            for ev in (a.get("evidence") or [])[:3]:
                book = (ev.get("book") or "?").strip()
                text = (ev.get("text") or "").strip()
                if text:
                    out.append(f"      依据《{book}》：「{text}」")

    themes = k.get("themes") or []
    if themes:
        out.append("  反复出现的主题：")
        for t in themes:
            theme = (t.get("theme") or "?").strip()
            bs = t.get("books") or []
            note = (t.get("note") or "").strip()
            tail = f"（{'、'.join(str(x) for x in bs)}）" if bs else ""
            out.append(f"    · {theme}{tail}：{note}")

    quotes = k.get("signature_quotes") or []
    if quotes:
        out.append("  最有代表性的原话：")
        for q in quotes:
            book = (q.get("book") or "?").strip()
            text = (q.get("text") or "").strip()
            note = (q.get("note") or "").strip()
            if text:
                out.append(f"    ·《{book}》「{text}」—— {note}")

    blind = k.get("blind_spots") or []
    if blind:
        out.append(f"  证据缺口：{'；'.join(str(x) for x in blind)}")

    return "\n".join(out) if out else "  （画像内容为空）"


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
        out.append(
            f"  划线摘录 (最多{_SAMPLE_BM_PER_BOOK}条，每条最多{_SAMPLE_BM_CHARS}字):"
        )
        for m in b["bookmarks"][:_SAMPLE_BM_PER_BOOK]:
            text = (m.get("markText") or "").strip()
            if not text:
                continue
            ch = m.get("chapterTitle", "")
            out.append(f"    [{ch}] 「{text[:_SAMPLE_BM_CHARS]}」")
            extra = (m.get("reviewContent") or "").strip()
            if extra:
                out.append(f"      ↳ 想法: {extra[:_SAMPLE_BM_CHARS]}")

    if has_rv and b.get("reviews"):
        out.append(f"  想法摘录 (最多{_SAMPLE_RV_PER_BOOK}条):")
        for r in b["reviews"][:_SAMPLE_RV_PER_BOOK]:
            content = (r.get("content") or "").strip()
            if content:
                out.append(f"    「{content[:_SAMPLE_RV_CHARS]}」")

    if has_br and b.get("bookReviews"):
        # 带评分的那条在前：它同时说明"打了多少分"和"为什么"
        brs = [r for r in b["bookReviews"] if (r.get("content") or "").strip()]
        brs = sorted(
            brs, key=lambda r: (r.get("star") is None, -(r.get("star") or 0))
        )[:_SAMPLE_BR_PER_BOOK]
        if brs:
            out.append("  书评摘录:")
            for r in brs:
                star = r.get("star")
                tag = f"[{_star_str(star)}] " if star is not None else "[未评分] "
                out.append(f"    {tag}「{r['content'].strip()[:_SAMPLE_BR_CHARS]}」")

    return "\n".join(out)


# ── 详细样本抽样 ───────────────────────────────────────

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


# ── 两阶段报告生成 ─────────────────────────────────────

async def generate_report(
    data: dict,
    llm_config: LLMConfig,
    style: str = "serious",
    search_config: SearchConfig | None = None,
    progress=None,
) -> dict:
    """两阶段生成阅读评价报告。

    第一轮只读痕迹 → 知识画像；第二轮拿画像 + 书目结构 → 最终报告。
    第一轮失败（超时、返回非 JSON、鉴权挂了）一律降级为空画像继续跑第二轮，
    不因为多了一次调用就把整条链路搞脆弱。
    """
    client = create_llm_client(llm_config)

    digest = ""
    hits: list[dict] = []
    if search_config and search_config.enabled:
        # 五道工序全在本地完成，不调用 LLM
        digest, hits = await enrich_books(
            data.get("books", []), search_config, progress=progress
        )

    brief = _render_brief()

    # ── 阶段一：知识画像 ──
    knowledge: dict | None = None
    traces = build_traces_view(data)
    if traces:
        if progress:
            await progress(
                f"第 1/2 步：正在读你留下的划线、想法与书评"
                f"（约 {estimate_tokens(traces)} tokens）..."
            )
        system1 = TRACES_SYSTEM.format(
            system_brief=brief,
            traces_task=_TRACES_TASK.format(
                knowledge_schema=KNOWLEDGE_SCHEMA,
                axes=" / ".join(KNOWLEDGE_AXES),
            ),
        )
        try:
            raw1 = await client.chat(
                system=system1,
                messages=[LLMMessage(
                    role="user",
                    content=(
                        "下面是这位微信读书用户自己留下的文字。请先读完，"
                        "再按要求的 JSON 格式输出知识画像。\n\n" + traces
                    ),
                )],
            )
            parsed = _parse_json(raw1)
            if isinstance(parsed, dict) and (parsed.get("headline") or parsed.get("axes")):
                knowledge = parsed
            else:
                print("[analyzer] 阶段一返回不是有效画像，降级继续")
        except Exception as e:  # noqa: BLE001
            print(f"[analyzer] 阶段一失败，降级继续: {e}")

    # ── 阶段二：最终报告 ──
    profile = build_reading_profile(data, digest, knowledge=knowledge)

    template = SARCASTIC_SYSTEM if style == "sarcastic" else SERIOUS_SYSTEM
    system2 = template.format(
        system_brief=brief,
        second_pass=_SECOND_PASS_TASK,
        report_schema=REPORT_SCHEMA,
        dimension_guide=_DIMENSION_GUIDE,
    )

    if progress:
        await progress(
            f"第 2/2 步：正在生成阅读评价报告"
            f"（约 {estimate_tokens(profile)} tokens）..."
        )

    raw = await client.chat(
        system=system2,
        messages=[LLMMessage(
            role="user",
            content=(
                "请分析以下微信读书用户的阅读数据。\n"
                "注意：这是两阶段分析的第二轮，数据里的"
                "「第一阶段 · 知识画像」是上一轮对痕迹的分析结果，请直接沿用。\n\n"
                + profile
            ),
        )],
    )

    report = _parse_report(raw)
    report["_style"] = style
    if hits:
        report["_search_hits"] = hits
    # 画像回填：模型没带回来（或带回来的结构不对）就用第一轮的原始结果，
    # 保证报告里始终有"AI 是读到哪些痕迹才这么说的"。
    if knowledge:
        kp = report.get("knowledge_profile")
        if not isinstance(kp, dict) or not (kp.get("headline") or kp.get("axes")):
            report["knowledge_profile"] = _compact_knowledge(knowledge)
        report["_knowledge_raw"] = knowledge
    return report


def _compact_knowledge(k: dict) -> dict:
    """把第一轮画像压成报告里要展示的形状（模型没回填时用）。"""
    axes = []
    for a in (k.get("axes") or []):
        name = (a.get("name") or "").strip()
        reading = (a.get("reading") or "").strip()
        if not name:
            continue
        axes.append({"name": name, "line": reading[:120]})
    return {
        "headline": (k.get("headline") or "").strip(),
        "axes": axes,
        "signature_quotes": (k.get("signature_quotes") or [])[:5],
        "blind_spots": (k.get("blind_spots") or [])[:5],
    }


def estimate_prompt_sizes(data: dict, search_digest: str = "") -> dict:
    """两阶段各自会送多少 token（给前端的省 token 提示用）。"""
    traces = build_traces_view(data)
    # 阶段二的画像部分用痕迹长度粗代（真实画像比痕迹短，这里偏保守）
    profile = build_reading_profile(data, search_digest, knowledge=None)
    t1, t2 = estimate_tokens(traces), estimate_tokens(profile)
    return {
        "stage1_tokens": t1,
        "stage2_tokens": t2,
        "tokens": t1 + t2,
        "chars": len(traces) + len(profile),
        "traces_chars": len(traces),
        "profile_chars": len(profile),
    }


# ── JSON 解析 ─────────────────────────────────────────

def _strip_fences(raw: str) -> str:
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
    return raw


def _parse_json(raw: str) -> dict | None:
    """尽力把 LLM 输出解析成 dict。解析不出来返回 None，交给调用方降级。"""
    text = _strip_fences(raw)
    for candidate in (text, _drop_trailing_commas(text)):
        try:
            obj = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _drop_trailing_commas(text: str) -> str:
    """去掉 `,}` / `,]` 这种尾逗号——模型写 JSON 最常见的失误。"""
    return re.sub(r",(\s*[}\]])", r"\1", text)


def _parse_report(raw: str) -> dict:
    """从 LLM 输出中提取报告 JSON。"""
    parsed = _parse_json(raw)
    if parsed is not None:
        return parsed
    return {"error": "parse_failed", "raw": _strip_fences(raw)[:500]}
