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

# curl/ca-certificates 给健康检查与下载用；中文字体让截图里中文正常渲染
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates \
        fonts-noto-cjk fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# 一次装齐两种内核：
#   - camoufox（默认）：Firefox 152 反检测内核，靠 `camoufox fetch` 下载
#   - chromium（备选）：`playwright install chromium`
# `install-deps firefox` 装的是 Firefox 运行时依赖，Camoufox 同为 Firefox 构建，通用。
#
# 另外两组非可选依赖：
#   xvfb + mesa —— 无 GPU 的容器里 Firefox 靠它们才能跑起来 WebGL。
#   实测缺了 Mesa 时 `canvas.getContext('webgl')` 直接返回 null，
#   而「没有 WebGL 的浏览器」本身就是极强的机器人特征。
#   Mesa 走 llvmpipe 软渲染，但 Camoufox 会在浏览器层把渲染器串伪造成
#   真实显卡，所以不会因此露馅。
RUN pip install -r requirements.txt \
    && playwright install --with-deps chromium \
    && playwright install-deps firefox \
    && apt-get install -y --no-install-recommends xvfb \
        libgl1-mesa-dri libglx-mesa0 libgl1 libegl1 libgles2 libglu1-mesa \
    && python -m camoufox fetch \
    && rm -rf /var/lib/apt/lists/* /tmp/* /root/.cache/pip

COPY app ./app
COPY static ./static

RUN mkdir -p /app/data

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers"]
