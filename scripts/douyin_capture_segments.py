#!/usr/bin/env python3
"""For a Douyin video where <video> uses blob:/MSE, capture the underlying
media segments by listening to Network responses via CDP.

Reload the page (or ask user to click "replay") — segments are usually
mp4/m4s from *.douyinvod.com or *.zjcdn.com. Concatenate them with ffmpeg.
"""
import argparse, json, re, subprocess, time
from pathlib import Path
from playwright.sync_api import sync_playwright

ap = argparse.ArgumentParser()
ap.add_argument("--week", required=True)
ap.add_argument("--vid", required=True, help="Douyin video id")
ap.add_argument("--seconds", type=int, default=25, help="how long to listen")
args = ap.parse_args()

REPO = Path(__file__).resolve().parents[1]
BASE = REPO / "assets" / "incoming" / args.week
DL2 = BASE / "dl2"; DL2.mkdir(parents=True, exist_ok=True)
TMP = DL2 / f"_segs_{args.vid}"; TMP.mkdir(exist_ok=True)

captured = []  # list of (url, bytes)

def on_resp(resp):
    u = resp.url
    ct = resp.headers.get("content-type", "")
    if not ("video" in ct or ".mp4" in u or ".m4s" in u):
        return
    if "zjcdn" not in u and "douyinvod" not in u and "byteimg" not in u and "amemv" not in u:
        return
    try:
        body = resp.body()
    except Exception:
        return
    print(f"  seg {len(captured):02d}: {len(body)/1024:.0f}KB  {u[:100]}")
    captured.append((u, body))

with sync_playwright() as p:
    b = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    ctx = b.contexts[0]
    target = None
    for pg in ctx.pages:
        if args.vid in (pg.url or ""):
            target = pg; break
    if not target:
        print(f"no open tab has vid {args.vid}"); raise SystemExit(1)
    print(f"attached to: {target.url}")
    target.on("response", on_resp)

    # trigger fresh media requests: reload
    print(f"reloading + listening for {args.seconds}s ...")
    try:
        target.reload(wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        print(f"  reload err (continuing): {e}")
    t0 = time.time()
    while time.time() - t0 < args.seconds:
        time.sleep(0.5)

print(f"\ncaptured {len(captured)} media segments")
if not captured:
    print("nothing captured — the tab may be showing a still frame; try scrolling / clicking play in Chrome first")
    raise SystemExit(2)

# Persist raw segments in order they arrived
for i, (u, body) in enumerate(captured):
    ext = ".mp4" if ".mp4" in u else (".m4s" if ".m4s" in u else ".bin")
    (TMP / f"{i:03d}{ext}").write_bytes(body)

# If a single mp4 was captured (progressive, not MSE) — use it directly
if len(captured) == 1 and ".mp4" in captured[0][0]:
    out = DL2 / f"douyin_{args.vid}.mp4"
    out.write_bytes(captured[0][1])
    print(f"✓ single mp4 saved: {out.name} ({out.stat().st_size//1024}KB)")
else:
    # Concat via ffmpeg (assume DASH-like init+media segments)
    concat = TMP / "list.txt"
    concat.write_text("\n".join(f"file '{p.name}'" for p in sorted(TMP.glob("*"))))
    out = DL2 / f"douyin_{args.vid}.mp4"
    r = subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat), "-c", "copy", str(out)],
        capture_output=True, text=True, cwd=str(TMP))
    if out.exists() and out.stat().st_size > 100_000:
        print(f"✓ concat mp4: {out.name} ({out.stat().st_size//1024}KB)")
    else:
        print(f"✗ ffmpeg concat failed:\n{r.stderr[-500:]}")
