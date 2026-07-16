#!/usr/bin/env python3
"""Universal cross-platform dance video harvester via logged-in CDP Chrome.

For each platform, hits the native search page, pulls up to `--pool-size` VIDEO
cards (100 default), sorts by likes desc, keeps published_at, prefers past
`--recent-days` (7 default) but doesn't hard-drop older, then downloads top N
via platform-specific mp4 fetch:
  - xhs   : playwright <video> stream capture (needs xsec_token URL)
  - tiktok / youtube / instagram / bilibili : yt-dlp with per-platform cookies
  - douyin: opens each video page, grabs playAddr via response interception

Writes:
  candidates/<platform>.json  (full pool, sorted by like desc, with published_at)
  dl2/<platform>_<id>.{mp4,json}  (downloaded top N)
"""
from __future__ import annotations
import argparse, json, re, subprocess, time, datetime as dt
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import sync_playwright

REPO = Path(__file__).resolve().parents[1]

parser = argparse.ArgumentParser()
parser.add_argument("--week", required=True)
parser.add_argument("--platforms", default=None,
                    help="pipe-separated; defaults to admin/settings.json:platforms")
parser.add_argument("--keywords", default=None,
                    help="pipe-separated; defaults to admin/settings.json:keywords")
parser.add_argument("--pool-size", type=int, default=100, help="max candidates per platform kept in candidates/*.json")
parser.add_argument("--per-keyword", type=int, default=25, help="max cards to consider per keyword before dedupe")
parser.add_argument("--recent-days", type=int, default=None,
                    help="prefer this many days; defaults to admin/settings.json:recent_days")
parser.add_argument("--download-per-platform", type=int, default=8, help="how many top videos to actually download per platform")
parser.add_argument("--append", action="store_true", help="append to existing candidates/<platform>.json instead of overwriting")
args = parser.parse_args()

# ── load user-saved settings ──
SETTINGS_PATH = REPO / "admin" / "settings.json"
_settings: dict = {}
if SETTINGS_PATH.exists():
    try:
        _settings = json.loads(SETTINGS_PATH.read_text())
        print(f"Loaded user settings from {SETTINGS_PATH}", flush=True)
    except Exception as e:
        print(f"Warning: cannot parse {SETTINGS_PATH}: {e}", flush=True)

def _resolve_list(cli_val: str | None, key: str, fallback: list[str]) -> list[str]:
    if cli_val:
        return [x.strip() for x in cli_val.split("|") if x.strip()]
    saved = _settings.get(key)
    if isinstance(saved, list) and saved:
        return [str(x).strip() for x in saved if str(x).strip()]
    return fallback

RESOLVED_PLATFORMS = _resolve_list(args.platforms, "platforms",
    ["xiaohongshu","tiktok","youtube","instagram","bilibili","douyin"])
RESOLVED_KEYWORDS = _resolve_list(args.keywords, "keywords",
    ["urban dance choreography","hiphop 编舞","kpop dance cover","jazz 编舞","street dance","choreography"])
if args.recent_days is None:
    args.recent_days = int(_settings.get("recent_days", 7))
print(f"Using platforms={RESOLVED_PLATFORMS}", flush=True)
print(f"Using keywords={RESOLVED_KEYWORDS}", flush=True)
print(f"recent_days={args.recent_days}", flush=True)

WEEK = args.week
BASE = REPO / "assets" / "incoming" / WEEK
DL2 = BASE / "dl2"; DL2.mkdir(parents=True, exist_ok=True)
CAND_DIR = BASE / "candidates"; CAND_DIR.mkdir(parents=True, exist_ok=True)

# ── shared classifiers ──
DANCE = [(r"urban","Urban Dance"),(r"jazz|爵士","Jazz"),
         (r"hiphop|hip[- ]?hop|嘻哈","Hip-hop"),(r"popping|机械","Popping"),
         (r"locking","Locking"),(r"kpop|k-pop|女团|男团|翻跳|cover","K-pop"),
         (r"choreo|编舞","Choreography"),(r"dance|舞","Dance")]
EXCLUDE = re.compile(r"tutorial|lesson|kids?|儿童|教学|分解|基础|入门|battle only", re.I)
NAV = {"综合","用户","视频","直播","照片","For You","Following","Home","Shorts","Subscriptions","Library"}

