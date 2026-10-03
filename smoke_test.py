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


def main() -> int:
    print("=" * 56)
    print(" 微信读书 · AI 阅读评价 —— 冒烟测试")
    print("=" * 56)

    for fn in (test_open_mode, test_auth_mode, test_session_eviction,
               test_readme_matches_routes):
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
