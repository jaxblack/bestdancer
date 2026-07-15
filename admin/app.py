#!/usr/bin/env python3
"""BestDancer local editorial dashboard. Run: python3 admin/app.py"""
from __future__ import annotations

import json
import mimetypes
import re
import subprocess
import sys
import threading
import uuid
from datetime import date, timedelta
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

REPO = Path(__file__).resolve().parents[1]
ADMIN = REPO / "admin"
WEEKLY = REPO / "config" / "weekly"
INCOMING = REPO / "assets" / "incoming"
PREVIEW_AUDIO = ADMIN / "audio"
PREVIEW_VIDEO = ADMIN / "video-preview"
VOICE_PRESETS = {
    "zh-CN-XiaoyiNeural": {"label": "晓伊", "style": "活力女声"},
    "zh-CN-XiaoxiaoNeural": {"label": "晓晓", "style": "元气女声"},
    "zh-CN-YunxiNeural": {"label": "云希", "style": "清爽男声"},
    "zh-CN-YunyangNeural": {"label": "云扬", "style": "磁性男声"},
    "zh-CN-YunxiaNeural": {"label": "云夏", "style": "轻快男声"},
    "zh-CN-YunjianNeural": {"label": "云健", "style": "沉稳男声"},
}
VOICE_RATES = {"+0%", "+12%", "+20%"}
DEFAULT_SETTINGS = {
    "keywords": ["urban dance 编舞", "编舞 完整", "kpop dance cover"],
    "platforms": ["douyin", "instagram", "tiktok", "xiaohongshu"],
    "top_limit": 30,
    "min_likes": 0,
    "videos_only": True,
    "recent_days": 7,
    "sort_by": "heat_desc",
}
PYTHON = REPO / ".venv" / "bin" / "python"
PYTHON_COMMAND = str(PYTHON) if PYTHON.exists() else sys.executable
JOBS: dict[str, dict] = {}


def iso_week() -> str:
    year, week, _ = date.today().isocalendar()
    return f"{year}-W{week:02d}"


WEEK_ID = re.compile(r"\d{4}-W\d{2}(?:-[AB])?$")


def config_path(week: str) -> Path:
    if not WEEK_ID.fullmatch(week):
        raise ValueError("工作区格式应为 YYYY-Www、YYYY-Www-A 或 YYYY-Www-B")
    return WEEKLY / f"{week}.json"


def blank_config(week: str) -> dict:
    return {
        "_readme": "Managed by the local BestDancer dashboard",
        "episode": {"week": week, "theme": "本周街舞热榜", "platforms": ["douyin"],
                    "voice": "young_female", "top_n": 5, "classic_n": 1},
        "this_week_candidates": [], "classics_pool": [], "picks": [],
        "classic_comeback": {}, "narration": [], "metadata": {"source": "dashboard"},
    }


def load_config(week: str) -> dict:
    path = config_path(week)
    if not path.exists():
        return blank_config(week)
    return json.loads(path.read_text(encoding="utf-8"))


def save_config(week: str, config: dict) -> None:
    config["episode"]["week"] = week
    WEEKLY.mkdir(parents=True, exist_ok=True)
    config_path(week).write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_settings() -> dict:
    path = ADMIN / "settings.json"
    if not path.exists():
        return DEFAULT_SETTINGS.copy()
    return {**DEFAULT_SETTINGS, **json.loads(path.read_text(encoding="utf-8"))}


