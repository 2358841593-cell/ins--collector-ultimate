#!/usr/bin/env zsh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${CDP_PORT:-9330}"
PROFILE_NAME="${INSTAGRAM_PROFILE:-instagram}"
PROFILE_BASE="${PROFILE_BASE:-$ROOT/.secrets/chrome-instagram-profiles}"
PROFILE_DIR="$PROFILE_BASE/$PROFILE_NAME"
INITIAL_URL="${INITIAL_URL:-https://www.instagram.com/}"
CHROME_APP="${CHROME_APP:-/Applications/Google Chrome.app}"
CHROME_BIN="$CHROME_APP/Contents/MacOS/Google Chrome"

if [[ ! "$PORT" =~ '^[0-9]+$' ]] || (( PORT < 1 || PORT > 65535 )); then
  echo "CDP_PORT 必须是 1-65535 的整数" >&2
  exit 2
fi
if [[ ! "$PROFILE_NAME" =~ '^[A-Za-z0-9._-]+$' ]]; then
  echo "INSTAGRAM_PROFILE 只能包含字母、数字、点、下划线和短横线" >&2
  exit 2
fi
if [[ ! -x "$CHROME_BIN" ]]; then
  echo "找不到 Chrome：$CHROME_BIN" >&2
  exit 2
fi

owner_pid="$(lsof -nP -t -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | head -1 || true)"
if [[ -n "$owner_pid" ]]; then
  command_line="$(ps -p "$owner_pid" -o command= 2>/dev/null || true)"
  if [[ "$command_line" == *"--user-data-dir=$PROFILE_DIR"* ]]; then
    echo "Instagram CDP Chrome 已运行：pid=$owner_pid port=$PORT profile=$PROFILE_DIR"
    exit 0
  fi
  echo "端口 $PORT 已被其他进程占用，拒绝连接或覆盖：pid=$owner_pid" >&2
  exit 1
fi

mkdir -p "$PROFILE_DIR"
open -n "$CHROME_APP" --args \
  "--user-data-dir=$PROFILE_DIR" \
  "--remote-debugging-port=$PORT" \
  --site-per-process \
  --no-first-run \
  --no-default-browser-check \
  --new-window \
  "$INITIAL_URL"

for _ in {1..40}; do
  if curl -fsS "http://127.0.0.1:$PORT/json/version" >/dev/null 2>&1; then
    owner_pid="$(lsof -nP -t -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | head -1 || true)"
    command_line="$(ps -p "$owner_pid" -o command= 2>/dev/null || true)"
    if [[ -n "$owner_pid" && "$command_line" == *"--user-data-dir=$PROFILE_DIR"* ]]; then
      echo "Instagram CDP Chrome 已启动：pid=$owner_pid port=$PORT profile=$PROFILE_DIR"
      exit 0
    fi
    echo "端口已响应，但进程 profile 不匹配；拒绝继续" >&2
    exit 1
  fi
  sleep 0.25
done

echo "Chrome 未在 10 秒内开放 CDP 端口 $PORT" >&2
exit 1
