"""全局常量与路径配置。

这里只放"不随会话变化"的东西：端口、超时、并发度、静态资源路径。
任何运行时状态都放 store.py。

部署相关的量全部走环境变量，本地默认值保持"双击就能跑"：
    PORT              监听端口，默认 8777
    HOST              监听地址，默认 127.0.0.1（仅本机可访问）
                       部署到公网时设成 0.0.0.0
    ACCESS_TOKEN      设置后所有 /api 请求都要带 ?token=... 或 X-Access-Token 头。
                       公网部署强烈建议设置：这是一个会拿用户 cookie 去请求
                       微信读书的中继，没有口令等于对全网开放。
    SESSION_TTL       会话空闲回收秒数，默认 7200（2 小时）
"""

import os
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
FRONTEND_DIR = ROOT_DIR / "frontend"
INDEX_HTML = FRONTEND_DIR / "index.html"
ARCHIVE_DIR = ROOT_DIR / "archive"

# ── HTTP 服务 ──────────────────────────────────────────
# 本地：127.0.0.1:8777，双击 start.command 直接可用。
# 部署：平台注入 PORT，所以端口必须从环境变量读，不能写死。
SERVER_PORT = int(os.environ.get("PORT") or 8777)
SERVER_HOST = os.environ.get("HOST") or "127.0.0.1"

# 公网部署时通常必须绑 0.0.0.0，平台只把对外端口转到容器内网卡上。
# 绑 127.0.0.1 的话容器外永远打不开——这是新手最常踩的一坑。
if os.environ.get("WEREREAD_DEPLOY") == "1":
    SERVER_HOST = os.environ.get("HOST") or "0.0.0.0"

# 访问口令。空字符串 = 不鉴权（仅限本机使用）。
ACCESS_TOKEN = os.environ.get("ACCESS_TOKEN") or ""

# 会话在内存里，不回收会随使用次数线性增长；公网实例必须设上限。
SESSION_TTL = int(os.environ.get("SESSION_TTL") or 7200)
MAX_SESSIONS = int(os.environ.get("MAX_SESSIONS") or 200)


def find_free_port(start: int = SERVER_PORT, tries: int = 10) -> int:
    """从 start 开始找第一个空闲端口。

    上一次进程没退干净时不打架、也不留下第二个僵尸进程——换端口继续跑。

    端口被平台占用时 PORT 是强制的（绑不上就起不来），此时不换端口直接报错。
    """

    import socket
    import time

    for port in range(start, start + tries):
        for attempt in (0, 1):
            # 上一次进程刚退出时端口会短暂不可绑定：先重试一次，真被长期占用才让位，
            # 免得大家手里 http://127.0.0.1:8777 的收藏每次都漂端口号
            with socket.socket() as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try:
                    s.bind((SERVER_HOST, port))
                    return port
                except OSError:
                    if attempt:
                        break
                    time.sleep(0.4)

    if os.environ.get("PORT"):
        # 平台给了固定端口：没有第二个选项，失败要让人看见
        raise OSError(f"端口 {SERVER_PORT} 被占用，且 PORT 由环境指定，无法顺延")
    return start

# ── 数据提取 ──
REQUEST_TIMEOUT = 30.0     # 单个接口请求超时
BOOK_CONCURRENCY = 5       # 并发抓书数量

# 分类取不到时填的占位名（详情没取回来，或平台本来就没给这个字段）。
# 它作为一个独立分类参与统计与配额抽样，与真实分类对齐；
# 名字描述的是这一次导出的结果，不是这些书的题材。
UNRECOGNIZED_CATEGORY = "未识别"

# ── 联网搜索 ──
SEARCH_TIMEOUT = 20.0      # 单次搜索请求超时
SEARCH_MAX_BOOKS = 8       # 一次分析最多为多少本书做联网补检
SEARCH_MAX_QUERIES = 2     # 每本书最多发几条检索式
SEARCH_SNIPPET_CHARS = 320 # 每条检索结果摘要保留的字符数
SEARCH_DIGEST_CHARS = 6000 # 补检摘要总长度上限：别把内容筛选省下的 token 又吃回去

# ── 内容筛选默认值 ──
# 0 表示不限制条数
DEFAULT_CONTENT_FILTER = {
    "keepBookmarks": True,
    "keepReviews": True,
    "keepBookReviews": True,
    "maxBookmarksPerBook": 0,
    "maxReviewsPerBook": 0,
    # 只保留"我打过分的书"的划线/想法，其余书只保留书单行。
    # 打分是一个明确的偏好表态，用它当中层筛选比按条数截断更省 token 也更有信息量。
    "ratedOnly": False,
}

# ── 本地上传文件的自选分类 ────────────────────────────
# 本地文件没有书名/分类/评分，进不了自动书单，但用户可能想让其中几本参与评价。
# 这里是可直接改的预设分类；前端也允许临时新增自定义分类名。
LOCAL_PICK_SLOTS = ("印象最深", "最想推荐", "启发性最大", "平时最有用")
