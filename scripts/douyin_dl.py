#!/usr/bin/env python3
"""Load Douyin video pages, intercept aweme/detail JSON, extract play URL, curl download."""
import json, re, time, subprocess, sys
sys.stdout.reconfigure(line_buffering=True)
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE = Path("/Users/jax/bestdancer/assets/incoming/2026-W29")
DL = BASE / "dl"
DL.mkdir(parents=True, exist_ok=True)
COOKIES = BASE / "cookies.txt"

urls = [l.strip() for l in (BASE / "urls_top15.txt").read_text().splitlines() if l.strip()]
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")

def extract_play_url(detail):
    try:
        aw = detail.get("aweme_detail") or detail.get("item_list", [{}])[0]
        video = aw.get("video", {})
        # try h264_url_list, url_list, play_addr
        for key in ("play_addr_h264", "play_addr", "play_addr_lowbr", "play_addr_265"):
            pa = video.get(key)
            if pa and pa.get("url_list"):
                return pa["url_list"][0]
        for key in ("h264_url_list", "url_list"):
            if video.get(key):
                return video[key][0]
    except Exception as e:
        print("extract err:", e)
    return None

results = []

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    ctx = browser.contexts[0]
    page = ctx.new_page()

    captured = {}  # aweme_id -> play_url
    def on_resp(resp):
        u = resp.url
        if "/aweme/v1/web/aweme/detail/" not in u:
            return
        m = re.search(r"aweme_id=(\d+)", u)
        if not m:
            return
        aid = m.group(1)
        try:
            data = resp.json()
        except Exception:
            try:
                txt = resp.text()
                data = json.loads(txt)
            except Exception:
                return
        play = extract_play_url(data)
        if play:
            captured[aid] = play
            print(f"  captured {aid} -> {play[:100]}")
    page.on("response", on_resp)

    for i, url in enumerate(urls, 1):
        vid = re.search(r"/video/(\d+)", url).group(1)
        out = DL / f"d{i:02d}__douyin__{vid}.mp4"
        if out.exists() and out.stat().st_size > 100_000:
            print(f"[{i:02d}] skip {out.name}")
            results.append({"i": i, "id": vid, "status": "skip", "size": out.stat().st_size})
            continue
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            print(f"[{i:02d}] goto err: {e}")
        # wait up to 8s for aweme/detail response
        for _ in range(16):
            if vid in captured:
                break
            time.sleep(0.5)
        play = captured.get(vid)
        if not play:
            print(f"[{i:02d}] {vid} no play url")
            results.append({"i": i, "id": vid, "status": "no_url"})
            continue
        # normalise scheme
        if play.startswith("//"):
            play = "https:" + play
        cmd = ["curl", "-sSL", "--max-time", "120", "-A", UA, "-e", url,
               "-b", str(COOKIES), "-o", str(out), play]
        r = subprocess.run(cmd, capture_output=True, text=True)
        size = out.stat().st_size if out.exists() else 0
        ok = r.returncode == 0 and size > 100_000
        print(f"[{i:02d}] {vid} rc={r.returncode} size={size} {'OK' if ok else 'FAIL'}")
        if not ok and out.exists():
            out.unlink()
        results.append({"i": i, "id": vid, "status": "ok" if ok else "fail",
                        "size": size, "play_url": play})

    page.close()

(BASE / "download_results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
ok = sum(1 for r in results if r.get("status") in ("ok", "skip"))
print(f"\n=== done: {ok}/{len(results)} ok ===")