def save_settings(settings: dict) -> None:
    (ADMIN / "settings.json").write_text(
        json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def normalize_candidate(item: dict, index: int) -> dict:
    candidate_id = item.get("id") or f"c{index}"
    voice = item.get("voice", "zh-CN-XiaoyiNeural")
    return {
        "id": candidate_id,
        "source": item.get("source", "抖音"),
        "creator": item.get("creator", ""),
        "title": item.get("title", ""),
        "song": item.get("song", ""),
        "duration_sec": item.get("duration_sec") or 0,
        "like": item.get("like") or 0,
        "play": item.get("play") or item.get("play_count") or 0,
        "tags": item.get("tags") or [],
        "url": item.get("url", ""),
        "dance_type": item.get("dance_type", "街舞"),
        "local_path": item.get("local_path", ""),
        "manual_note": item.get("manual_note", ""),
        "narration": item.get("narration", ""),
        "voice": voice if voice in VOICE_PRESETS else "zh-CN-XiaoyiNeural",
        "voice_rate": item.get("voice_rate", "+20%"),
        "clip_start_sec": max(0, float(item.get("clip_start_sec") or 0)),
        "clip_end_sec": max(0, float(item.get("clip_end_sec") or 0)),
        "source_desc": item.get("source_desc", ""),
        "download_status": item.get("download_status", "unknown"),
        "candidate_tier": "backup" if item.get("candidate_tier") == "backup" else "top",
    }


def selected_ids(config: dict) -> list[str]:
    top = [pick.get("id") for pick in config.get("picks", []) if pick.get("id")]
    classic = config.get("classic_comeback", {}).get("id")
    return top + ([classic] if classic else [])


def default_narration(item: dict, rank: int | None = None, classic: bool = False) -> str:
    creator = str(item.get("creator", "")).lstrip("@") or "这位编舞者"
    dance_type = item.get("dance_type", "街舞") or "街舞"
    prefix = "特别加映" if classic else f"第{rank}名" if rank else "本周推荐"
    return f"{prefix}，{dance_type}，来自 {creator}。"


def workspaces(recent_weeks: int = 12) -> list[dict]:
    recent_weeks = max(1, min(recent_weeks, 52))
    weeks = {
        f"{week_date.isocalendar().year}-W{week_date.isocalendar().week:02d}"
        for offset in range(recent_weeks)
        for week_date in [date.today() - timedelta(weeks=offset)]
    }
    weeks.update(path.stem for path in WEEKLY.glob("????-W??*.json") if WEEK_ID.fullmatch(path.stem))
    summaries = []
    for week in sorted(weeks, reverse=True):
        config = load_config(week)
        candidates = len(config.get("this_week_candidates", [])) + len(config.get("classics_pool", []))
        summaries.append({"week": week, "candidates": candidates, "selected": len(selected_ids(config)),
                  "configured": config_path(week).exists()})
    return summaries


def build_editor_config(week: str, payload: dict) -> dict:
    old = load_config(week)
    entries = [normalize_candidate(item, i + 1) for i, item in enumerate(payload.get("candidates", []))]
    selected = payload.get("selected", [])[:6]
    candidates = {item["id"]: item for item in entries}
    duplicates = historical_urls(week)
    selected_urls = [canonical_url(candidates[candidate_id].get("url", "")) for candidate_id in selected if candidate_id in candidates]
    repeated = [url for url in selected_urls if url and (url in duplicates or selected_urls.count(url) > 1)]
    if repeated:
        raise ValueError("本期入选含有往期或本期重复视频，请更换候选")
    picks, narration = [], []
    for rank, candidate_id in enumerate(selected[:5], 1):
        item = candidates.get(candidate_id)
        if not item:
            continue
        difficulty = item.pop("difficulty", None) or {"stars": 3.0, "fit": item["dance_type"], "scores": {}}
        picks.append({"rank": rank, "id": candidate_id, "reason": item.get("manual_note", ""),
                      "highlight_hint": "", "cut_suggestion": "", "difficulty": difficulty})
        narration.append({"segment": "top", "rank": rank, "vo": item.get("narration", "").strip() or default_narration(item, rank),
                  "voice": item.get("voice", "zh-CN-XiaoyiNeural"),
                  "voice_rate": item.get("voice_rate", "+20%"), "subtitle": [],
                          "on_screen": {"stars": difficulty.get("stars", 3.0), "tag": f"本周No.{rank}",
                                        "core_moves": [item["dance_type"]]}, "beginner_tip": ""})
    classic_id = selected[5] if len(selected) > 5 and selected[5] in candidates else None
    classic_pool = [candidates.pop(classic_id)] if classic_id else []
    classic = {}
    if classic_id:
        classic = {"id": classic_id, "reason": "特别加映", "difficulty": {"stars": 3.0, "fit": "基础练习", "scores": {}}}
        narration.append({"segment": "classic", "rank": None, "vo": default_narration(classic_pool[0], classic=True), "voice": "zh-CN-XiaoyiNeural",
                  "voice_rate": "+20%", "subtitle": [],
                          "on_screen": {"stars": 3.0, "tag": "特别加映", "core_moves": [classic_pool[0]["dance_type"]]},
                          "beginner_tip": ""})
    metadata = {**old.get("metadata", {})}
    if "video_description" in payload:
        metadata["video_description"] = str(payload.get("video_description", "")).strip()
    config = {**old, "this_week_candidates": list(candidates.values()), "classics_pool": classic_pool,
              "picks": picks, "classic_comeback": classic, "narration": narration, "metadata": metadata}
    config["episode"].update(payload.get("episode", {}))
    return config


def canonical_url(value: str) -> str:
    parsed = urlparse(value.strip())
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path.rstrip('/')}"


