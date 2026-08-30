#!/usr/bin/env python3
"""mute_browser.py — 把 CDP 调试 Chrome 里所有页面的声音掐掉。

抓取脚本会在真实浏览器里打开抖音/TikTok 详情页, 这些站点会自动播放并出声,
在旁边干活的人会被吵到。

用法:
    python3 scripts/mute_browser.py --once     # 立刻静音一次现有页面
    python3 scripts/mute_browser.py            # 常驻, 新开的页面也会被静音

治本的做法是启动 Chrome 时就带 --mute-audio (scripts/auto_episode.py 已经这么做),
这个脚本用来处理"Chrome 已经在跑、不想重启"的情况。
"""
from __future__ import annotations

import argparse
import time

from playwright.sync_api import sync_playwright

CDP = "http://127.0.0.1:9222"

# 页面内自带一个定时器, 这样即使播放器之后才插入 <video> 也会被静音
MUTE_JS = """
(() => {
  const mute = () => document.querySelectorAll('video,audio').forEach(m => {
      try { m.muted = true; m.volume = 0; } catch (e) {}
  });
  mute();
  if (!window.__bd_mute_timer) window.__bd_mute_timer = setInterval(mute, 800);
  return document.querySelectorAll('video,audio').length;
})()
"""


def mute_all(browser, verbose: bool) -> int:
    total = 0
    for ctx in browser.contexts:
        for page in list(ctx.pages):
            try:
                total += page.evaluate(MUTE_JS) or 0
            except Exception:
                continue
    if verbose:
        print(f"[mute] 已静音 {total} 个媒体元素", flush=True)
    return total


def main() -> int:
    ap = argparse.ArgumentParser(description="给调试 Chrome 里的页面静音")
    ap.add_argument("--once", action="store_true", help="只跑一次, 不常驻")
    ap.add_argument("--interval", type=float, default=3.0, help="常驻模式的轮询间隔秒数")
    args = ap.parse_args()

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(CDP)
        except Exception as e:
            print(f"[mute] 连不上 {CDP}: {e}")
            return 2
        if args.once:
            mute_all(browser, verbose=True)
            return 0
        print(f"[mute] 常驻静音中 (每 {args.interval}s 扫一遍), Ctrl-C 退出", flush=True)
        while True:
            mute_all(browser, verbose=False)
            time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
