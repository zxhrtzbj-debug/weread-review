"""示例数据预设（Demo）。

目的：不动微信读书账号也能跑通「审核 → 内容筛选 → AI 评价 → 报告」全流程，
用于本地联调前端与 prompt，也用于给 AI 演示效果。

数据是本地生成的假数据，不联网、不含任何真实划线内容。
"""

from __future__ import annotations

import random
import time

_BOOKS = [
    {
        "title": "三体", "author": "刘慈欣", "category": "科幻",
        "rating": 9.3, "intro": "文化大革命如火如荼进行的同时，军方探寻外星文明的绝秘计划"
                               "「红岸工程」取得了突破性进展。",
        "bm": 42, "rv": 6, "br": 1,
        "bm_texts": [
            "给岁月以文明，而不是给文明以岁月。",
            "弱小和无知不是生存的障碍，傲慢才是。",
            "宇宙就是一座黑暗森林，每个文明都是带枪的猎人。",
        ],
        "rv_texts": ["大刘把社会学实验做成了宇宙尺度，冷酷但自洽。"],
        "br_text": ["重读依然震撼，第二部的黑暗森林推演是硬科幻的巅峰之一。"],
    },
    {
        "title": "置身事内", "author": "兰小欢", "category": "经济",
        "rating": 9.1, "intro": "本书以地方政府投融资为主线，解释中国经济中的政府行为。",
        "bm": 31, "rv": 9, "br": 0,
        "bm_texts": [
            "政府不只是裁判，也是场上最大的球员。",
            "土地财政的本质是把未来的收益贴现到现在。",
        ],
        "rv_texts": [
            "读完才明白地方债为什么长成这样，比看新闻有用得多。",
            "作者把复杂的财政关系讲得像故事一样清楚。",
        ],
        "br_text": [],
    },
    {
        "title": "翦商", "author": "李硕", "category": "历史",
        "rating": 8.9, "intro": "从人祭制度的兴衰，重述夏商周之变。",
        "bm": 27, "rv": 4, "br": 1,
        "bm_texts": [
            "周人翦商的过程，也是一场对人祭宗教的彻底否定。",
            "考古材料一旦摆出来，传说的年代就有了重量。",
        ],
        "rv_texts": ["写法很大胆，论证链条长但每一步都给了出处。"],
        "br_text": ["把甲骨文、考古与文献串起来的能力极强，争议也大，值得一读。"],
    },
    {
        "title": "娱乐至死", "author": "尼尔·波兹曼", "category": "社会学",
        "rating": 8.5, "intro": "电视时代的一切公众话语都日渐以娱乐的方式出现。",
        "bm": 18, "rv": 3, "br": 0,
        "bm_texts": ["一切公众话语都日渐以娱乐的方式出现，并成为一种文化精神。"],
        "rv_texts": ["四十年前的预言，今天看像在描述短视频。"],
        "br_text": [],
    },
    {
        "title": "寂静的春天", "author": "蕾切尔·卡森", "category": "科普",
        "rating": 8.7, "intro": "以DDT为代表的杀虫剂如何进入生态链并最终回到人体。",
        "bm": 12, "rv": 2, "br": 1,
        "bm_texts": ["人类正在失去预见的能力，因为对自然的征服欲太强。"],
        "rv_texts": ["现代环保运动的起点，文笔比很多小说还好。"],
        "br_text": ["科普写作的标杆，情绪克制但证据锋利。"],
    },
    {
        "title": "技术的本质", "author": "布莱恩·阿瑟", "category": "科技",
        "rating": 8.2, "intro": "技术是被捕获并加以利用的现象的集合。",
        "bm": 9, "rv": 1, "br": 0,
        "bm_texts": ["技术是对现象的编程。"],
        "rv_texts": ["概念密度很高，适合慢慢读。"],
        "br_text": [],
    },
    {
        "title": "规模", "author": "杰弗里·韦斯特", "category": "科普",
        "rating": 8.4, "intro": "",  # 故意留空：触发联网补检
        "bm": 0, "rv": 0, "br": 0,
        "bm_texts": [], "rv_texts": [], "br_text": [],
    },
    {
        "title": "禅与摩托车维修艺术", "author": "罗伯特·M·波西格", "category": "哲学",
        "rating": 0.0,  # 故意无评分：触发联网补检
        "intro": "",
        "bm": 0, "rv": 0, "br": 0,
        "bm_texts": [], "rv_texts": [], "br_text": [],
    },
]


def build_demo_data() -> dict:
    """构造一份结构与真实提取结果完全一致的示例数据。"""
    now = int(time.time())
    books = []
    for i, spec in enumerate(_BOOKS):
        bid = f"demo{i + 1:03d}"
        rnd = random.Random(bid)

        bookmarks = [
            {
                "bookId": bid,
                "chapterUid": 100 + j,
                "chapterTitle": f"第 {j + 1} 章",
                "range": f"{100 + j}-{110 + j}",
                "markText": text,
                "createTime": now - (i * 7 + j) * 86400,
            }
            for j, text in enumerate(spec["bm_texts"])
        ]
        # 真实账号的划线条数远多于样例文本，补足数量让筛选效果可测
        filler = spec["bm"] - len(bookmarks)
        for j in range(max(0, filler)):
            bookmarks.append({
                "bookId": bid,
                "chapterUid": 200 + j,
                "chapterTitle": f"第 {len(spec['bm_texts']) + j + 1} 章",
                "range": f"{200 + j}-{210 + j}",
                "markText": f"（示例划线 {j + 1}）" + "这是一段用于测试 token 消耗的填充文本。" * rnd.randint(1, 3),
                "createTime": now - (i * 7 + j) * 86400,
            })

        reviews = [
            {
                "content": text,
                "abstract": "",
                "chapterTitle": f"第 {j + 1} 章",
                "createTime": now - (i * 7 + j) * 86400,
                "type": 1,
            }
            for j, text in enumerate(spec["rv_texts"])
        ]
        for j in range(max(0, spec["rv"] - len(reviews))):
            reviews.append({
                "content": f"（示例想法 {j + 1}）批量填充，用于测试内容筛选。",
                "abstract": "",
                "chapterTitle": f"第 {j + 1} 章",
                "createTime": now - (i * 7 + j) * 86400,
                "type": 1,
            })

        book_reviews = [
            {"content": text, "createTime": now - i * 86400, "type": 4}
            for text in spec["br_text"]
        ]

        books.append({
            "bookId": bid,
            "title": spec["title"],
            "author": spec["author"],
            "cover": "",
            "category": spec["category"],
            "rating": spec["rating"],
            "intro": spec["intro"],
            "totalBookmarks": len(bookmarks),
            "totalReviews": len(reviews),
            "totalBookReviews": len(book_reviews),
            "bookmarks": bookmarks,
            "reviews": reviews,
            "bookReviews": book_reviews,
        })

    from services.weread import compute_stats

    return {"books": books, "stats": compute_stats(books), "_demo": True}
