#!/usr/bin/env python3
"""evaluate_segments.py — 逐段评估入选舞段, 差的直接换掉由后面的顶上。

和 pipeline/evaluate_demo.py 的分工:
  evaluate_demo    看**成片整体** (规格/黑屏/响度/节奏/钩子/合规)
  evaluate_segments 看**每一支舞段本身** —— 舞种对不对、标题贴不贴画面、难度几星、
                    表现力够不够。表现力太差的直接淘汰, 名次由后面的舞段顺延。

关键点: 抽帧用的是**渲染真正会用到的那个时间窗**(复用 render_demo.stable_window),
所以评的就是观众会看到的画面, 而不是整支原片。

用法:
    python3 pipeline/evaluate_segments.py 2026-W31-C
    python3 pipeline/evaluate_segments.py 2026-W31-C --apply   # 回写并淘汰差的
    python3 pipeline/evaluate_segments.py 2026-W31-C --min-score 60
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "pipeline"))
from codex_client import find_codex, run_codex_json  # noqa: E402

WEEKLY = REPO / "config" / "weekly"
OUTPUT = REPO / "output"

DANCE_TYPES = ["Hip-hop", "Urban", "Jazz", "K-pop", "Popping", "Locking", "Breaking",
               "House", "Waacking", "Vogue", "Dancehall", "Belly", "Latin",
               "Ballet", "中国舞", "民族舞", "鬼步舞", "Choreography"]


def _load_render():
    spec = importlib.util.spec_from_file_location("render_demo", REPO / "pipeline" / "render_demo.py")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except SystemExit:
        pass
    return mod


SEGMENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["dance_type", "dance_type_ok", "title_ok", "title_suggestion",
                 "difficulty_stars", "difficulty_reason", "expressiveness",
                 "vertical_fit", "overall", "replace", "replace_reason", "suggestions"],
    "properties": {
        "dance_type": {"type": "string", "enum": DANCE_TYPES + ["Unknown"]},
        "dance_type_ok": {"type": "boolean"},
        "title_ok": {"type": "boolean"},
        "title_suggestion": {"type": "string"},
        "difficulty_stars": {"type": "integer", "minimum": 1, "maximum": 5},
        "difficulty_reason": {"type": "string"},
        "expressiveness": {"type": "integer", "minimum": 0, "maximum": 100},
        "vertical_fit": {"type": "integer", "minimum": 0, "maximum": 100},
        "overall": {"type": "integer", "minimum": 0, "maximum": 100},
        "replace": {"type": "boolean"},
        "replace_reason": {"type": "string"},
        "suggestions": {"type": "array", "items": {"type": "string"}},
    },
}

PROMPT_TPL = """你在给「本周热舞」这个栏目做**单支舞段验收**。栏目面向中文跳舞初学者,
竖版 9:16 短视频, 每期 TOP5 + 特别加映。

下面这些帧来自**成片里实际会用到的那一段**(不是整支原片), 按时间顺序排列。

这一段当前的标注:
- 名次: {rank}
- 舞种: {dance_type}
- 标题: {title}
- 作者: {creator}
- 星级: {stars}
- 取用区间: 原片 {start:.1f}s 起, 共 {dur:.1f}s (原片总长 {total:.1f}s)
- 平台描述原文: {desc}

请逐项判断:
1. dance_type / dance_type_ok: 画面里**实际**是什么舞种(从 {types} 里选)?
   和当前标注一致吗? 以身体动作和服装为准, 别被标题带跑。
2. title_ok / title_suggestion: 当前标题贴合画面内容吗? 不贴合就给一个更好的:
   **中文优先、不超过 12 个字、要能概括这支舞的看点**(例如"双人配合 Urban"
   "女团齐舞翻跳")。标题已经合适就把原标题填进 title_suggestion。
3. difficulty_stars / difficulty_reason: 对**零基础初学者**而言的难度, 1-5 星。
   参考: 1=跟着比划就行; 3=需要练几遍的常规编舞; 5=含高难技巧/极快速度/大幅度地板动作。
4. expressiveness: 这一段的**表现力**打分 0-100 —— 动作是否舒展有力、有没有记忆点、
   镜头是否拍清楚了舞者、看完会不会想学。这是最重要的一项。
5. vertical_fit: 竖版适配 0-100 —— 舞者在 9:16 画面里是否够大、有没有被裁掉、
   主体是否在中间。横屏赛事/舞台录像裁成竖版后人物很小的, 这项要打低分。
