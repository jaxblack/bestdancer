#!/bin/bash
# Run discovery as N independent Python processes (one per platform/keyword pair)
# to avoid the Playwright memory accumulation after ~5 keywords in one process.
set -u
WEEK="${1:-2026-W30-A}"
PLATFORMS="${2:-xiaohongshu tiktok instagram}"
KEYWORDS_FILE="${3:-admin/settings.json}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
source .venv/bin/activate 2>/dev/null || true
unset http_proxy https_proxy all_proxy

# read keywords from settings.json
readarray -t KWS < <(python3 -c "
import json
d = json.load(open('$KEYWORDS_FILE'))
for k in d.get('keywords', []): print(k)
")

mkdir -p "assets/incoming/$WEEK/candidates"

# clear old files so first run overwrites
for p in $PLATFORMS; do
    rm -f "assets/incoming/$WEEK/candidates/$p.json"
done

for p in $PLATFORMS; do
    for kw in "${KWS[@]}"; do
        echo "════ $p × $kw ════"
        python3 -u scripts/discover_universal.py \
            --week "$WEEK" \
            --platforms "$p" \
            --keywords "$kw" \
            --pool-size 100 \
            --per-keyword 25 \
            --append \
            2>&1 | grep -E '^\[|^=>|Error|Traceback' | head -20
    done
done

echo ""
echo "════ FINAL POOL SIZES ════"
for p in $PLATFORMS; do
    f="assets/incoming/$WEEK/candidates/$p.json"
    if [ -f "$f" ]; then
        n=$(python3 -c "import json; d=json.load(open('$f')); print(len(d))")
        r=$(python3 -c "
import json, datetime
d = json.load(open('$f'))
today = datetime.date.today()
recent = sum(1 for c in d if c.get('published_at') and (today - datetime.date.fromisoformat(c['published_at'])).days <= 7)
print(recent)
")
        echo "  $p: $n total, $r within 7 days"
    fi
done
