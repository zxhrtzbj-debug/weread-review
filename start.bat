@echo off
REM Windows 启动脚本。等价于 macOS 的 start.command。
REM
REM 刻意不做的事：不建虚拟环境、不 pip install、不写 __pycache__、不常驻。
REM 后端零第三方依赖，HTTP 层/数据模型/HTTP client 全是标准库实现。

setlocal
cd /d "%~dp0"

echo ============================================================
echo   微信读书 · AI 阅读评价
echo ============================================================

REM ── 1. 找 Python 3.10+（py -3 优先，其次试各个版本号）──
set "PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 set "PY=py -3"

if not defined PY (
  for %%V in (3.13 3.12 3.11 3.10 3) do (
    if not defined PY (
      python %%V -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
      if not errorlevel 1 set "PY=python %%V"
    )
  )
)

if not defined PY (
  echo [X] 没找到 Python 3.10+
  echo     去 https://www.python.org/downloads/ 下载安装，
  echo     安装时记得勾选 "Add Python to PATH"。
  echo.
  pause
  exit /b 1
)

for /f "delims=" %%v in ('%PY% -V 2^>^&1') do set "PYVER=%%v"
echo Python: %PYVER%

REM ── 2. 起服务（前台）。Ctrl+C 或关窗口即完全退出 ──
%PY% -u backend\main.py

echo.
echo 服务已停止。本次运行没有在项目里留下任何文件或进程。
endlocal