def historical_urls(active_week: str) -> set[str]:
    urls = set()
    for path in WEEKLY.glob("????-W??*.json"):
        if path.stem == active_week or not WEEK_ID.fullmatch(path.stem):
            continue
        try:
            config = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        for item in config.get("this_week_candidates", []) + config.get("classics_pool", []):
            normalized = canonical_url(item.get("url", ""))
            if normalized:
                urls.add(normalized)
    return urls


def import_downloads(week: str) -> dict:
    incoming = INCOMING / week
    base = incoming / "dl2"
    config = load_config(week)
    current = {canonical_url(c["url"]): c for c in config.get("this_week_candidates", []) if c.get("url")}
    historical = historical_urls(week)
    generic_candidates = incoming / "candidates"
    for candidate_path in sorted(generic_candidates.glob("*.json")):
        for index, item in enumerate(json.loads(candidate_path.read_text(encoding="utf-8")), 1):
            url = item.get("url", "")
            if not url or item.get("media_type") not in {None, "video"}:
                continue
            if canonical_url(url) in historical:
                continue
            normalized_url = canonical_url(url)
            current[normalized_url] = normalize_candidate({
                **item, "id": current.get(normalized_url, {}).get("id", item.get("id", f"g{index}")),
            }, index)
    ranked_path = incoming / "ranked_candidates.json"
    if ranked_path.exists():
        for index, item in enumerate(json.loads(ranked_path.read_text(encoding="utf-8")), 1):
            video_id = str(item.get("id", ""))
            if not video_id:
                continue
            url = f"https://www.douyin.com/video/{video_id}"
            if canonical_url(url) in historical:
                continue
            local_video = base / f"{video_id}.mp4"
            normalized_url = canonical_url(url)
            current[normalized_url] = normalize_candidate({
                "id": current.get(normalized_url, {}).get("id", f"c{index}"), "creator": "@" + item.get("author", ""),
                "title": item.get("desc", "")[:60], "source_desc": item.get("desc", ""), "like": item.get("like", 0),
                "play": item.get("play_count", 0), "duration_sec": item.get("duration_sec", 0),
                "tags": item.get("tags", []), "url": url, "dance_type": item.get("dance_type", "街舞"),
                "download_status": item.get("download_status", "unknown"),
                "local_path": str(local_video.relative_to(REPO)) if local_video.exists() else "",
                "candidate_tier": "top" if index <= 10 else "backup",
            }, index)
    for index, meta_path in enumerate(sorted(base.glob("*.json")), 1):
        item = json.loads(meta_path.read_text(encoding="utf-8"))
        video_id = str(item.get("id", meta_path.stem))
        url = f"https://www.douyin.com/video/{video_id}"
        if canonical_url(url) in historical:
            continue
        normalized_url = canonical_url(url)
        current[normalized_url] = normalize_candidate({
            "id": current.get(normalized_url, {}).get("id", f"c{index}"), "creator": "@" + item.get("author", ""),
            "title": item.get("desc", "")[:60], "source_desc": item.get("desc", ""), "like": item.get("like", 0),
            "play": item.get("play_count", 0), "duration_sec": item.get("duration_sec", 0),
            "tags": item.get("tags", []), "url": url, "dance_type": item.get("dance_type", "街舞"),
                "download_status": "downloaded",
            "local_path": str(meta_path.with_suffix(".mp4").relative_to(REPO)),
            "candidate_tier": "top" if index <= 10 else "backup",
        }, index)
    config["this_week_candidates"] = sorted(current.values(), key=lambda c: c.get("like", 0), reverse=True)
    save_config(week, config)
    return config


