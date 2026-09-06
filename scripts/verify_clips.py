#!/usr/bin/env python3
"""verify_clips.py — 逐支素材核对「画面 vs 名字」, 保证成片里的舞种和作者对得上。

为什么需要这一步:
  候选池里的 title / creator / dance_type 来自**搜索卡片的 DOM 文本**, 很不可靠 ——
  卡片里作者行和标题行会串位, dance_type 更是拿标题关键词猜的。W31-C 实测因此翻车:
  画面明明是坐着比手势的"手势舞", 却被标成 "Urban 街舞"; 资讯搬运号的截图被当成编舞。

这里做两件事:
  1. **权威元数据回填**: 下载完成后 dl2/<平台>_<id>.json 里的 author/title 是平台自己
     给的 (抖音来自 aweme_detail, TikTok/YouTube 来自 yt-dlp), 比爬卡片准。用它覆盖候选。
  2. **画面核对**: 抽 3 帧交给 codex, 判断这支到底是不是舞蹈、真实舞种是什么、
     画面里能不能看到原平台作者水印。非舞蹈的直接淘汰, 舞种以画面为准改写。

结果带缓存 (按 mp4 大小+mtime), 重复跑不会重复烧 token。

用法:
    python3 scripts/verify_clips.py 2026-W31-C
    python3 scripts/verify_clips.py 2026-W31-C --no-vision   # 只做元数据回填
    python3 scripts/verify_clips.py 2026-W31-C --apply       # 直接改写周配置
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "pipeline"))
from codex_client import find_codex, run_codex_json  # noqa: E402

WEEKLY = REPO / "config" / "weekly"
INCOMING = REPO / "assets" / "incoming"

# 允许出现在成片上的舞种词 —— 必须和 admin 下拉、render 的角标保持一致。
# 覆盖面要够宽: 只给 Urban/Choreography 几个选项时, 模型会把肚皮舞/东方舞硬塞进
# Choreography, 成片就写错舞种 (W31-C 实测被评估器抓到)。
DANCE_TYPES = ["Hip-hop", "Urban", "Jazz", "K-pop", "Popping", "Locking", "Breaking",
               "House", "Waacking", "Vogue", "Dancehall", "Belly", "Latin",
               "Ballet", "中国舞", "民族舞", "鬼步舞", "Choreography"]

CLIP_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["is_dance", "dance_type", "people_count", "watermark_visible",
                 "visible_handle", "scene", "confidence", "reject_reason"],
    "properties": {
        "is_dance": {"type": "boolean"},
        "dance_type": {"type": "string", "enum": DANCE_TYPES + ["Unknown"]},
        "people_count": {"type": "integer", "minimum": 0},
        "watermark_visible": {"type": "boolean"},
        "visible_handle": {"type": "string"},
        "scene": {"type": "string"},
        "confidence": {"type": "integer", "minimum": 0, "maximum": 100},
        "reject_reason": {"type": "string"},
    },
}

PROMPT_TPL = """你在给一个面向中文跳舞初学者的每周热舞榜栏目做**素材验收**。
下面是同一支候选视频里按时间顺序抽出的几帧画面。

平台给出的元数据(仅供参考, 可能不准确, 以画面为准):
- 平台: {platform}
- 作者: {author}
- 标题/简介: {desc}
- 时长: {duration}s

请只根据画面回答:
1. is_dance: 这是不是一支**真正在跳舞的编舞/翻跳视频**?
   下面这些一律算 false: 只坐着或站着比手势的「手势舞」、对口型/唱歌、
   资讯或新闻截图、纯文字卡、聊天录屏、带货或穿搭展示、纯走位vlog、
   舞蹈教学分解讲解(没有完整跳)、以及画面里根本没有人在跳的。
2. dance_type: 画面里实际的舞种, 从 {types} 里选一个; 看不出来就填 Unknown。
   注意别被标题带跑 —— 以身体动作和服装为准。
   Choreography 只在"确实是成套编舞但归不进任何具体舞种"时才用, 别拿它当万能兜底;
   看到肚皮舞/东方舞就选 Belly, 看到鬼步舞/曳步舞就选 鬼步舞, 以此类推。
