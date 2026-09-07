#!/bin/bash
# BestDancer 每日生产任务：最近 1 天最热候选 -> 三层 evaluation -> 迭代通过 -> 抖音发布
set -euo pipefail

REPO="/Users/jax/bestdancer"
PYTHON="$REPO/.venv/bin/python"
LOG_DIR="$REPO/output/logs"
LOCK_DIR="$REPO/output/.daily-automation.lock"
SOUND_MUTED_BY_JOB=false
ORIGINAL_OUTPUT_VOLUME=""
ORIGINAL_OUTPUT_MUTED=""

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
export BESTDANCER_AI_PROVIDER="copilot"
export BESTDANCER_COPILOT_KEYCHAIN_SERVICE="bestdancer-copilot-github-token"

# 不在仓库/plist 写明文 key。Copilot CLI 官方读取 COPILOT_GITHUB_TOKEN；
# 安装脚本把它放进 macOS Keychain。这里只检查存在性；真正调用 Copilot 时
# codex_client 才读取并只传给 copilot 子进程，ffmpeg/yt-dlp/浏览器不会继承。
if ! security find-generic-password \
    -a jax -s "$BESTDANCER_COPILOT_KEYCHAIN_SERVICE" >/dev/null 2>&1; then
  echo "ERROR: Keychain 缺 bestdancer-copilot-github-token"
  echo "运行 scripts/install_daily_launchd.sh 重新安装"
  exit 2
fi

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
  if [ "$SOUND_MUTED_BY_JOB" = true ]; then
    # 无论成功、失败、SIGINT/SIGTERM 都恢复任务开始前的音量和静音状态。
    /usr/bin/osascript -e \
      "set volume output volume $ORIGINAL_OUTPUT_VOLUME" >/dev/null 2>&1 || true
    if [ "$ORIGINAL_OUTPUT_MUTED" = "true" ]; then
      /usr/bin/osascript -e "set volume with output muted" >/dev/null 2>&1 || true
    else
      /usr/bin/osascript -e "set volume without output muted" >/dev/null 2>&1 || true
    fi
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 系统声音已恢复: volume=$ORIGINAL_OUTPUT_VOLUME muted=$ORIGINAL_OUTPUT_MUTED"
  fi
  rm -f "$LOCK_DIR/pid"
  rmdir "$LOCK_DIR" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

cd "$REPO"

if [ "${1:-}" = "--dry-run" ]; then
  copilot --version
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

# 浏览器虽然也带 --mute-audio，但定时任务可能复用主浏览器页面；生成阶段还会调用
# 多个音视频工具。实际生产开始前直接静音整个 macOS，结束时由 trap 原样恢复。
ORIGINAL_OUTPUT_VOLUME="$(/usr/bin/osascript \
  -e 'output volume of (get volume settings)')"
ORIGINAL_OUTPUT_MUTED="$(/usr/bin/osascript \
  -e 'output muted of (get volume settings)')"
if ! /usr/bin/osascript -e "set volume with output muted" >/dev/null; then
  echo "ERROR: 无法静音 macOS 系统声音，拒绝启动可能出声的自动任务"
  exit 2
fi
SOUND_MUTED_BY_JOB=true
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 系统声音已静音（任务结束后恢复）"

# caffeinate 保证长时间的发现/下载/渲染不会因空闲睡眠中断。
/usr/bin/caffeinate -i "$PYTHON" -u scripts/auto_episode.py \
  --calendar-target \
  --daily-filename \
  --recent-days 1 \
  --strict-recent \
  --discover-timeout 420 \
  --segment-rounds 5 \
  --max-attempts 5 \
  --publish

echo "[$(date '+%Y-%m-%d %H:%M:%S')] BestDancer daily done"
