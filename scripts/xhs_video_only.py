#!/usr/bin/env python3
"""Xiaohongshu VIDEO-ONLY discovery + download via logged-in Chrome CDP.

Fixes vs previous approach:
- Fetches the search_result/<id>?xsec_token=... URL (needed for the <video> to
  render on the detail page).
- Only keeps notes flagged as videos on the search grid.
- Detail page waits for real <video>.currentSrc, then downloads via context.request.

Writes assets/incoming/<week>/dl2/xiaohongshu_<id>.mp4 + <id>.json.
"""
from __future__ import annotations
import argparse
import json
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import sync_playwright

REPO = Path(__file__).resolve().parents[1]

parser = argparse.ArgumentParser()
parser.add_argument("--week", required=True)
parser.add_argument("--keywords", default="hiphop 编舞|urban dance 编舞|jazz 编舞|kpop dance cover|choreography|街舞 编舞")
parser.add_argument("--per-keyword", type=int, default=8, help="max notes to keep per keyword")
parser.add_argument("--max-download", type=int, default=20, help="global download cap")
args = parser.parse_args()

WEEK = args.week
BASE = REPO / "assets" / "incoming" / WEEK
DL2 = BASE / "dl2"
DL2.mkdir(parents=True, exist_ok=True)
CAND_DIR = BASE / "candidates"
CAND_DIR.mkdir(parents=True, exist_ok=True)

DANCE = [(r"urban","Urban Dance"),(r"jazz|爵士","Jazz"),
         (r"hiphop|hip[- ]?hop|嘻哈","Hip-hop"),(r"popping|机械","Popping"),
         (r"locking","Locking"),(r"kpop|k-pop|女团|男团|翻跳|cover","K-pop"),
         (r"choreo|编舞","Choreography"),(r"dance|舞","Dance")]

EXCLUDE = re.compile(r"教学|分解|基础|入门|battle|萌娃|儿童|kids?", re.I)

def infer_dance(text: str) -> str:
    t = (text or "").lower()
    for pat, name in DANCE:
        if re.search(pat, t):
            return name
    return "Street"

def note_id(url: str) -> str:
    m = re.search(r"/(?:explore|search_result)/([0-9a-f]+)", url, re.I)
    return m.group(1) if m else ""

# ── discover ────────────────────────────────────────────
def discover_keyword(page, kw: str, per_keyword: int) -> list[dict]:
    url = f"https://www.xiaohongshu.com/search_result/?keyword={quote(kw)}&type=51"
    page.goto(url, timeout=45000, wait_until="domcontentloaded")
    time.sleep(3)
    # click 视频 filter
    try:
        loc = page.get_by_text("视频", exact=True)
        if loc.count():
            loc.last.click()
            time.sleep(2.5)
    except Exception:
        pass
    # scroll to load more
    for _ in range(2):
        page.mouse.wheel(0, 1500)
        time.sleep(1.2)
    # pull all anchor cards with is-video detection
    cards = page.locator("a").evaluate_all("""anchors => anchors
        .filter(a => a.href.includes('/search_result/') && a.href.includes('xsec_token'))
        .map(a => {
            const card = a.closest('section, div[class*=note], div[class*=Card]') || a.parentElement;
            const html = card ? card.innerHTML : '';
            const hasVideo = html.includes('play-icon') || html.includes('video-mask') || !!card?.querySelector('[class*=play]');
            const text = (card?.innerText || '').slice(0, 200);
            return { href: a.href, hasVideo, text };
        })
        .filter((v,i,arr) => arr.findIndex(x=>x.href===v.href)===i)
    """)
    kept = []
    for c in cards:
        if not c["hasVideo"]:
            continue
        text = c["text"]
        if EXCLUDE.search(text):
            continue
        lines = [x.strip() for x in text.splitlines() if x.strip()]
        # parse: title / author / date / likes (approx)
        title = lines[0] if lines else ""
        author = lines[1] if len(lines) > 1 else "unknown"
        like = 0
        for ln in lines:
            m = re.match(r"^([\d.]+)\s*[wW]?$", ln)
            if m:
                v = float(m.group(1))
                like = int(v * (10000 if "w" in ln.lower() else 1))
                break
        vid = note_id(c["href"])
        if not vid:
            continue
        kept.append({
            "id": vid, "url": c["href"], "title": title[:200],
            "author": author, "like": like, "source_desc": text[:400],
            "keyword": kw,
        })
    # dedupe by id, sort by like desc
    seen = {}
    for k in kept:
        if k["id"] not in seen or k["like"] > seen[k["id"]]["like"]:
            seen[k["id"]] = k
    result = sorted(seen.values(), key=lambda x: x["like"], reverse=True)[:per_keyword]
    return result