def infer_dance(text: str) -> str:
    t = (text or "").lower()
    for pat, name in DANCE:
        if re.search(pat, t): return name
    return "Street"

def parse_compact_number(s: str) -> int:
    s = (s or "").strip().replace(",", "")
    m = re.match(r"^([\d.]+)\s*([KkMmWw万])?$", s)
    if not m: return 0
    v = float(m.group(1))
    unit = (m.group(2) or "").lower()
    return int(v * {"k":1_000,"m":1_000_000,"w":10_000,"万":10_000}.get(unit, 1))

def parse_date(s: str) -> str | None:
    """Return YYYY-MM-DD if we can parse, else None. Handles: '3天前', '2小时前',
    '2024-11-16', '5-31', '07-08', 'Jul 15', etc."""
    if not s: return None
    s = s.strip()
    today = dt.date.today()
    m = re.match(r"^(\d+)\s*(天|小时|分钟|day|hour|minute)s?\s*(ago|前)?$", s, re.I)
    if m:
        n = int(m.group(1)); unit = m.group(2)
        if unit in ("天","day"):
            return (today - dt.timedelta(days=n)).isoformat()
        else:
            return today.isoformat()
    if s in ("昨天","yesterday","Yesterday"):
        return (today - dt.timedelta(days=1)).isoformat()
    if s in ("今天","today","Today"):
        return today.isoformat()
    # YYYY-MM-DD
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", s)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    # MM-DD or M-DD  (assume current year)
    m = re.match(r"^(\d{1,2})-(\d{1,2})$", s)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        year = today.year
        # if in the future, assume last year
        try:
            candidate = dt.date(year, month, day)
            if candidate > today: candidate = dt.date(year-1, month, day)
            return candidate.isoformat()
        except ValueError:
            return None
    return None

def days_since(iso: str | None) -> int | None:
    if not iso: return None
    try:
        d = dt.date.fromisoformat(iso)
        return (dt.date.today() - d).days
    except Exception:
        return None

def probe_duration(mp4: Path) -> int:
    try:
        r = subprocess.run(["ffprobe","-v","error","-show_entries","format=duration",
                            "-of","default=nokey=1:noprint_wrappers=1", str(mp4)],
                           capture_output=True, text=True, timeout=15)
        return int(float(r.stdout.strip() or 0))
    except Exception: return 0

# ── per-platform discover ──
def discover_xhs(page, kw: str, per_kw: int) -> list[dict]:
    page.goto(f"https://www.xiaohongshu.com/search_result/?keyword={quote(kw)}&type=51",
              timeout=45000, wait_until="domcontentloaded")
    time.sleep(3)
    try:
        loc = page.get_by_text("视频", exact=True)
        if loc.count(): loc.last.click(); time.sleep(2)
    except Exception: pass
    for _ in range(4):
        page.mouse.wheel(0, 2000); time.sleep(0.9)
    cards = page.locator("a").evaluate_all("""anchors => anchors
        .filter(a => a.href.includes('/search_result/') && a.href.includes('xsec_token'))
        .map(a => {
            const card = a.closest('section') || a.parentElement;
            const html = card ? card.innerHTML : '';
            const hasVideo = html.includes('play-icon') || html.includes('video-mask');
            return { href: a.href, hasVideo, text: (card?.innerText||'').slice(0,300) };
        })
        .filter((v,i,arr) => arr.findIndex(x=>x.href===v.href)===i)
    """)
    out = []
    for c in cards[:per_kw*2]:
        if not c["hasVideo"]: continue
        text = c["text"]
        if EXCLUDE.search(text): continue
        lines = [x.strip() for x in text.splitlines() if x.strip()]
        title = lines[0] if lines else ""
        author = lines[1] if len(lines) > 1 else "unknown"
        # last line is often the like count, second-to-last date
        published = None; like = 0
        for ln in lines:
            if parse_date(ln): published = parse_date(ln)
            n = parse_compact_number(ln)
            if n and n > like: like = n
        vid_m = re.search(r"/search_result/([0-9a-f]+)", c["href"])
        if not vid_m: continue
        out.append({"id": vid_m.group(1), "platform": "xiaohongshu",
                    "url": c["href"], "title": title[:200], "author": author,
                    "like": like, "published_at": published,
                    "source_desc": text[:400], "keyword": kw})
    return out[:per_kw]

