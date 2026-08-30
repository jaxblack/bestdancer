#!/usr/bin/env python3
"""Download videos from candidates/{tiktok,xiaohongshu,instagram,youtube}.json using yt-dlp.

Populates assets/incoming/<week>/dl2/<platform>_<id>.{mp4,json} so that
rebuild_from_dl2.py can merge them into the weekly config alongside Douyin.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--week", required=True)
parser.add_argument("--max-per-platform", type=int, default=6)
parser.add_argument("--platforms", default="tiktok|xiaohongshu|instagram|youtube",
                    help="Pipe-separated platform ids to download")
args = parser.parse_args()

REPO = Path(__file__).resolve().parents[1]
BASE = REPO / "assets" / "incoming" / args.week
CANDS = BASE / "candidates"
DL2 = BASE / "dl2"
DL2.mkdir(parents=True, exist_ok=True)
COOKIES = BASE / "cookies.txt"

PLATFORMS = [p for p in args.platforms.split("|") if p]

BOT_CHECK_MARKERS = ("sign in to confirm", "not a bot", "login required",
                     "private video", "members-only")


def platform_ready(platform: str) -> tuple[bool, str]:
    """开跑前先真下一支, 确认这个平台当前能不能用。

    只查元数据是不够的 —— YouTube 在没有有效登录态时**元数据能取到,
    但媒体流会被限速到 0 字节**, 每支都要卡满 socket 超时才失败, 12 支就是半小时
    白等。这里硬性 75 秒封顶并检查落盘字节数: 正常平台几秒就下完一支短视频,
    被限速的平台 75 秒后仍是 0 字节, 直接整个平台跳过。
    """
    cand_file = CANDS / f"{platform}.json"
    if not cand_file.exists():
        return False, "没有候选文件"
    try:
        items = json.loads(cand_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False, "候选文件解析失败"
    url = next((it.get("url") for it in items if it.get("url")), None)
    if not url:
        return False, "候选里没有可用链接"

    # 试前几支而不是只试第一支: 单支可能因为年龄限制/会员专属/已删除而失败,
    # 不能因此判定整个平台不可用 (实测 YouTube 就被第一支拖累误判过)。
    urls = [it["url"] for it in items if it.get("url")][:3]
    last_err = ""
    for url in urls:
        ok, err = _probe_one(platform, url)
        if ok:
            return True, ""
        last_err = err
        if "需要登录态" in err:
            return False, err
    return False, last_err or "试下载失败"


def _probe_one(platform: str, url: str) -> tuple[bool, str]:
    probe_dir = DL2 / ".probe"
    if probe_dir.exists():
        shutil.rmtree(probe_dir, ignore_errors=True)
    probe_dir.mkdir(parents=True, exist_ok=True)
    cmd = ["yt-dlp", "--no-warnings", "--no-playlist", "--no-part",
           "--socket-timeout", "12", "--retries", "1", "--fragment-retries", "1",
           "-f", "mp4/bestvideo+bestaudio/best",
           "-o", str(probe_dir / "probe.%(ext)s"), url]
    cookie = BASE / f"cookies_{platform}.txt"
    if platform == "douyin" and COOKIES.exists():
        cmd += ["--cookies", str(COOKIES)]
    elif cookie.exists():
        cmd += ["--cookies", str(cookie)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=75)
        err = ((r.stderr or "") + (r.stdout or "")).lower()
    except subprocess.TimeoutExpired:
        r, err = None, "timeout"
    got = max((f.stat().st_size for f in probe_dir.glob("probe.*")), default=0)
    shutil.rmtree(probe_dir, ignore_errors=True)

    if got > 50_000:
        return True, ""
    if any(m in err for m in BOT_CHECK_MARKERS):
        return False, (f"需要登录态: 请在调试 Chrome "
                       f"(--user-data-dir=~/.chrome-debug-profile) 里登录 {platform} 后重试")
    if err == "timeout":
        return False, "试下载 75 秒没拿到数据 (多半是登录态失效被限速)"
    tail = (r.stderr or "").strip().splitlines()[-1][:160] if r and r.stderr else "未知原因"
    return False, f"试下载失败: {tail}"


DANCE_KEYWORDS = ["urban", "hiphop", "hip-hop", "kpop", "k-pop", "jazz", "choreo", "编舞",
                  "dance", "舞蹈", "翻跳", "cover"]

def infer_dance_type(text: str) -> str:
    t = (text or "").lower()
    mapping = [
        (r"urban", "Urban Dance"),
        (r"jazz|爵士", "Jazz"),
        (r"hiphop|hip[- ]?hop|嘻哈", "Hip-hop"),
        (r"popping|机械", "Popping"),
        (r"locking", "Locking"),
        (r"kpop|k-pop|女团|男团|翻跳|cover", "K-pop"),
        (r"choreo|编舞", "Choreography"),
    ]
    for pat, name in mapping:
        if re.search(pat, t):
            return name
    return "Street"

def extract_id(url: str, platform: str) -> str:
    if platform == "tiktok":
        m = re.search(r"/video/(\d+)", url)
    elif platform == "youtube":
        m = re.search(r"[?&]v=([\w-]+)", url) or re.search(r"/shorts/([\w-]+)", url)
    elif platform == "instagram":
        m = re.search(r"/reel/([\w-]+)", url) or re.search(r"/p/([\w-]+)", url)
    elif platform == "xiaohongshu":
        m = re.search(r"/explore/([\w-]+)", url)
    else:
        m = None
    return m.group(1) if m else re.sub(r"\W+", "_", url)[-24:]

def yt_dlp_download(url: str, out_template: str, platform: str) -> tuple[bool, dict]:
    """Return (ok, meta). meta is yt-dlp's info dict, if available."""
    info_path = Path(out_template).with_suffix(".info.json")
    cmd = ["yt-dlp", "--no-warnings", "--no-playlist", "--write-info-json",
           "-f", "mp4/bestvideo+bestaudio/best",
           "-o", out_template, url]
    # Per-platform cookies (Netscape file exported from CDP).
    # Douyin uses legacy cookies.txt; others use cookies_<platform>.txt if present.
    per_platform_cookie = BASE / f"cookies_{platform}.txt"
    if platform == "douyin" and COOKIES.exists():
        cmd += ["--cookies", str(COOKIES)]
    elif per_platform_cookie.exists():
        cmd += ["--cookies", str(per_platform_cookie)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return False, {}
    if r.returncode != 0:
        # print first line of stderr for diagnosis
        first_err = (r.stderr or "").strip().splitlines()[:2]
        print(f"    yt-dlp failed rc={r.returncode}: {' | '.join(first_err)}", flush=True)
        return False, {}
    meta = {}
    if info_path.exists():
        try:
            meta = json.loads(info_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return True, meta

def process_platform(platform: str) -> int:
    cand_file = CANDS / f"{platform}.json"
    if not cand_file.exists():
        print(f"[{platform}] no candidates file, skipping")
        return 0
    items = json.loads(cand_file.read_text(encoding="utf-8"))
    if not items:
        print(f"[{platform}] empty candidates, skipping")
        return 0
    # only pick "top" tier (or first N if no tier)
    tops = [it for it in items if it.get("candidate_tier", "top") == "top"] or items
    downloaded = 0
    for it in tops[: args.max_per_platform]:
        url = it.get("url", "")
        if not url:
            continue
        # Filter: title/desc must contain a dance keyword; skip obvious non-dance
        text = " ".join(str(it.get(k, "")) for k in ("title", "source_desc"))
        if not any(kw in text.lower() for kw in DANCE_KEYWORDS):
            print(f"[{platform}] skip non-dance: {url}")
            continue
        vid = extract_id(url, platform)
        out = DL2 / f"{platform}_{vid}.%(ext)s"
        target_mp4 = DL2 / f"{platform}_{vid}.mp4"
        if target_mp4.exists() and target_mp4.stat().st_size > 300_000:
            print(f"[{platform}] {vid} already downloaded, skipping")
            downloaded += 1
            continue
        print(f"[{platform}] downloading {url}", flush=True)
        ok, meta = yt_dlp_download(url, str(out), platform)
        # find the actual downloaded file (yt-dlp may pick different ext)
        real_mp4 = next(DL2.glob(f"{platform}_{vid}.*"), None)
        # normalize non-.mp4 files
        if real_mp4 and real_mp4.suffix != ".mp4" and real_mp4.suffix not in (".info", ".json"):
            new_path = real_mp4.with_suffix(".mp4")
            try:
                real_mp4.rename(new_path)
                real_mp4 = new_path
            except Exception:
                pass
        if not ok or not real_mp4 or not real_mp4.exists() or real_mp4.stat().st_size < 300_000:
            print(f"    → failed for {vid}", flush=True)
            continue
        title = meta.get("title") or it.get("title", "")
        desc = meta.get("description") or it.get("source_desc", "")
        author = meta.get("uploader") or meta.get("channel") or it.get("creator", "").lstrip("@")
        dur = int(meta.get("duration") or it.get("duration_sec") or 0)
        likes = int(meta.get("like_count") or it.get("like", 0) or 0)
        # Save normalized meta json (same shape as douyin dl2/*.json)
        norm = {
            "id": vid,
            "platform": platform,
            "source": it.get("source", platform),
            "desc": desc[:500],
            "author": author,
            "duration_sec": dur,
            "like": likes,
            "play_count": int(meta.get("view_count") or 0),
            "tags": meta.get("tags", []) or [],
            "play_url": meta.get("webpage_url") or url,
            "url": url,
            "dance_type": infer_dance_type(f"{title} {desc} {' '.join(meta.get('tags', []) or [])}"),
        }
        (DL2 / f"{platform}_{vid}.json").write_text(
            json.dumps(norm, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        # remove yt-dlp side-car
        try:
            (DL2 / f"{platform}_{vid}.info.json").unlink(missing_ok=True)
        except TypeError:
            pass
        downloaded += 1
        print(f"    → OK {vid} [{norm['dance_type']}] {author} {dur}s ❤{likes}", flush=True)
    return downloaded

if __name__ == "__main__":
    total = 0
    for platform in PLATFORMS:
        ok, why = platform_ready(platform)
        if not ok:
            print(f"[{platform}] 跳过 —— {why}", flush=True)
            continue
        total += process_platform(platform)
    print(f"\n=== cross-platform DONE: {total} videos downloaded ===")
