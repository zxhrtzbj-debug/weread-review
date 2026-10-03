#!/bin/bash
# 双击即可用：起服务 → 自动开浏览器。关掉窗口就彻底结束，不留下任何东西。
#
# 这里刻意不做的事：
#   · 不建虚拟环境      —— 项目目录里不会出现 .venv
#   · 不 pip install    —— 后端零第三方依赖，HTTP/模型/HTTP client 全是标准库
#   · 不写 __pycache__  —— main.py 里设了 sys.dont_write_bytecode
#   · 不常驻            —— 服务跑在前台，终端没了进程就没了
# 唯一的例外是你主动要扫码登录（那时才需要装 playwright），见文末提示。
set -u
cd "$(dirname "$0")" || exit 1
PORT=8777

echo "════════════════════════════════════════"
echo " 微信读书 · AI 阅读评价"
echo "════════════════════════════════════════"

# ── 1. 找 Python 3.10+（不挑来源，系统自带即可）──
PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1; then
    if "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
      PY="$c"; break
    fi
  fi
done
if [ -z "$PY" ]; then
  echo "❌ 没找到 Python 3.10+。"
  echo "   macOS 自带 Python 3.9 的话，用 brew install python 装一个即可。"
  read -r -p "按回车关闭…"
  exit 1
fi
echo "Python: $("$PY" -V 2>&1)"

# ── 2. 起服务（前台）。任何退出方式都走这里的收尾 ──
cleanup() {
  if [ -n "${SRV_PID:-}" ] && kill -0 "$SRV_PID" 2>/dev/null; then
    kill "$SRV_PID" 2>/dev/null
    wait "$SRV_PID" 2>/dev/null
  fi
}
trap cleanup EXIT INT TERM HUP

# -u：不缓冲，起服务过程在终端里看得见
"$PY" -u backend/main.py &
SRV_PID=$!
wait "$SRV_PID"

echo ""
echo "服务已停止。本次运行没有在项目里留下任何文件或进程。"