def discover_tiktok(page, kw: str, per_kw: int) -> list[dict]:
    page.goto(f"https://www.tiktok.com/search?q={quote(kw)}",
              timeout=40000, wait_until="domcontentloaded")
    time.sleep(5)
    for _ in range(3):
        page.mouse.wheel(0, 2000); time.sleep(1.0)
    cards = page.locator('a[href*="/video/"]').evaluate_all("""anchors => anchors.map(a => {
        const card = a.closest('div[class*=DivItemContainer], article') || a.parentElement;
        return { href: a.href, text: (card?.innerText||'').slice(0,400) };
    }).filter((v,i,arr) => arr.findIndex(x=>x.href===v.href)===i)""")
    out = []
    for c in cards[:per_kw*2]:
        vid_m = re.search(r"/video/(\d+)", c["href"])
        if not vid_m: continue
        creator_m = re.search(r"tiktok\.com/@([^/]+)/", c["href"])
        creator = creator_m.group(1) if creator_m else "unknown"
        lines = [x.strip() for x in c["text"].splitlines() if x.strip()]
        content = [l for l in lines if l not in NAV][:6]
        if EXCLUDE.search(" ".join(content)): continue
        title = content[0] if content else ""
        published = None; like = 0
        for ln in content:
            d = parse_date(ln)
            if d and not published: published = d
            n = parse_compact_number(ln)
            if n > like: like = n
        out.append({"id": vid_m.group(1), "platform": "tiktok",
                    "url": c["href"], "title": title[:200], "author": creator,
                    "like": like, "published_at": published,
                    "source_desc": c["text"][:400], "keyword": kw})
    return out[:per_kw]

def discover_youtube(page, kw: str, per_kw: int) -> list[dict]:
    # sp=EgIIBQ%3D%3D = "this week" filter
    page.goto(f"https://www.youtube.com/results?search_query={quote(kw)}&sp=EgIIBQ%253D%253D",
              timeout=40000, wait_until="domcontentloaded")
    time.sleep(4)
    for _ in range(3):
        page.mouse.wheel(0, 2500); time.sleep(1.0)
    cards = page.locator("ytd-video-renderer, ytd-rich-item-renderer").evaluate_all("""renderers =>
        renderers.map(r => {
            const a = r.querySelector('a#video-title, a#thumbnail');
            const title_el = r.querySelector('#video-title');
            const meta = r.querySelector('#metadata-line');
            const channel = r.querySelector('#channel-name, ytd-channel-name');
            return {
                href: a?.href || '',
                title: title_el?.textContent?.trim() || '',
                meta: meta?.textContent?.trim() || '',
                channel: channel?.textContent?.trim() || '',
            };
        }).filter(r => r.href.includes('/watch?v='))
    """)
    out = []
    for c in cards[:per_kw*2]:
        vid_m = re.search(r"[?&]v=([\w-]+)", c["href"])
        if not vid_m: continue
        text = f"{c['title']} {c['meta']}"
        if EXCLUDE.search(text): continue
        published = None; like = 0
        # views + published in meta_line: "5.4K views • 3 days ago"
        for tok in re.split(r"[•·|]", c["meta"]):
            tok = tok.strip()
            d = parse_date(tok)
            if d and not published: published = d
            m = re.match(r"^([\d.]+)\s*[KkMm]?\s*(views|次观看)?$", tok, re.I)
            if m:
                n = parse_compact_number(tok.split()[0])
                if n > like: like = n
        out.append({"id": vid_m.group(1), "platform": "youtube",
                    "url": f"https://www.youtube.com/watch?v={vid_m.group(1)}",
                    "title": c["title"][:200], "author": c["channel"][:80],
                    "like": like, "published_at": published,
                    "source_desc": text[:400], "keyword": kw})
    return out[:per_kw]

