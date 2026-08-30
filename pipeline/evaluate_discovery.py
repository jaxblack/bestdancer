#!/usr/bin/env python3
"""evaluate_discovery.py — 给每个平台的「采集效果」打标准化分数。

为什么需要:
  各平台搜索页结构不同, 关键词和筛选条件要反复调。但"这次调得比上次好吗"如果只能
  靠肉眼翻 candidates/*.json, 根本没法收敛。这里把采集质量拆成可量化的维度, 每次
  改完关键词/筛选条件跑一次, 分数和历史对比就能看出方向对不对。

  实测第一次跑就暴露: instagram 的 published_at / like / author 提取率全是 0%,
  也就是说它的"最近 7 天""按热度排序"全是摆设 —— 这种问题看总数是看不出来的。

七个维度 (每个 0-100, 加权成综合分):
  yield        采集量      —— 够不够挑
  metadata     元数据完整度 —— 日期/热度/作者能不能解析出来 (筛选的前提)
  recency      时效        —— 落在 recent_days 内的比例 (时间筛选框是否真的生效)
  heat         热度        —— 点赞中位数/高位数
  download     可下载性     —— 候选真能落盘的比例
  relevance    街舞匹配度   —— 下载到的片子里真的是舞蹈的比例 (来自 verify_clips 判定)
  visual       画面        —— 分辨率/黑边/时长适配

用法:
    python3 pipeline/evaluate_discovery.py 2026-W31-C
    python3 pipeline/evaluate_discovery.py 2026-W31-C --platforms douyin|tiktok
    python3 pipeline/evaluate_discovery.py 2026-W31-C --compare   # 和历史比
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import statistics
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
INCOMING = REPO / "assets" / "incoming"
OUTPUT = REPO / "output"
SETTINGS = REPO / "admin" / "settings.json"

# 综合分权重。metadata 和 relevance 给得重: 前者决定筛选能不能做,
# 后者决定选出来的东西能不能用 —— 这两个塌了, 采多少都是白采。
WEIGHTS = {
    "yield": 0.10,
    "metadata": 0.20,
    "recency": 0.18,
    "heat": 0.10,
    "download": 0.14,
    "relevance": 0.20,
    "visual": 0.08,
}

TARGET_POOL = 60          # 够挑的候选数
GOOD_LIKE_MEDIAN = 20_000  # 点赞中位数到这个量级算"热度合格"
DUR_MIN, DUR_MAX = 8, 180


def pct(numerator: float, denominator: float) -> float:
    return 100.0 * numerator / denominator if denominator else 0.0


def clamp(x: float) -> float:
    return max(0.0, min(100.0, x))


def days_since(s: str | None) -> int | None:
    if not s:
        return None
    try:
        return (dt.date.today() - dt.date.fromisoformat(s)).days
    except (ValueError, TypeError):
        return None


def ffprobe_stream(mp4: Path) -> dict:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,duration,bit_rate",
             "-show_entries", "format=duration,bit_rate", "-of", "json", str(mp4)],
            capture_output=True, text=True, timeout=30)
        d = json.loads(r.stdout or "{}")
    except Exception:
        return {}
    st = (d.get("streams") or [{}])[0]
    fmt = d.get("format") or {}
    return {
        "width": int(st.get("width") or 0),
        "height": int(st.get("height") or 0),
        "duration": float(st.get("duration") or fmt.get("duration") or 0),
        "bit_rate": int(st.get("bit_rate") or fmt.get("bit_rate") or 0),
    }


def letterbox_ratio(mp4: Path, info: dict) -> float:
    """用 cropdetect 估算上下黑边占比。

    有些"竖版"素材其实是横版视频上下补黑边硬凑成 9:16, 裁切后主体只占半屏 ——
    评估器在成片上点名过"下方大面积纯黑区域"。这里在选材阶段就量化出来。
    """
    h = info.get("height") or 0
    if h <= 0:
        return 0.0
    try:
        r = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-ss", "1", "-i", str(mp4),
             "-vf", "cropdetect=24:2:0", "-frames:v", "80", "-an", "-f", "null", "-"],
            capture_output=True, text=True, timeout=60)
    except Exception:
        return 0.0
    crops = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", (r.stderr or "") + (r.stdout or ""))
    if not crops:
        return 0.0
    ch = max(int(c[1]) for c in crops)
    return clamp(100.0 * (h - ch) / h) / 100.0


def load_verdicts(week: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for f in OUTPUT.glob("verify/*/verdicts.json"):
        try:
            out.update(json.loads(f.read_text()))
        except Exception:
            continue
    return out


def downloaded_by_platform(week: str) -> dict[str, list[Path]]:
    out: dict[str, list[Path]] = {}
    for mp4 in (INCOMING / week / "dl2").glob("*.mp4"):
        if mp4.name.endswith(".info.mp4"):
            continue
        plat = mp4.stem.split("_", 1)[0]
        out.setdefault(plat, []).append(mp4)
    return out


def vid_of(mp4: Path) -> str:
    return mp4.stem.split("_", 1)[-1]


def pool_vids(cands: list[dict]) -> set[str]:
    """当前候选池里的视频 id 集合。"""
    out = set()
    for c in cands:
        m = re.search(r"/video/([\w-]+)|/(?:explore|search_result)/([0-9a-f]+)"
                      r"|/reel/([\w-]+)|/shorts/([\w-]+)|[?&]v=([\w-]+)|youtu\.be/([\w-]+)",
                      c.get("url") or "")
        if m:
            v = next((g for g in m.groups() if g), None)
            if v:
                out.add(v)
        if c.get("id"):
            out.add(str(c["id"]))
    return out


def score_platform(platform: str, cands: list[dict], clips: list[Path],
                   verdicts: dict[str, dict], recent_days: int,
                   deep_visual: bool) -> dict:
    n = len(cands)
    notes: list[str] = []
    dims: dict[str, float | None] = {}
    # 只统计**当前候选池里**的素材。否则改完关键词后, 分数还被上一轮关键词下载的
    # 老素材拖着走, 根本看不出这次调整的效果 (调参循环就失效了)。
    in_pool = pool_vids(cands)
    stale = [c for c in clips if vid_of(c) not in in_pool]
    clips = [c for c in clips if vid_of(c) in in_pool]

    # ── yield 采集量 ──
    dims["yield"] = clamp(pct(n, TARGET_POOL))
    if n < TARGET_POOL:
        notes.append(f"候选只有 {n} 条 (目标 {TARGET_POOL}): 关键词太窄或被限流")
    if stale:
        notes.append(f"另有 {len(stale)} 支已下载素材不属于当前候选池 "
                     f"(上一轮关键词留下的), 未计入匹配度/画面")

    # ── metadata 元数据完整度 ──
    date_cov = pct(sum(1 for c in cands if c.get("published_at")), n)
    like_cov = pct(sum(1 for c in cands if (c.get("like") or 0) > 0), n)
    author_cov = pct(sum(1 for c in cands
                         if (c.get("author") or "unknown") not in ("", "unknown")), n)
    dims["metadata"] = (date_cov + like_cov + author_cov) / 3 if n else 0.0
    if n and date_cov < 50:
        notes.append(f"发布时间解析率仅 {date_cov:.0f}% → 时间筛选形同虚设, "
                     f"要修 discover_universal 里 {platform} 的日期选择器")
    if n and like_cov < 50:
        notes.append(f"点赞解析率仅 {like_cov:.0f}% → 热度排序无效, 只能按出现顺序取")
    if n and author_cov < 50:
        notes.append(f"作者解析率仅 {author_cov:.0f}% → 署名要靠下载后的元数据兜底")

    # ── recency 时效 ──
    dated = [d for d in (days_since(c.get("published_at")) for c in cands) if d is not None]
    if dated:
        recent = sum(1 for d in dated if d <= recent_days)
        dims["recency"] = clamp(pct(recent, len(dated)))
        notes.append(f"有日期的 {len(dated)} 条里 {recent} 条在 {recent_days} 天内 "
                     f"(中位 {statistics.median(dated):.0f} 天)")
    else:
        dims["recency"] = None
        notes.append("没有任何可解析日期 → 时效无法评估")

    # ── heat 热度 ──
    likes = [c.get("like") or 0 for c in cands if (c.get("like") or 0) > 0]
    if likes:
        med = statistics.median(likes)
        dims["heat"] = clamp(100.0 * med / GOOD_LIKE_MEDIAN)
        notes.append(f"点赞中位 {med:,.0f} / 最高 {max(likes):,}")
    else:
        dims["heat"] = None

    # ── download 可下载性 ──
    # 分母用"被真正尝试过的那批" (候选池前 max_per_platform 条) 不好还原,
    # 这里用一个务实口径: 落盘数 / min(候选数, 12)
    attempted = min(n, 12) if n else 0
    dims["download"] = clamp(pct(len(clips), attempted)) if attempted else None
    if attempted and not clips:
        notes.append("一支都没下下来: 登录态失效 / 被限速 / 下载器没接这个平台")

    # ── relevance 街舞匹配度 ──
    judged = [verdicts[vid_of(c)] for c in clips if vid_of(c) in verdicts]
    if judged:
        dance = [v for v in judged if v.get("is_dance")]
        dims["relevance"] = clamp(pct(len(dance), len(judged)))
        conf = statistics.mean([v.get("confidence", 0) for v in judged]) if judged else 0
        notes.append(f"画面核对 {len(judged)} 支, 真舞蹈 {len(dance)} 支 (判定把握 {conf:.0f})")
        if len(dance) < len(judged):
            bad = [v.get("reject_reason", "")[:24] for v in judged if not v.get("is_dance")]
            notes.append("非舞蹈样本: " + "; ".join(x for x in bad[:3] if x))
    else:
        dims["relevance"] = None
        if clips:
            notes.append("下载了但还没跑 verify_clips.py, 匹配度未知")

    # ── visual 画面 ──
    if clips and deep_visual:
        vis: list[float] = []
        letterboxed = 0
        for mp4 in clips:
            info = ffprobe_stream(mp4)
            s = 100.0
            if info.get("height", 0) < 720:
                s -= 25
            dur = info.get("duration", 0)
            if not (DUR_MIN <= dur <= DUR_MAX):
                s -= 30
            lb = letterbox_ratio(mp4, info)
            if lb > 0.12:
                s -= 35
                letterboxed += 1
            if info.get("bit_rate", 0) and info["bit_rate"] < 800_000:
                s -= 15
            vis.append(clamp(s))
        dims["visual"] = statistics.mean(vis) if vis else None
        if letterboxed:
            notes.append(f"{letterboxed}/{len(clips)} 支有明显黑边 (横版补黑凑竖版), "
                         f"成片里会露出大片黑区")
    else:
        dims["visual"] = None

    # ── 综合分: 只对能评估的维度加权, 权重重新归一 ──
    usable = {k: v for k, v in dims.items() if v is not None}
    wsum = sum(WEIGHTS[k] for k in usable)
    overall = sum(WEIGHTS[k] * v for k, v in usable.items()) / wsum if wsum else 0.0

    return {
        "platform": platform,
        "candidates": n,
        "downloaded": len(clips),
        "dimensions": {k: (round(v, 1) if v is not None else None) for k, v in dims.items()},
        "coverage": {"date": round(date_cov, 1), "like": round(like_cov, 1),
                     "author": round(author_cov, 1)},
        "overall": round(overall, 1),
        "evaluated_dims": sorted(usable),
        "notes": notes,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="给各平台的采集效果打分")
    ap.add_argument("week")
    ap.add_argument("--platforms", default=None, help="竖线分隔; 默认评估所有有候选文件的平台")
    ap.add_argument("--recent-days", type=int, default=None)
    ap.add_argument("--no-visual", action="store_true", help="跳过逐支 ffprobe/cropdetect")
    ap.add_argument("--compare", action="store_true", help="和历史记录对比")
    args = ap.parse_args()

    week = args.week
    cand_dir = INCOMING / week / "candidates"
    if not cand_dir.exists():
        print(f"[disc] 没有 {cand_dir}", file=sys.stderr)
        return 2

    settings = {}
    if SETTINGS.exists():
        try:
            settings = json.loads(SETTINGS.read_text())
        except json.JSONDecodeError:
            pass
    recent_days = args.recent_days or int(settings.get("recent_days", 7))

    if args.platforms:
        platforms = [p for p in args.platforms.split("|") if p]
    else:
        platforms = sorted(f.stem for f in cand_dir.glob("*.json") if not f.stem.startswith("_"))

    verdicts = load_verdicts(week)
    dl = downloaded_by_platform(week)

    results = []
    for p in platforms:
        f = cand_dir / f"{p}.json"
        try:
            cands = json.loads(f.read_text()) if f.exists() else []
        except json.JSONDecodeError:
            cands = []
        results.append(score_platform(p, cands, dl.get(p, []), verdicts,
                                      recent_days, not args.no_visual))

    results.sort(key=lambda r: -r["overall"])

    # ── 输出 ──
    print(f"\n采集效果评分 · {week} (recent_days={recent_days}, 关键词 {len(settings.get('keywords', []))} 个)")
    print("=" * 96)
    header = (f"{'平台':<11}{'综合':>6}{'采集':>6}{'元数据':>7}{'时效':>6}"
              f"{'热度':>6}{'下载':>6}{'匹配':>6}{'画面':>6}   候选/落盘")
    print(header)
    print("-" * 96)
    for r in results:
        d = r["dimensions"]
        def cell(k):
            v = d.get(k)
            return f"{v:>6.0f}" if v is not None else f"{'—':>6}"
        print(f"{r['platform']:<11}{r['overall']:>6.0f}" + "".join(
            cell(k) for k in ["yield", "metadata", "recency", "heat",
                              "download", "relevance", "visual"])
              + f"   {r['candidates']:>3}/{r['downloaded']:<3}")
    print("-" * 96)

    for r in results:
        if r["notes"]:
            print(f"\n[{r['platform']}] 综合 {r['overall']:.0f}")
            for nline in r["notes"]:
                print(f"   · {nline}")

    # ── 存档 + 历史对比 ──
    out_dir = OUTPUT / "discovery_eval" / week
    out_dir.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "week": week,
        "evaluated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "recent_days": recent_days,
        "keywords": settings.get("keywords", []),
        "platforms": results,
    }
    (out_dir / "report.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")

    history_path = OUTPUT / "discovery_eval" / "history.jsonl"
    history_path.parent.mkdir(parents=True, exist_ok=True)
    with history_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(snapshot, ensure_ascii=False) + "\n")

    if args.compare and history_path.exists():
        rows = [json.loads(l) for l in history_path.read_text().splitlines() if l.strip()]
        rows = rows[:-1][-5:]  # 不含刚写进去的这条
        if rows:
            print("\n历史对比 (同平台综合分):")
            for prev in rows:
                pm = {p["platform"]: p["overall"] for p in prev["platforms"]}
                cur = {p["platform"]: p["overall"] for p in results}
                diffs = []
                for k in sorted(set(pm) & set(cur)):
                    delta = cur[k] - pm[k]
                    diffs.append(f"{k} {pm[k]:.0f}→{cur[k]:.0f} ({delta:+.0f})")
                print(f"   {prev['evaluated_at']}: " + "; ".join(diffs))

    print(f"\n报告: {(out_dir / 'report.json').relative_to(REPO)}")
    print(f"历史: {history_path.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
