#!/usr/bin/env python3
"""冒烟测试：起一个真实实例，把关键接口挨个打一遍。

为什么需要它
──────────
零依赖项目最怕的是"改完不知道有没有弄坏"。这个脚本不依赖 pytest，
标准库就能跑，所以 CI 里 `python3 smoke_test.py` 一条命令就能验证。

覆盖：
  · 无鉴权模式（本地默认）
  · 有鉴权模式（公网部署）
  · 会话回收
  · demo 全流程能跑通

用法：
    python3 smoke_test.py
退出码 0 = 全过，1 = 有失败。
"""

from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PY = sys.executable

_passed: list[str] = []
_failed: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    if ok:
        _passed.append(name)
        print(f"  \033[32m✓\033[0m {name}")
    else:
        _failed.append(f"{name} — {detail}")
        print(f"  \033[31m✗\033[0m {name}  {detail}")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def request(
    base: str, path: str, *, method: str = "GET", body: dict | None = None,
    token: str = "", headers: dict | None = None,
) -> tuple[int, dict | str]:
    url = f"{base}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("X-Access-Token", token)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(raw)
            except json.JSONDecodeError:
                return r.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, raw
    except Exception as e:  # noqa: BLE001
        return 0, str(e)


class Server:
    """起一个后台实例，退出时确保杀掉。"""

    def __init__(self, **env: str):
        self.port = free_port()
        self.env = env
        self.proc: subprocess.Popen | None = None

    def __enter__(self) -> str:
        env = {**os.environ, "PORT": str(self.port), **self.env}
        self.proc = subprocess.Popen(
            [PY, "-u", str(ROOT / "backend" / "main.py"), "--no-browser"],
            cwd=str(ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        base = f"http://127.0.0.1:{self.port}"
        for _ in range(80):  # 最多等 8 秒
            if self.proc.poll() is not None:
                out = self.proc.stdout.read().decode("utf-8", "replace")
                raise RuntimeError(f"进程提前退出：\n{out}")
            status, _ = request(base, "/api/ping")
            if status == 200:
                return base
            time.sleep(0.1)
        raise RuntimeError("服务 8 秒内没起来")

    def __exit__(self, *exc) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


# ── 1. 无鉴权模式（本地默认）────────────────────────────
def test_open_mode() -> None:
    print("\n[1] 本地模式（无鉴权）")
    with Server() as base:
        status, body = request(base, "/api/ping")
        check("GET /api/ping 返回 200", status == 200, str(body))
        check("ping 标明零依赖", isinstance(body, dict) and body.get("deps") == "stdlib-only")

        status, body = request(base, "/api/health")
        check("GET /api/health 返回 200", status == 200, str(body))
        check("health 带版本号", isinstance(body, dict) and "version" in body)
        check("health 无需口令", isinstance(body, dict) and body.get("auth_required") is False)

        status, body = request(base, "/")
        check("GET / 返回首页", status == 200 and isinstance(body, str) and "微信读书" in body)

        status, body = request(base, "/api/auth/session", method="POST")
        check("建会话返回 uid", status == 200 and "uid" in body, str(body))
        uid = body.get("uid") if isinstance(body, dict) else None

        # demo 全流程：不登录也能跑通，这是新人第一眼看到的东西
        status, body = request(base, f"/api/demo/load/{uid}", method="POST")
        check("载入 demo 数据", status == 200, str(body)[:200])

        status, body = request(base, f"/api/data/result/{uid}")
        ok = status == 200 and isinstance(body, dict)
        check("取回 demo 数据", ok, str(body)[:200])
        if ok:
            books = (body.get("data") or body).get("books") or []
            check("demo 有书", len(books) > 0, f"books={len(books)}")

        status, body = request(base, f"/api/analysis/estimate/{uid}")
        check("token 估算可用", status == 200, str(body)[:200])

        status, body = request(base, "/api/nope")
        check("未知路由返回 404", status == 404, str(status))


# ── 2. 鉴权模式（公网部署）─────────────────────────────
def test_auth_mode() -> None:
    print("\n[2] 公网模式（ACCESS_TOKEN=test-token-123）")
    with Server(ACCESS_TOKEN="test-token-123") as base:
        status, _ = request(base, "/api/ping")
        check("免鉴权：/api/ping 仍可访问", status == 200, str(status))

        status, _ = request(base, "/")
        check("免鉴权：首页仍可访问", status == 200, str(status))

        status, _ = request(base, "/api/health")
        check("免鉴权：/api/health 仍可访问", status == 200, str(status))

        status, body = request(base, "/api/auth/session", method="POST")
        check("无口令被拒（401）", status == 401, f"实际 {status} {str(body)[:80]}")

        status, body = request(base, "/api/auth/session", method="POST", token="wrong")
        check("错口令被拒（401）", status == 401, f"实际 {status} {str(body)[:80]}")

        status, body = request(
            base, "/api/auth/session", method="POST", token="test-token-123"
        )
        check("头带正确口令放行", status == 200 and "uid" in body, str(body)[:120])

        # SSE 只能走 query，前端 EventSource 就是这么传令牌的
        status, body = request(
            base, "/api/auth/session?token=test-token-123", method="POST"
        )
        check("query 带口令放行（SSE 用）", status == 200 and "uid" in body, str(body)[:120])

        # 前端首次打开页面不带 token，必须靠 /api/ping 免鉴权才能自检
        status, body = request(base, "/api/health")
        check(
            "health 报告需要口令（前端据此提示）",
            status == 200 and isinstance(body, dict) and body.get("auth_required") is True,
            str(body)[:120],
        )

        # 带口令真的能跑通 demo 流程（cookie 会经过服务端转发）
        _, s = request(base, "/api/auth/session?token=test-token-123", method="POST")
        uid = s["uid"]
        status, _ = request(
            base, f"/api/demo/load/{uid}?token=test-token-123", method="POST"
        )
        check("鉴权模式下 demo 可用", status == 200, str(status))


# ── 3. 会话回收 ───────────────────────────────────────
def test_session_eviction() -> None:
    print("\n[3] 会话回收（SESSION_TTL=2, MAX_SESSIONS=3）")
    with Server(SESSION_TTL="2", MAX_SESSIONS="3") as base:
        uids = []
        for _ in range(3):
            _, body = request(base, "/api/auth/session", method="POST")
            uids.append(body["uid"])
        time.sleep(3.5)  # 超过 TTL

        status, body = request(base, f"/api/data/result/{uids[0]}")
        check("超时会话已被回收（404）", status == 404, f"实际 {status}")

        # 再建一个触发 _evict，看上限是否生效
        for _ in range(4):
            request(base, "/api/auth/session", method="POST")
        _, body = request(base, "/api/health")
        n = body.get("store", {}).get("sessions")
        check("会话数不超上限", isinstance(n, int) and n <= 3, f"sessions={n}")


# ── 4. 文档与代码一致 ─────────────────────────────────
def test_readme_matches_routes() -> None:
    """README 里的接口清单必须和实际注册的一致。

    这类文档漂移最隐蔽：接口删了文档还留着，读者照着调得到 404。
    """
    print("\n[4] README 与代码一致性")
    sys.path.insert(0, str(ROOT / "backend"))
    sys.dont_write_bytecode = True
    import stdhttp  # noqa: E402
    import routers.analysis, routers.auth, routers.data, routers.llm, main  # noqa: E402,F401

    # 从 ROUTES 反推路径：把 (?P<uid>[^/]+) 还原成 {uid}，与 README 同一套写法
    live = set()
    for _method, pattern, _fn in stdhttp.ROUTES:
        path = re.sub(r"\(\?P<\w+>\[\^/\]\+\)", "{}", pattern.pattern[1:-1])
        path = re.sub(r"\{[^}]+\}", "{}", path)
        live.add(path)

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    block = re.search(r"```\n(GET    /.*?)```", readme, re.S)
    check("README 有接口清单", block is not None)
    if not block:
        return

    documented = set()
    for line in block.group(1).splitlines():
        m = re.match(r"\s*((?:GET|POST|PUT|DELETE)(?:/[A-Z]+)*)\s+(/\S*)", line)
        if not m:
            continue
        path = m.group(2).split("?")[0].strip()
        # 归一化参数名：{uid} / {sid} 统一成 {}
        path = re.sub(r"\{[^}]+\}", "{}", path)
        documented.add(path)

    missing = sorted(live - documented)
    check("README 覆盖全部路由", not missing, f"漏写: {missing}" if missing else "")

    extra = sorted(documented - live)
    check(
        "README 没有多写不存在的路由",
        not extra,
        f"多写: {extra}" if extra else "",
    )


# ── 5. 前端URL 拼接（静态层） ─────────────────────────
def test_frontend_url_building() -> None:
    """带 uid/sid 的路径必须把动态段放在 query 之前。

    这个坑很隐蔽：`apiUrl('/api/x/') + uid` 在本地（无口令）完全正常，
    一旦线上设了 ACCESS_TOKEN 就变成 `?token=口令uid`，路由 404、二维码空白，
    而本地怎么测都测不出来。

    这里只做静态结构检查。真正执行 JS 验证拼接结果的检查在 CI 的 frontend
    job 里（node侧）——Python 的 exec 跑不了 `const` 这类 JS 语法。
    """
    print("\n[5] 前端 URL 拼接（静态）")
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")

    m = re.search(r"function apiUrl\(([^)]*)\)\s*\{(.*?)\n\}", html, re.S)
    check("找到 apiUrl 定义", m is not None)
    if not m:
        return

    params = m.group(1)
    check("apiUrl 签名含 tail 参数", "tail" in params, f"实际: ({params})")

    # 老写法（apiUrl(x) + y）必须已经全部消失
    legacy = re.findall(r"apiUrl\([^)]*\)\s*\+", html)
    check(
        "没有 apiUrl(x) + y 的老写法",
        not legacy,
        f"仍有 {len(legacy)} 处: {legacy[:3]}" if legacy else "",
    )

    # 每个带动态段的调用都必须把动态段作为参数传进去，而不是在外面拼
    tails = re.findall(r"apiUrl\('/api/[^']*',\s*([\w.$]+)\)", html)
    check("动态段都作为 tail 参数传入", len(tails) >= 20, f"只找到 {len(tails)} 处")

    # 动态段里不能出现裸对象属性截断（正则批量替换踩过的坑：
    # `apiUrl(x, data).sid` 说明 data.sid 被误当成 data）
    for bad in re.findall(r"apiUrl\([^)]*\)\s*\.\s*\w", html):
        check(
            "没有 apiUrl(...).prop 这种误伤写法",
            False,
            f"发现: {bad}",
        )
        break
    else:
        check("没有 apiUrl(...).prop 这种误伤写法", True)


# ── 6. 两阶段分析 ─────────────────────────────────────
def test_analyzer_two_stage() -> None:
    """书单配额抽样的三个分支、痕迹视图、背景陈述、阶段一失败的降级。

    这些都是纯函数/纯字符串，不需要起服务，所以放在这里和接口测试一起跑。
    """
    print("\n[6] 两阶段分析（配额抽样 / 痕迹视图 / 背景陈述 / 降级）")
    sys.path.insert(0, str(ROOT / "backend"))
    sys.dont_write_bytecode = True
    import asyncio  # noqa: E402
    from services import analyzer  # noqa: E402
    from services.demo import build_demo_data  # noqa: E402
    from services.llm import LLMConfig  # noqa: E402

    def book(i: int, cat: str, my=None, rating: float = 7.0, traces: int = 0) -> dict:
        b = {
            "bookId": f"b{i}", "title": f"书{i}", "author": "A", "category": cat,
            "rating": rating, "myRating": my, "source": "shelf",
            "totalBookmarks": traces, "totalReviews": 0, "totalBookReviews": 0,
            "bookmarks": [], "reviews": [], "bookReviews": [],
        }
        if traces:
            b["bookmarks"] = [
                {"markText": f"划线{j}", "chapterTitle": "第一章"} for j in range(traces)
            ]
        return b

    # ── 分支一：有我的评分的书 少于 配额 → 全取 + 从"只有社区评分"里交替补齐
    books = (
        [book(i, "A", rating=6.0 + i * 0.1) for i in range(2)]          # 参考分类 2 本
        + [book(10 + i, "B", my=(4 if i < 3 else None),
                rating=5.0 + i * 0.3) for i in range(10)]               # 3 本有评分
        + [book(30 + i, "C", my=3 + (i % 3),
                rating=8.0 - i * 0.1) for i in range(6)]                # 6 本全有评分
    )
    kept, info = analyzer.sample_shelf_list(books)
    check("参考分类取书最少的那个", info.get("ref_category") == "A", str(info))
    check("配额 = max(参考分类本数, 5)", info.get("quota") == 5, str(info))
    check("参考分类全部保留", sum(1 for b in kept if b["category"] == "A") == 2)
    check("有评分的书不足配额时先全取",
          sum(1 for b in kept if b["category"] == "B" and b["myRating"] is not None) == 3)
    check("不足的份额从无评分的书里补齐到配额",
          sum(1 for b in kept if b["category"] == "B") == 5,
          str(sum(1 for b in kept if b["category"] == "B")))
    check("有评分的书超额时截取到配额",
          sum(1 for b in kept if b["category"] == "C") == 5,
          str(sum(1 for b in kept if b["category"] == "C")))
    check("清单总量 = Σ各类配额", info.get("kept") == 12, str(info))

    # ── 分支二：有我的评分的书 正好等于 配额 → 直接取这些
    books2 = (
        [book(i, "A", rating=6.0) for i in range(2)]
        + [book(100 + i, "D", my=4, rating=7.0 + i * 0.1) for i in range(5)]
        + [book(200 + i, "D", my=None, rating=9.9 - i * 0.1) for i in range(4)]
    )
    kept2, _ = analyzer.sample_shelf_list(books2)
    picked_d = [b for b in kept2 if b["category"] == "D"]
    check("有评分的书正好等于配额时直接取这些",
          len(picked_d) == 5 and all(b["myRating"] is not None for b in picked_d),
          f"n={len(picked_d)}")

    # ── 分支三：分类只有一个时不做配额，全部保留
    kept3, info3 = analyzer.sample_shelf_list([book(i, "Z") for i in range(9)])
    check("单一分类不抽样", len(kept3) == 9 and info3["categories"] == 1, str(info3))

    # ── 交替顺序：最高 → 最低 → 次高 → 次低
    pool = [book(i, "P", rating=float(i)) for i in range(1, 7)]
    alt = analyzer._pick_alternating(pool, analyzer._community_key, 4)
    order = [b["rating"] for b in alt]
    check("交替抽样顺序为 最高/最低/次高/次低",
          order == [6.0, 1.0, 5.0, 2.0], str(order))

    # ── 痕迹视图
    data = build_demo_data()
    traces = analyzer.build_traces_view(data)
    check("痕迹视图非空", len(traces) > 100, f"{len(traces)} 字符")
    check("痕迹视图标明总数与摘录数", "划线共" in traces and "以下摘录" in traces)
    check("痕迹视图受字符预算封顶",
          len(traces) <= analyzer._TRACE_CHAR_BUDGET + 4000, f"{len(traces)}")

    # ── 背景陈述的关键条款
    # 条款本身要写成肯定式的口径说明；同时断言里面没有禁令式措辞——
    # 实测"不要建议平衡其他领域"这类句子会被模型直接当成小标题执行。
    brief = analyzer._render_brief()
    for key in ("书籍清单", "配额抽样", "详细样本", "分类分布", "信息密度",
                "展示上限", "本次纳入"):
        check(f"背景陈述含「{key}」", key in brief)
    for bad in ("红线", "无效", "禁止", "不要", "不代表", "不等于", "不是全量"):
        check(f"背景陈述不含禁令式措辞「{bad}」", bad not in brief)

    # 正文里的口径声明同样要肯定式
    profile_view = analyzer.build_reading_profile(build_demo_data())
    for bad in ("不代表用户没有", "不要据此推断", "不是全量"):
        check(f"书目视图不含「{bad}」", bad not in profile_view)
    check("痕迹视图不含「不要把」", "不要把" not in traces)

    # ── 阶段一失败必须降级，不能拖垮整条链路
    report_json = json.dumps({
        "overall_score": 7, "summary": "s", "dimensions": [], "strengths": [],
        "weaknesses": [], "book_picks": {"top": [], "flop": []},
        "recommendations": [], "one_liner": "x",
    })

    class Stage1Boom:
        def __init__(self, _cfg):
            self.seen = []

        async def chat(self, system: str, messages) -> str:
            self.seen.append(system)
            if "阅读痕迹分析师" in system:
                raise RuntimeError("阶段一炸了")
            return report_json

    holder = {}
    original = analyzer.create_llm_client

    def boom_factory(cfg):
        c = Stage1Boom(cfg)
        holder["client"] = c
        return c

    analyzer.create_llm_client = boom_factory
    try:
        out = asyncio.run(
            analyzer.generate_report(build_demo_data(), LLMConfig(), "serious")
        )
    finally:
        analyzer.create_llm_client = original

    check("阶段一失败仍能出报告", out.get("overall_score") == 7, str(out)[:160])
    check("阶段一失败时不伪造画像", out.get("knowledge_profile") is None)
    check("两个阶段都各自带过完整背景",
          len(holder.get("client").seen) == 2
          and all("数据说明书" in s for s in holder["client"].seen),
          f"calls={len(holder.get('client').seen)}")

    # ── 阶段一成功 → 画像回填进最终报告
    profile_json = json.dumps({
        "headline": "一个爱追问机制的人",
        "trace_overview": "共 12 条划线",
        "axes": [{"name": "思维方式", "reading": "追因果链", "signals": ["s"],
                  "evidence": [{"book": "三体", "text": "弱小和无知不是生存的障碍"}]}],
        "themes": [], "signature_quotes": [{"book": "三体", "text": "t", "note": "n"}],
        "blind_spots": ["缺"],
    })

    class BothOk:
        def __init__(self, _cfg):
            self.seen = []

        async def chat(self, system: str, messages) -> str:
            self.seen.append(system)
            if "阅读痕迹分析师" in system:
                return profile_json
            return report_json

    holder2 = {}
    analyzer.create_llm_client = lambda cfg: (holder2.setdefault("c", BothOk(cfg)))
    try:
        out2 = asyncio.run(
            analyzer.generate_report(build_demo_data(), LLMConfig(), "serious")
        )
    finally:
        analyzer.create_llm_client = original

    kp = out2.get("knowledge_profile")
    check("阶段一成功时画像回填进报告",
          isinstance(kp, dict) and kp.get("headline") == "一个爱追问机制的人", str(kp)[:160])
    check("画像带维度", isinstance(kp, dict) and len(kp.get("axes") or []) == 1)
    seen = holder2.get("c").seen
    check("阶段二提示词说明了这是第二轮",
          len(seen) == 2 and "第二轮" in seen[1], f"calls={len(seen)}")
    profile_txt = analyzer.build_reading_profile(
        build_demo_data(), knowledge=json.loads(profile_json)
    )
    check("阶段二正文含画像小节", "第一阶段 · 知识画像" in profile_txt)
    check("阶段二正文含书单抽样说明", "参考分类" in profile_txt)
    check("阶段二正文分类分布标为全量", "全量统计" in profile_txt)


def main() -> int:
    print("=" * 56)
    print(" 微信读书 · AI 阅读评价 —— 冒烟测试")
    print("=" * 56)

    for fn in (test_open_mode, test_auth_mode, test_session_eviction,
               test_readme_matches_routes, test_frontend_url_building,
               test_analyzer_two_stage):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            _failed.append(f"{fn.__name__} 崩了: {e}")
            print(f"  \033[31m✗ {fn.__name__} 崩了: {e}\033[0m")

    print("\n" + "─" * 56)
    print(f"通过 {len(_passed)}，失败 {len(_failed)}")
    if _failed:
        print("\n失败明细：")
        for f in _failed:
            print(f"  · {f}")
        return 1
    print("全部通过 ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
