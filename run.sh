#!/usr/bin/env bash
# 一条本地命令启动：家政服务临时取消与空跑费核算系统
# 用法：./run.sh   （默认 http://127.0.0.1:8000 ）
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND="$ROOT/backend"
PORT="${PORT:-8000}"
HOST="${HOST:-127.0.0.1}"

PY="$(command -v python3 || command -v python)"

# 若缺依赖则安装（一次性）。PEP668 系统用 --break-system-packages 兜底。
if ! "$PY" -c "import fastapi, uvicorn, sqlalchemy" >/dev/null 2>&1; then
  echo "[run] 首次运行，安装 Python 依赖..."
  "$PY" -m pip install -r "$BACKEND/requirements.txt" 2>/dev/null \
    || "$PY" -m pip install --break-system-packages -r "$BACKEND/requirements.txt"
fi

mkdir -p "$BACKEND/data"
echo "[run] 启动服务：http://$HOST:$PORT  （前端在根路径 /，API 文档 /docs）"
# 单 worker：应用层事件串行锁与演示时钟为单进程设计
cd "$BACKEND"
exec "$PY" -m uvicorn app.main:app --host "$HOST" --port "$PORT" --workers 1
