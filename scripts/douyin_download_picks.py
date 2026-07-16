#!/usr/bin/env python3
"""Download Douyin picks via CDP-attached Chrome: navigate, intercept
/aweme/v1/web/aweme/detail/ response, extract play URL, curl.

Writes assets/incoming/<week>/dl2/douyin_<id>.{mp4,json} so rebuild_from_dl2
picks them up alongside TikTok/xhs.
"""
import argparse, json, re, subprocess, sys, time
from pathlib import Path
from playwright.sync_api import sync_playwright

ap = argparse.ArgumentParser()
ap.add_argument("--week", required=True)
args = ap.parse_args()

REPO = Path(__file__).resolve().parents[1]
CFG = REPO / "config" / "weekly" / f"{args.week}.json"
BASE = REPO / "assets" / "incoming" / args.week
DL2 = BASE / "dl2"; DL2.mkdir(parents=True, exist_ok=True)
COOKIES = BASE / "cookies.txt"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")

cfg = json.loads(CFG.read_text(encoding="utf-8"))
by_id = {c["id"]: c for c in cfg.get("this_week_candidates", []) + cfg.get("classics_pool", [])}
picks = [p for p in cfg.get("picks", [])
         if "douyin.com" in by_id.get(p["id"], {}).get("url", "")]
print(f"Douyin picks to fetch: {len(picks)}")

def extract_play_url(detail):
    try:
        aw = detail.get("aweme_detail") or detail.get("item_list", [{}])[0]
        video = aw.get("video", {})
        for key in ("play_addr_h264", "play_addr", "play_addr_lowbr", "play_addr_265"):
            pa = video.get(key)
            if pa and pa.get("url_list"):
                return pa["url_list"][0], aw
        for key in ("h264_url_list", "url_list"):
            if video.get(key):
                return video[key][0], aw
    except Exception as e:
        print("  extract err:", e)
    return None, None

captured = {}  # aweme_id -> (play_url, aweme_meta)

def on_resp(resp):
    u = resp.url
    if "/aweme/v1/web/aweme/detail/" not in u: return
    m = re.search(r"aweme_id=(\d+)", u)
    if not m: return
    aid = m.group(1)
    try: data = resp.json()
    except Exception:
        try: data = json.loads(resp.text())
        except Exception: return
    play, aw = extract_play_url(data)
    if play:
        captured[aid] = (play, aw)
        print(f"  captured {aid}")

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    ctx = browser.contexts[0]
    page = ctx.new_page()
    page.on("response", on_resp)

    for pk in picks:
        cand = by_id[pk["id"]]
        url = cand["url"]
        vid = re.search(r"/video/(\d+)", url).group(1)
        stem = f"douyin_{vid}"
        mp4 = DL2 / f"{stem}.mp4"
        meta_out = DL2 / f"{stem}.json"
        if mp4.exists() and mp4.stat().st_size > 100_000 and meta_out.exists():
            print(f"[skip] {stem}.mp4"); continue

        print(f"\n════ #{pk.get('rank')} {cand.get('creator','')} — {url}")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            print(f"  goto err: {e}")
        for _ in range(24):
            if vid in captured: break
            time.sleep(0.5)
        pair = captured.get(vid)
        if not pair:
            print(f"  ✗ no aweme/detail response for {vid}")
            continue
        play, aw = pair
        if play.startswith("//"): play = "https:" + play

        cmd = ["curl", "-sSL", "--max-time", "180", "-A", UA, "-e", url,
               "-b", str(COOKIES), "-o", str(mp4), play]
        r = subprocess.run(cmd, capture_output=True, text=True)
        size = mp4.stat().st_size if mp4.exists() else 0
        if size < 100_000:
            print(f"  ✗ mp4 too small ({size}B), curl stderr: {r.stderr[:200]}")
            continue

        # extract meta from aweme dict
        author = ((aw.get("author") or {}).get("nickname")
                  or cand.get("creator", "").lstrip("@"))
        desc = aw.get("desc") or cand.get("title", "")
        duration = int((aw.get("video") or {}).get("duration", 0)) // 1000
        stats = aw.get("statistics") or {}
        meta = {
            "id": vid, "platform": "douyin", "url": url,
            "author": author, "title": desc, "desc": desc,
            "duration_sec": duration,
            "like": int(stats.get("digg_count") or cand.get("like") or 0),
            "play": int(stats.get("play_count") or cand.get("play") or 0),
            "dance_type": cand.get("dance_type", "街舞"),
        }
        meta_out.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
        print(f"  ✓ {size//1024}KB, author={author}, duration={duration}s")

print("\n═══ done ═══")
