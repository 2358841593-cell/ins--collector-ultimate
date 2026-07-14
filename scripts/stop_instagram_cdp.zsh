#!/usr/bin/env zsh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${CDP_PORT:-9330}"
PROFILE_NAME="${INSTAGRAM_PROFILE:-instagram}"
PROFILE_BASE="${PROFILE_BASE:-$ROOT/.secrets/chrome-instagram-profiles}"
PROFILE_DIR="$PROFILE_BASE/$PROFILE_NAME"

pid="$(lsof -nP -t -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | head -1 || true)"
if [[ -z "$pid" ]]; then
  echo "端口 $PORT 没有运行中的 CDP Chrome"
  exit 0
fi

command_line="$(ps -p "$pid" -o command= 2>/dev/null || true)"
if [[ "$command_line" != *"/Google Chrome"* || "$command_line" != *"--user-data-dir=$PROFILE_DIR"* ]]; then
  echo "拒绝停止：端口 $PORT 的进程不属于 profile $PROFILE_DIR（pid=$pid）" >&2
  exit 1
fi

echo "停止 Instagram CDP Chrome：pid=$pid port=$PORT profile=$PROFILE_DIR"
kill "$pid"
for _ in {1..20}; do
  if ! kill -0 "$pid" >/dev/null 2>&1; then
    echo "已停止"
    exit 0
  fi
  sleep 0.25
done

command_line="$(ps -p "$pid" -o command= 2>/dev/null || true)"
if [[ "$command_line" == *"/Google Chrome"* && "$command_line" == *"--user-data-dir=$PROFILE_DIR"* ]]; then
  kill -9 "$pid"
  echo "已强制停止"
  exit 0
fi

echo "进程归属已变化，拒绝发送 SIGKILL" >&2
exit 1
