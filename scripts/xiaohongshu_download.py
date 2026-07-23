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
    # 兼容 /explore/<id> 和 /search_result/<id> 两种 URL
    match = re.search(r"/(?:explore|search_result)/([0-9a-f]+)", url, re.I)
    return match.group(1) if match else ""


def capture_video_url(page, candidate: dict) -> str:
    target_note = note_id(candidate.get("url", ""))
    if not target_note:
        raise ValueError("候选链接不是小红书笔记")
    original_url = candidate.get("url", "")
    captured: list[str] = []

    def observe(response) -> None:
        url = response.url
        if "sns-video" in url and ".mp4" in url and url not in captured:
            captured.append(url)

    page.on("response", observe)
    try:
        # 拟人化: 先在探索页停留 (打开APP), 再直接从候选 URL 打开这条笔记.
        # 走搜索找目标太不稳定 (搜出来常常没有目标笔记), 且高频搜索反而更像 bot.
        # 真人常见操作: 收到链接 -> 打开 -> 看视频.
        try:
            page.goto("https://www.xiaohongshu.com/explore",
                      wait_until="domcontentloaded", timeout=45000)
            idle(2.5, 5.0)
            for _ in range(random.randint(1, 2)):
                human_scroll(page, total=random.randint(600, 1400))
                idle(1.0, 2.5)
            wiggle_cursor(page, moves=2)
        except Exception:
            pass
        # 打开笔记页. 优先用 /explore/<id> 格式 (笔记详情页, 直接有 video),
        # 避免 /search_result/<id> 落到搜索页.
        note_url = original_url
        if "/search_result/" in note_url:
            note_url = note_url.replace("/search_result/", "/explore/")
        page.goto(note_url, wait_until="domcontentloaded", timeout=45000)
        idle(2.5, 5.5)  # 看画面
        # 找 video (笔记也可能是搜索结果页里嵌入了; 若还是搜索结果, 点进第一条)
        video_loc = page.locator("video").first
        try:
            video_loc.wait_for(state="attached", timeout=15000)
        except Exception:
            # 兜底: 若还是搜索结果页, 尝试点这条笔记
            try:
                link = page.locator(f'a[href*="/explore/{target_note}"]').first
                if link.count() == 0:
                    link = page.locator(f'a[href*="{target_note}"]').first
                link.click(timeout=8000, force=True)
                idle(2.0, 4.0)
                video_loc.wait_for(state="attached", timeout=20000)
            except Exception:
                raise ValueError("笔记页未出现 video 元素")
        idle(1.5, 3.5)
        # 优先: 直接从 <video src> 属性读 mp4 URL (笔记加载后就有, 无需 click).
        # 这比模拟播放器 click 更拟人 (真人打开笔记就已经看到视频了) 且更稳.
        try:
            src = video_loc.get_attribute("src", timeout=3000) or ""
            if src.endswith(".mp4") or ("sns-video" in src and ".mp4" in src):
                return src
        except Exception:
            pass
        # 兜底: click 播放器触发媒体请求 (force 避免叠层拦截)
        try:
            video_loc.click(position={"x": 4, "y": 4}, timeout=5000, force=True)
        except Exception:
            pass
        deadline = time.monotonic() + 25
        while not captured and time.monotonic() < deadline:
            time.sleep(random.uniform(0.2, 0.5))
        if captured:
            return captured[-1]
        # 最后再试一次读 src
        try:
            src = video_loc.get_attribute("src", timeout=2000) or ""
            if ".mp4" in src:
                return src
        except Exception:
            pass
        raise ValueError("已打开笔记, 但未捕获到 MP4 媒体请求")
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