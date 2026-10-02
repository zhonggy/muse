#!/usr/bin/env bash
# 服务器一键部署（Docker Compose）
set -euo pipefail
cd "$(dirname "$0")/.."

if ! command -v docker >/dev/null 2>&1; then
  echo "未检测到 docker，请先安装 Docker Engine + Compose 插件" >&2
  exit 1
fi

if [ ! -f .env ]; then
  cp .env.example .env
  # 自动生成随机控制台密码与加密密钥
  PW=$(head -c 12 /dev/urandom | base64 | tr -d '/+=' | head -c 16)
  KEY=$(docker run --rm python:3.12-slim python -c \
    "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())")
  sed -i.bak "s|^MUSE_CONSOLE_PASSWORD=.*|MUSE_CONSOLE_PASSWORD=${PW}|" .env
  sed -i.bak "s|^MUSE_SECRET_KEY=.*|MUSE_SECRET_KEY=${KEY}|" .env
  rm -f .env.bak
  echo "==================================================="
  echo " 已生成 .env"
  echo " 控制台账号：admin"
  echo " 控制台密码：${PW}"
  echo " （同时写入 .env，可自行修改）"
  echo "==================================================="
fi

mkdir -p data

docker compose build
docker compose up -d
docker compose ps

PORT=$(grep -E '^HOST_PORT=' .env | cut -d= -f2 || true)
echo
echo "控制台地址： http://<服务器IP>:${PORT:-8080}/"
echo "查看日志：   docker compose logs -f"
