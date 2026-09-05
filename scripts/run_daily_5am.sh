#!/bin/bash
# BestDancer 每日生产任务：最近 1 天最热候选 -> 三层 evaluation -> 迭代通过 -> 抖音发布
set -euo pipefail

REPO="/Users/jax/bestdancer"
PYTHON="$REPO/.venv/bin/python"
LOG_DIR="$REPO/output/logs"
LOCK_DIR="$REPO/output/.daily-automation.lock"

mkdir -p "$LOG_DIR"
LOG="$LOG_DIR/daily_$(date +%Y-%m-%d).log"
exec >>"$LOG" 2>&1

echo
echo "================================================================"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] BestDancer daily start"

# launchd 的 PATH 很短，显式补上 ffmpeg/yt-dlp/node/deno 所在目录。
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export HOME="/Users/jax"
export PYTHONUNBUFFERED=1
export NO_PROXY="localhost,127.0.0.1,::1"
export no_proxy="$NO_PROXY"

# 本机跨平台访问依赖本地代理；代理没启动时不设置，避免所有请求立即失败。
if /usr/bin/nc -z 127.0.0.1 1087 >/dev/null 2>&1; then
  export http_proxy="http://127.0.0.1:1087"
  export https_proxy="$http_proxy"
  export HTTP_PROXY="$http_proxy"
  export HTTPS_PROXY="$http_proxy"
fi
if /usr/bin/nc -z 127.0.0.1 1086 >/dev/null 2>&1; then
  export all_proxy="socks5h://127.0.0.1:1086"
  export ALL_PROXY="$all_proxy"
fi

if [ ! -x "$PYTHON" ]; then
  echo "ERROR: Python venv 不存在: $PYTHON"
  exit 2
fi

# 防止上一次任务还没完成又重入；异常中断留下的死锁会自动识别 PID 并清理。
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  OLD_PID="$(cat "$LOCK_DIR/pid" 2>/dev/null || true)"
  if [[ "$OLD_PID" =~ ^[0-9]+$ ]] && kill -0 "$OLD_PID" 2>/dev/null; then
    echo "SKIP: 上一轮仍在运行 (PID $OLD_PID)"
    exit 0
  fi
  rm -f "$LOCK_DIR/pid"
  rmdir "$LOCK_DIR" 2>/dev/null || true
  mkdir "$LOCK_DIR"
fi
echo "$$" >"$LOCK_DIR/pid"
cleanup() {
  rm -f "$LOCK_DIR/pid"
  rmdir "$LOCK_DIR" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

cd "$REPO"

if [ "${1:-}" = "--dry-run" ]; then
  "$PYTHON" -m py_compile \
    scripts/auto_episode.py \
    scripts/discover_loop.py \
    scripts/discover_universal.py \
    pipeline/evaluate_discovery.py \
    pipeline/evaluate_segments.py \
    pipeline/evaluate_demo.py \
    pipeline/render_demo.py \
    scripts/upload_to_douyin.py
  "$PYTHON" - <<'PY'
import importlib.util
spec = importlib.util.spec_from_file_location("auto", "scripts/auto_episode.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
week, edition = mod.calendar_target()
print(f"DRY RUN OK: next target={week}-{edition}, window=1 day, strict_recent=true")
PY
  exit 0
fi

# caffeinate 保证长时间的发现/下载/渲染不会因空闲睡眠中断。
/usr/bin/caffeinate -i "$PYTHON" -u scripts/auto_episode.py \
  --calendar-target \
  --recent-days 1 \
  --strict-recent \
  --discover-timeout 420 \
  --max-attempts 5 \
  --publish

echo "[$(date '+%Y-%m-%d %H:%M:%S')] BestDancer daily done"
