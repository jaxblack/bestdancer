#!/usr/bin/env python3
"""evaluate_demo.py — 成片自动评估闸门。

两层评估:
  1. 硬指标 (确定性, 不花钱): ffprobe 规格 / blackdetect 黑屏 / freezedetect 卡帧 /
     silencedetect 长静音 / ebur128 响度 + config 一致性 (期号、名次、素材是否落盘、
     口播和画面文案是否对得上)。任何一条踩线 = blocker, 直接不及格。
  2. 内容评估 (调本机 codex CLI): 按渲染清单逐段抽帧, 连同"这一段画面上应该出现
     什么" 一起喂给 codex exec --output-schema, 让它挑毛病并按维度打分。

输出 output/eval/<week>/report.json + report.md, 退出码 0=及格 / 1=不及格,
方便上层脚本 (scripts/auto_episode.py) 拿来当"及格才允许发布"的闸门。

用法:
    python3 pipeline/evaluate_demo.py 2026-W31-C
    python3 pipeline/evaluate_demo.py 2026-W31-C --threshold 85
    python3 pipeline/evaluate_demo.py 2026-W31-C --no-llm     # 只跑硬指标
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from codex_client import CODEX_ENV, find_codex, run_codex_json  # noqa: E402

OUTPUT = REPO / "output"
WEEKLY = REPO / "config" / "weekly"

# 成片规格约定 (见 pipeline/render_demo.py)
EXPECT_W, EXPECT_H = 720, 1280
MIN_TOTAL_SEC = 45.0
MAX_TOTAL_SEC = 210.0
# edge-tts + sidechain 之后的合理整体响度区间
MIN_LUFS, MAX_LUFS = -26.0, -9.0


# ────────────────────────── 基础工具 ──────────────────────────

def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ffprobe_json(video: Path) -> dict:
    r = run(["ffprobe", "-v", "error", "-show_format", "-show_streams",
             "-of", "json", str(video)])
    try:
        return json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return {}


# ────────────────────────── 第一层: 硬指标 ──────────────────────────

def issue(severity: str, area: str, description: str, suggested_fix: str = "",
          segment_index: int = -1, timestamp_sec: float = -1.0) -> dict:
    return {"severity": severity, "area": area, "segment_index": segment_index,
            "timestamp_sec": timestamp_sec, "description": description,
            "suggested_fix": suggested_fix, "source": "tech"}


def check_specs(video: Path, probe: dict) -> tuple[dict, list[dict]]:
    issues: list[dict] = []
    vs = next((s for s in probe.get("streams", []) if s.get("codec_type") == "video"), {})
    as_ = next((s for s in probe.get("streams", []) if s.get("codec_type") == "audio"), None)
    duration = float(probe.get("format", {}).get("duration") or 0)
    width, height = int(vs.get("width") or 0), int(vs.get("height") or 0)

    facts = {
        "duration_sec": round(duration, 2),
        "width": width, "height": height,
        "video_codec": vs.get("codec_name"),
        "audio_codec": (as_ or {}).get("codec_name"),
        "size_mb": round(video.stat().st_size / 1024 / 1024, 1),
    }
    if width != EXPECT_W or height != EXPECT_H:
        issues.append(issue("blocker", "规格",
                            f"分辨率 {width}x{height} 不是约定的竖版 {EXPECT_W}x{EXPECT_H}",
                            "检查 render_demo.py 的 W/H 与 normalize_clip 的 crop"))
    if as_ is None:
        issues.append(issue("blocker", "音频", "成片没有音频轨",
                            "TTS 合成失败, 看 render 日志里的 edge-tts / SAPI 回退"))
    if duration < MIN_TOTAL_SEC:
        issues.append(issue("blocker", "时长", f"总时长仅 {duration:.1f}s, 短于下限 {MIN_TOTAL_SEC}s",
                            "多半是有段落没渲染进去, 核对 manifest 段数"))
    elif duration > MAX_TOTAL_SEC:
        issues.append(issue("major", "时长", f"总时长 {duration:.1f}s 超过上限 {MAX_TOTAL_SEC}s",
                            "把 first_sentences 收到 2 句, 或调低 max_dur"))
    return facts, issues


def check_black_freeze_silence(video: Path, has_audio: bool = True) -> tuple[dict, list[dict]]:
    """一次 ffmpeg 过完 blackdetect + freezedetect (+ 有音轨时的 silencedetect)。"""
    issues: list[dict] = []
    fc = ("[0:v]blackdetect=d=0.6:pic_th=0.98[bd];"
          "[bd]freezedetect=n=-60dB:d=2.5[v]")
    maps = ["-map", "[v]"]
    if has_audio:
        fc += ";[0:a]silencedetect=n=-45dB:d=2.5[a]"
        maps += ["-map", "[a]"]
    r = run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(video),
             "-filter_complex", fc, *maps, "-f", "null", "-"])
    log = (r.stderr or "") + (r.stdout or "")

    blacks = [(float(m.group(1)), float(m.group(2)))
              for m in re.finditer(r"black_start:([\d.]+) black_end:([\d.]+)", log)]
    freezes = [float(m.group(1)) for m in re.finditer(r"freeze_start: ([\d.]+)", log)]

    # silence_start / silence_end 是分行打出来的, 按出现顺序配对, 不要跨事件贪婪匹配
    silences: list[tuple[float, float]] = []
    pending: float | None = None
    for m in re.finditer(r"silence_(start|end): (-?[\d.]+)", log):
        kind, value = m.group(1), float(m.group(2))
        if kind == "start":
            pending = value
        elif pending is not None:
            silences.append((pending, value))
            pending = None

    for start, end in blacks:
        if end - start >= 0.6:
            issues.append(issue("blocker", "黑屏",
                                f"{start:.1f}s–{end:.1f}s 全黑 {end - start:.1f}s",
                                "该段素材没归一化成功或 clip_start 超出片长",
                                timestamp_sec=start))
    for start in freezes:
        issues.append(issue("major", "卡帧", f"{start:.1f}s 起画面静止超过 2.5s",
                            "真片被拉长补帧了, 把该段 adur 收到真片长度以内",
                            timestamp_sec=start))
    for start, end in silences:
        if end - start >= 2.5:
            issues.append(issue("major", "静音", f"{start:.1f}s–{end:.1f}s 无声 {end - start:.1f}s",
                                "口播 wav 缺失或 BGM 铺底断了", timestamp_sec=start))
    return {"black_intervals": blacks, "freeze_starts": freezes,
            "silence_intervals": silences}, issues


def check_loudness(video: Path) -> tuple[dict, list[dict]]:
    issues: list[dict] = []
    r = run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(video),
             "-filter_complex", "ebur128=peak=true", "-f", "null", "-"])
    log = r.stderr or ""
    tail = log[-2500:]
    lufs = re.search(r"I:\s*(-?[\d.]+) LUFS", tail)
    peak = re.search(r"Peak:\s*(-?[\d.]+) dBFS", tail)
    facts = {"integrated_lufs": float(lufs.group(1)) if lufs else None,
             "true_peak_dbfs": float(peak.group(1)) if peak else None}
    if facts["integrated_lufs"] is not None:
        if facts["integrated_lufs"] < MIN_LUFS:
            issues.append(issue("major", "响度",
                                f"整体响度 {facts['integrated_lufs']:.1f} LUFS 太轻 (<{MIN_LUFS})",
                                "调高 render 里 voice 的 volume 或降低 sidechain 压制"))
        elif facts["integrated_lufs"] > MAX_LUFS:
            issues.append(issue("minor", "响度",
                                f"整体响度 {facts['integrated_lufs']:.1f} LUFS 偏炸 (>{MAX_LUFS})",
                                "降低 amix 后的 alimiter limit"))
    if facts["true_peak_dbfs"] is not None and facts["true_peak_dbfs"] > -1.5:
        issues.append(issue(
            "major", "响度",
            f"编码后峰值 {facts['true_peak_dbfs']:.2f} dBFS 超过 -1.5dBFS 上限",
            "loudnorm 预留更大 true-peak 余量，并在 AAC 编码前增加 limiter"))
    return facts, issues


def check_audio_balance(manifest: dict) -> tuple[dict, list[dict]]:
    """检查人声与原片声的相对响度，而不是只看最终整片 LUFS。"""
    mix = manifest.get("audio_mix") or {}
    issues: list[dict] = []
    delta = mix.get("pre_duck_delta_db")
    ratio = mix.get("ducking_ratio")
    facts = {
        "voice_active_lufs": mix.get("voice_active_lufs"),
        "bed_lufs": mix.get("bed_lufs"),
        "voice_bed_delta_db": delta,
        "ducking_ratio": ratio,
        "final_target_lufs": mix.get("final_target_lufs"),
    }
    if delta is None:
        issues.append(issue(
            "major", "音频平衡",
            "manifest 没有记录人声/原片声的相对响度，无法验证听感平衡",
            "用当前版本 render_demo.py 重渲，写入 audio_mix 指标"))
    elif delta > 5:
        issues.append(issue(
            "major", "音频平衡",
            f"人声比原片声高 {delta:.1f}dB，听感会压住舞蹈原声",
            "降低 voice_gain、提高 bed_gain，并把 sidechain ratio 控制在 3:1 左右"))
    elif delta < -3:
        issues.append(issue(
            "major", "音频平衡",
            f"人声比原片声低 {-delta:.1f}dB，口播可能被音乐淹没",
            "提高 voice_gain 或降低 bed_gain"))
    if ratio is not None and ratio > 6:
        issues.append(issue(
            "major", "音频平衡",
            f"sidechain ducking={ratio}:1 过强，讲话时原片声会突然塌下去",
            "使用 2.5:1–4:1 的温和 ducking"))
    return facts, issues


def normalize_name(s: str) -> str:
    """把名字压成"可比对形态"。

    口播文本在 render_demo 里过了 sanitize_tts: @句柄被整个删掉, `_ . -` 和中英文
    标点全变成空格。所以画面署名不能直接拿去 `in vo` 判断 —— 实测
    "蛋丝儿不熬夜！" / "_mo.on_" 这类名字 100% 误报。两边都归一化后再比。
    """
    s = re.sub(r"[@_\.\-]+", " ", s or "")
    s = re.sub(r"[，。！？、；：,\.!\?;:·・]+", " ", s)
    return re.sub(r"\s+", "", s).lower()


def clip_vid(source_clip: str | None) -> str | None:
    """从 `c3__douyin__7679412623074661702.mp4` 里取出平台视频 id。
    候选 id (c3) 是按期重新编号的, 跨期比对必须用视频 id。"""
    if not source_clip:
        return None
    stem = Path(source_clip).stem
    parts = stem.split("__")
    return parts[-1] if len(parts) >= 2 else stem


def check_config(week: str, manifest: dict) -> tuple[dict, list[dict]]:
    """内容侧的确定性校验 —— 历史上翻过车的点全部固化成断言。"""
    issues: list[dict] = []
    cfg_path = WEEKLY / f"{week}.json"
    if not cfg_path.exists():
        return {}, [issue("blocker", "配置", f"找不到 {cfg_path.relative_to(REPO)}")]
    cfg = json.loads(cfg_path.read_text())

    picks = cfg.get("picks", [])
    narration = cfg.get("narration", [])
    facts = {"picks": len(picks), "narration": len(narration),
             "has_classic": bool(cfg.get("classic_comeback"))}

    # 踩过的坑 1: episode.week 没跟着 top-level week 同步 -> 片头念错第几篇
    ep_week = (cfg.get("episode") or {}).get("week")
    if ep_week and ep_week != cfg.get("week"):
        issues.append(issue("blocker", "期号",
                            f"episode.week={ep_week} 与 week={cfg.get('week')} 不一致",
                            "daily_auto_generate 里强制同步 episode.week"))
    if cfg.get("week") != week:
        issues.append(issue("blocker", "期号", f"config.week={cfg.get('week')} 与目标 {week} 不一致"))

    if len(picks) != 5:
        issues.append(issue("major", "策展", f"TOP 数量为 {len(picks)}, 约定是 5 支"))

    ranks = [p.get("rank") for p in picks]
    if sorted(r for r in ranks if r) != list(range(1, len(picks) + 1)):
        issues.append(issue("major", "策展", f"名次不连续: {ranks}"))

    # 踩过的坑 2: 文案里的作者/名次跟实际选的候选对不上
    for seg in manifest.get("segments", []):
        if seg["type"] not in ("top", "classic"):
            continue
        expect = seg.get("expect_on_screen", {})
        creator = (expect.get("creator") or "").lstrip("@").strip()
        vo = seg.get("vo", "")
        if creator and normalize_name(creator) not in normalize_name(vo):
            issues.append(issue("major", "文案",
                                f"第{seg['index']}段画面署名 @{creator} 没出现在口播里: {vo!r}",
                                "重建 narration, 别复用上一期模板文案",
                                segment_index=seg["index"], timestamp_sec=seg["start_sec"]))
        if seg["type"] == "top" and seg.get("rank"):
            if f"第{seg['rank']}" not in vo and f"No.{seg['rank']}" not in vo:
                issues.append(issue("major", "文案",
                                    f"第{seg['index']}段是 TOP{seg['rank']} 但口播没报名次: {vo!r}",
                                    "narration 用 mkvo() 统一生成",
                                    segment_index=seg["index"], timestamp_sec=seg["start_sec"]))
        if not seg.get("has_real_footage"):
            issues.append(issue("blocker", "素材",
                                f"第{seg['index']}段没有真片背景, 用的是占位动画",
                                "入选舞段必须先下载落盘再渲染",
                                segment_index=seg["index"], timestamp_sec=seg["start_sec"]))

    # 踩过的坑 3: 跨期复用素材, 观众一眼看出重复。
    # 比对必须落到平台视频 id —— 同一支片子这期叫 c1 下期可能叫 c7, 比文件名永远比不出来。
    used_now = {clip_vid(s.get("source_clip")) for s in manifest.get("segments", [])}
    used_now.discard(None)
    for other in WEEKLY.glob("????-W??*.json"):
        if other.stem == week:
            continue
        other_manifest = OUTPUT / f"{other.stem}_manifest.json"
        if not other_manifest.exists():
            continue
        try:
            prev = json.loads(other_manifest.read_text())
        except json.JSONDecodeError:
            continue
        prev_vids = {clip_vid(s.get("source_clip")) for s in prev.get("segments", [])}
        prev_vids.discard(None)
        dupes = used_now & prev_vids
        if dupes:
            issues.append(issue("blocker", "去重",
                                f"与 {other.stem} 用了同一批素材: {sorted(dupes)}",
                                "used_urls() 跨期去重漏了, 检查候选 URL 回填"))
    return facts, issues


# ────────────────────────── 第二层: codex 内容评估 ──────────────────────────

OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["overall_score", "verdict", "summary", "dimensions", "issues",
                 "repair_actions"],
    "properties": {
        "overall_score": {"type": "integer", "minimum": 0, "maximum": 100},
        "verdict": {"type": "string", "enum": ["pass", "revise", "reject"]},
        "summary": {"type": "string"},
        "dimensions": {
            "type": "object",
            "additionalProperties": False,
            "required": ["content_accuracy", "visual_quality", "text_readability",
                         "pacing", "hook_strength", "compliance"],
            "properties": {
                name: {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["score", "comment"],
                    "properties": {
                        "score": {"type": "integer", "minimum": 0, "maximum": 100},
                        "comment": {"type": "string"},
                    },
                }
                for name in ["content_accuracy", "visual_quality", "text_readability",
                             "pacing", "hook_strength", "compliance"]
            },
        },
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["severity", "area", "segment_index", "timestamp_sec",
                             "description", "suggested_fix", "auto_fixable"],
                "properties": {
                    "severity": {"type": "string", "enum": ["blocker", "major", "minor"]},
                    "area": {"type": "string"},
                    "segment_index": {"type": "integer"},
                    "timestamp_sec": {"type": "number"},
                    "description": {"type": "string"},
                    "suggested_fix": {"type": "string"},
                    "auto_fixable": {"type": "boolean"},
                },
            },
        },
        "repair_actions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["action", "segment_index", "value_string",
                             "value_number", "rationale"],
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "replace_segment",
                            "retitle_segment",
                            "set_creator",
                            "set_dance_type",
                            "set_difficulty",
                            "set_clip_start",
                            "shorten_segment",
                            "brighten_segment",
                            "shorten_all_segments",
                            "strengthen_intro",
                        ],
                    },
                    "segment_index": {"type": "integer"},
                    "value_string": {"type": "string"},
                    "value_number": {"type": "number"},
                    "rationale": {"type": "string"},
                },
            },
        },
    },
}

PROMPT_HEADER = """你是「本周热舞」这个短视频栏目的审片人。栏目定位: 面向中文跳舞初学者的
每周热门编舞 TOP5 推荐 + 特别加映, 竖版 9:16, 首发抖音/小红书。

