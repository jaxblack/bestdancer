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
import argparse, json, re, random, subprocess, time, datetime as dt
from pathlib import Path
from urllib.parse import quote

from playwright.sync_api import sync_playwright

REPO = Path(__file__).resolve().parents[1]
import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent))
from human import (jitter_sleep, idle, cooldown, wiggle_cursor,
                   human_scroll, human_search, wait_for_cards)

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
    ["tiktok","youtube","instagram","bilibili","douyin"])  # xhs 已停用 (2026-07)
RESOLVED_KEYWORDS = _resolve_list(args.keywords, "keywords",
    ["urban dance choreography","hiphop 编舞","kpop dance cover","jazz 编舞","street dance","choreography"])


def keywords_for(platform: str) -> list[str]:
    """按平台取关键词。

    各平台的搜索语义差很多: 抖音搜"舞"会捞回一堆手势舞和资讯号 (实测采集评分里
    街舞匹配度只有 33), 而 TikTok 搜中文词几乎没结果。所以 settings.json 支持
    platform_keywords 覆盖, 没配就退回全局 keywords。命令行 --keywords 优先级最高。
    """
    if args.keywords:
        return RESOLVED_KEYWORDS
    per = (_settings.get("platform_keywords") or {}).get(platform)
    if isinstance(per, list) and per:
        return [str(x).strip() for x in per if str(x).strip()]
    return RESOLVED_KEYWORDS

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
EXCLUDE = re.compile(
    r"tutorial|lesson|kids?|儿童|教学|分解|基础|入门|battle only"
    # 抖音泛关键词("舞"/"dance")会大量捞到这些非编舞内容, 评估器实测把它们
    # 全判成 content_accuracy 不合格(画面是坐着比手势/资讯截图, 却标成 Urban 街舞)
    r"|手势舞|口型|对口型|资讯|新闻|预告|花絮|直播回放|抽奖|带货|穿搭",
    re.I)
NAV = {"综合","用户","视频","直播","照片","For You","Following","Home","Shorts","Subscriptions","Library"}

def infer_dance(text: str) -> str:
    t = (text or "").lower()
    for pat, name in DANCE:
        if re.search(pat, t): return name
    return "Street"


def search_and_wait(page, label: str, home_url: str, search_url_tpl: str, kw: str,
                    card_selector: str, input_selector: str, timeout_s: float = 30.0,
                    require_in_url: str | None = None) -> int:
    """拟人搜索 → 等结果渲染 → 渲染不出来就回退到规范搜索 URL 再等一次。

    三个实测坑:
      1. 结果懒加载 (抖音 ~12s), 之前 idle 几秒就抓 DOM, 常年 0 条被误判为风控;
      2. 从首页搜索框回车, 抖音会落到 /jingxuan/search/<kw> 这个另一套版式,
         里面根本没有 a[href*="/video/"], 必须回退到 /search/<kw>?type=video;
      3. 有些平台的筛选条件写在 URL 参数里 (YouTube 的 sp=「本周内」)。从搜索框
         打字回车会落到**不带该参数**的普通结果页, 筛选静默失效 —— 搜回来全是几年前
         的视频。require_in_url 指定必须出现在最终 URL 里的标记, 缺了就强制跳转。
    """
    human_search(page, home_url, search_url_tpl, kw, search_input_selector=input_selector)
    n = wait_for_cards(page, card_selector, timeout_s=timeout_s)
    need_redirect = n == 0 or (require_in_url and require_in_url not in (page.url or ""))
    if need_redirect:
        why = "无结果" if n == 0 else f"URL 缺少 {require_in_url} (筛选没生效)"
        try:
            page.goto(search_url_tpl.format(kw=quote(kw)),
                      wait_until="domcontentloaded", timeout=45_000)
        except Exception:
            pass
        n = wait_for_cards(page, card_selector, timeout_s=timeout_s)
        print(f"[{label}] {kw!r} 首页搜索{why}, 改走规范搜索 URL -> {n}", flush=True)
    else:
        print(f"[{label}] {kw!r} results rendered: {n}", flush=True)
    return n