6. overall: 综合这一段值不值得放进本期榜单 0-100。
7. replace / replace_reason: 如果这一段**表现力太差或竖版适配太差, 不值得占一个名次**,
   就 replace=true 并说明原因。宁缺毋滥 —— 榜单只有 5 个位置。
8. suggestions: 针对这一段的**具体**修改建议(例如"从 40s 起截更好, 那里是副歌高潮"
   "标题应改为 xxx" "该段人物过小建议换素材"), 每条一句话。

只输出符合给定 JSON schema 的结果。"""


def extract_window_frames(clip: Path, start: float, dur: float,
                          out_dir: Path, tag: str, n: int = 4) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    frames = []
    for k in range(n):
        t = start + dur * (k + 0.5) / n
        p = out_dir / f"{tag}_{k}.jpg"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.2f}",
                        "-i", str(clip), "-frames:v", "1", "-vf", "scale=480:-2",
                        "-q:v", "3", str(p)], capture_output=True)
        if p.exists() and p.stat().st_size > 0:
            frames.append(p)
    return frames


def probe_dur(p: Path) -> float:
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=nk=1:nw=1", str(p)], capture_output=True, text=True)
        return float(r.stdout.strip() or 0)
    except Exception:
        return 0.0


def main() -> int:
    ap = argparse.ArgumentParser(description="逐段评估入选舞段")
    ap.add_argument("week")
    ap.add_argument("--apply", action="store_true", help="回写舞种/标题/星级并淘汰差段")
    ap.add_argument("--min-score", type=int, default=60, help="overall 低于此值即淘汰")
    ap.add_argument("--min-expressiveness", type=int, default=55)
    ap.add_argument("--model", default=None)
    ap.add_argument("--timeout", type=int, default=420)
    ap.add_argument("--workers", type=int, default=4)
    args = ap.parse_args()

    week = args.week
    cfg_path = WEEKLY / f"{week}.json"
    if not cfg_path.exists():
        print(f"[seg] 找不到 {cfg_path}", file=sys.stderr)
        return 2
    cfg = json.loads(cfg_path.read_text())
    render = _load_render()

    items = [("top", p) for p in cfg.get("picks", [])]
    if cfg.get("classic_comeback"):
        items.append(("classic", cfg["classic_comeback"]))
    if not items:
        print("[seg] 本期还没有入选舞段", file=sys.stderr)
        return 2

    codex_bin = find_codex()
    if not codex_bin:
        print("[seg] 找不到 codex CLI", file=sys.stderr)
        return 2

    work = OUTPUT / "segments_eval" / week
    work.mkdir(parents=True, exist_ok=True)
    cache_path = work / "cache.json"
    cache = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text())
        except json.JSONDecodeError:
            cache = {}
    fp = hashlib.sha1((PROMPT_TPL + json.dumps(SEGMENT_SCHEMA, sort_keys=True)).encode()).hexdigest()[:8]

    # 渲染时每段实际时长(和 render_demo 的 rank_max 保持一致)
    rank_dur = {5: 11.5, 4: 12.5, 3: 13.5, 2: 14.5, 1: 15.5}

    jobs = []
    for kind, pk in items:
        clip = render.find_clip(week, pk.get("id"))
        if not clip:
            print(f"[seg] {pk.get('id')} 没有落盘素材, 跳过")
            continue
        total = probe_dur(clip)
        want = rank_dur.get(pk.get("rank"), 13.0) if kind == "top" else 13.0
        want = min(want, max(total - 0.2, 1.0))
        start = pk.get("clip_start_sec") or render.stable_window(clip, want, total, "ffmpeg")
        key = f"{clip.name}:{start:.2f}:{want:.2f}:{fp}"
        jobs.append({"kind": kind, "pick": pk, "clip": clip, "total": total,
                     "start": float(start), "want": float(want), "key": key})

    def run_one(j):
        if j["key"] in cache:
            return j, cache[j["key"]], ""
        pk = j["pick"]
        tag = f"{pk.get('id')}_{pk.get('rank') or 'x'}"
        frames = extract_window_frames(j["clip"], j["start"], j["want"], work / "frames", tag)
        if not frames:
            return j, None, "抽帧失败"
        prompt = PROMPT_TPL.format(
            rank=pk.get("rank") or "特别加映",
            dance_type=pk.get("dance_type", ""), title=pk.get("title", "")[:80],
            creator=pk.get("creator", ""),
            stars=(pk.get("difficulty") or {}).get("stars", "?"),
            start=j["start"], dur=j["want"], total=j["total"],
            desc=(pk.get("source_desc") or "")[:200],
            types="/".join(DANCE_TYPES))
        res, err = run_codex_json(prompt, SEGMENT_SCHEMA, work, images=frames,
                                  model=args.model, timeout=args.timeout,
                                  codex_bin=codex_bin, tag=tag)
        return j, res, err

    print(f"[seg] 逐段评估 {len(jobs)} 段 (缓存 {sum(1 for j in jobs if j['key'] in cache)} 段)")
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for j, res, err in ex.map(run_one, jobs):
            if res is None:
                print(f"  ? {j['pick'].get('id')} 评估失败: {err[:90]}")
                continue
            cache[j["key"]] = res
            results.append((j, res))
    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")

    # ── 汇报 ──
    drop_ids, drop_urls = [], []
    print()
    for j, r in sorted(results, key=lambda x: (x[0]["pick"].get("rank") or 99)):
        pk = j["pick"]
        rank = pk.get("rank") or "加映"
        bad = (r["replace"] or r["overall"] < args.min_score
               or r["expressiveness"] < args.min_expressiveness)
        mark = "✗淘汰" if bad else "✓保留"
        print(f"{mark} No.{rank} {pk.get('creator','')} [{r['dance_type']}] "
              f"综合{r['overall']} 表现力{r['expressiveness']} 竖版{r['vertical_fit']} "
              f"{r['difficulty_stars']}星")
        if not r["dance_type_ok"]:
            print(f"      舞种应为 {r['dance_type']} (原 {pk.get('dance_type')})")
        if not r["title_ok"]:
            print(f"      标题建议: {r['title_suggestion']}")
        print(f"      难度: {r['difficulty_reason'][:70]}")
        for s in r["suggestions"][:2]:
            print(f"      · {s[:78]}")
        if bad:
            print(f"      淘汰原因: {r['replace_reason'][:78]}")
            drop_ids.append(pk.get("id"))
            if pk.get("url"):
                drop_urls.append(pk["url"])

    if args.apply:
        by_id = {c.get("id"): c for c in cfg.get("this_week_candidates", [])}
        for j, r in results:
            pk = j["pick"]
            cand = by_id.get(pk.get("id"))
            targets = [pk] + ([cand] if cand else [])
            for t in targets:
                # 舞种以画面为准
                if not r["dance_type_ok"] and r["dance_type"] != "Unknown":
                    t["dance_type"] = r["dance_type"]
                # 标题换成更贴画面的短标题
                if not r["title_ok"] and r["title_suggestion"].strip():
                    t["title"] = r["title_suggestion"].strip()[:40]
                # 难度星级: 之前全片写死 3 星, 现在按画面给
                t.setdefault("difficulty", {})
                t["difficulty"]["stars"] = r["difficulty_stars"]
                t["difficulty"]["fit"] = t.get("dance_type", "")
                t["segment_eval"] = {k: r[k] for k in
                                     ("overall", "expressiveness", "vertical_fit",
                                      "difficulty_stars", "suggestions")}
        if drop_ids:
            cfg["picks"] = [p for p in cfg.get("picks", []) if p.get("id") not in drop_ids]
            if (cfg.get("classic_comeback") or {}).get("id") in drop_ids:
                cfg["classic_comeback"] = {}
            cfg["this_week_candidates"] = [c for c in cfg.get("this_week_candidates", [])
                                           if c.get("id") not in drop_ids]
            cfg["deleted_ids"] = sorted(set(cfg.get("deleted_ids", [])) | set(drop_ids))
            # 按 URL 记, 候选 id 每轮会重编号
            cfg["_rejected_urls"] = sorted(set(cfg.get("_rejected_urls", [])) | set(drop_urls))
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n[seg] 已回写 {cfg_path.relative_to(REPO)}"
              f" (淘汰 {len(drop_ids)} 段, 剩余 picks {len(cfg.get('picks', []))})")
        if drop_ids:
            print("[seg] 重新跑一次组稿即可由后面的舞段顶替名次")

    (work / "report.json").write_text(json.dumps(
        {"week": week, "min_score": args.min_score,
         "segments": [{"id": j["pick"].get("id"), "rank": j["pick"].get("rank"),
                       "creator": j["pick"].get("creator"),
                       "start_sec": j["start"], "dur_sec": j["want"], **r}
                      for j, r in results]},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[seg] 报告: {(work / 'report.json').relative_to(REPO)}")
    return 1 if drop_ids else 0


if __name__ == "__main__":
    raise SystemExit(main())
