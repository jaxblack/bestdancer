#!/usr/bin/env python3
"""Fetch the remaining KEEP videos from /tmp/fetch4.log."""
import json, re, subprocess, time, sys
sys.stdout.reconfigure(line_buffering=True) if hasattr(sys.stdout, 'reconfigure') else None
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE = Path("/Users/jax/bestdancer/assets/incoming/2026-W29")
DL = BASE / "dl2"
COOKIES = BASE / "cookies.txt"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")

# Parse KEEP ids from log
log = Path("/tmp/w29_run.log").read_text()
keep_ids = []
for line in log.split("\n"):
    m = re.match(r'^\[\d+\] (\d+) [\d.]+s .* KEEP', line)
    if m:
        vid = m.group(1)
        if not (DL / f"{vid}.mp4").exists():
            keep_ids.append(vid)
print(f"To fetch: {keep_ids}")

DANCE_TYPES = [
    (r"urban", "Urban Dance"),
    (r"jazz|爵士", "Jazz"),
    (r"hiphop|hip[- ]?hop|嘻哈", "Hip-hop"),
    (r"popping|机械", "Popping"),
    (r"locking", "Locking"),
    (r"kpop|k-pop|女团|男团|翻跳|cover", "K-pop"),
    (r"choreo|编舞", "编舞 Choreography"),
]
def dance_type(text):
    t = text.lower()
    for pat, name in DANCE_TYPES:
        if re.search(pat, t):
            return name
    return "街舞 Street"

def extract(data):
    aw = data.get("aweme_detail") or (data.get("item_list") or [{}])[0]
    if not aw: return None
    author = aw.get("author") or {}
    video = aw.get("video") or {}
    stats = aw.get("statistics") or {}
    text_extra = aw.get("text_extra") or []
    play = None
    for k in ("play_addr_h264","play_addr","play_addr_lowbr","play_addr_265"):
        pa = video.get(k)
        if pa and pa.get("url_list"):
            play = pa["url_list"][0]; break
    return {
        "id": aw.get("aweme_id"),
        "desc": aw.get("desc",""),
        "author": author.get("nickname",""),
        "sec_uid": author.get("sec_uid",""),
        "duration_sec": round((video.get("duration") or 0)/1000, 1),
        "like": stats.get("digg_count",0),
        "tags": [t.get("hashtag_name") for t in text_extra if t.get("hashtag_name")],
        "play_url": play,
    }

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    ctx = browser.contexts[0]
    page = ctx.new_page()
    captured = {}
    def on_resp(r):
        if "/aweme/v1/web/aweme/detail/" not in r.url: return
        try: data = r.json()
        except Exception:
            try: data = json.loads(r.text())
            except Exception: return
        d = extract(data)
        if d and d.get("id"): captured[d["id"]] = d
    page.on("response", on_resp)

    for vid in keep_ids:
        vurl = f"https://www.douyin.com/video/{vid}"
        print(f"-> {vid}", flush=True)
        try:
            with page.expect_response(
                lambda r,v=vid: "/aweme/v1/web/aweme/detail/" in r.url and v in r.url,
                timeout=10000):
                try: page.goto(vurl, wait_until="commit", timeout=10000)
                except Exception: pass
        except Exception as e:
            print(f"   no-detail {type(e).__name__}"); continue
        time.sleep(0.3)
        d = captured.get(vid)
        if not d or not d.get("play_url"):
            print("   no play"); continue
        play = d["play_url"]
        if play.startswith("//"): play = "https:" + play
        out = DL / f"{vid}.mp4"
        r = subprocess.run(["curl","-sSL","--max-time","60","-A",UA,"-e",vurl,
                            "-b",str(COOKIES),"-o",str(out),play],
                           capture_output=True, text=True, timeout=90)
        size = out.stat().st_size if out.exists() else 0
        if r.returncode==0 and size>300_000:
            d["dance_type"] = dance_type(d["desc"] + " " + " ".join(d["tags"]))
            d["local_file"] = out.name
            (DL / f"{vid}.json").write_text(json.dumps(d, ensure_ascii=False, indent=2))
            print(f"   OK {size//1024}KB [{d['dance_type']}] {d['author']}")
        else:
            if out.exists(): out.unlink()
            print(f"   FAIL size={size}")

    page.close()
print("done")
