#!/usr/bin/env python3
"""Search Douyin for 街舞, filter last week, sort by likes, extract top video URLs."""
import json, re, time, sys
from pathlib import Path
from playwright.sync_api import sync_playwright

OUT_DIR = Path("/Users/jax/bestdancer/assets/incoming/2026-W29")
OUT_DIR.mkdir(parents=True, exist_ok=True)
URLS_FILE = OUT_DIR / "urls.txt"
META_FILE = OUT_DIR / "scrape_meta.json"
SHOT = Path("/Users/jax/bestdancer/output/douyin_search.png")

# publish_time: 1=今天 7=一周内 30=一月内; sort_type: 0=综合 1=最新 2=最多点赞
SEARCH_URL = "https://www.douyin.com/search/街舞?publish_time=7&sort_type=2&source=switch_tab&type=video"

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    ctx = browser.contexts[0]
    page = ctx.new_page()
    page.goto(SEARCH_URL, wait_until="domcontentloaded", timeout=60000)
    time.sleep(6)
    # scroll to load more
    for i in range(8):
        page.mouse.wheel(0, 3000)
        time.sleep(1.5)
    page.screenshot(path=str(SHOT), full_page=False)
    html = page.content()
    # extract /video/{id} links
    ids = []
    seen = set()
    for m in re.finditer(r'/video/(\d{15,25})', html):
        vid = m.group(1)
        if vid not in seen:
            seen.add(vid)
            ids.append(vid)
    # also try to pull like counts via DOM
    items = []
    try:
        cards = page.locator('a[href*="/video/"]').all()
        for c in cards[:60]:
            href = c.get_attribute('href') or ''
            m = re.search(r'/video/(\d{15,25})', href)
            if not m:
                continue
            vid = m.group(1)
            txt = ''
            try:
                txt = c.inner_text(timeout=200)
            except Exception:
                pass
            items.append({"id": vid, "text": txt[:200]})
    except Exception as e:
        print("dom-extract err:", e, file=sys.stderr)
    urls = [f"https://www.douyin.com/video/{v}" for v in ids[:30]]
    URLS_FILE.write_text("\n".join(urls) + "\n", encoding="utf-8")
    META_FILE.write_text(json.dumps({
        "search_url": SEARCH_URL,
        "url_count": len(urls),
        "top_ids": ids[:30],
        "dom_items_sample": items[:20],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"found": len(ids), "written": len(urls), "urls_file": str(URLS_FILE)}, ensure_ascii=False))