3. people_count: 画面里同时在跳的人数。
4. watermark_visible: 画面里能不能看到**原平台的作者水印或 @句柄**
   (抖音右下角/小红书/TikTok 的用户名浮层)。仅作记录用, 不影响是否入选 ——
   本栏目靠成片画面常驻的 @作者 署名来满足署名要求。
5. visible_handle: 如果画面上能读到 @句柄或用户名, 原样写出来; 读不到就填空字符串。
6. scene: 一句话描述画面内容。7. confidence: 你对 is_dance 和 dance_type 判断的把握 0-100。
8. reject_reason: 如果 is_dance=false, 用一句话说明这是什么内容。

只输出符合给定 JSON schema 的结果。"""


def probe_duration(mp4: Path) -> float:
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=nk=1:nw=1", str(mp4)],
                           capture_output=True, text=True)
        return float(r.stdout.strip() or 0)
    except Exception:
        return 0.0


def extract_frames(mp4: Path, out_dir: Path, n: int = 3) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    dur = probe_duration(mp4)
    if dur <= 0:
        return []
    frames = []
    for k in range(n):
        # 均匀取点, 跳过片头片尾 (常是黑场或标题卡)
        t = dur * (k + 1) / (n + 1)
        path = out_dir / f"{mp4.stem}_{k}.jpg"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.2f}",
                        "-i", str(mp4), "-frames:v", "1", "-vf", "scale=480:-2",
                        "-q:v", "4", str(path)], capture_output=True)
        if path.exists() and path.stat().st_size > 0:
            frames.append(path)
    return frames


def cache_key(mp4: Path) -> str:
    st = mp4.stat()
    # 带上 prompt/schema 指纹: 改了判定口径就该重新判, 否则会拿旧口径的结论
    fingerprint = hashlib.sha1(
        (PROMPT_TPL + json.dumps(CLIP_SCHEMA, sort_keys=True)).encode()
    ).hexdigest()[:8]
    return f"{mp4.name}:{st.st_size}:{int(st.st_mtime)}:{fingerprint}"


def verify_one(mp4: Path, meta: dict, work_dir: Path, codex_bin: str,
               model: str | None, timeout: int) -> tuple[dict | None, str]:
    frames = extract_frames(mp4, work_dir / "frames")
    if not frames:
        return None, "抽帧失败"
    prompt = PROMPT_TPL.format(
        platform=meta.get("platform", ""),
        author=meta.get("author", ""),
        desc=(meta.get("desc") or meta.get("title") or "")[:300],
        duration=meta.get("duration_sec", 0),
        types="/".join(DANCE_TYPES),
    )
    return run_codex_json(prompt, CLIP_SCHEMA, work_dir, images=frames,
                          model=model, timeout=timeout, codex_bin=codex_bin,
                          tag=mp4.stem)


def load_dl2_meta(week: str) -> dict[str, dict]:
    """dl2/<平台>_<id>.json 是平台权威元数据, 按视频 id 索引。

    有些 mp4 是早先中断的那轮下下来的, 只有视频没有 json 边车 (下载器发现文件已存在
    就直接 skip 了)。这些也必须纳入核对, 否则它们会绕过画面判定进成片。
    """
    base = INCOMING / week / "dl2"
    out: dict[str, dict] = {}
    for mp4 in base.glob("*.mp4"):
        if mp4.name.endswith(".info.mp4"):
            continue
        stem = mp4.stem
        meta: dict = {}
        side = mp4.with_suffix(".json")
        if side.exists():
            try:
                meta = json.loads(side.read_text())
            except json.JSONDecodeError:
                meta = {}
        if not meta:
            plat, _, rest = stem.partition("_")
            meta = {"id": rest or stem, "platform": plat, "author": "", "desc": ""}
        vid = str(meta.get("id") or stem.split("_", 1)[-1])
        meta["_mp4"] = str(mp4)
        out[vid] = meta
    return out


def extract_vid(url: str) -> str | None:
    m = re.search(r"/video/([\w-]+)|/(?:explore|search_result)/([0-9a-f]+)"
                  r"|/reel/([\w-]+)|/shorts/([\w-]+)|[?&]v=([\w-]+)|youtu\.be/([\w-]+)",
                  url or "")
    return next((g for g in m.groups() if g), None) if m else None


def main() -> int:
    ap = argparse.ArgumentParser(description="核对素材画面与舞种/作者是否一致")
    ap.add_argument("week")
    ap.add_argument("--no-vision", action="store_true", help="只做权威元数据回填")
    ap.add_argument("--apply", action="store_true", help="把结果写回周配置")
    ap.add_argument("--model", default=None)
    ap.add_argument("--timeout", type=int, default=420)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--min-confidence", type=int, default=60,
                    help="低于这个把握度的判定不用来改写舞种")
    args = ap.parse_args()

    week = args.week
    cfg_path = WEEKLY / f"{week}.json"
    if not cfg_path.exists():
        print(f"[verify] 找不到 {cfg_path}", file=sys.stderr)
        return 2
    cfg = json.loads(cfg_path.read_text())
    dl2 = load_dl2_meta(week)
    print(f"[verify] 已下载素材 {len(dl2)} 支")

    work_dir = REPO / "output" / "verify" / week
    work_dir.mkdir(parents=True, exist_ok=True)
    cache_path = work_dir / "cache.json"
    cache: dict[str, dict] = {}
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text())
        except json.JSONDecodeError:
            cache = {}

    # ── 1. 权威元数据回填 ──
    fixed_meta = 0
    for cand in cfg.get("this_week_candidates", []):
        vid = extract_vid(cand.get("url", ""))
        meta = dl2.get(vid or "")
        if not meta:
            continue
        author = (meta.get("author") or "").strip()
        if author and cand.get("creator", "").lstrip("@") != author:
            cand["creator"] = "@" + author
            fixed_meta += 1
        # 标题以平台 desc 为准 (爬卡片经常把作者行/时长行当成标题)
        desc = (meta.get("desc") or meta.get("title") or "").strip()
        if desc:
            cand["source_desc"] = desc[:400]
            cand["title"] = re.sub(r"\s+", " ", desc.split("\n")[0])[:120]
        if meta.get("duration_sec"):
            cand["duration_sec"] = meta["duration_sec"]
        if meta.get("like"):
            cand["like"] = meta["like"]
        cand["local_path"] = str(Path(meta["_mp4"]).relative_to(REPO))
        cand["download_status"] = "downloaded"
    print(f"[verify] 权威元数据回填: 修正 {fixed_meta} 个作者名")

    # ── 2. 画面核对 ──
    verdicts: dict[str, dict] = {}
    vision_failures: list[tuple[str, str]] = []
    if not args.no_vision:
        codex_bin = find_codex()
        if not codex_bin:
            print("[verify] 找不到 AI CLI，无法执行画面核对", file=sys.stderr)
            return 2
        else:
            todo = []
            for vid, meta in dl2.items():
                mp4 = Path(meta["_mp4"])
                key = cache_key(mp4)
                if key in cache:
                    verdicts[vid] = cache[key]
                else:
                    todo.append((vid, meta, mp4, key))
            print(f"[verify] 画面核对: 缓存命中 {len(verdicts)}, 待判定 {len(todo)}")

            def work(item):
                vid, meta, mp4, key = item
                res, err = verify_one(mp4, meta, work_dir, codex_bin, args.model, args.timeout)
                return vid, key, res, err

            if todo:
                with ThreadPoolExecutor(max_workers=args.workers) as ex:
                    for vid, key, res, err in ex.map(work, todo):
                        if res:
                            verdicts[vid] = res
                            cache[key] = res
                            flag = "✓舞蹈" if res["is_dance"] else "✗非舞蹈"
                            print(f"  {flag} {vid} [{res['dance_type']}] "
                                  f"{res['people_count']}人 水印={'有' if res['watermark_visible'] else '无'} "
                                  f"conf={res['confidence']} — {res['scene'][:44]}", flush=True)
                        else:
                            print(f"  ? {vid} 核对失败: {err[:120]}", flush=True)
                            vision_failures.append((vid, err))
                cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2),
                                      encoding="utf-8")

    # ── 3. 应用判定 ──
    rejected: list[str] = []
    retyped = 0
    mismatched: list[str] = []
    for cand in cfg.get("this_week_candidates", []):
        vid = extract_vid(cand.get("url", ""))
        v = verdicts.get(vid or "")
        if not v:
            continue
        cand["vision"] = {k: v[k] for k in ("is_dance", "dance_type", "people_count",
                                            "watermark_visible", "scene", "confidence")}
        if not v["is_dance"]:
            rejected.append(cand["id"])
            continue
        # 舞种以画面为准, 但把握度太低就不动
        if v["dance_type"] != "Unknown" and v["confidence"] >= args.min_confidence:
            if cand.get("dance_type") != v["dance_type"]:
                retyped += 1
            cand["dance_type"] = v["dance_type"]
        # 画面读到的 @句柄和元数据作者对不上 -> 多半是搬运号
        handle = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]", "", (v.get("visible_handle") or "")).lower()
        author = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]", "", cand.get("creator", "")).lower()
        if handle and author and handle not in author and author not in handle:
            mismatched.append(f"{cand['id']}(画面@{v['visible_handle']} vs 元数据{cand['creator']})")

    print(f"[verify] 画面判定: 淘汰非舞蹈 {len(rejected)} 支, 修正舞种 {retyped} 支")
    if rejected:
        print(f"         淘汰: {rejected}")
    if mismatched:
        print(f"[verify] ⚠️ 署名存疑 (疑似搬运): {mismatched}")

    if args.apply:
        rejected_urls = sorted({c.get("url") for c in cfg.get("this_week_candidates", [])
                                if c["id"] in set(rejected) and c.get("url")})
        cfg["this_week_candidates"] = [c for c in cfg.get("this_week_candidates", [])
                                       if c["id"] not in rejected]
        cfg["deleted_ids"] = sorted(set(cfg.get("deleted_ids", [])) | set(rejected))
        # 候选 id (cN) 每轮会重编号, 所以淘汰名单必须按 URL 记, 才能在
        # auto_episode 重建候选池时继续生效
        cfg["_rejected_urls"] = sorted(set(cfg.get("_rejected_urls", [])) | set(rejected_urls))
        cfg["picks"] = [p for p in cfg.get("picks", []) if p.get("id") not in rejected]
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[verify] 已写回 {cfg_path.relative_to(REPO)} "
              f"(剩余候选 {len(cfg['this_week_candidates'])}, "
              f"累计淘汰 URL {len(cfg['_rejected_urls'])})")

    report = {"week": week, "verified": len(verdicts), "rejected": rejected,
              "retyped": retyped, "mismatched": mismatched,
              "meta_fixed": fixed_meta}
    (work_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                          encoding="utf-8")
    # 按**平台视频 id** 落一份判定, 这是给 daily_auto_generate.build_week 用的单一真源。
    # 候选池每轮都会重建、候选 id (cN) 每轮重编号, 判定结果挂在配置里迟早会丢;
    # 挂在视频 id 上就永远对得上。
    (work_dir / "verdicts.json").write_text(
        json.dumps(verdicts, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[verify] 判定已存 {(work_dir / 'verdicts.json').relative_to(REPO)} "
          f"({len(verdicts)} 支, 按视频 id 索引)")
    if not args.no_vision and vision_failures:
        print(f"[verify] ERROR: {len(vision_failures)} 支已下载素材核对失败，"
              "拒绝让未核对素材进入组稿")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
