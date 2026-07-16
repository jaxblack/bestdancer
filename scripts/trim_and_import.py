#!/usr/bin/env python3
"""Trim each platform's candidates/*.json to top N by (recent_bonus, like).
Then POST /api/import so the dashboard immediately shows them.
"""
import json, datetime, argparse, urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

parser = argparse.ArgumentParser()
parser.add_argument("--week", required=True)
parser.add_argument("--per-platform", type=int, default=20)
parser.add_argument("--recent-days", type=int, default=7)
parser.add_argument("--dashboard", default="http://127.0.0.1:8787")
args = parser.parse_args()

cand_dir = REPO / "assets" / "incoming" / args.week / "candidates"
today = datetime.date.today()

def days_since(iso):
    if not iso: return None
    try: return (today - datetime.date.fromisoformat(iso)).days
    except Exception: return None

def rank(c):
    days = days_since(c.get("published_at"))
    recent = 10_000_000 if (days is not None and days <= args.recent_days) else 0
    return recent + (c.get("like") or 0)

kept_summary = {}
for fp in sorted(cand_dir.glob("*.json")):
    if fp.name.startswith("_"): continue
    platform = fp.stem
    d = json.loads(fp.read_text())
    if platform == "instagram":
        # filter out empties: need at least a non-blank title
        d = [c for c in d if (c.get("title") or "").strip()]
    d.sort(key=rank, reverse=True)
    trimmed = d[:args.per_platform]
    fp.write_text(json.dumps(trimmed, ensure_ascii=False, indent=2))
    recent = sum(1 for c in trimmed if (days_since(c.get("published_at")) or 999) <= args.recent_days)
    top_like = max((c.get("like",0) for c in trimmed), default=0)
    kept_summary[platform] = (len(trimmed), recent, top_like)
    print(f"  {platform:<14} kept {len(trimmed):>2}  (recent {recent:>2}, top ❤{top_like:,})")

# trigger dashboard import
print("\ntriggering /api/import ...")
try:
    req = urllib.request.Request(
        f"{args.dashboard}/api/import",
        data=json.dumps({"week": args.week}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST")
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read())
    cfg = result.get("config", {})
    total = len(cfg.get("this_week_candidates", []))
    print(f"  dashboard config now has {total} candidates for {args.week}")
except Exception as e:
    print(f"  FAILED: {e}")