def start_job(name: str, command: list[str]) -> dict:
    job_id = uuid.uuid4().hex[:8]
    job = {"id": job_id, "name": name, "status": "running", "output": "", "command": command}
    JOBS[job_id] = job

    def run() -> None:
        try:
            process = subprocess.Popen(command, cwd=REPO, text=True, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, bufsize=1)
            for line in process.stdout or []:
                job["output"] = (job["output"] + line)[-12000:]
            job["status"] = "done" if process.wait() == 0 else "failed"
        except Exception as error:  # noqa: BLE001
            job["status"] = "failed"
            job["output"] = f"{type(error).__name__}: {error}"

    threading.Thread(target=run, daemon=True).start()
    return job


def synthesize_preview(week: str, candidate_id: str, text: str, voice: str, rate: str) -> str:
    if voice not in VOICE_PRESETS:
        raise ValueError("不支持的配音音色")
    if rate not in VOICE_RATES:
        raise ValueError("不支持的配音语速")
    text = " ".join(text.split())
    if not text:
        raise ValueError("请先填写口播文案")
    if len(text) > 300:
        raise ValueError("试听文案不能超过 300 个字符")
    safe_week = re.fullmatch(r"\d{4}-W\d{2}", week)
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", candidate_id)[:64]
    if not safe_week or not safe_id:
        raise ValueError("无效的工作区或候选编号")
    output_dir = PREVIEW_AUDIO / week
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{safe_id}.mp3"
    tts_code = (
        "import asyncio, sys, edge_tts; "
        "asyncio.run(edge_tts.Communicate(sys.argv[1], sys.argv[2], rate=sys.argv[3]).save(sys.argv[4]))"
    )
    try:
        subprocess.run(
            [PYTHON_COMMAND, "-c", tts_code, text, voice, rate, str(output)],
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "未知错误").strip()
        raise ValueError(f"试听生成失败: {detail[-240:]}") from error
    return f"/audio/{week}/{safe_id}.mp3"


def candidate_video_path(week: str, candidate_id: str) -> Path:
    config = load_config(week)
    candidates = config.get("this_week_candidates", []) + config.get("classics_pool", [])
    candidate = next((item for item in candidates if item.get("id") == candidate_id), None)
    if not candidate or not candidate.get("local_path"):
        raise ValueError("请先下载或同步本地视频后再预览片段")
    source = (REPO / candidate["local_path"]).resolve()
    incoming = (INCOMING / week).resolve()
    if incoming not in source.parents or not source.is_file():
        raise ValueError("本地视频路径无效")
    return source


def render_clip_preview(week: str, candidate_id: str, start: float, end: float) -> str:
    if not re.fullmatch(r"\d{4}-W\d{2}", week):
        raise ValueError("无效的工作区")
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", candidate_id)[:64]
    if not safe_id or start < 0 or end <= start:
        raise ValueError("结束时间必须大于开始时间")
    duration = min(end - start, 60.0)
    source = candidate_video_path(week, candidate_id)
    output_dir = PREVIEW_VIDEO / week
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"{safe_id}.mp4"
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{start:.3f}", "-i", str(source),
             "-t", f"{duration:.3f}", "-movflags", "+faststart", "-c:v", "libx264", "-c:a", "aac", str(output)],
            check=True, capture_output=True, text=True,
        )
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "未知错误").strip()
        raise ValueError(f"视频预览生成失败: {detail[-240:]}") from error
    return f"/video-preview/{week}/{safe_id}.mp4"


