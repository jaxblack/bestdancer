#!/usr/bin/env python3
"""One-shot cross-platform pipeline: discover → download → rebuild config.

Usage:
    python3 scripts/run_weekly.py --week 2026-W29 \\
      --platforms "douyin|tiktok|xiaohongshu|instagram|youtube" \\
      --keywords "urban dance 编舞|hiphop 编舞|choreography"
"""
import argparse
import subprocess
import sys
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--week", required=True)
parser.add_argument("--platforms", default="douyin|tiktok|xiaohongshu|instagram|youtube")
parser.add_argument("--keywords",
                    default="urban dance 编舞|hiphop 编舞|kpop dance cover|jazz 编舞|choreography")
parser.add_argument("--top", type=int, default=15)
parser.add_argument("--backup", type=int, default=8)
parser.add_argument("--recent-days", type=int, default=90)
parser.add_argument("--max-per-platform", type=int, default=4,
                    help="max videos to actually download per non-douyin platform")
parser.add_argument("--skip-discover", action="store_true", help="reuse existing candidates/*.json")
parser.add_argument("--skip-download", action="store_true", help="reuse existing dl2/")
args = parser.parse_args()

REPO = Path(__file__).resolve().parents[1]

def run(step: str, cmd: list[str]) -> None:
    print(f"\n{'='*60}\n[STEP] {step}\n{'='*60}\n$ {' '.join(cmd)}\n", flush=True)
    r = subprocess.run(cmd, cwd=REPO)
    if r.returncode != 0:
        print(f"[!] {step} exited with {r.returncode} — continuing to next step", flush=True)

# 1. discover across platforms (each with hard timeout)
if not args.skip_discover:
    run("discover", [
        sys.executable, "scripts/discover_followed.py",
        "--week", args.week,
        "--platforms", args.platforms,
        "--keywords", args.keywords,
        "--top", str(args.top),
        "--backup-limit", str(args.backup),
        "--videos-only",
        "--recent-days", str(args.recent_days),
    ])

# 2. download non-douyin platforms via yt-dlp
if not args.skip_download:
    non_douyin = [p for p in args.platforms.split("|") if p and p != "douyin"]
    if non_douyin:
        run("cross-platform download", [
            sys.executable, "scripts/download_cross_platform.py",
            "--week", args.week,
            "--platforms", "|".join(non_douyin),
            "--max-per-platform", str(args.max_per_platform),
        ])

# 3. rebuild config from all dl2/*.json (all platforms)
run("rebuild config", [
    sys.executable, "scripts/rebuild_from_dl2.py",
    "--week", args.week,
])

print("\nDone. Review config/weekly/{}.json, then run pipeline/render_demo.py {}".format(args.week, args.week))
