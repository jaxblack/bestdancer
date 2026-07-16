#!/usr/bin/env python3
"""Grab Douyin picks' <video>.currentSrc from ALREADY-OPEN CDP tabs and curl
them. Won't call page.goto() — user has manually opened the tabs so no
verification challenge is triggered.

Writes assets/incoming/<week>/dl2/douyin_<id>.{mp4,json}
"""
import argparse, json, re, subprocess, sys
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
douyin_picks = [p for p in cfg.get("picks", [])
                if "douyin.com" in by_id.get(p["id"], {}).get("url", "")]
# also include classic_comeback if it's douyin
sp = cfg.get("classic_comeback", {})
if sp.get("id") and "douyin" in by_id.get(sp["id"], {}).get("url", ""):
    douyin_picks.append({"id": sp["id"], "rank": "SP"})
# resolve short links (v.douyin.com/xxxx) to canonical /video/<id>
def _resolve(u):
    if "/video/" in u:
        m = re.search(r"/video/(\d+)", u)
        return m.group(1) if m else None
    if "v.douyin.com" in u:
        try:
            r = subprocess.run(["curl","-sI","-L","-o","/dev/null","-w","%{url_effective}",u],
                               capture_output=True, text=True, timeout=15)
            m = re.search(r"/video/(\d+)", r.stdout or "")
            return m.group(1) if m else None
        except Exception: return None
    return None
want = {}
for p in douyin_picks:
    cand = by_id[p["id"]]
    vid = _resolve(cand.get("url",""))
    if vid: want[vid] = (p, cand)
print(f"want {len(want)} douyin: {list(want.keys())}")

with sync_playwright() as p:
    b = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    ctx = b.contexts[0]
    for pg in list(ctx.pages):
        # Prefer canonical /video/<id> URL over search-modal pages (search page has multiple videos, wrong one leaks)
        m1 = re.search(r"douyin\.com/video/(\d+)", pg.url or "")
        m2 = re.search(r"modal_id=(\d+)", pg.url or "")
        m = m1 if m1 else m2
        if not m or m.group(1) not in want: continue
        vid = m.group(1)
        pk, cand = want[vid]
        stem = f"douyin_{vid}"
        mp4 = DL2 / f"{stem}.mp4"
        meta_out = DL2 / f"{stem}.json"
        if mp4.exists() and mp4.stat().st_size > 100_000 and meta_out.exists():
            print(f"[skip] {stem}"); continue
        try:
            info = pg.evaluate("""() => {
                const v = document.querySelector('video');
                if (!v) return {src:'', duration:0, author:'', desc:''};
                const cs = v.currentSrc;
                const srcs = [...v.querySelectorAll('source')].map(s => s.src);
                const direct = (srcs.find(s => /zjcdn|douyinvod/.test(s))) || cs;
                const authorNode = document.querySelector('a[href*="/user/"] span, .author-name, [class*=userName]');
                const descNode = document.querySelector('[class*=D8h] h1, [class*=title] h1, meta[name=description]');
                return {
                    src: direct, duration: v.duration || 0,
                    author: authorNode ? authorNode.textContent.trim() : '',
                    desc: (descNode && descNode.getAttribute) ? (descNode.getAttribute('content') || descNode.textContent || '').trim() : (descNode ? descNode.textContent.trim() : ''),
                };
            }""")
        except Exception as e:
            print(f"  eval err {vid}: {e}"); continue
        src = info.get("src")
        if not src:
            print(f"  ✗ {vid} no <video> src"); continue
        print(f"\n═══ [{vid}] duration={info.get('duration')}s")
        print(f"    src={src[:120]}")
        cmd = ["curl", "-sSL", "--max-time", "180",
               "-A", UA, "-e", pg.url, "-b", str(COOKIES),
               "-H", "Referer: " + pg.url,
               "-o", str(mp4), src]
        r = subprocess.run(cmd, capture_output=True, text=True)
        size = mp4.stat().st_size if mp4.exists() else 0
        if size < 100_000:
            print(f"    ✗ too small ({size}B) — curl: {r.stderr[:200]}")
            continue
        author = info.get("author") or cand.get("creator", "").lstrip("@")
        desc = info.get("desc") or cand.get("title", "")
        meta = {
            "id": vid, "platform": "douyin", "url": pg.url,
            "author": author, "title": desc, "desc": desc,
            "duration_sec": int(info.get("duration") or 0),
            "like": int(cand.get("like") or 0),
            "play": int(cand.get("play") or 0),
            "dance_type": cand.get("dance_type", "街舞"),
        }
        meta_out.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
        print(f"    ✓ {size//1024}KB, author={author}, desc={desc[:40]}")

print("\n═══ done ═══")
