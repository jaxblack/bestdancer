#!/usr/bin/env python3
"""Rebuild config/weekly/2026-W29.json from real Douyin scrape data.

- Copy the first N usable douyin downloads into cN/k1__douyin__<id>.mp4
- Use the real dom_items_sample text as source of truth for creator, caption,
  duration, likes, publish time.
- Generate narration that talks about the ACTUAL clip (style, moves inferred
  from hashtags/caption) instead of made-up filler.
"""
import json, re, subprocess, shutil
from pathlib import Path

REPO = Path("/Users/jax/bestdancer")
WEEK = "2026-W29"
BASE = REPO / "assets" / "incoming" / WEEK
DL = BASE / "dl"
OUT_CFG = REPO / "config" / "weekly" / f"{WEEK}.json"
META = json.loads((BASE / "scrape_meta.json").read_text())

# ------- style inference from caption + hashtags -------
STYLE_MAP = [
    (r"jazz|爵士",           "jazz",     "爵士风格，注重身体线条和 groove 感"),
    (r"hiphop|嘻哈|hip hop",  "hiphop",   "hip-hop 律动，重点在下沉 bounce 和节奏感"),
    (r"breaking|地板|空翻|b[- ]?boy", "breaking", "breaking 地板技巧，含高难度力量动作"),
    (r"popping|机械|hit",     "popping",  "popping 机械律动，练 hit 顿点和收放"),
    (r"locking",              "locking",  "locking 锁舞，靠手臂锁点和挑逗表情"),
    (r"kpop|k-pop|aespa|women",    "kpop",   "kpop 女团编舞，动作齐整、镜头感强"),
    (r"battle|同盟|内战|挑战|比赛",  "battle", "battle/比赛现场，能量密度大"),
    (r"编舞|choreo|翻跳|dance film|film",  "choreo", "编舞短片，动作设计感强"),
    (r"零基础|入门|基础|基本功|课堂|教学|分解", "class", "教学/基本功向，节奏偏慢便于跟练"),
    (r"萌娃|小小|少年",       "kids",     "小朋友/学员表演，动作偏简单可爱"),
    (r"路演|活动|舞台|表演",   "stage",    "路演/舞台表演，配合队形"),
]

def infer_style(text):
    text_lc = text.lower()
    hits = []
    for pat, tag, desc in STYLE_MAP:
        if re.search(pat, text_lc):
            hits.append((tag, desc))
    return hits

def extract_hashtags(text):
    return [h.strip() for h in re.findall(r'#([^\s#]+)', text) if h.strip()]

def clean_caption(text):
    # remove leading '合集\nMM:SS\nN\n' block, trailing '@xxx\nX小时前'
    lines = [l for l in text.split("\n") if l.strip()]
    # drop pure digits / time / '合集'
    body_lines = []
    for l in lines:
        if l == "合集": continue
        if re.match(r'^\d{1,2}:\d{2}$', l): continue
        if re.match(r'^\d+$', l): continue          # like count
        if l.startswith("@"): continue              # author line
        if re.match(r'^\d+\s*(分钟|小时|天)前$', l): continue
        body_lines.append(l)
    return " ".join(body_lines).strip()

def parse_item(item):
    text = item["text"]
    lines = [l for l in text.split("\n") if l.strip()]
    # duration is first "MM:SS"
    dur = 30
    likes = 0
    creator = ""
    posted = ""
    for l in lines:
        if re.match(r'^\d{1,2}:\d{2}$', l) and dur == 30:
            mm, ss = l.split(":")
            dur = int(mm) * 60 + int(ss)
        elif re.match(r'^\d+$', l) and likes == 0:
            likes = int(l)
        elif l.startswith("@"):
            creator = l
        elif re.match(r'^\d+\s*(分钟|小时|天)前$', l):
            posted = l
    return {
        "id": item["id"],
        "duration_sec": dur,
        "like": likes,
        "creator": creator,
        "posted": posted,
        "caption": clean_caption(text),
        "hashtags": extract_hashtags(text),
        "styles": infer_style(text),
    }

# Parse all sampled items
parsed = {it["id"]: parse_item(it) for it in META.get("dom_items_sample", [])}

# Build download map from dl/ files
dl_files = sorted(DL.glob("d*__douyin__*.mp4"))
usable = []
for f in dl_files:
    m = re.search(r'^d\d+__douyin__(\d+)\.mp4$', f.name)
    if not m: continue
    vid = m.group(1)
    if vid not in parsed:
        continue
    # skip files < 500KB (likely HLS master, no video)
    if f.stat().st_size < 500_000:
        continue
    usable.append((f, parsed[vid]))

# Sort by likes desc, take top 6 for c1..c5, k1
usable.sort(key=lambda x: x[1]["like"], reverse=True)
picks = usable[:6]
labels = ["c1", "c2", "c3", "c4", "c5", "k1"]

print(f"Using {len(picks)} videos (sorted by likes desc):")
for lab, (f, info) in zip(labels, picks):
    print(f"  {lab}: {info['id']} likes={info['like']:>4} dur={info['duration_sec']}s creator={info['creator']}")
    print(f"        caption: {info['caption'][:80]}")
    print(f"        styles: {[s[0] for s in info['styles']]}")

