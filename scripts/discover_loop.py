#!/usr/bin/env python3
"""Orchestrator: run discover_universal.py as N independent subprocesses,
one per (platform, keyword) pair, using --append so results accumulate.
Avoids Playwright memory accumulation that killed the single-process run
after ~5 keywords."""
import argparse, json, subprocess, sys, datetime as dt
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

parser = argparse.ArgumentParser()
parser.add_argument("--week", required=True)
parser.add_argument("--platforms", default=None,
                    help="space or pipe separated; defaults to settings.json:platforms")
parser.add_argument("--keywords", default=None,
                    help="pipe separated; defaults to settings.json:keywords")
parser.add_argument("--pool-size", type=int, default=100)
parser.add_argument("--per-keyword", type=int, default=25)
parser.add_argument("--per-run-timeout", type=int, default=120)
args = parser.parse_args()

settings = json.loads((REPO / "admin" / "settings.json").read_text())

def _split(cli, key):
    if cli:
        return [x for x in cli.replace("|", " ").split() if x]
    return list(settings.get(key, []))

platforms = _split(args.platforms, "platforms")
keywords = _split(args.keywords, "keywords")


def keywords_for(platform: str) -> list[str]:
    """按平台取关键词 (settings.json:platform_keywords), 没配就用全局。
    各平台搜索语义差很多: 抖音搜"舞"全是手势舞, TikTok 搜中文几乎没结果。"""
    if args.keywords:
        return keywords
    per = (settings.get("platform_keywords") or {}).get(platform)
    if isinstance(per, list) and per:
        return [str(x).strip() for x in per if str(x).strip()]
    return keywords


print(f"orchestrator: platforms={platforms}", flush=True)
for _p in platforms:
    print(f"  {_p}: {keywords_for(_p)}", flush=True)

cand_dir = REPO / "assets" / "incoming" / args.week / "candidates"
cand_dir.mkdir(parents=True, exist_ok=True)

# reset per-platform files
for p in platforms:
    fp = cand_dir / f"{p}.json"
    if fp.exists(): fp.unlink()

for p in platforms:
    kws = keywords_for(p)
    for i, kw in enumerate(kws):
        print(f"\n════ {p} × {kw} ({i+1}/{len(kws)}) ════", flush=True)
        cmd = [sys.executable, "-u", "scripts/discover_universal.py",
               "--week", args.week, "--platforms", p, "--keywords", kw,
               "--pool-size", str(args.pool_size),
               "--per-keyword", str(args.per_keyword), "--append"]
        try:
            r = subprocess.run(cmd, cwd=str(REPO),
                               capture_output=True, text=True,
                               timeout=args.per_run_timeout)
            out = (r.stdout or "") + (r.stderr or "")
            for line in out.splitlines():
                if any(t in line for t in ["[", "=>", "Error", "Traceback", "failed"]):
                    print("  " + line, flush=True)
            if r.returncode != 0:
                print(f"  (rc={r.returncode})", flush=True)
        except subprocess.TimeoutExpired:
            print(f"  TIMEOUT after {args.per_run_timeout}s", flush=True)

print("\n════ FINAL POOL SIZES ════", flush=True)
today = dt.date.today()
for p in platforms:
    fp = cand_dir / f"{p}.json"
    if not fp.exists():
        print(f"  {p}: (empty)", flush=True); continue
    try:
        d = json.loads(fp.read_text())
    except Exception:
        print(f"  {p}: (parse error)", flush=True); continue
    recent = sum(1 for c in d if c.get("published_at") and
                 (today - dt.date.fromisoformat(c["published_at"])).days <= 7)
    top_like = max((c.get("like", 0) for c in d), default=0)
    print(f"  {p}: {len(d)} total, {recent} within 7d, top ❤{top_like}", flush=True)
