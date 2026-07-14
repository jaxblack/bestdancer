#!/usr/bin/env python3
"""本周热舞 · 素材入库 (manual-upload intake)

抖音 / 韩舞有反爬时，改为人工下载 + 上传：把片段放进
    assets/incoming/<week>/
文件名约定（双下划线分隔）：
    <id>__<source>__<slug>.<ext>
      id     : c1..cN 本周新舞 / k1..kN 经典回归候选
      source : kdance(韩舞) | douyin(抖音)
      slug   : 英文 / 拼音短标题，用连字符
    例: c1__kdance__spark.mp4 , c2__douyin__hand-clap.mp4 , k1__kdance__classic-hit.mp4

用法:
    python pipeline/intake.py 2026-W29

把扫描到的片段合并进 config/weekly/<week>.json 的 this_week_candidates /
classics_pool，补 local_path 与 duration_sec（有 ffprobe 时），并保留已填的人工字段。
仅用标准库；ffprobe 缺失时 duration_sec 记为 null。
"""
from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
SOURCE_MAP = {"kdance": "韩舞", "douyin": "抖音", "韩舞": "韩舞", "抖音": "抖音"}
REPO = Path(__file__).resolve().parents[1]


def probe_duration(path: Path) -> int | None:
    """用 ffprobe 读时长（秒，取整）；无 ffprobe 或失败则返回 None。"""
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True, text=True, timeout=30,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    value = out.stdout.strip()
    if out.returncode != 0 or not value:
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


def parse_name(stem: str) -> tuple[str, str, str] | None:
    """<id>__<source>__<slug> -> (id, 归一化 source, slug)；不符合返回 None。"""
    parts = stem.split("__")
    if len(parts) < 3:
        return None
    cid, source, slug = parts[0], parts[1], "__".join(parts[2:])
    if not cid or not slug:
        return None
    return cid.strip(), SOURCE_MAP.get(source.strip().lower(), source.strip()), slug.strip()


def blank_candidate(cid: str, source: str, slug: str, local_path: str, duration: int | None) -> dict:
    return {
        "id": cid,
        "source": source,
        "title": slug.replace("-", " "),
        "creator": "",
        "song": "",
        "duration_sec": duration,
        "play": None,
        "like": None,
        "share": None,
        "tags": [],
        "url": "",
        "local_path": local_path,
        "move_notes": "",
    }


def load_config(week: str) -> dict:
    cfg = REPO / "config" / "weekly" / f"{week}.json"
    if cfg.exists():
        return json.loads(cfg.read_text(encoding="utf-8"))
    return {
        "episode": {
            "week": week, "theme": "", "platforms": ["douyin", "xiaohongshu"],
            "voice": "young_female", "top_n": 5, "classic_n": 1,
        },
        "this_week_candidates": [],
        "classics_pool": [],
        "picks": [],
        "classic_comeback": {},
        "narration": "",
        "metadata": "",
    }


def upsert(items: list, cand: dict) -> str:
    """按 id 合并：已存在则只补空字段 + 刷新 local_path；否则新增。"""
    for existing in items:
        if existing.get("id") == cand["id"]:
            existing["local_path"] = cand["local_path"]
            if not existing.get("duration_sec") and cand["duration_sec"]:
                existing["duration_sec"] = cand["duration_sec"]
            if not existing.get("title"):
                existing["title"] = cand["title"]
            if not existing.get("source"):
                existing["source"] = cand["source"]
            return "updated"
    items.append(cand)
    return "added"


def main() -> int:
    parser = argparse.ArgumentParser(description="register uploaded clips into the weekly config")
    parser.add_argument("week", help="ISO week, e.g. 2026-W29")
    args = parser.parse_args()

    incoming = REPO / "assets" / "incoming" / args.week
    if not incoming.is_dir():
        print(f"[!] 目录不存在: {incoming}")
        print("    先建目录并放入片段（命名见脚本头注释）。")
        return 1

    cfg = load_config(args.week)
    added = updated = skipped = 0

    for f in sorted(incoming.iterdir()):
        if f.suffix.lower() not in VIDEO_EXTS:
            continue
        parsed = parse_name(f.stem)
        if not parsed:
            print(f"[skip] 命名不符合 <id>__<source>__<slug>: {f.name}")
            skipped += 1
            continue
        cid, source, slug = parsed
        rel = f.relative_to(REPO).as_posix()
        cand = blank_candidate(cid, source, slug, rel, probe_duration(f))
        bucket = cfg["classics_pool"] if cid.lower().startswith("k") else cfg["this_week_candidates"]
        result = upsert(bucket, cand)
        if result == "added":
            added += 1
        else:
            updated += 1
        print(f"[{result}] {cid} <- {f.name}")

    out = REPO / "config" / "weekly" / f"{args.week}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\n写入 {out.relative_to(REPO).as_posix()} | +{added} 新增 / {updated} 更新 / {skipped} 跳过")
    print("下一步：补全 creator/song/url/move_notes 后，跑 prompt 01->02->03->04。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
