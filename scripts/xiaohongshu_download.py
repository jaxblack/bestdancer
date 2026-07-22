#!/usr/bin/env python3
"""Download editor-selected Xiaohongshu video notes through the logged-in Chrome session."""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import sync_playwright

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
from human import (jitter_sleep, idle, cooldown, wiggle_cursor,
                   human_scroll, human_search, visit_home_first)

REPO = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument("--week", required=True)
parser.add_argument("--ids", required=True, help="Pipe-separated candidate IDs")
args = parser.parse_args()


def note_id(url: str) -> str:
    match = re.search(r"/explore/([0-9a-f]+)", url, re.I)
    return match.group(1) if match else ""


def capture_video_url(page, candidate: dict) -> str:
    target_note = note_id(candidate.get("url", ""))
    if not target_note:
        raise ValueError("候选链接不是小红书笔记")
    captured: list[str] = []

    def observe(response) -> None:
        url = response.url
        if "sns-video" in url and ".mp4" in url and url not in captured:
            captured.append(url)

    page.on("response", observe)
    try:
        link = None
        queries = [candidate["title"], "urban dance 编舞", "编舞 完整"]
        random.shuffle(queries)  # 顺序不固定
        for query in queries:
            # 通过搜索框逐字输入而非 direct search URL
            try:
                human_search(page, "https://www.xiaohongshu.com",
                             "https://www.xiaohongshu.com/search_result/?keyword={kw}&type=51",
                             query, search_input_selector='input[placeholder*="搜索"], input#search-input')
            except Exception:
                page.goto(f"https://www.xiaohongshu.com/search_result/?keyword={quote(query)}&type=51",
                          wait_until="domcontentloaded", timeout=60000)
            idle(2.5, 5.0)  # 搜索出结果先看看
            video_tab = page.get_by_text("视频", exact=True)
            if video_tab.count():
                video_tab.last.click(timeout=10000)
                idle(2.0, 4.0)
            # 拟人化: 页面加载后先滚动几屏看内容, 才去找目标
            for _ in range(random.randint(1, 3)):
                human_scroll(page, total=random.randint(700, 1400))
                idle(1.0, 2.5)
            matches = page.locator(f'a[href*="/explore/{target_note}"]')
            for index in range(matches.count()):
                possible = matches.nth(index)
                if possible.is_visible():
                    link = possible
                    break
            if link is None and matches.count():
                link = matches.last
            if link is not None:
                break
        if link is None:
            raise ValueError("搜索结果未找到原笔记；请在 Chrome 中确认仍可见")
        wiggle_cursor(page, moves=2)
        link.click(timeout=15000, force=True)
        page.locator("video").first.wait_for(state="attached", timeout=30000)
        idle(1.5, 3.5)  # 视频出现后停留 (拟人 "打开一支笔记看两秒")
        page.locator("video").first.click(position={"x": 4, "y": 4}, timeout=5000)
        deadline = time.monotonic() + 25
        while not captured and time.monotonic() < deadline:
            time.sleep(random.uniform(0.2, 0.5))
        if not captured:
            raise ValueError("笔记已打开，但未捕获到 MP4 媒体请求")
        return captured[-1]
    finally:
        page.remove_listener("response", observe)


def main() -> int:
    config_path = REPO / "config" / "weekly" / f"{args.week}.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    wanted = set(filter(None, args.ids.split("|")))
    candidates = config.get("this_week_candidates", []) + config.get("classics_pool", [])
    selected = [item for item in candidates if item.get("id") in wanted]
    if len(selected) != len(wanted):
        raise ValueError("存在未找到的候选 ID")
    if any(item.get("source") != "小红书" for item in selected):
        raise ValueError("xiaohongshu_download.py 只能下载小红书候选")

    output_dir = REPO / "assets" / "incoming" / args.week
    output_dir.mkdir(parents=True, exist_ok=True)
    failures = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp("http://127.0.0.1:9222")
        context = browser.contexts[0]
        page = context.new_page()
        try:
            # 洗牌下载顺序 (拟人 —— 不按候选池排序批量抓)
            selected_shuffled = list(selected)
            random.shuffle(selected_shuffled)
            consecutive_fail = 0
            for idx, candidate in enumerate(selected_shuffled):
                try:
                    media_url = capture_video_url(page, candidate)
                    # 请求响应之间也别秒发, 拟人有个"启动播放器"的间隔
                    idle(1.0, 2.5)
                    response = context.request.get(media_url, timeout=120000)
                    if not response.ok:
                        raise ValueError(f"媒体下载失败: HTTP {response.status}")
                    body = response.body()
                    if len(body) < 100_000:
                        raise ValueError("媒体响应过小，未写入")
                    filename = f"{candidate['id']}__xiaohongshu__.mp4"
                    destination = output_dir / filename
                    destination.write_bytes(body)
                    candidate["local_path"] = str(destination.relative_to(REPO))
                    candidate["download_status"] = "downloaded"
                    print(f"downloaded {candidate['id']} -> {destination.relative_to(REPO)}", flush=True)
                    consecutive_fail = 0
                except Exception as error:  # noqa: BLE001
                    failures.append(f"{candidate['id']}: {error}")
                    print(f"failed {candidate['id']}: {error}", flush=True)
                    consecutive_fail += 1
                    if consecutive_fail >= 3:
                        print("连续 3 支失败, 疑似风控, 长冷却 5-10 分后终止", flush=True)
                        time.sleep(random.uniform(300, 600))
                        break
                # 每支之间冷却 —— xhs 特别保守 60-180s
                if idx < len(selected_shuffled) - 1:
                    cooldown(60, 180)
        finally:
            page.close()

    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if failures:
        raise SystemExit("; ".join(failures))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())