我给你的是一支刚渲染出来的成片按段抽出来的帧 (按时间顺序附在后面), 以及这支片子的
渲染清单——清单里写明了每一段的时间区间、以及那一段画面上「本来应该出现」的名次角标、
舞种/曲名、作者署名、星级和口播字幕。

请逐段对照着挑毛病, 重点看这几类真实翻过车的问题:
1. 内容对不上 (content_accuracy): 顶部写的作者/名次/舞种, 跟这一段真实画面里的人和舞
   是不是同一支? 有没有出现"标题说双人 Urban, 画面却是单人 Kpop"这种错位?
   片头口播念的期号 (第几篇) 跟清单里的 edition 对不对得上?
2. 画面质量 (visual_quality): 裁切有没有把人切掉/切头, 画面是否糊、抖、过暗, 有没有
   黑边、拉伸变形、原平台水印被裁掉 (署名合规要求必须保留原作者水印)。
3. 文字可读性 (text_readability): 字幕/角标有没有被遮挡、压边、串行、超框、错别字,
   底部大字幕跟画面主体是否打架。
4. 节奏 (pacing): 每段长度是否合适, 有没有明显该切没切、或者一段拖太久。
5. 开场吸引力 (hook_strength): 前 3 秒能不能留住人。
6. 合规 (compliance): 判定标准以本栏目的合规红线为准 ——
   **只要该段画面上能清晰读到 `@原作者` 署名, 就算合规**。
   原平台水印(抖音右下角浮层等)有当然更好, 但**没有不算问题**: 下载路径拿到的
   本来就是无水印版, 画面常驻的 @作者 已经能让观众追溯到原作者。
   这里真正要挑的是: 署名缺失、署名被遮挡/截断/看不清、署名和画面里的人明显不是
   同一个作者、以及出现了不该露出的个人信息(手机号/身份证/住址等)。
   不要因为"看不到原平台水印"就判不合规。