# Copy files with new names + delete old ones
for old in BASE.glob("c*__*__*.mp4"):
    old.unlink()
for old in BASE.glob("k*__*__*.mp4"):
    old.unlink()
for lab, (f, info) in zip(labels, picks):
    dst = BASE / f"{lab}__douyin__{info['id']}.mp4"
    shutil.copy(f, dst)

# Build config
def narration_for(info, rank, is_classic=False):
    """Generate a 1-sentence VO based on real caption/hashtags/styles."""
    styles = info["styles"]
    style_desc = styles[0][1] if styles else "街舞片段"
    likes = info["like"]
    creator = info["creator"].lstrip("@")
    dur = info["duration_sec"]

    # pick the most descriptive fragment of caption (< 40 chars)
    cap = info["caption"]
    # take first clause
    frag = re.split(r'[。！？!?]|\s{2,}', cap)[0][:60] if cap else ""
    # strip hashtags left over
    frag = re.sub(r'#\S+', '', frag).strip(' ,，、')

    prefix = "特别加映" if is_classic else f"第{rank}名"
    # Short punchy 1-liner
    if frag:
        return f"{prefix}，{style_desc}，{frag}。"
    return f"{prefix}，{style_desc}，作者 {creator}。"

def subtitle_lines_for(info, rank, is_classic=False):
    """Multi-line subtitle overlay (small text bottom)."""
    creator = info["creator"]
    hashtags = info["hashtags"][:3]
    dur = info["duration_sec"]
    likes = info["like"]
    lines = []
    if hashtags:
        lines.append(" ".join("#" + h for h in hashtags))
    src = f"{creator}｜抖音｜{dur}s｜❤ {likes}"
    lines.append(src)
    return lines

candidates = []
picks_list = []
classic_entry = None
narration = []

for idx, (lab, (f, info)) in enumerate(zip(labels, picks)):
    is_classic = (lab == "k1")
    rank = None if is_classic else idx + 1
    style_tags = [s[0] for s in info["styles"]] or ["street"]
    url = f"https://www.douyin.com/video/{info['id']}"

    cand = {
        "id": lab,
        "source": "抖音",
        "creator": info["creator"],
        "title": info["caption"][:80] if info["caption"] else "抖音街舞",
        "song": "",
        "duration_sec": info["duration_sec"],
        "play": 0,
        "like": info["like"],
        "share": 0,
        "tags": style_tags,
        "url": url,
        "move_notes": info["caption"][:200],
        "local_path": f"assets/incoming/{WEEK}/{lab}__douyin__{info['id']}.mp4",
        "hashtags": info["hashtags"],
        "posted": info["posted"],
    }

    if is_classic:
        candidates_target = "classics_pool"
        classic_entry = {
            "id": lab,
            "reason": "同期热度榜内的额外加映",
            "difficulty": {"scores": {"tempo": 3, "complexity": 3, "control": 3, "memory": 3, "stamina": 3},
                           "weighted": 3.0, "stars": 3.0,
                           "fit": "看兴趣挑战", "hardest_part": "跟原视频节奏"},
        }
    else:
        picks_list.append({
            "rank": rank,
            "id": lab,
            "reason": info["caption"][:80],
            "highlight_hint": ", ".join(info["hashtags"][:2]),
            "cut_suggestion": "",
            "difficulty": {"scores": {"tempo": 3, "complexity": 3, "control": 3, "memory": 3, "stamina": 3},
                           "weighted": 3.0, "stars": 3.0,
                           "fit": info["styles"][0][1] if info["styles"] else "街舞爱好者",
                           "hardest_part": (info["styles"][0][0] if info["styles"] else "跟节奏")},
        })

    narration.append({
        "segment": "classic" if is_classic else "top",
        "rank": rank,
        "vo": narration_for(info, rank, is_classic),
        "subtitle": subtitle_lines_for(info, rank, is_classic),
        "on_screen": {
            "stars": 3.0,
            "tag": "特别加映" if is_classic else f"本周No.{rank}",
            "core_moves": style_tags[:3],
        },
        "beginner_tip": "",
    })

    if is_classic:
        # store separately below
        pass
    candidates.append(cand)

# Split into this_week / classics
this_week = [c for c in candidates if not c["id"].startswith("k")]
classics = [c for c in candidates if c["id"].startswith("k")]

cfg = {
    "_readme": "auto-generated by scripts/rebuild_config.py from real 抖音 scrape data",
    "episode": {
        "week": WEEK,
        "theme": "抖音街舞·真实热度榜",
        "platforms": ["douyin"],
        "voice": "young_female",
        "top_n": 5,
        "classic_n": 1,
    },
    "this_week_candidates": this_week,
    "classics_pool": classics,
    "picks": picks_list,
    "classic_comeback": classic_entry,
    "narration": narration,
    "metadata": {"note": "抖音候选采集于 " + META.get("search_url", "")},
}

OUT_CFG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))
print(f"\nWrote {OUT_CFG}")
print(f"local mp4s:")
subprocess.run(["ls", "-la", str(BASE)], check=True)
