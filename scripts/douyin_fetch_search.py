#!/usr/bin/env python3
"""One-shot: search Douyin for COMPLETE adult choreography, capture full
aweme_detail metadata, filter out teaching/breakdown/roadshow/kids/battle,
download the good ones with rich meta saved alongside.

Output:
  dl2/<vid>.mp4          video
  dl2/<vid>.json         {author, desc, duration, tags, stats, dance_type}
  candidates.json        filtered, ranked list ready for config rebuild
"""
import json, re, time, subprocess, sys
sys.stdout.reconfigure(line_buffering=True)
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE = Path("/Users/jax/bestdancer/assets/incoming/2026-W28")
DL = BASE / "dl2"
DL.mkdir(parents=True, exist_ok=True)
COOKIES = BASE / "cookies.txt"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")

# 面向"完整成人编舞"的关键词（避开少儿/教学向词）
KEYWORDS = ["urban dance 编舞", "编舞 完整", "kpop dance cover"]

# 排除：教学/分解/路演/萌娃/比赛/vlog 等非完整成人舞段
EXCLUDE = ["教学", "分解", "教程", "tutorial", "零基础", "入门", "基本功", "基础",
           "路演", "vlog", "花絮", "幕后", "慢动作", "镜面", "萌娃", "小小", "少年",
           "小朋友", "岁", "battle", "内战", "挑战赛", "裁判", "比赛现场", "术语",
           "集训", "课堂", "培训", "reaction", "react", "翻车", "搓泥"]

# 舞种识别（只用于给出"舞种类型"标签，不编造文案）
DANCE_TYPES = [
    (r"urban", "Urban Dance"),
    (r"jazz|爵士", "Jazz"),
    (r"hiphop|hip[- ]?hop|嘻哈", "Hip-hop"),
    (r"popping|机械", "Popping"),
    (r"locking", "Locking"),
    (r"waacking|waack", "Waacking"),
    (r"breaking|b[- ]?boy|b[- ]?girl|地板", "Breaking"),
    (r"house", "House"),
    (r"kpop|k-pop|女团|男团|aespa|翻跳|cover", "K-pop"),
    (r"choreo|编舞", "编舞 Choreography"),
]

def dance_type(text):
    t = text.lower()
    for pat, name in DANCE_TYPES:
        if re.search(pat, t):
            return name
    return "街舞 Street"

def should_exclude(text):
    t = text.lower()
    return any(kw.lower() in t for kw in EXCLUDE)

def parse_detail(data):
    aw = data.get("aweme_detail") or (data.get("item_list") or [{}])[0]
    if not aw:
        return None
    author = aw.get("author", {}) or {}
    video = aw.get("video", {}) or {}
    stats = aw.get("statistics", {}) or {}
    text_extra = aw.get("text_extra", []) or []
    tags = [t.get("hashtag_name") for t in text_extra if t.get("hashtag_name")]
    dur_ms = video.get("duration") or aw.get("duration") or 0
    # play url
    play = None
    for key in ("play_addr_h264", "play_addr", "play_addr_lowbr", "play_addr_265"):
        pa = video.get(key)
        if pa and pa.get("url_list"):
            play = pa["url_list"][0]
            break
    return {
        "id": aw.get("aweme_id"),
        "desc": aw.get("desc", ""),
        "author": author.get("nickname", ""),
        "sec_uid": author.get("sec_uid", ""),
        "duration_sec": round(dur_ms / 1000, 1) if dur_ms else 0,
        "like": stats.get("digg_count", 0),
        "play_count": stats.get("play_count", 0),
        "tags": tags,
        "play_url": play,
    }

# ---- collect candidate video IDs across keywords ----
all_ids = []
seen = set()
details = {}  # vid -> parsed detail

