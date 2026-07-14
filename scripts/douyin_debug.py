#!/usr/bin/env python3
"""Debug: load one Douyin video page, log ALL requests to see what patterns are used."""
import time
from playwright.sync_api import sync_playwright

URL = "https://www.douyin.com/video/7662323978061629861"

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    ctx = browser.contexts[0]
    page = ctx.new_page()
    reqs = []
    def on_req(req):
        reqs.append((req.resource_type, req.method, req.url))
    page.on("request", on_req)
    page.goto(URL, wait_until="domcontentloaded", timeout=45000)
    time.sleep(8)
    page.screenshot(path="/Users/jax/bestdancer/output/douyin_video_debug.png")
    # print media-ish requests
    print(f"total reqs: {len(reqs)}")
    for rt, m, u in reqs:
        if rt in ("media", "xhr", "fetch") or any(k in u for k in ["mp4", "tos", "video", "media", "obj", "aweme"]):
            print(f"[{rt}] {u[:200]}")
    page.close()