# ── detail-page video capture ───────────────────────────
def capture_video_stream(page, tokened_url: str) -> str:
    urls: list[str] = []
    def obs(r):
        u = r.url
        if any(k in u for k in [".mp4", "sns-video", "video/tos", "video_mp4"]) and u not in urls:
            urls.append(u)
    page.on("response", obs)
    try:
        page.goto(tokened_url, timeout=45000, wait_until="domcontentloaded")
        # give the SPA time to instantiate the player
        for _ in range(20):
            time.sleep(0.6)
            if page.locator("video").count():
                break
        # trigger playback via JS
        try:
            page.evaluate("""() => {
                const v = document.querySelector('video');
                if (v) { v.muted = true; try{ v.play(); }catch(e){} }
            }""")
        except Exception:
            pass
        # wait up to 15s for stream url
        for _ in range(30):
            time.sleep(0.5)
            if urls:
                break
        # fallback: read v.currentSrc
        if not urls:
            src = ""
            try:
                src = page.evaluate("""() => {
                    const v = document.querySelector('video');
                    return v ? (v.currentSrc || v.src || '') : '';
                }""") or ""
            except Exception:
                pass
            if src.startswith("http"):
                urls.append(src)
    finally:
        page.remove_listener("response", obs)
    if not urls:
        raise RuntimeError("no video stream captured")
    urls.sort(key=lambda u: (".mp4" in u, "sns-video" in u), reverse=True)
    return urls[0]

def probe_duration(mp4: Path) -> int:
    try:
        r = subprocess.run(["ffprobe","-v","error","-show_entries","format=duration",
                            "-of","default=nokey=1:noprint_wrappers=1", str(mp4)],
                           capture_output=True, text=True, timeout=15)
        return int(float(r.stdout.strip() or 0))
    except Exception:
        return 0

# ── main ────────────────────────────────────────────────
keywords = [k.strip() for k in args.keywords.split("|") if k.strip()]
all_cands: list[dict] = []
with sync_playwright() as p:
    b = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    ctx = b.contexts[0]
    page = ctx.new_page()
    for kw in keywords:
        try:
            got = discover_keyword(page, kw, args.per_keyword)
            print(f"[discover] {kw!r} -> {len(got)} video notes", flush=True)
            all_cands.extend(got)
        except Exception as e:
            print(f"[discover] {kw!r} failed: {e}", flush=True)

    # dedupe globally by id, keep highest like
    seen: dict[str, dict] = {}
    for c in all_cands:
        if c["id"] not in seen or c["like"] > seen[c["id"]]["like"]:
            seen[c["id"]] = c
    ranked = sorted(seen.values(), key=lambda x: x["like"], reverse=True)
    (CAND_DIR / "xiaohongshu.json").write_text(
        json.dumps(ranked, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"=== total unique video candidates: {len(ranked)}; downloading up to {args.max_download} ===",
          flush=True)

    downloaded = 0
    for i, c in enumerate(ranked):
        if downloaded >= args.max_download:
            break
        vid = c["id"]
        mp4_path = DL2 / f"xiaohongshu_{vid}.mp4"
        json_path = DL2 / f"xiaohongshu_{vid}.json"
        if mp4_path.exists() and json_path.exists():
            print(f"[dl {i+1}] {vid} exists, skip", flush=True)
            downloaded += 1
            continue
        print(f"[dl {i+1}/{len(ranked)}] {vid} ❤{c['like']} {c['author'][:20]} | {c['title'][:40]}", flush=True)
        try:
            stream_url = capture_video_stream(page, c["url"])
            resp = ctx.request.get(stream_url, timeout=120000)
            if not resp.ok:
                print(f"  fetch HTTP {resp.status}", flush=True); continue
            body = resp.body()
            if len(body) < 100_000:
                print(f"  too small {len(body)}B", flush=True); continue
            mp4_path.write_bytes(body)
            dur = probe_duration(mp4_path)
            meta = {
                "id": vid, "platform": "xiaohongshu", "source": "小红书",
                "desc": c["title"][:500], "author": c["author"],
                "duration_sec": dur, "like": c["like"], "play_count": 0,
                "tags": [], "play_url": c["url"], "url": c["url"],
                "dance_type": infer_dance(c["title"]),
            }
            json_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  OK {mp4_path.stat().st_size//1024}KB [{meta['dance_type']}] {dur}s", flush=True)
            downloaded += 1
        except Exception as e:
            print(f"  ERR {e.__class__.__name__}: {str(e)[:120]}", flush=True)
    page.close()

print(f"=== done: {downloaded} downloaded ===")
