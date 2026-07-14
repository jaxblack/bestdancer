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
DEFAULT_SETTINGS = {
    "keywords": ["urban dance 编舞", "编舞 完整", "kpop dance cover"],
    "platforms": ["douyin"],
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


def config_path(week: str) -> Path:
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
        "source_desc": item.get("source_desc", ""),
        "download_status": item.get("download_status", "unknown"),
    }


def selected_ids(config: dict) -> list[str]:
    top = [pick.get("id") for pick in config.get("picks", []) if pick.get("id")]
    classic = config.get("classic_comeback", {}).get("id")
    return top + ([classic] if classic else [])


def workspaces(recent_weeks: int = 12) -> list[dict]:
    recent_weeks = max(1, min(recent_weeks, 52))
    weeks = {
        f"{week_date.isocalendar().year}-W{week_date.isocalendar().week:02d}"
        for offset in range(recent_weeks)
        for week_date in [date.today() - timedelta(weeks=offset)]
    }
    weeks.update(path.stem for path in WEEKLY.glob("????-W??.json"))
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
    picks, narration = [], []
    for rank, candidate_id in enumerate(selected[:5], 1):
        item = candidates.get(candidate_id)
        if not item:
            continue
        difficulty = item.pop("difficulty", None) or {"stars": 3.0, "fit": item["dance_type"], "scores": {}}
        picks.append({"rank": rank, "id": candidate_id, "reason": item.get("manual_note", ""),
                      "highlight_hint": "", "cut_suggestion": "", "difficulty": difficulty})
        narration.append({"segment": "top", "rank": rank, "vo": item.get("narration", ""), "subtitle": [],
                          "on_screen": {"stars": difficulty.get("stars", 3.0), "tag": f"本周No.{rank}",
                                        "core_moves": [item["dance_type"]]}, "beginner_tip": ""})
    classic_id = selected[5] if len(selected) > 5 and selected[5] in candidates else None
    classic_pool = [candidates.pop(classic_id)] if classic_id else []
    classic = {}
    if classic_id:
        classic = {"id": classic_id, "reason": "特别加映", "difficulty": {"stars": 3.0, "fit": "基础练习", "scores": {}}}
        narration.append({"segment": "classic", "rank": None, "vo": "", "subtitle": [],
                          "on_screen": {"stars": 3.0, "tag": "特别加映", "core_moves": [classic_pool[0]["dance_type"]]},
                          "beginner_tip": ""})
    config = {**old, "this_week_candidates": list(candidates.values()), "classics_pool": classic_pool,
              "picks": picks, "classic_comeback": classic, "narration": narration}
    config["episode"].update(payload.get("episode", {}))
    return config


def import_downloads(week: str) -> dict:
    incoming = INCOMING / week
    base = incoming / "dl2"
    config = load_config(week)
    current = {c["url"]: c for c in config.get("this_week_candidates", []) if c.get("url")}
    generic_candidates = incoming / "candidates"
    for candidate_path in sorted(generic_candidates.glob("*.json")):
        for index, item in enumerate(json.loads(candidate_path.read_text(encoding="utf-8")), 1):
            url = item.get("url", "")
            if not url:
                continue
            current[url] = normalize_candidate({
                **item, "id": current.get(url, {}).get("id", item.get("id", f"g{index}")),
            }, index)
    ranked_path = incoming / "ranked_candidates.json"
    if ranked_path.exists():
        for index, item in enumerate(json.loads(ranked_path.read_text(encoding="utf-8")), 1):
            video_id = str(item.get("id", ""))
            if not video_id:
                continue
            url = f"https://www.douyin.com/video/{video_id}"
            local_video = base / f"{video_id}.mp4"
            current[url] = normalize_candidate({
                "id": current.get(url, {}).get("id", f"c{index}"), "creator": "@" + item.get("author", ""),
                "title": item.get("desc", "")[:60], "source_desc": item.get("desc", ""), "like": item.get("like", 0),
                "play": item.get("play_count", 0), "duration_sec": item.get("duration_sec", 0),
                "tags": item.get("tags", []), "url": url, "dance_type": item.get("dance_type", "街舞"),
                "download_status": item.get("download_status", "unknown"),
                "local_path": str(local_video.relative_to(REPO)) if local_video.exists() else "",
            }, index)
    for index, meta_path in enumerate(sorted(base.glob("*.json")), 1):
        item = json.loads(meta_path.read_text(encoding="utf-8"))
        video_id = str(item.get("id", meta_path.stem))
        url = f"https://www.douyin.com/video/{video_id}"
        current[url] = normalize_candidate({
            "id": current.get(url, {}).get("id", f"c{index}"), "creator": "@" + item.get("author", ""),
            "title": item.get("desc", "")[:60], "source_desc": item.get("desc", ""), "like": item.get("like", 0),
            "play": item.get("play_count", 0), "duration_sec": item.get("duration_sec", 0),
            "tags": item.get("tags", []), "url": url, "dance_type": item.get("dance_type", "街舞"),
                "download_status": "downloaded",
            "local_path": str(meta_path.with_suffix(".mp4").relative_to(REPO)),
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