def parse_compact_number(s: str) -> int:
    s = (s or "").strip().replace(",", "")
    # 容忍量词后缀: "42万次观看" / "1.2K views" / "3.4亿播放"
    s = re.sub(r"\s*(次观看|次播放|观看|播放|views?|plays?|likes?|个赞)\s*$", "", s, flags=re.I)
    m = re.match(r"^([\d.]+)\s*([KkMmBbWw万亿千])?$", s.strip())
    if not m: return 0
    v = float(m.group(1))
    unit = (m.group(2) or "").lower()
    return int(v * {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000,
                    "w": 10_000, "万": 10_000, "亿": 100_000_000, "千": 1_000}.get(unit, 1))

def parse_date(s: str) -> str | None:
    """Return YYYY-MM-DD if we can parse, else None. Handles: '3天前', '2小时前',
    '3周前', '7个月前', '1年前', '2024-11-16', '5-31', '07-08', 'Jul 15', etc."""
    if not s: return None
    s = s.strip()
    today = dt.date.today()
    # 相对时间。YouTube 中文界面用"周前/个月前/年前", 之前完全没覆盖 ->
    # published_at 恒为 None, 时效维度直接瞎掉 (采集评分实测 youtube 日期解析率 0%)
    m = re.match(r"^(\d+)\s*(个?月|周|星期|天|小时|分钟|"
                 r"month|week|day|hour|minute|year|年)s?\s*(ago|前)?$", s, re.I)
    if m:
        n = int(m.group(1)); unit = m.group(2).lower()
        if unit in ("天", "day"):
            return (today - dt.timedelta(days=n)).isoformat()
        if unit in ("周", "星期", "week"):
            return (today - dt.timedelta(weeks=n)).isoformat()
        if unit in ("月", "个月", "month"):
            return (today - dt.timedelta(days=30 * n)).isoformat()
        if unit in ("年", "year"):
            return (today - dt.timedelta(days=365 * n)).isoformat()
        return today.isoformat()   # 小时/分钟级 = 今天
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
    # 小红书反爬升级得快 —— 比其他平台更保守:
    # (1) 每次搜索前先在首页/发现页停留浏览 (拟人 "打开APP随便刷刷")
    # (2) 慢速滚动次数少 (不贪多), 结束再逛几下探索页
    try:
        page.goto("https://www.xiaohongshu.com/explore", timeout=30000, wait_until="domcontentloaded")
        idle(3.5, 7.0)  # 打开落地页停留
        # 探索页刷几屏 (不涉及搜索, 纯浏览行为)
        for _ in range(random.randint(1, 3)):
            human_scroll(page, total=random.randint(800, 1600))
            idle(1.5, 4.0)
        wiggle_cursor(page, moves=random.randint(2, 4))
    except Exception:
        pass
    human_search(page, "https://www.xiaohongshu.com",
                 "https://www.xiaohongshu.com/search_result/?keyword={kw}&type=51",
                 kw, search_input_selector='input[placeholder*="搜索"], input#search-input')
    idle(2.0, 4.5)  # 搜索出结果后先看一会儿
    try:
        loc = page.get_by_text("视频", exact=True)
        if loc.count():
            loc.last.click(); idle(2.0, 4.5)
    except Exception: pass
    # 滚动次数少而慢, 每次拉动量小
    for _ in range(random.randint(2, 4)):
        human_scroll(page, total=random.randint(900, 1600))
        idle(1.2, 3.2)
    wiggle_cursor(page, moves=random.randint(2, 3))
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
    """TikTok 走**接口拦截**: /api/search/item/full/ 里有 createTime / stats.diggCount /
    author.uniqueId / desc, 比爬卡片文本准得多。

    爬 DOM 的老做法在中文界面下卡片里根本没有日期文本, 发布时间只能靠猜 ——
    采集评分里 tiktok 时效只有 24 分 (中位 44 天), 拿不到准确日期就没法按时效排序。
    """
    blobs: list[str] = []

    def on_response(r):
        try:
            if "/api/search/item/full" in r.url or "/api/search/general/full" in r.url:
                blobs.append(r.text())
        except Exception:
            pass

    page.on("response", on_response)
    try:
        search_and_wait(page, "tiktok", "https://www.tiktok.com",
                        "https://www.tiktok.com/search/video?q={kw}", kw,
                        'a[href*="/video/"]',
                        'input[type="search"], input[placeholder*="Search"]')
        for _ in range(random.randint(3, 5)):
            human_scroll(page, total=random.randint(1600, 2400))
            idle(1.2, 2.4)
        wiggle_cursor(page)
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass

    seen: dict[str, dict] = {}
    for raw in blobs:
        try:
            data = json.loads(raw)
        except Exception:
            continue
        for entry in (data.get("item_list") or data.get("data") or []):
            if not isinstance(entry, dict):
                continue
            it = entry.get("item") if "item" in entry else entry
            if not isinstance(it, dict) or not it.get("id"):
                continue
            vid = str(it["id"])
            if vid in seen:
                continue
            desc = it.get("desc") or ""
            if EXCLUDE.search(desc):
                continue
            author = ((it.get("author") or {}).get("uniqueId") or "unknown")
            ct = it.get("createTime")
            published = None
            if isinstance(ct, (int, float, str)) and str(ct).isdigit() and int(ct) > 0:
                published = dt.date.fromtimestamp(int(ct)).isoformat()
            stats = it.get("stats") or {}
            seen[vid] = {
                "id": vid, "platform": "tiktok",
                "url": f"https://www.tiktok.com/@{author}/video/{vid}",
                "title": re.sub(r"\s+", " ", desc)[:200], "author": author[:80],
                "like": int(stats.get("diggCount") or 0),
                "published_at": published,
                "source_desc": desc[:400], "keyword": kw,
            }
    out = list(seen.values())
    # 接口一次能给 70 条, 但 per_kw 只留 25 —— 按插入顺序砍会把少数几条新片砍掉。
    # 先按「近 recent_days 内优先, 再按点赞」排序再截断, 时效才保得住。
    out.sort(key=lambda c: (
        1 if (c.get("published_at") and (days_since(c["published_at"]) or 999) <= args.recent_days) else 0,
        c.get("like") or 0), reverse=True)
    if not out:
        # 接口没截到 (改版/被缓存) 就退回爬卡片, 至少还能拿到链接
        cards = page.locator('a[href*="/video/"]').evaluate_all(
            """anchors => anchors.map(a => a.href)
                 .filter((v,i,arr) => arr.indexOf(v)===i)""")
        for href in cards[:per_kw]:
            vid_m = re.search(r"/video/(\d+)", href)
            if not vid_m:
                continue
            creator_m = re.search(r"tiktok\.com/@([^/]+)/", href)
            out.append({"id": vid_m.group(1), "platform": "tiktok", "url": href,
                        "title": "", "author": creator_m.group(1) if creator_m else "unknown",
                        "like": 0, "published_at": None, "source_desc": "", "keyword": kw})
        print(f"[tiktok] {kw!r} 接口未截到, 回退爬卡片 -> {len(out)} 条(无元数据)", flush=True)
    else:
        recent = sum(1 for c in out
                     if c["published_at"] and days_since(c["published_at"]) <= args.recent_days)
        print(f"[tiktok] {kw!r} 接口拿到 {len(out)} 条 (近 {args.recent_days} 天 {recent} 条)",
              flush=True)
    return out[:per_kw]