打分规则: 每个维度 0-100。overall_score 要能反映"这条能不能直接发出去",
不要因为都是小毛病就给高分, 也不要因为一个小瑕疵就打到不及格。
verdict: pass=可以直接发; revise=要改了再发; reject=方向性错误得重做。
issues 里 severity 用 blocker(必须改)/major(建议改)/minor(可忍受);
auto_fixable=true 表示这个问题能靠改渲染参数/重选片段/重生成文案解决,
不需要重新去找素材。segment_index 用清单里的段号, 对不上就填 -1。

最后把能自动执行的修改写进 repair_actions。它不是泛泛建议, 而是下一轮渲染会直接执行的
动作, 必须严格使用下面的 action:
- replace_segment: 整段表现力/画质/竖版适配很差, 换后面的候选顶上。value 留空/0。
- retitle_segment: 标题错或不贴画面。value_string 放不超过 12 字的中文新标题。
- set_creator: 画面里能清楚读到原作者水印、且与当前署名不一致。value_string 放准确的
  @作者；无法确认哪个才对时不要猜, 用 replace_segment。
- set_dance_type: 舞种标错。value_string 放正确舞种。
- set_difficulty: 难度星级错。value_number 放 1-5。
- set_clip_start: 选段起点明显不好、但同一原片后面可能更好。value_number 放建议的原片秒数;
  只有你能从上下文确定原片时间时才用, 否则用 replace_segment。
