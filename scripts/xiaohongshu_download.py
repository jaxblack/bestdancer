#!/usr/bin/env python3
"""Download editor-selected Xiaohongshu video notes through the logged-in Chrome session."""
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import sync_playwright

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
        for query in (candidate["title"], "urban dance 编舞", "编舞 完整"):
            search_url = f"https://www.xiaohongshu.com/search_result/?keyword={quote(query)}&type=51"
            page.goto(search_url, wait_until="domcontentloaded", timeout=60000)
            time.sleep(2)
            video_tab = page.get_by_text("视频", exact=True)
            if video_tab.count():
                video_tab.last.click(timeout=10000)
                time.sleep(1.5)
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
        link.click(timeout=15000, force=True)
        page.locator("video").first.wait_for(state="attached", timeout=30000)
        page.locator("video").first.click(position={"x": 4, "y": 4}, timeout=5000)
        deadline = time.monotonic() + 20
        while not captured and time.monotonic() < deadline:
            time.sleep(0.25)
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
            for candidate in selected:
                try:
                    media_url = capture_video_url(page, candidate)
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
                except Exception as error:  # noqa: BLE001
                    failures.append(f"{candidate['id']}: {error}")
                    print(f"failed {candidate['id']}: {error}", flush=True)
        finally:
            page.close()

    config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if failures:
        raise SystemExit("; ".join(failures))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())