def discover_youtube(page, kw: str, per_kw: int) -> list[dict]:
    # sp 是 YouTube 的筛选 protobuf。两个坑:
    #   1. 之前用的 EgIIBQ%3D%3D 其实是「今年内」不是「本周内」(本周内是 IIAw),
    #      所以搜回来一堆几个月前的; 这里用 EgQIAxAB = 上传时间「本周」+ 类型「视频」。
    #   2. 值里的 %3D 不能再编码一次 —— 写成 %253D YouTube 直接忽略整个参数。
    search_and_wait(page, "youtube", "https://www.youtube.com",
                    "https://www.youtube.com/results?search_query={kw}&sp=EgQIAxAB", kw,
                    "ytd-video-renderer, ytd-rich-item-renderer",
                    'input#search, input[name="search_query"]',
                    require_in_url="sp=")
    for _ in range(random.randint(2, 4)):
        human_scroll(page, total=random.randint(1800, 2800))
        idle(0.7, 1.9)
    wiggle_cursor(page)
    cards = page.locator("ytd-video-renderer, ytd-rich-item-renderer").evaluate_all("""renderers =>
        renderers.map(r => {
            const a = r.querySelector('a#video-title, a#thumbnail');
            const title_el = r.querySelector('#video-title');
            const channel = r.querySelector('#channel-name, ytd-channel-name');
            // #metadata-line 的 textContent 里全是换行和空白, 按 • 切会切出带换行的脏
            // token, 正则一个都匹配不上。直接取渲染好的 span 列表干净得多。
            const bits = [...r.querySelectorAll('#metadata-line span, .inline-metadata-item')]
                .map(s => s.textContent.trim()).filter(Boolean);
            return {
                href: a?.href || '',
                title: title_el?.textContent?.trim() || '',
                bits: [...new Set(bits)],
                channel: channel?.textContent?.trim() || '',
            };
        }).filter(r => r.href.includes('/watch?v='))
    """)
    out = []
    for c in cards[:per_kw*2]:
        vid_m = re.search(r"[?&]v=([\w-]+)", c["href"])
        if not vid_m: continue
        bits = c.get("bits") or []
        text = f"{c['title']} {' '.join(bits)}"
        if EXCLUDE.search(text): continue
        published = None; like = 0
        for tok in bits:
            d = parse_date(tok)
            if d and not published:
                published = d
                continue
            n = parse_compact_number(tok)
            if n > like: like = n
        out.append({"id": vid_m.group(1), "platform": "youtube",
                    "url": f"https://www.youtube.com/watch?v={vid_m.group(1)}",
                    "title": c["title"][:200], "author": c["channel"][:80],
                    "like": like, "published_at": published,
                    "source_desc": text[:400], "keyword": kw})
    return out[:per_kw]

