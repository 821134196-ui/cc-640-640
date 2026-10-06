#!/usr/bin/env bash
# 一条本地命令启动：./run.sh
set -euo pipefail
cd "$(dirname "$0")"

export PYTHONPATH="$(pwd):${PYTHONPATH:-}"
export APP_DB="${APP_DB:-$(pwd)/data/app.db}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"

PY=python3
if [ -x ".venv/bin/python" ]; then PY=".venv/bin/python"; fi

echo "启动家政临时取消与空跑费核算系统 ..."
echo "  页面: http://${HOST}:${PORT}/   接口文档: http://${HOST}:${PORT}/docs"
exec "$PY" -m uvicorn app.main:app --host "$HOST" --port "$PORT"