- shorten_segment: 某段拖沓。value_number 放下一版段长秒数(10-15)。
- brighten_segment: 某段整体太暗但素材仍值得保留。value_number 放亮度增量(0.03-0.25)。
- shorten_all_segments: 全片节奏慢。segment_index=-1, value_number 放缩放比例(0.7-0.95)。
- strengthen_intro: 开场弱。segment_index=0, value_number 放新片头时长(1.6-2.6)。

只给**确定能改善本轮问题**的动作; 不要为 minor 小问题乱改。单轮最多替换 2 段,
避免整期风格一次全变。每个 blocker/major 如果有明确自动修法, 至少给一个 repair_action。
"""


def extract_frames(video: Path, manifest: dict, out_dir: Path,
                   per_segment: int) -> list[dict]:
    """按段抽帧: 每段在 [start, end] 内均匀取点, 避开首尾 0.4s 的淡入淡出。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.jpg"):
        old.unlink()
    frames: list[dict] = []
    for seg in manifest.get("segments", []):
        start, end = seg["start_sec"], seg["end_sec"]
        span = max(0.0, end - start - 0.8)
        n = 1 if seg["type"] in ("intro", "outro") else per_segment
        for k in range(n):
            frac = (k + 1) / (n + 1)
            t = start + 0.4 + span * frac
            name = f"seg{seg['index']:02d}_{t:07.2f}s.jpg"
            path = out_dir / name
            r = run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.3f}",
                     "-i", str(video), "-frames:v", "1",
                     "-vf", "scale=540:-2", "-q:v", "3", str(path)])
            if path.exists() and path.stat().st_size > 0:
                frames.append({"path": path, "segment_index": seg["index"],
                               "timestamp_sec": round(t, 2)})
            elif r.returncode != 0:
                print(f"[eval] 抽帧失败 {name}: {(r.stderr or '')[-160:]}", file=sys.stderr)
    return frames