INSTAGRAM_ENRICH_BUDGET = 14   # 每个关键词最多开这么多详情页补元数据


def instagram_post_meta(page, url: str) -> dict | None:
    """开一次 Instagram 详情页, 从 og:description 和 <time> 读作者/点赞/日期/文案。

    og:description 形如 "1,234 likes, 56 comments - handle on July 12, 2026: "..."";
    og:title 形如 "Han Jia Yi on Instagram: ..."。
    """
    try:
        page.goto(url, timeout=30_000, wait_until="domcontentloaded")
    except Exception:
        return None
    jitter_sleep(1.6, 3.2)
    try:
        info = page.evaluate("""() => ({
            desc: document.querySelector('meta[property="og:description"]')?.getAttribute('content') || '',
            ogTitle: document.querySelector('meta[property="og:title"]')?.getAttribute('content') || '',
            dt: document.querySelector('time')?.getAttribute('datetime') || '',
        })""")
    except Exception:
        return None
    desc = info.get("desc") or ""
    og = info.get("ogTitle") or ""
    # 作者名要同时吃下英文和中文界面两种格式:
    #   en: "1,234 likes, 56 comments - handle on July 12, 2026: "..."
    #   zh: "4,828 likes, 96 comments -  theeyeflash999，August 26, 2026 : "..."
    author = "unknown"
    m = re.search(r"comments?\s*[-–—]\s*([A-Za-z0-9._]+)\s*(?:[，,]|\s+on\s)", desc)
    if m:
        author = m.group(1)
    else:
        m = re.match(r"^(.+?)\s+on Instagram", og)
        if m:
            author = m.group(1).strip()
        else:
            # zh: "Instagram 用户 THE EYE FLASH千胜帝 : "..."" —— 这是昵称不是句柄, 但聊胜于无
            m = re.match(r"^Instagram\s*用户\s*(.+?)\s*[:：]", og)
            if m:
                author = m.group(1).strip()
    like = 0
    m2 = re.search(r"([\d,]+)\s+likes?", desc)
    if m2:
        like = int(m2.group(1).replace(",", ""))
    published = (info.get("dt") or "")[:10] or None
    if not published:
        # desc 里带绝对日期 "August 26, 2026", 比 <time> 更常在
        m3 = re.search(r"([A-Z][a-z]+ \d{1,2}, \d{4})", desc)
        if m3:
            try:
                published = dt.datetime.strptime(m3.group(1), "%B %d, %Y").date().isoformat()
            except ValueError:
                pass
    title = re.sub(r'^.*?[:：]\s*"?', "", desc).split('"')[0][:200] if desc else ""
    if author == "unknown" and not like and not published:
        return None
    return {"author": author, "like": like, "published_at": published,
            "title": re.sub(r"\s+", " ", title).strip()}


