#!/usr/bin/env bash
# 服务器一键部署（Docker Compose）
# 用法： bash scripts/deploy.sh
set -euo pipefail
cd "$(dirname "$0")/.."

# ------------------------------------------------------------------ 检查
if ! command -v docker >/dev/null 2>&1; then
  cat >&2 <<'EOF'
未检测到 docker。先装 Docker（Ubuntu / Debian）：

  curl -fsSL https://get.docker.com | sh
  sudo usermod -aG docker $USER     # 重新登录后生效
EOF
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  cat >&2 <<'EOF'
未检测到 `docker compose`（v2 插件）。
  Ubuntu / Debian:  sudo apt-get install -y docker-compose-plugin
  旧版请把脚本里的 `docker compose` 换成 `docker-compose`
EOF
  exit 1
fi

# ------------------------------------------------------------------ 随机数
# 注意：这些函数要在 `set -euo pipefail` 下安全运行，
# 所以不用 `... | head -c N`（会因 SIGPIPE 让整个脚本挂掉），
# 而是先取全量、再在 shell 里截断，并对长度做校验。

rand_password() {
  local out=""
  if command -v openssl >/dev/null 2>&1; then
    out="$(openssl rand -base64 48 2>/dev/null | tr -dc 'A-Za-z0-9' || true)"
  fi
  if [ "${#out}" -lt 18 ] && [ -r /dev/urandom ]; then
    out="$(head -c 96 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' || true)"
  fi
  # 兜底保证非空、长度足够
  out="${out}x7Qp2Lm9Kd4Vz8Rn6Tb3Wy5Jc1Hf0Gs"
  printf '%s' "${out:0:18}"
}

rand_fernet_key() {
  # Fernet key = urlsafe base64 of 32 random bytes = 44 个字符
  local out=""
  if command -v python3 >/dev/null 2>&1; then
    out="$(python3 -c 'import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())' 2>/dev/null || true)"
  fi
  if [ "${#out}" -ne 44 ] && command -v openssl >/dev/null 2>&1; then
    out="$(openssl rand -base64 32 2>/dev/null | tr '+/' '-_' | tr -d '\n' || true)"
  fi
  if [ "${#out}" -ne 44 ] && [ -r /dev/urandom ]; then
    out="$(head -c 32 /dev/urandom | base64 | tr '+/' '-_' | tr -d '\n' || true)"
  fi
  if [ "${#out}" -ne 44 ] && command -v docker >/dev/null 2>&1; then
    out="$(docker run --rm python:3.12-slim python -c \
      'import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())' 2>/dev/null || true)"
  fi
  printf '%s' "$out"
}

set_env_var() {
  # 跨平台改写 .env 里某个键（GNU sed 与 BSD sed 的 -i 不兼容，用临时文件）
  local key="$1" value="$2" file=".env" tmp
  tmp="$(mktemp)"
  awk -v k="$key" -v v="$value" '
    BEGIN { done = 0 }
    $0 ~ "^"k"=" { print k"="v; done = 1; next }
    { print }
    END { if (!done) print k"="v }
  ' "$file" > "$tmp"
  mv "$tmp" "$file"
}

# ------------------------------------------------------------------ .env
FRESH_ENV=0
if [ ! -f .env ]; then
  cp .env.example .env
  FRESH_ENV=1

  PW="$(rand_password)"
  KEY="$(rand_fernet_key)"
  set_env_var MUSE_CONSOLE_PASSWORD "$PW"
  set_env_var MUSE_SECRET_KEY "$KEY"
  chmod 600 .env 2>/dev/null || true

  if [ "${#KEY}" -ne 44 ]; then
    echo "⚠️  自动生成 MUSE_SECRET_KEY 失败，请手动填写：" >&2
    echo "     python3 -c \"import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())\"" >&2
  fi
fi

# shellcheck disable=SC1091
PORT="$(grep -E '^HOST_PORT=' .env | tail -1 | cut -d= -f2 || true)"
PORT="${PORT:-8080}"
CONSOLE_USER="$(grep -E '^MUSE_CONSOLE_USER=' .env | tail -1 | cut -d= -f2 || true)"
CONSOLE_PASS="$(grep -E '^MUSE_CONSOLE_PASSWORD=' .env | tail -1 | cut -d= -f2 || true)"

# ------------------------------------------------------------------ 构建
mkdir -p data

echo "→ 构建镜像（首次约 3–6 分钟，要下 Chromium）"
docker compose build

echo "→ 启动容器"
docker compose up -d

echo "→ 等待健康检查"
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:${PORT}/healthz" >/dev/null 2>&1; then
    HEALTHY=1
    break
  fi
  sleep 2
done

echo
docker compose ps
echo

IP="$(hostname -I 2>/dev/null | awk '{print $1}' || echo '<服务器IP>')"
echo "==================================================="
echo " 控制台地址： http://${IP}:${PORT}/"
echo " 登录账号：   ${CONSOLE_USER:-admin}"
if [ "$FRESH_ENV" = "1" ]; then
  echo " 登录密码：   ${CONSOLE_PASS}"
  echo " （已写入 .env，chmod 600）"
else
  echo " 登录密码：   见 .env 里的 MUSE_CONSOLE_PASSWORD"
fi
echo "==================================================="
if [ "${HEALTHY:-0}" != "1" ]; then
  echo
  echo "⚠️  健康检查未通过，看一下日志："
  echo "     docker compose logs --tail=80"
  exit 1
fi
echo
echo "下一步：打开控制台 → 「设置」页配 skymail 账号 → 点「测试连接」"
echo "查看日志： docker compose logs -f"