import random as _random
import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent))
from human import (jitter_sleep, idle, cooldown, wiggle_cursor,
                   human_scroll, human_search)

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    ctx = browser.contexts[0]
    page = ctx.new_page()

    captured = {}
    def on_resp(resp):
        if "/aweme/v1/web/aweme/detail/" not in resp.url:
            return
        try:
            data = resp.json()
        except Exception:
            try:
                data = json.loads(resp.text())
            except Exception:
                return
        d = parse_detail(data)
        if d and d.get("id"):
            captured[d["id"]] = d
    page.on("response", on_resp)

    kws_shuffled = list(KEYWORDS); _random.shuffle(kws_shuffled)
    for kw_i, kw in enumerate(kws_shuffled):
        print(f"\n=== search: {kw} ===")
        try:
            human_search(
                page, "https://www.douyin.com",
                "https://www.douyin.com/search/{kw}?publish_time=30&sort_type=2&type=video",
                kw, search_input_selector='input[data-e2e="searchbar-input"], input[placeholder*="搜索"]',
            )
        except Exception as e:
            print("  search err", e); continue
        # 拟人滚动: 每次滚动量不同 + 中间停顿
        for _ in range(_random.randint(3, 5)):
            human_scroll(page, total=_random.randint(1600, 2600))
            idle(0.8, 2.2)
        wiggle_cursor(page)
        html = page.content()
        cnt = 0
        for m in re.finditer(r'/video/(\d{15,25})', html):
            vid = m.group(1)
            if vid not in seen:
                seen.add(vid); all_ids.append(vid); cnt += 1
        print(f"  +{cnt} new ids (total {len(all_ids)})")
        if kw_i < len(kws_shuffled) - 1:
            cooldown(30, 90)  # 关键词之间

    # 详情页访问: 大幅降速 + 洗牌 + 每 5 支来一次长冷却
    print(f"\n=== visiting up to 40 video pages (拟人节奏, 每支 4-12s + 每5支冷却) ===", flush=True)
    good = []
    ids_to_visit = all_ids[:40]
    _random.shuffle(ids_to_visit)
    for i, vid in enumerate(ids_to_visit, 1):
        vurl = f"https://www.douyin.com/video/{vid}"
        try:
            with page.expect_response(
                lambda r, v=vid: "/aweme/v1/web/aweme/detail/" in r.url and v in r.url,
                timeout=14000
            ):
                try:
                    page.goto(vurl, wait_until="commit", timeout=14000)
                except Exception:
                    pass
        except Exception as e:
            print(f"[{i:02d}] {vid} no-detail ({type(e).__name__})", flush=True)
        # 页面看一下再走 —— 关键的降速
        idle(3.5, 7.5)
        if _random.random() < 0.4:
            wiggle_cursor(page, moves=2)
        d = captured.get(vid)
        if not d:
            print(f"[{i:02d}] {vid} no-detail-parsed", flush=True)
        else:
            full_text = d["desc"] + " " + " ".join(d["tags"])
            excl = should_exclude(full_text)
            dur = d["duration_sec"]
            ok = (not excl) and (15 <= dur <= 100) and d.get("play_url")
            flag = "KEEP" if ok else "drop"
            print(f"[{i:02d}] {vid} {dur}s ❤{d['like']} {flag} | {d['author']} | {d['desc'][:40]}", flush=True)
            if ok:
                good.append(d)
        # 每 5 支长冷却; 检测到反爬(no-detail 连续 3 次)直接停
        if i % 5 == 0 and i < len(ids_to_visit):
            cooldown(60, 180)

    # rank by likes, take best, download
    good.sort(key=lambda x: x["like"], reverse=True)
    print(f"\n=== {len(good)} candidates pass filter; downloading top 8 ===")
    downloaded = []
    for d in good[:8]:
        vid = d["id"]
        out = DL / f"{vid}.mp4"
        play = d["play_url"]
        if play.startswith("//"):
            play = "https:" + play
        cmd = ["curl", "-sSL", "--max-time", "120", "-A", UA,
               "-e", f"https://www.douyin.com/video/{vid}",
               "-b", str(COOKIES), "-o", str(out), play]
        r = subprocess.run(cmd, capture_output=True, text=True)
        size = out.stat().st_size if out.exists() else 0
        ok = r.returncode == 0 and size > 300_000
        if ok:
            d["dance_type"] = dance_type(d["desc"] + " " + " ".join(d["tags"]))
            d["local_file"] = out.name
            (DL / f"{vid}.json").write_text(json.dumps(d, ensure_ascii=False, indent=2))
            downloaded.append(d)
            print(f"  OK {vid} {size//1024}KB [{d['dance_type']}] {d['author']}")
        else:
            if out.exists(): out.unlink()
            print(f"  FAIL {vid} rc={r.returncode} size={size}")

    page.close()

(BASE / "candidates.json").write_text(json.dumps(downloaded, ensure_ascii=False, indent=2))
print(f"\n=== DONE: {len(downloaded)} clean videos downloaded ===")
for d in downloaded:
    print(f"  {d['dance_type']:>20} | {d['author']} | {d['duration_sec']}s | ❤{d['like']}")