def build_prompt(manifest: dict, frames: list[dict], tech: dict) -> str:
    lines = [PROMPT_HEADER, "", "## 渲染清单", ""]
    lines.append(f"期号: {manifest.get('week')}  总时长: {manifest.get('total_sec')}s  "
                 f"画幅: {manifest.get('size')}")
    for seg in manifest.get("segments", []):
        exp = seg.get("expect_on_screen", {})
        lines.append(
            f"- 第{seg['index']}段 [{seg['type']}] {seg['start_sec']:.1f}s–{seg['end_sec']:.1f}s"
            f" | 角标: {exp.get('tag','')} | 标题: {exp.get('title','')}"
            f" | 署名: {exp.get('creator','')} | 星级: {exp.get('stars')}"
            f" | 素材: {seg.get('source_clip') or '(占位)'}")
        lines.append(f"  口播: {seg.get('vo','')}")
    lines += ["", "## 附图顺序", ""]
    for i, f in enumerate(frames, 1):
        lines.append(f"图{i}: 第{f['segment_index']}段, {f['timestamp_sec']:.2f}s")
    lines += ["", "## 已跑过的硬指标 (确定性检查, 供参考, 不用重复报)", "",
              "```json", json.dumps(tech, ensure_ascii=False, indent=2), "```", "",
              "请只输出符合给定 JSON schema 的结果。"]
    return "\n".join(lines)