def discover_instagram(page, kw: str, per_kw: int) -> list[dict]:
    page.goto(f"https://www.instagram.com/explore/search/keyword/?q={quote(kw)}",
              timeout=40000, wait_until="domcontentloaded")
    time.sleep(5)
    for _ in range(3):
        page.mouse.wheel(0, 2000); time.sleep(1.0)
    cards = page.locator('a[href*="/reel/"], a[href*="/p/"]').evaluate_all("""anchors =>
        anchors.map(a => {
            const img = a.querySelector('img');
            const alt = img?.getAttribute('alt') || '';
            return { href: a.href, alt };
        }).filter((v,i,arr) => arr.findIndex(x=>x.href===v.href)===i)
    """)
    out = []
    for c in cards[:per_kw*2]:
        vid_m = re.search(r"/(reel|p)/([\w-]+)", c["href"])
        if not vid_m: continue
        if EXCLUDE.search(c["alt"]): continue
        out.append({"id": vid_m.group(2), "platform": "instagram",
                    "url": c["href"].split("?")[0],
                    "title": c["alt"][:200], "author": "unknown",
                    "like": 0, "published_at": None,
                    "source_desc": c["alt"][:400], "keyword": kw})
    return out[:per_kw]

def discover_bilibili(page, kw: str, per_kw: int) -> list[dict]:
    # &order=click 按播放, &order=stow 按收藏, &order=pubdate 按发布, &duration=1 短
    page.goto(f"https://search.bilibili.com/video?keyword={quote(kw)}&order=click",
              timeout=40000, wait_until="domcontentloaded")
    time.sleep(4)
    for _ in range(3):
        page.mouse.wheel(0, 2000); time.sleep(0.8)
    cards = page.locator(".video-list-item, .bili-video-card").evaluate_all("""items =>
        items.map(it => {
            const a = it.querySelector('a[href*="/video/BV"]');
            return {
                href: a?.href || '',
                text: (it.innerText || '').slice(0, 400),
            };
        }).filter((v,i,arr) => v.href && arr.findIndex(x=>x.href===v.href)===i)
    """)
    out = []
    for c in cards[:per_kw*2]:
        m = re.search(r"/video/(BV[\w]+)", c["href"])
        if not m: continue
        text = c["text"]
        if EXCLUDE.search(text): continue
        lines = [x.strip() for x in text.splitlines() if x.strip()]
        title = lines[0] if lines else ""
        author = lines[-2] if len(lines) >= 2 else "unknown"
        published = None; like = 0
        for ln in lines:
            d = parse_date(ln)
            if d and not published: published = d
            n = parse_compact_number(ln)
            if n > like: like = n
        out.append({"id": m.group(1), "platform": "bilibili",
                    "url": f"https://www.bilibili.com/video/{m.group(1)}",
                    "title": title[:200], "author": author[:80],
                    "like": like, "published_at": published,
                    "source_desc": text[:400], "keyword": kw})
    return out[:per_kw]

def discover_douyin(page, kw: str, per_kw: int) -> list[dict]:
    page.goto(f"https://www.douyin.com/search/{quote(kw)}?type=video",
              timeout=40000, wait_until="domcontentloaded")
    time.sleep(5)
    for _ in range(3):
        page.mouse.wheel(0, 2000); time.sleep(1.0)
    cards = page.locator('a[href*="/video/"]').evaluate_all("""anchors => anchors.map(a => {
        const card = a.closest('li, div[class*=result]') || a.parentElement?.parentElement;
        return { href: a.href, text: (card?.innerText||'').slice(0,400) };
    }).filter((v,i,arr) => arr.findIndex(x=>x.href===v.href)===i)""")
    out = []
    for c in cards[:per_kw*2]:
        vid_m = re.search(r"/video/(\d+)", c["href"])
        if not vid_m: continue
        text = c["text"]
        if EXCLUDE.search(text): continue
        lines = [x.strip() for x in text.splitlines() if x.strip()]
        # Douyin card layout: [duration MM:SS, like_count, title, @author, relative_date]
        # Filter out the leading "MM:SS" duration and pure numeric like lines.
        duration_re = re.compile(r"^\d{1,2}:\d{2}$")
        title = ""; author = "unknown"; like = 0; published = None
        for ln in lines:
            if duration_re.match(ln): continue
            if not title and ln.startswith("@"):
                # weird edge, author before title
                author = ln.lstrip("@")[:80]; continue
            if not title and (parse_compact_number(ln) and re.match(r"^[\d.]+[\s万wWkKmM]*$", ln)):
                # pure numeric = like count
                like = max(like, parse_compact_number(ln)); continue
            if not title:
                # first non-duration, non-numeric, non-@ line is the title
                title = ln[:200]; continue
            if ln.startswith("@") and author == "unknown":
                author = ln.lstrip("@")[:80]; continue
            d = parse_date(ln)
            if d and not published: published = d
            # any additional numeric — treat as extra like signal
            n = parse_compact_number(ln)
            if n > like and not ln.startswith("@") and not d: like = n
        if not title:
            continue
        out.append({"id": vid_m.group(1), "platform": "douyin",
                    "url": f"https://www.douyin.com/video/{vid_m.group(1)}",
                    "title": title, "author": author,
                    "like": like, "published_at": published,
                    "source_desc": text[:400], "keyword": kw})
    return out[:per_kw]