def _walk_media(obj) -> list[dict]:
    """从任意嵌套 JSON 里捞出「看起来像一条帖子」的对象。

    Instagram 的 graphql 返回层级深且经常改名 (xdt_api__v1__... 之类), 按固定路径取
    很容易一改版就全空。按特征找 (有 code/pk, 且带 like_count/taken_at/user) 稳得多。
    """
    found: list[dict] = []
    if isinstance(obj, dict):
        if ("code" in obj or "pk" in obj) and any(
                k in obj for k in ("like_count", "taken_at", "user")):
            found.append(obj)
        for v in obj.values():
            found += _walk_media(v)
    elif isinstance(obj, list):
        for v in obj:
            found += _walk_media(v)
    return found


def discover_instagram(page, kw: str, per_kw: int) -> list[dict]:
    """Instagram 走**响应拦截**而不是爬 DOM。

    实测搜索结果网格里 <a> 既没有 alt 也没有任何文字, 作者/点赞/日期一个都拿不到
    (采集评分里 instagram 三项覆盖率全是 0%, 时间筛选和热度排序完全是摆设)。
    但页面渲染用的 graphql/query 响应里 code/like_count/taken_at/user.username/
    caption 一应俱全, 直接截这个。
    """
    blobs: list[str] = []

    def on_response(r):
        try:
            if "/graphql" in r.url or "/api/v1/" in r.url:
                if "json" in (r.headers.get("content-type") or ""):
                    blobs.append(r.text())
        except Exception:
            pass

    page.on("response", on_response)
    try:
        search_and_wait(page, "instagram", "https://www.instagram.com",
                        "https://www.instagram.com/explore/search/keyword/?q={kw}", kw,
                        'a[href*="/reel/"], a[href*="/p/"]',
                        'input[placeholder*="Search"], input[aria-label*="Search"]')
        # 每次滚动触发一页 graphql, 多滚几次才有量
        for _ in range(random.randint(6, 9)):
            human_scroll(page, total=random.randint(1600, 2600))
            idle(1.4, 2.6)
        wiggle_cursor(page)
        # 首屏结果是服务端直出、塞在 <script> 里的, 不走 graphql —— 只截响应会漏掉一大半
        try:
            blobs += page.evaluate(
                """() => [...document.querySelectorAll('script')]
                       .map(s => s.textContent || '')
                       .filter(t => t.length > 200 && (t.includes('"like_count"') || t.includes('"taken_at"')))""")
        except Exception:
            pass
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass

    seen: dict[str, dict] = {}
    for raw in blobs:
        data = None
        try:
            data = json.loads(raw)
        except Exception:
            # <script> 里常是 requireLazy(...)({...}) 这类包裹, 把里面的 JSON 抠出来
            for m in re.finditer(r'(\{"[\s\S]{200,}?\})\s*[;,)\]]', raw):
                try:
                    data = json.loads(m.group(1))
                    break
                except Exception:
                    continue
        if data is None:
            continue
        for m in _walk_media(data):
            code = m.get("code")
            if not code or code in seen:
                continue
            # media_type: 1=图片 2=视频。栏目只要视频
            vt = m.get("media_type")
            if vt not in (2, None) and not m.get("video_versions"):
                continue
            caption = m.get("caption")
            text = (caption or {}).get("text", "") if isinstance(caption, dict) else ""
            if EXCLUDE.search(text):
                continue
            taken = m.get("taken_at")
            published = None
            if isinstance(taken, (int, float)) and taken > 0:
                published = dt.date.fromtimestamp(taken).isoformat()
            user = m.get("user") or {}
            seen[code] = {
                "id": code, "platform": "instagram",
                "url": f"https://www.instagram.com/reel/{code}/",
                "title": re.sub(r"\s+", " ", text)[:200],
                "author": (user.get("username") or "unknown")[:80],
                "like": int(m.get("like_count") or 0),
                "published_at": published,
                "source_desc": text[:400], "keyword": kw,
            }
    out = list(seen.values())

    # DOM 网格里的 <a> 数量远多于 graphql 能覆盖的 (实测 48 vs 3), 但网格本身
    # 一个字段都读不到。所以: 缺元数据的那批, 逐个开详情页读 og:description +
    # <time datetime> 补齐 (这套解析在 scripts/instagram_enrich_meta.py 里验证过)。
    # 有预算上限, 避免为了几条元数据把账号跑进风控。
    try:
        anchors = page.locator('a[href*="/reel/"], a[href*="/p/"]').evaluate_all(
            "as_ => [...new Set(as_.map(a => a.href.split('?')[0]))]")
    except Exception:
        anchors = []
    budget = max(0, min(INSTAGRAM_ENRICH_BUDGET, per_kw - len(out)))
    todo = [u for u in anchors
            if (re.search(r"/(?:reel|p)/([\w-]+)", u) or [None])
            and re.search(r"/(?:reel|p)/([\w-]+)", u).group(1) not in seen][:budget]
    if todo:
        print(f"[instagram] {kw!r} 网格另有 {len(anchors)} 条, 逐个补元数据 {len(todo)} 条",
              flush=True)
    for u in todo:
        code = re.search(r"/(?:reel|p)/([\w-]+)", u).group(1)
        meta = instagram_post_meta(page, u)
        if not meta:
            continue
        if EXCLUDE.search(meta.get("title", "")):
            continue
        out.append({"id": code, "platform": "instagram",
                    "url": f"https://www.instagram.com/reel/{code}/",
                    "title": meta.get("title", "")[:200],
                    "author": meta.get("author", "unknown")[:80],
                    "like": meta.get("like", 0),
                    "published_at": meta.get("published_at"),
                    "source_desc": meta.get("title", "")[:400], "keyword": kw})
        jitter_sleep(1.5, 4.0)

    print(f"[instagram] {kw!r} 拦截 {len(blobs)} 个 JSON 响应 -> {len(out)} 条带元数据", flush=True)
    return out[:per_kw]

