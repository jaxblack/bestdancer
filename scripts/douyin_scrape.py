#!/usr/bin/env python3
"""Connect to local Chrome via CDP, verify Douyin login, search 街舞, collect top videos."""
import sys, json, time
from playwright.sync_api import sync_playwright

def main():
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.new_page()
        page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=60000)
        time.sleep(5)
        # check login state
        title = page.title()
        url = page.url
        # try to detect logged-in avatar
        logged_in = False
        try:
            # login button presence often indicates logged OUT
            html = page.content()
            logged_in = ("退出登录" in html) or ("我的作品" in html) or ('avatar' in html.lower())
        except Exception as e:
            pass
        print(json.dumps({"title": title, "url": url, "logged_in_guess": logged_in}, ensure_ascii=False))
        # screenshot for manual verification
        page.screenshot(path="/Users/jax/bestdancer/output/douyin_home.png")
        print("SCREENSHOT: /Users/jax/bestdancer/output/douyin_home.png")

if __name__ == "__main__":
    main()
