#!/bin/bash
# 安装/更新 BestDancer 每日任务。Key 进入 macOS Keychain，不进入仓库或 plist。
set -euo pipefail

REPO="/Users/jax/bestdancer"
PLIST_SRC="$REPO/config/com.bestdancer.daily.plist"
PLIST_DEST="$HOME/Library/LaunchAgents/com.bestdancer.daily.plist"
SERVICE="bestdancer-copilot-github-token"
ACCOUNT="${USER:-jax}"
UID_NUM="$(id -u)"

export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

command -v copilot >/dev/null
command -v gh >/dev/null
test -f "$PLIST_SRC"
test -x "$REPO/scripts/run_daily_5am.sh"

# 优先保留已经专门配置的 Copilot token；首次安装时用当前 gh 凭据初始化。
# security 的输出全部丢弃，避免 token 出现在日志。
if ! security find-generic-password \
    -a "$ACCOUNT" -s "$SERVICE" >/dev/null 2>&1; then
  TOKEN="$(gh auth token)"
  # `security -w "$TOKEN"` 会把 secret 暴露在进程参数里。不给 -w 值，让 security
  # 从 stdin 读取两次确认；stdout/stderr 不进入安装日志。
  printf '%s\n%s\n' "$TOKEN" "$TOKEN" | security add-generic-password -U \
    -a "$ACCOUNT" -s "$SERVICE" -w >/dev/null 2>&1
  unset TOKEN
  echo "Copilot token 已存入 macOS Keychain: $SERVICE"
else
  echo "沿用 Keychain 中的 Copilot token: $SERVICE"
fi

mkdir -p "$HOME/Library/LaunchAgents" "$REPO/output/logs"
cp "$PLIST_SRC" "$PLIST_DEST"
plutil -lint "$PLIST_DEST"

launchctl bootout "gui/$UID_NUM/com.bestdancer.daily" >/dev/null 2>&1 || true
launchctl bootstrap "gui/$UID_NUM" "$PLIST_DEST"
launchctl enable "gui/$UID_NUM/com.bestdancer.daily"

"$REPO/scripts/run_daily_5am.sh" --dry-run
echo "已安装: com.bestdancer.daily (每天 05:00)"