def discover_bilibili(page, kw: str, per_kw: int) -> list[dict]:
    # &order=click 按播放, &order=stow 按收藏, &order=pubdate 按发布, &duration=1 短
    human_search(page, "https://www.bilibili.com",
                 "https://search.bilibili.com/video?keyword={kw}&order=click",
                 kw, search_input_selector='input.nav-search-input, input[placeholder*="搜索"]')
    for _ in range(random.randint(2, 4)):
        human_scroll(page, total=random.randint(1400, 2200))
        idle(0.6, 1.6)
    wiggle_cursor(page)
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
    # 抖音风控最狠 -> 从首页搜索 + 更慢
    search_and_wait(page, "douyin", "https://www.douyin.com",
                    "https://www.douyin.com/search/{kw}?type=video", kw,
                    'a[href*="/video/"]',
                    'input[data-e2e="searchbar-input"], input[placeholder*="搜索"]')
    for _ in range(random.randint(2, 4)):
        human_scroll(page, total=random.randint(1400, 2200))
        idle(1.0, 2.4)
    wiggle_cursor(page)
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


def rank_key(c):
    days = days_since(c.get("published_at"))
    recency_bonus = 0
    if days is not None and days <= args.recent_days:
        recency_bonus = 10_000_000  # push recent to top
    return (recency_bonus + (c.get("like") or 0))