DISCOVERERS = {
    "xiaohongshu": discover_xhs, "tiktok": discover_tiktok,
    "youtube": discover_youtube, "instagram": discover_instagram,
    "bilibili": discover_bilibili, "douyin": discover_douyin,
}

# ── main discovery ──
platforms = RESOLVED_PLATFORMS
keywords = RESOLVED_KEYWORDS
print(f"Discovering across {platforms} with {len(keywords)} keywords, pool={args.pool_size}", flush=True)

pools: dict[str, list[dict]] = {p: [] for p in platforms}
with sync_playwright() as p_ctx:
    b = p_ctx.chromium.connect_over_cdp("http://127.0.0.1:9222")
    ctx = b.contexts[0]
    for platform in platforms:
        fn = DISCOVERERS.get(platform)
        if not fn:
            print(f"[{platform}] no discoverer, skip"); continue
        page = ctx.new_page()
        consecutive_fail = 0
        try:
            for kw in keywords:
                try:
                    got = fn(page, kw, args.per_keyword)
                    print(f"[{platform}] {kw!r} -> {len(got)} cards", flush=True)
                    pools[platform].extend(got)
                    if len(got) > 0:
                        consecutive_fail = 0
                    else:
                        consecutive_fail += 1
                except Exception as e:
                    print(f"[{platform}] {kw!r} failed: {e.__class__.__name__}: {str(e)[:80]}", flush=True)
                    consecutive_fail += 1
                if consecutive_fail >= 2:
                    print(f"[{platform}] aborting after {consecutive_fail} consecutive failures (likely verify/captcha)", flush=True)
                    break
        finally:
            try: page.close()
            except Exception: pass

# ── rank + save candidates ──
def rank_key(c):
    days = days_since(c.get("published_at"))
    recency_bonus = 0
    if days is not None and days <= args.recent_days:
        recency_bonus = 10_000_000  # push recent to top
    return (recency_bonus + (c.get("like") or 0))

for platform, items in pools.items():
    seen: dict[str, dict] = {}
    # merge with existing file when --append
    if args.append:
        prev_path = CAND_DIR / f"{platform}.json"
        if prev_path.exists():
            try:
                for c in json.loads(prev_path.read_text()):
                    if c.get("id"): seen[c["id"]] = c
            except Exception: pass
    for c in items:
        if c["id"] not in seen or (c.get("like") or 0) > (seen[c["id"]].get("like") or 0):
            seen[c["id"]] = c
    uniq = sorted(seen.values(), key=rank_key, reverse=True)[:args.pool_size]
    (CAND_DIR / f"{platform}.json").write_text(
        json.dumps(uniq, ensure_ascii=False, indent=2), encoding="utf-8")
    recent = sum(1 for c in uniq if (days_since(c.get("published_at")) or 999) <= args.recent_days)
    print(f"=> {platform}: {len(uniq)} candidates saved  (recent≤{args.recent_days}d: {recent})", flush=True)

# save summary
summary = {"generated_at": dt.datetime.now().isoformat(),
           "platforms": {p: len(pools[p]) for p in platforms},
           "keywords": keywords, "pool_size": args.pool_size,
           "recent_days_preference": args.recent_days}
(CAND_DIR / "_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
print(f"\nCandidates saved. Run download_universal.py --week {WEEK} to fetch media.", flush=True)