def run_codex(codex_bin: str, prompt: str, frames: list[dict], work_dir: Path,
              model: str | None, timeout: int) -> tuple[dict | None, str]:
    return run_codex_json(prompt, OUTPUT_SCHEMA, work_dir,
                          images=[f["path"] for f in frames],
                          model=model, timeout=timeout, codex_bin=codex_bin,
                          tag="eval")


# ────────────────────────── 汇总 ──────────────────────────

def severity_penalty(issues: list[dict]) -> int:
    weights = {"blocker": 40, "major": 12, "minor": 3}
    return sum(weights.get(i.get("severity", "minor"), 3) for i in issues)


def write_report(report: dict, eval_dir: Path) -> None:
    (eval_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [f"# 成片评估 · {report['week']}", "",
             f"- 结论: **{'及格' if report['passed'] else '不及格'}** "
             f"(总分 {report['final_score']} / 阈值 {report['threshold']})",
             f"- 成片: `{report['video']}`",
             f"- 硬指标: {report['tech_facts'].get('duration_sec')}s  "
             f"{report['tech_facts'].get('width')}x{report['tech_facts'].get('height')}  "
             f"{report['tech_facts'].get('size_mb')}MB  "
             f"{report['tech_facts'].get('integrated_lufs')} LUFS", ""]
    llm = report.get("llm") or {}
    if llm:
        lines += [f"- 模型判定: {llm.get('verdict')} ({llm.get('overall_score')} 分)",
                  f"- 概述: {llm.get('summary','')}", "", "## 维度分", ""]
        for name, d in (llm.get("dimensions") or {}).items():
            lines.append(f"- {name}: {d.get('score')} — {d.get('comment','')}")
    lines += ["", "## 问题清单", ""]
    if not report["issues"]:
        lines.append("(无)")
    for i in report["issues"]:
        ts = f"{i['timestamp_sec']:.1f}s" if i.get("timestamp_sec", -1) >= 0 else "—"
        lines.append(f"- [{i['severity']}] ({i.get('source','llm')}/{i.get('area','')}) "
                     f"seg{i.get('segment_index', -1)} @{ts}: {i['description']}")
        if i.get("suggested_fix"):
            lines.append(f"  - 修法: {i['suggested_fix']}")
    (eval_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description="给成片打分, 及格才允许发布")
    ap.add_argument("week", help="如 2026-W31-C")
    ap.add_argument("--threshold", type=int, default=80, help="及格线, 默认 80")
    ap.add_argument("--frames-per-segment", type=int, default=2)
    ap.add_argument("--no-llm", action="store_true", help="只跑硬指标, 不调 codex")
    ap.add_argument("--model", default=None, help="传给 codex -m 的模型名")
    ap.add_argument("--timeout", type=int, default=900, help="codex 超时秒数")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    week = args.week
    video = OUTPUT / f"{week}_demo.mp4"
    manifest_path = OUTPUT / f"{week}_manifest.json"
    if not video.exists():
        print(f"[eval] 找不到成片 {video}", file=sys.stderr)
        return 2
    manifest = {}
    manifest_missing = not manifest_path.exists()
    if not manifest_missing:
        manifest = json.loads(manifest_path.read_text())

    eval_dir = OUTPUT / "eval" / week
    eval_dir.mkdir(parents=True, exist_ok=True)

    # 第一层
    probe = ffprobe_json(video)
    spec_facts, issues = check_specs(video, probe)
    if not manifest:
        # 没有渲染清单 = 逐段内容断言(署名/名次/真片/跨期去重)全部失效。
        # 这时候绝不能因为"没检查出问题"就放行, 必须 fail-closed。
        print(f"[eval] 警告: 没有 {manifest_path.name}, 逐段对照失效; "
              f"重新跑一次 render_demo.py 就会生成。", file=sys.stderr)
        issues.append(issue("blocker", "清单",
                            f"缺少 {manifest_path.name}, 无法逐段核对署名/名次/真片来源",
                            "用当前版本的 pipeline/render_demo.py 重渲一次即可生成"))
        total = spec_facts["duration_sec"]
        manifest = {"week": week, "total_sec": total, "size": [spec_facts["width"], spec_facts["height"]],
                    "segments": [{"index": 0, "type": "top", "start_sec": 0.0, "end_sec": total,
                                  "candidate_id": None, "rank": None, "source_clip": None,
                                  "has_real_footage": True, "expect_on_screen": {}, "vo": ""}]}
        args.frames_per_segment = max(args.frames_per_segment, 10)

    av_facts, av_issues = check_black_freeze_silence(
        video, has_audio=spec_facts.get("audio_codec") is not None)
    loud_facts, loud_issues = check_loudness(video)
    balance_facts, balance_issues = check_audio_balance(manifest)
    cfg_facts, cfg_issues = check_config(week, manifest)
    issues += av_issues + loud_issues + balance_issues + cfg_issues
    tech_facts = {
        **spec_facts, **loud_facts, **balance_facts, **cfg_facts, **av_facts}

    if not args.quiet:
        print(f"[eval] 硬指标: {spec_facts['duration_sec']}s "
              f"{spec_facts['width']}x{spec_facts['height']} "
              f"{loud_facts.get('integrated_lufs')} LUFS, "
              f"发现 {len(issues)} 个问题")

    # 第二层
    llm: dict | None = None
    llm_error = ""
    if not args.no_llm:
        codex_bin = find_codex()
        if not codex_bin:
            llm_error = (f"找不到 codex CLI (PATH 里没有, 也没在 VS Code 扩展里找到)。"
                         f" 可以用 {CODEX_ENV}=/path/to/codex 指定。")
        else:
            all_segments = manifest.get("segments", [])
            dance_segments = [
                seg for seg in all_segments if seg.get("type") in ("top", "classic")]
            if args.frames_per_segment < 1 or not all_segments or not dance_segments:
                detail = (
                    f"frames_per_segment={args.frames_per_segment}, "
                    f"segments={len(all_segments)}, dance_segments={len(dance_segments)}")
                issues.append(issue(
                    "blocker", "抽帧",
                    f"视觉评估没有可用的舞段覆盖: {detail}",
                    "frames_per_segment 必须 >=1，且 manifest 至少包含一个 top/classic 段"))
                llm_error = f"视觉覆盖无效 ({detail}), 拒绝无舞段评分"
                frames = []
            else:
                frames = extract_frames(video, manifest, eval_dir / "frames",
                                        args.frames_per_segment)
            counts: dict[int, int] = {}
            for frame in frames:
                counts[frame["segment_index"]] = counts.get(frame["segment_index"], 0) + 1
            missing_frames = []
            for seg in all_segments:
                expected = 1 if seg["type"] in ("intro", "outro") else args.frames_per_segment
                actual = counts.get(seg["index"], 0)
                if actual < expected:
                    missing_frames.append((seg["index"], actual, expected))
            tech_facts["frame_coverage"] = {
                "extracted": len(frames),
                "expected": sum(
                    1 if seg["type"] in ("intro", "outro") else args.frames_per_segment
                    for seg in all_segments),
                "missing_segments": missing_frames,
            }
            if llm_error:
                pass
            elif missing_frames:
                detail = ", ".join(
                    f"seg{idx}={actual}/{expected}"
                    for idx, actual, expected in missing_frames)
                issues.append(issue(
                    "blocker", "抽帧",
                    f"视觉评估抽帧不完整: {detail}",
                    "检查 ffmpeg 解码错误；所有段落抽帧齐全后才能放行"))
                llm_error = f"抽帧覆盖不完整 ({detail}), 拒绝无图/少图评分"
            else:
                if not args.quiet:
                    print(f"[eval] 抽了 {len(frames)} 帧, 调 codex ({codex_bin}) 评分中…")
                prompt = build_prompt(manifest, frames, tech_facts)
                (eval_dir / "prompt.md").write_text(prompt, encoding="utf-8")
                llm, llm_error = run_codex(codex_bin, prompt, frames, eval_dir,
                                           args.model, args.timeout)
    if llm_error and not args.quiet:
        print(f"[eval] 内容评估未完成: {llm_error}", file=sys.stderr)

    if llm:
        for i in llm.get("issues", []):
            i.setdefault("source", "llm")
        issues += llm.get("issues", [])

    base = llm.get("overall_score") if llm else 100
    final = max(0, min(100, base - severity_penalty([i for i in issues if i.get("source") == "tech"])))
    has_blocker = any(i.get("severity") == "blocker" for i in issues)
    # 闸门 fail-closed: 要求跑内容评估却没跑成, 一律按不及格处理, 不能当"没问题"放行
    llm_missing = (not args.no_llm) and llm is None
    # prompt 明确定义 revise="要改了再发"。只排除 reject 会让 revise 高分版进发布分支,
    # 违反闸门语义；有 LLM 时必须明确 verdict=pass。
    llm_pass = args.no_llm or (llm is not None and llm.get("verdict") == "pass")
    passed = (final >= args.threshold and not has_blocker
              and not llm_missing and llm_pass)

    report = {
        "week": week,
        "video": video.relative_to(REPO).as_posix(),
        "video_sha256": file_sha256(video),
        "threshold": args.threshold,
        "final_score": final,
        "passed": passed,
        "has_blocker": has_blocker,
        "llm_error": llm_error,
        "llm_missing": llm_missing,
        "llm": llm,
        "tech_facts": tech_facts,
        "issues": sorted(issues, key=lambda i: {"blocker": 0, "major": 1, "minor": 2}
                         .get(i.get("severity"), 3)),
    }
    write_report(report, eval_dir)

    if not args.quiet:
        print(f"[eval] 总分 {final} / 阈值 {args.threshold} -> "
              f"{'及格, 可以发' if passed else '不及格, 需要修'}")
        for i in report["issues"][:12]:
            print(f"   [{i['severity']}] {i.get('area','')}: {i['description']}")
        print(f"[eval] 报告: {(eval_dir / 'report.md').relative_to(REPO)}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
