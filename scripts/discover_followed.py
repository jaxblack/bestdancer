#!/usr/bin/env python3
"""Discover candidates from selected platform feeds and keyword search pages."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlparse

parser = argparse.ArgumentParser(description="Discover videos from followed-platform feeds")
parser.add_argument("--week", required=True)
parser.add_argument("--platforms", required=True, help="Pipe-separated platform ids")
parser.add_argument("--keywords", default="", help="Pipe-separated keywords")
parser.add_argument("--top", type=int, default=30)
parser.add_argument("--min-likes", type=int, default=0)
parser.add_argument("--videos-only", action="store_true")
parser.add_argument("--recent-days", type=int, default=7)
parser.add_argument("--sort", choices=["heat_desc"], default="heat_desc")
args = parser.parse_args()

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "assets" / "incoming" / args.week / "candidates"
OUT.mkdir(parents=True, exist_ok=True)
platforms = [platform for platform in args.platforms.split("|") if platform]
keywords = [keyword.lower() for keyword in args.keywords.split("|") if keyword]
newest_allowed_date = date.today() - timedelta(days=args.recent_days)

SEARCHES = {
    "xiaohongshu": ("小红书", lambda keyword: f"https://www.xiaohongshu.com/search_result/?keyword={quote(keyword)}&type=51"),
    "instagram": ("Instagram", lambda keyword: f"https://www.instagram.com/explore/search/keyword/?q={quote(keyword)}"),
    "tiktok": ("TikTok", lambda keyword: f"https://www.tiktok.com/search?q={quote(keyword)}"),
}

LINK_MATCHERS = {
    "xiaohongshu": re.compile(r"^/explore/[0-9a-f]+", re.I),
    "instagram": re.compile(r"^/(?:p|reel)/[^/]+", re.I),
    "tiktok": re.compile(r"^/@[^/]+/video/\d+", re.I),
}


def write_candidates(platform: str, items: list[dict]) -> None:
    (OUT / f"{platform}.json").write_text(
        json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


if "douyin" in platforms:
    command = [sys.executable, "scripts/douyin_fetch_clean.py", "--mode", "discover", "--week", args.week,
               "--top", str(args.top), "--keywords", args.keywords]
    print("=== Douyin: https://www.douyin.com/follow ===", flush=True)
    subprocess.run(command, cwd=REPO, check=False)

other_platforms = [platform for platform in platforms if platform in SEARCHES]
if not other_platforms:
    raise SystemExit(0)

from playwright.sync_api import sync_playwright


def compact_number(value: str) -> int:
    match = re.search(r"([\d,.]+)\s*([kmb万]?)", value, re.I)
    if not match:
        return 0
    number = float(match.group(1).replace(",", ""))
    return int(number * {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000, "万": 10_000}.get(match.group(2).lower(), 1))


def parse_published_date(value: str) -> date | None:
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    match = re.fullmatch(r"(\d{1,2})-(\d{1,2})", value)
    if not match:
        return None
    try:
        candidate = date.today().replace(month=int(match.group(1)), day=int(match.group(2)))
    except ValueError:
        return None
    return candidate if candidate <= date.today() else candidate.replace(year=candidate.year - 1)


def extract_detail(page, platform: str, url: str) -> dict:
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    time.sleep(1.5)
    metadata = page.locator("meta").evaluate_all("""metas => Object.fromEntries(
        metas.map(meta => [meta.getAttribute('property') || meta.getAttribute('name'), meta.content]).filter(([key]) => key)
    )""")
    description = metadata.get("og:description") or metadata.get("description") or ""
    title = metadata.get("og:title") or metadata.get("twitter:title") or ""
    likes_match = re.search(r"([\d,.]+\s*[KMBkmb万]?)\s*(?:likes?|赞)", description, re.I)
    published_match = re.search(r"([A-Z][a-z]+\s+\d{1,2},\s+\d{4})", description)
    creator_match = re.search(r"-\s+([\w.]+)[,，]", description) or re.search(r"@([\w.]+)", title)
    is_video = "/reel/" in url or "reel" in title.lower() or page.locator("video").count() > 0
    return {
        "title": description.split('"', 2)[1][:120] if '"' in description else title[:120],
        "source_desc": description[:1000],
        "creator": f"@{creator_match.group(1)}" if creator_match else "",
        "like": compact_number(likes_match.group(1)) if likes_match else 0,
        "is_video": is_video,
        "published_at": parse_published_date(published_match.group(1)) if published_match else None,
    }


def tiktok_cards(page) -> list[dict]:
    video_tab = page.get_by_role("button", name="视频", exact=True)
    if video_tab.count():
        video_tab.click()
        time.sleep(1.5)
    cards = page.locator('a[href*="/video/"]').evaluate_all("""anchors => anchors.map(anchor => {
        const card = anchor.closest('[class*=DivItemContainer], [class*=DivItem], article') || anchor.parentElement?.parentElement?.parentElement;
        return { url: anchor.href, text: (card?.innerText || anchor.innerText || '').trim() };
    })""")
    results = []
    for card in cards:
        lines = [line.strip() for line in card["text"].splitlines() if line.strip()]
        published_at = parse_published_date(lines[-1]) if lines else None
        if len(lines) < 4 or not published_at:
            continue
        results.append({"url": card["url"], "like": compact_number(lines[0]), "title": lines[1][:120],
                        "source_desc": lines[1], "creator": f"@{lines[2]}", "is_video": True,
                        "published_at": published_at})
    return results


def search_candidates(context, platform: str, source: str, search_url) -> list[dict]:
    result_page = context.new_page()
    detail_page = context.new_page()
    found_urls: list[str] = []
    card_by_url: dict[str, dict] = {}
    try:
        for keyword in keywords:
            url = search_url(keyword)
            result_page.goto(url, wait_until="domcontentloaded", timeout=60000)
            time.sleep(3)
            body_text = result_page.locator("body").inner_text()
            if "登录后查看搜索结果" in body_text or "登录以搜索热门内容" in body_text:
                raise RuntimeError(f"{source} 公开搜索页未登录，请在 Chrome 中登录 {urlparse(url).netloc}")
            if platform == "tiktok":
                for card in tiktok_cards(result_page):
                    if card["url"] not in found_urls:
                        found_urls.append(card["url"])
                        card_by_url[card["url"]] = card
                continue
            for _ in range(3):
                result_page.mouse.wheel(0, 1800)
                time.sleep(0.6)
            for href in result_page.locator("a[href]").evaluate_all("anchors => anchors.map(anchor => anchor.href)"):
                path = urlparse(href).path
                if LINK_MATCHERS[platform].match(path) and href not in found_urls:
                    found_urls.append(href)
                if len(found_urls) >= args.top * 3:
                    break
        if not found_urls:
            raise RuntimeError(f"{source} 搜索页未返回可见结果；请确认公开站已登录并能显示搜索卡片")
        items = []
        for href in found_urls:
            if platform == "tiktok":
                details = card_by_url[href]
            else:
                try:
                    details = extract_detail(detail_page, platform, href)
                except Exception as error:  # noqa: BLE001
                    print(f"  skipped {href}: {type(error).__name__}", flush=True)
                    continue
            if args.videos_only and not details["is_video"]:
                continue
            if details["like"] < args.min_likes:
                continue
            if not details["published_at"] or details["published_at"] < newest_allowed_date:
                continue
            items.append({
                "id": f"{platform}-{len(items) + 1}", "source": source, "url": href,
                "title": details["title"] or "待人工补充", "source_desc": details["source_desc"],
                "creator": details["creator"], "like": details["like"], "play_count": 0,
                "duration_sec": 0, "dance_type": "Urban", "download_status": "link_only",
            })
            if len(items) >= args.top:
                break
        return sorted(items, key=lambda item: item["like"], reverse=True)
    finally:
        result_page.close()
        detail_page.close()

with sync_playwright() as playwright:
    browser = playwright.chromium.connect_over_cdp("http://127.0.0.1:9222")
    context = browser.contexts[0]
    for platform in other_platforms:
        source, search_url = SEARCHES[platform]
        print(f"=== {source}: keyword search ===", flush=True)
        try:
            items = search_candidates(context, platform, source, search_url)
            write_candidates(platform, items)
            print(f"  saved {len(items)} filtered candidates", flush=True)
        except Exception as error:  # noqa: BLE001
            write_candidates(platform, [])
            print(f"  failed: {type(error).__name__}: {error}", flush=True)