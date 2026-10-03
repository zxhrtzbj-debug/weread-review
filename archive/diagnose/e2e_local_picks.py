"""端到端：真起服务，跑一遍本地文件分类 / 改判 / ratedOnly 三个新接口。

跑法：cd review_weread && python3 archive/diagnose/e2e_local_picks.py
（会自己在随机端口起后端，跑完关掉）
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
PY = sys.executable
sys.path.insert(0, str(BACKEND))  # 直接复用后端的 prompt 组装做断言


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def req(method: str, port: int, path: str, body=None):
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, method=method,
                              headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def main() -> None:
    port = free_port()
    proc = subprocess.Popen(
        [PY, "main.py"], cwd=str(BACKEND),
        env={"PORT": str(port), "HOST": "127.0.0.1", "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )

    ok = True
    try:
        # 等服务起来
        for _ in range(60):
            try:
                code, _ = req("GET", port, "/api/ping")
                if code == 200:
                    break
            except Exception:
                pass
            time.sleep(0.5)
        else:
            print("后端没起来：\n", proc.stdout.read() if proc.stdout else "")
            sys.exit(1)

        code, sess = req("POST", port, "/api/auth/session")
        uid = sess["uid"]
        print(f"[1] 会话 {uid}")

        code, d = req("POST", port, f"/api/demo/load/{uid}")
        print(f"[2] 示例数据: {code} stats={ {k: d['stats'][k] for k in ('totalBooks','localFiles','ratedBooks','avgMyRating')} }")

        code, res = req("GET", port, f"/api/data/result/{uid}")
        books = res["books"]
        print(f"[3] 书单 {len(books)} 本（本地文件应已被排除）：{ [b['title'][:16] for b in books] }")
        ok &= all(b["source"] != "local" for b in books)

        code, lp = req("GET", port, f"/api/data/local-picks/{uid}")
        files = lp["files"]
        print(f"[4] 本地文件 {len(files)} 本：{ [f['title'] for f in files] }")
        print(f"    预设分类 {lp['slots']}")
        ok &= len(files) == 2

        bid = files[0]["bookId"]
        code, saved = req("POST", port, f"/api/data/local-picks/{uid}", {
            "picks": {bid: {"slot": "印象最深", "note": "这是我自己的年度规划，后来兑现了七成"}},
        })
        print(f"[5] 保存选择 {code}: {saved['picks']}")

        code, res = req("GET", port, f"/api/data/result/{uid}")
        picked = [b for b in res["books"] if b.get("userSlot")]
        print(f"[6] 书单 {len(res['books'])} 本，选入 {len(picked)} 本")
        for b in picked:
            print(f"    · {b['title']}｜分类={b['userSlot']}｜自述={b['userNote']}")
        ok &= len(picked) == 1 and picked[0]["userNote"].startswith("这是我自己的")

        # prompt 里本地文件单独成节
        from services.analyzer import build_reading_profile
        profile = build_reading_profile(res)
        ok &= "用户自选的本地上传文件" in profile
        ok &= "这是我自己的年度规划" in profile
        print(f"[7] prompt 含本地文件专节 = {'用户自选的本地上传文件' in profile}，含用户自述 = {'这是我自己的年度规划' in profile}")

        # 改判：把这本放回上架书单
        code, ov = req("POST", port, f"/api/data/source-override/{uid}",
                       {"bookId": bid, "source": "weread"})
        code, res = req("GET", port, f"/api/data/result/{uid}")
        back = [b for b in res["books"] if b["bookId"] == bid]
        print(f"[8] 改判回上架书籍 {code}，该书回到书单 = {bool(back)}，"
              f"userSlot={back[0].get('userSlot') if back else None}")
        ok &= bool(back)

        # 撤销改判
        req("POST", port, f"/api/data/source-override/{uid}", {"bookId": bid, "source": None})
        req("POST", port, f"/api/data/local-picks/{uid}",
            {"picks": {bid: {"slot": "印象最深", "note": "x"}}})

        # ratedOnly
        code, cf = req("POST", port, f"/api/data/content-filter/{uid}", {"ratedOnly": True})
        code, res = req("GET", port, f"/api/data/result/{uid}")
        with_content = [b["title"] for b in res["books"]
                        if b.get("bookmarks") or b.get("reviews")]
        rated = [b["title"] for b in res["books"] if b.get("hasMyRating")]
        print(f"[9] ratedOnly: 有内容的书 {with_content}，打过分的书 {rated}")
        ok &= set(with_content) <= set(rated)

        req("POST", port, f"/api/data/content-filter/{uid}", {"ratedOnly": False})
        code, est = req("GET", port, f"/api/analysis/estimate/{uid}")
        print(f"[10] estimate: {est['tokens']} tokens，ratedBooks={est['counts']['ratedBooks']}，"
              f"kept={est['kept']}")
        ok &= est["counts"]["ratedBooks"] == 2

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    print("\n" + ("端到端全部通过 ✓" if ok else "有断言失败 ✗"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