class App(SimpleHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:
        print("[admin] " + fmt % args)

    def send_json(self, data: dict, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict:
        size = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(size) or b"{}")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/state":
            week = (parsed.query.split("week=", 1)[-1] or iso_week()).split("&", 1)[0]
            config = load_config(week)
            self.send_json({"week": week, "config": config, "settings": load_settings(),
                            "selected": selected_ids(config), "jobs": list(JOBS.values())[-5:]})
            return
        if parsed.path == "/api/workspaces":
            recent = int(parse_qs(parsed.query).get("recent", [12])[0])
            self.send_json({"workspaces": workspaces(recent)})
            return
        if parsed.path == "/api/jobs":
            self.send_json({"jobs": list(JOBS.values())[-10:]})
            return
        if parsed.path == "/":
            self.path = "/index.html"
        return super().do_GET()

    def do_POST(self) -> None:  # noqa: N802
        try:
            body = self.read_json()
            if self.path == "/api/settings":
                settings = {"keywords": body.get("keywords", []), "platforms": body.get("platforms", []),
                            "top_limit": int(body.get("top_limit", 30)),
                            "min_likes": int(body.get("min_likes", 0)),
                            "videos_only": bool(body.get("videos_only", True)),
                            "recent_days": int(body.get("recent_days", 7)),
                            "sort_by": body.get("sort_by", "heat_desc")}
                save_settings(settings)
                self.send_json({"ok": True, "settings": settings})
                return
            if self.path == "/api/save":
                week = body["week"]
                config = build_editor_config(week, body)
                save_config(week, config)
                self.send_json({"ok": True, "config": config})
                return
            if self.path == "/api/manual-link":
                week, url = body["week"], body["url"].strip()
                if canonical_url(url) in historical_urls(week):
                    raise ValueError("该视频已在往期使用，不能重复加入候选池")
                config = load_config(week)
                candidates = config["this_week_candidates"]
                candidates.append(normalize_candidate({"id": f"m{len(candidates) + 1}", "url": url,
                                                       "source": "投稿", "title": "待抓取投稿", "manual_note": body.get("note", "")}, len(candidates) + 1))
                save_config(week, config)
                self.send_json({"ok": True, "config": config})
                return
            if self.path == "/api/import":
                self.send_json({"ok": True, "config": import_downloads(body["week"])})
                return
            if self.path == "/api/voice-preview":
                audio_url = synthesize_preview(
                    body["week"], body["candidate_id"], body.get("text", ""),
                    body.get("voice", "zh-CN-XiaoyiNeural"), body.get("rate", "+20%"),
                )
                self.send_json({"ok": True, "audio_url": audio_url})
                return
            if self.path == "/api/clip-preview":
                video_url = render_clip_preview(
                    body["week"], body["candidate_id"], float(body.get("start", 0)), float(body.get("end", 0)),
                )
                self.send_json({"ok": True, "video_url": video_url})
                return
            if self.path == "/api/action":
                week, action = body["week"], body["action"]
                if action == "discover":
                    settings = load_settings()
                    if not settings["platforms"]:
                        raise ValueError("请至少选择一个发现平台")
                    cmd = [PYTHON_COMMAND, "scripts/discover_followed.py", "--week", week,
                           "--platforms", "|".join(settings["platforms"]), "--top", str(settings["top_limit"]),
                              "--keywords", "|".join(settings["keywords"]), "--min-likes", str(settings.get("min_likes", 0)),
                              "--recent-days", str(settings.get("recent_days", 7)),
                              "--sort", settings.get("sort_by", "heat_desc")]
                    if settings.get("videos_only", True):
                        cmd.append("--videos-only")
                elif action == "download":
                    config = load_config(week)
                    candidates = {candidate.get("id"): candidate for candidate in config.get("this_week_candidates", [])}
                    candidates.update({candidate.get("id"): candidate for candidate in config.get("classics_pool", [])})
                    video_ids = []
                    for candidate_id in selected_ids(config):
                        match = re.search(r"/video/(\d+)", candidates.get(candidate_id, {}).get("url", ""))
                        if match:
                            video_ids.append(match.group(1))
                    if not video_ids:
                        raise ValueError("请先在候选池细筛并保存至少一支抖音视频")
                    cmd = [PYTHON_COMMAND, "scripts/douyin_fetch_clean.py", "--mode", "download", "--week", week,
                           "--ids", "|".join(video_ids)]
                elif action == "render":
                    cmd = [PYTHON_COMMAND, "pipeline/render_demo.py", week]
                else:
                    raise ValueError("Unsupported action")
                self.send_json({"ok": True, "job": start_job(action, cmd)})
                return
            self.send_json({"error": "Not found"}, HTTPStatus.NOT_FOUND)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            self.send_json({"error": str(error)}, HTTPStatus.BAD_REQUEST)


def main() -> None:
    handler = partial(App, directory=str(ADMIN))
    server = ThreadingHTTPServer(("127.0.0.1", 8787), handler)
    print("BestDancer dashboard: http://127.0.0.1:8787")
    server.serve_forever()


if __name__ == "__main__":
    main()