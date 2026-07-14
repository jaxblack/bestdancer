#!/usr/bin/env python3
"""Check Douyin login state on the debug Chrome."""
import json, time
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    ctx = browser.contexts[0]
    # reuse an existing douyin tab if present
    page = None
    for pg in ctx.pages:
        if "douyin.com" in pg.url:
            page = pg
            break
    if page is None:
        page = ctx.new_page()
        page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=60000)
    else:
        try:
            page.reload(wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass
    time.sleep(4)
    html = page.content()
    # "登录" button text in top-right means logged OUT
    has_login_btn = page.locator("text=登录").count()
    logged_in = ("退出登录" in html) or ("我的作品" in html) or ("发消息" in html)
    print(json.dumps({"url": page.url, "login_btn_count": has_login_btn, "logged_in": logged_in}, ensure_ascii=False))
    page.screenshot(path="/Users/jax/bestdancer/output/douyin_check.png")