def save_platform_pool(platform: str, items: list[dict]) -> int:
    """Rank + persist one platform's pool. Called right after each platform
    finishes (before any cooldown) so a killed/timed-out run never loses cards."""
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
    return len(uniq)

pools: dict[str, list[dict]] = {p: [] for p in platforms}
with sync_playwright() as p_ctx:
    b = p_ctx.chromium.connect_over_cdp("http://127.0.0.1:9222")
    ctx = b.contexts[0]
    # 拟人化: 平台顺序随机, 关键词顺序随机, 每期只跑部分组合
    platforms_shuffled = list(platforms)
    random.shuffle(platforms_shuffled)
    for plat_i, platform in enumerate(platforms_shuffled):
        is_last_platform = plat_i == len(platforms_shuffled) - 1
        fn = DISCOVERERS.get(platform)
        if not fn:
            print(f"[{platform}] no discoverer, skip"); continue
        page = ctx.new_page()
        consecutive_fail = 0
        try:
            kws_shuffled = keywords_for(platform)[:]
            random.shuffle(kws_shuffled)
            for kw_i, kw in enumerate(kws_shuffled):
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
                    print(f"[{platform}] STOP: {consecutive_fail} 连续失败, 疑似被风控", flush=True)
                    # 只有后面还有平台要跑时才长冷却; 否则白等一场, 反而可能被上层 timeout 杀掉
                    if not is_last_platform:
                        print(f"[{platform}] 长冷却 5-10 分", flush=True)
                        time.sleep(random.uniform(300, 600))
                    break
                # 关键词之间: 短冷却 (拟人); xhs 更保守 (它反爬升级快)
                if kw_i < len(kws_shuffled) - 1:
                    if platform == "xiaohongshu":
                        cooldown(min_s=60, max_s=180)
                    else:
                        cooldown(min_s=25, max_s=75)
        finally:
            try: page.close()
            except Exception: pass
        # 先落盘再冷却: 上层 discover_loop 有 timeout, 冷却期间被杀不能丢结果
        save_platform_pool(platform, pools[platform])
        # 平台之间: 大冷却 (最后一个平台不用等)
        if not is_last_platform:
            cooldown(min_s=90, max_s=240)

# ── rank + save candidates ──
for platform in platforms:
    save_platform_pool(platform, pools[platform])

# save summary
summary = {"generated_at": dt.datetime.now().isoformat(),
           "platforms": {p: len(pools[p]) for p in platforms},
           "keywords": keywords, "pool_size": args.pool_size,
           "recent_days_preference": args.recent_days}
(CAND_DIR / "_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
print(f"\nCandidates saved. Run download_universal.py --week {WEEK} to fetch media.", flush=True)
