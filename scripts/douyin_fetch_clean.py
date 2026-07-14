#!/usr/bin/env python3
"""Discover or download Douyin choreography candidates from the following tab.

Output:
  dl2/<vid>.mp4          video
  dl2/<vid>.json         {author, desc, duration, tags, stats, dance_type}
  candidates.json        filtered, ranked list ready for config rebuild
"""
import argparse
import json, re, time, subprocess, sys
sys.stdout.reconfigure(line_buffering=True)
from pathlib import Path
from urllib.parse import quote

parser = argparse.ArgumentParser(description="Discover or download Douyin choreography candidates")
parser.add_argument("--week", default="2026-W29", help="Target ISO week, e.g. 2026-W30")
parser.add_argument("--keywords", default="urban dance 编舞|编舞 完整|kpop dance cover",
                    help="Pipe-separated search keywords")
parser.add_argument("--mode", choices=("discover", "download"), default="discover")
parser.add_argument("--source", choices=("follow", "search"), default="follow",
                    help="Discover from the followed feed or keyword search")
parser.add_argument("--top", type=int, default=30, help="Number of ranked candidates to retain")
parser.add_argument("--ids", default="", help="Pipe-separated Douyin video IDs selected for download")
args = parser.parse_args()

REPO = Path(__file__).resolve().parents[1]
BASE = REPO / "assets" / "incoming" / args.week
DL = BASE / "dl2"
DL.mkdir(parents=True, exist_ok=True)
COOKIES = BASE / "cookies.txt"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")

# 面向"完整成人编舞"的关键词（避开少儿/教学向词）
KEYWORDS = [keyword.strip() for keyword in args.keywords.split("|") if keyword.strip()]

ADAPTERS = json.loads((REPO / "config" / "platform_adapters.json").read_text(encoding="utf-8"))
DOUYIN_ADAPTER = ADAPTERS["adapters"]["douyin"]

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


def collect_search_ids(page):
    """Use the verified native Douyin search controls and return visible video IDs."""
    filters = DOUYIN_ADAPTER["native_filters"]
    sort = filters["sort"]["default_for_weekly_discovery"]
    published_at = filters["published_at"]["default_for_weekly_discovery"]
    all_ids = []
    seen = set()

    for keyword in KEYWORDS:
        search_url = DOUYIN_ADAPTER["search_entry"].format(keyword=quote(keyword))
        print(f"\n=== searching Douyin: {keyword} ===", flush=True)
        page.goto(search_url, wait_until="domcontentloaded", timeout=60000)
        time.sleep(3)
        body_text = page.locator("body").inner_text()
        if "登录后即可搜索更多精彩视频" in body_text:
            raise RuntimeError("抖音搜索筛选需要先在 Chrome 中登录")
        video_tab = page.get_by_text("视频", exact=True)
        if video_tab.count():
            video_tab.first.click()
            time.sleep(1)
        filter_button = page.get_by_text("筛选", exact=True)
        if not filter_button.count():
            raise RuntimeError("抖音搜索页未找到原生筛选入口")
        filter_button.first.click()
        time.sleep(0.4)
        for label in (sort, published_at):
            option = page.get_by_text(label, exact=True)
            if not option.count():
                raise RuntimeError(f"抖音筛选面板未找到选项: {label}")
            option.last.click()
            time.sleep(0.8)
        for match in re.finditer(r"/video/(\d{15,25})", page.content()):
            video_id = match.group(1)
            if video_id not in seen:
                seen.add(video_id)
                all_ids.append(video_id)
    return all_ids

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

