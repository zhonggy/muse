# 必须钉死 Debian 发行版代号：
# `python:3.12-slim` 是滚动 tag，已经指向 Debian 13 (trixie)，
# 而 Playwright 1.49.1 不认识 trixie，会回退到 ubuntu20.04 的依赖列表，
# 导致 `ttf-unifont` / `ttf-ubuntu-font-family` 装不上、构建失败。
# 换 Playwright 版本时才能动这里。
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    MUSE_DATA_DIR=/app/data

WORKDIR /app

# Playwright 系统依赖（中文字体用于截图里正确渲染中文）
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates fonts-noto-cjk fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt \
    && playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY app ./app
COPY static ./static

RUN mkdir -p /app/data

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
