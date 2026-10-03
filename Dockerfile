# 微信读书 · AI 阅读评价 —— 容器镜像
#
# 零第三方依赖，所以这里没有 pip install，只装 Python。
# 体积压到 ~150MB 而不是带 Chromium 的 1.5GB。

FROM python:3.12-slim

# 中文书籍的搜索结果与报告全是 UTF-8，容器默认 locale 必须是 UTF-8，
# 否则 demo 数据里的中文在容器内就是乱码
ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8777 \
    HOST=0.0.0.0 \
    WEREREAD_DEPLOY=1

WORKDIR /app

# 只拷代码，不带 .git 和本地垃圾
COPY backend/ ./backend/
COPY frontend/ ./frontend/

# 以非 root 跑：容器逃逸时影响面小一些
RUN useradd -m -u 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8777

# 没有 HEALTHCHECK 用 urllib 而非 curl：slim 镜像里没有 curl。
# 写成单行且不嵌套 f-string：sh -c 里嵌套引号极易踩坑。
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import os,sys,urllib.request; p=os.environ.get('PORT','8777'); sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+p+'/api/health',timeout=4).status==200 else 1)"

CMD ["python", "-u", "backend/main.py"]