# ---- download manually selected candidates only ----
def download_selected() -> int:
    ranked_path = BASE / "ranked_candidates.json"
    if not ranked_path.exists():
        print("[!] 先运行粗筛，生成 ranked_candidates.json")
        return 1
    wanted = {video_id for video_id in args.ids.split("|") if video_id}
    if not wanted:
        print("[!] 没有收到细筛后的入选视频")
        return 1
    ranked = json.loads(ranked_path.read_text(encoding="utf-8"))
    downloaded = []
    for item in ranked:
        video_id = str(item.get("id", ""))
        if video_id not in wanted:
            continue
        out = DL / f"{video_id}.mp4"
        play = item.get("play_url") or ""
        if play.startswith("//"):
            play = "https:" + play
        if not play:
            print(f"[skip] {video_id} 没有可下载地址")
            continue
        cmd = ["curl", "-sSL", "--max-time", "120", "-A", UA,
               "-e", f"https://www.douyin.com/video/{video_id}", "-b", str(COOKIES), "-o", str(out), play]
        result = subprocess.run(cmd, capture_output=True, text=True)
        size = out.stat().st_size if out.exists() else 0
        if result.returncode == 0 and size > 300_000:
            item["download_status"] = "downloaded"
            item["local_file"] = out.name
            (DL / f"{video_id}.json").write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")
            downloaded.append(item)
            print(f"[ok] {video_id} {size // 1024}KB")
        else:
            item["download_status"] = "failed"
            if out.exists():
                out.unlink()
            print(f"[fail] {video_id} rc={result.returncode} size={size}")
    ranked_path.write_text(json.dumps(ranked, ensure_ascii=False, indent=2), encoding="utf-8")
    (BASE / "downloaded_candidates.json").write_text(json.dumps(downloaded, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"DONE: {len(downloaded)}/{len(wanted)} selected videos downloaded")
    return 0


if args.mode == "download":
    raise SystemExit(download_selected())


from playwright.sync_api import sync_playwright

# ---- collect candidate video IDs from followed accounts ----
all_ids = []
seen = set()

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

    if args.source == "search":
        all_ids = collect_search_ids(page)
        print(f"  found {len(all_ids)} unique videos from keyword search")
    else:
        follow_url = "https://www.douyin.com/follow"
        print("\n=== scanning Douyin following tab ===")
        page.goto(follow_url, wait_until="domcontentloaded", timeout=60000)
        time.sleep(5)
        for _ in range(10):
            page.mouse.wheel(0, 3000)
            time.sleep(1.0)
        for match in re.finditer(r"/video/(\d{15,25})", page.content()):
            video_id = match.group(1)
            if video_id not in seen:
                seen.add(video_id)
                all_ids.append(video_id)
        print(f"  found {len(all_ids)} unique videos from followed accounts")

    visit_count = min(len(all_ids), max(args.top * 3, 40))
    print(f"\n=== visiting {visit_count} video pages to grab metadata ===", flush=True)
    good = []
    for i, vid in enumerate(all_ids[:visit_count], 1):
        vurl = f"https://www.douyin.com/video/{vid}"
        try:
            # navigate + concurrently wait for aweme/detail response
            with page.expect_response(
                lambda r, v=vid: "/aweme/v1/web/aweme/detail/" in r.url and v in r.url,
                timeout=12000
            ):
                try:
                    page.goto(vurl, wait_until="commit", timeout=12000)
                except Exception:
                    pass
        except Exception as e:
            print(f"[{i:02d}] {vid} no-detail ({type(e).__name__})", flush=True)
            continue
        # give handler a beat to parse
        time.sleep(0.4)
        d = captured.get(vid)
        if not d:
            print(f"[{i:02d}] {vid} no-detail-parsed", flush=True)
            continue
        full_text = d["desc"] + " " + " ".join(d["tags"])
        excl = should_exclude(full_text)
        dur = d["duration_sec"]
        keyword_ok = not KEYWORDS or any(keyword.lower() in full_text.lower() for keyword in KEYWORDS)
        ok = (not excl) and keyword_ok and (15 <= dur <= 100)
        flag = "KEEP" if ok else "drop"
        print(f"[{i:02d}] {vid} {dur}s ❤{d['like']} {flag} | {d['author']} | {d['desc'][:40]}", flush=True)
        if ok:
            d["dance_type"] = dance_type(full_text)
            d["download_status"] = "ready" if d.get("play_url") else "unavailable"
            good.append(d)

    # rank by likes, take best, download
    good.sort(key=lambda x: x["like"], reverse=True)
    (BASE / "ranked_candidates.json").write_text(
        json.dumps(good[:args.top], ensure_ascii=False, indent=2), encoding="utf-8"
    )
    page.close()

print(f"\n=== DONE: {min(len(good), args.top)} candidates ready for manual fine filtering ===")
