#!/usr/bin/env bash
# 本地开发启动（不依赖 Docker）
set -euo pipefail
cd "$(dirname "$0")/.."

PY=${PY:-python3}
VENV=.venv

if [ ! -d "$VENV" ]; then
  echo "→ 创建虚拟环境 $VENV"
  $PY -m venv "$VENV"
fi

PYBIN="$VENV/bin/python"
[ -x "$PYBIN" ] || PYBIN="$VENV/Scripts/python.exe"   # Windows (Git Bash)

echo "→ 安装依赖"
"$PYBIN" -m pip install -q -r requirements.txt
"$PYBIN" -m playwright install chromium

[ -f .env ] || { cp .env.example .env; echo "→ 已生成 .env，请按需修改"; }

echo "→ 启动 http://127.0.0.1:${PORT:-8080}"
exec "$PYBIN" -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8080}" --reload
