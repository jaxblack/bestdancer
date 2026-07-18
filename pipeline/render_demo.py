#!/usr/bin/env python3
"""本周热舞 · demo 样片渲染器（竖版手机端 9:16，<60s）

从 config/weekly/<week>.json 渲染一支竖屏短视频：
片头 -> TOP5(星级+字幕) -> 经典回归 -> 片尾。

- 配音：edge-tts 神经女声(默认 zh-CN-XiaoxiaoNeural，自然、抖音风)，失败回退 Windows SAPI。
- 画面：每支若 assets/incoming/<week>/<id>__*.mp4 有真片，就用真片当背景(裁成竖屏)，
  叠加紧凑信息条；没有真片则用动态霓虹占位。
- 口播走短句，整片压到 60 秒内。

用法: python pipeline/render_demo.py 2026-W29
可选环境变量: TTS_VOICE(默认 zh-CN-XiaoxiaoNeural), TTS_RATE(默认 +6%)
依赖: Pillow, numpy, imageio, imageio-ffmpeg, edge-tts。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import shutil
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

REPO = Path(__file__).resolve().parents[1]
W, H = 720, 1280          # 竖版 9:16
FPS = 24
GAP = 0.25
MARGIN = 44
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}

BG = (11, 11, 20)
CA = (255, 46, 154)       # 品红
CB = (22, 224, 255)       # 青
ACCENT = (255, 225, 77)
WHITE = (245, 245, 250)
GRAY = (165, 165, 180)

VOICE = os.environ.get("TTS_VOICE", "zh-CN-XiaoyiNeural")   # 晓伊：活泼年轻女声
RATE = os.environ.get("TTS_RATE", "+18%")                   # 提速，去掉拖沓 AI 味
TAIL = 3.0                                                  # 结尾纯 BGM 时长
BGM_DIR = REPO / "assets" / "bgm"
BGM_PATH = BGM_DIR / "ayo_jason_nevins_remix.mp3"
FALLBACK_BGM_PATH = BGM_DIR / "auto_beat.wav"
sys.path.insert(0, str(Path(__file__).parent))


# 跨平台中文字体候选：Windows 微软雅黑 / macOS PingFang·STHeiti·Hiragino / Linux Noto
_FONT_CANDIDATES_BOLD = [
    "C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/msyh.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
]
_FONT_CANDIDATES_REG = [
    "C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyhl.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
]


def load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    for p in (_FONT_CANDIDATES_BOLD if bold else _FONT_CANDIDATES_REG):
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:  # noqa: BLE001
                continue
    return ImageFont.load_default()


F_TAG = load_font(30, True)
F_TITLE = load_font(60, True)
F_SUB = load_font(34)
F_SMALL = load_font(27)
F_STAR = load_font(48, True)
F_NUM = load_font(38, True)
F_HUGE = load_font(112, True)
F_MID = load_font(52, True)
F_CHIP = load_font(27, True)


def clean(s: str) -> str:
    if not s:
        return ""
    for junk in ["_示例", "（示例）", "(示例)", "示例编舞", "示例"]:
        s = s.replace(junk, "")
    return s.strip()


def sanitize_tts(vo: str) -> str:
    """去掉 @句柄，避免神经语音把英文 ID 逐字母念；同时抹掉标点，让 TTS 顺读不停顿。"""
    vo = re.sub(r"@\S+\s*的", "", vo)
    vo = re.sub(r"@\S+\s*", "", vo)
    # 中英文标点全替换为空格 —— 抖音风口播不需要"逗号顿号"被念/顿
    vo = re.sub(r"[，。！？、；：,\.!\?;:]+", " ", vo)
    # 作者名/song 里的下划线/点/连字符也不发音（例如 ___rhyan / a.b.c）
    vo = re.sub(r"[_\.\-]+", " ", vo)
    vo = re.sub(r"\s+", " ", vo).strip()
    return clean(vo)


def first_sentences(text: str, n: int = 3) -> str:
    """取前 n 句（按。！？切），控制口播时长。"""
    parts = [p for p in re.split(r"(?<=[。！？])", text) if p.strip()]
    return "".join(parts[:n])


def wrap(draw, text, font, max_w):
    lines, cur = [], ""
    for ch in text:
        if draw.textlength(cur + ch, font=font) <= max_w:
            cur += ch
        else:
            lines.append(cur)
            cur = ch
    if cur:
        lines.append(cur)
    return lines


def build_segments(cfg: dict) -> list[dict]:
    cands = {c["id"]: c for c in cfg.get("this_week_candidates", [])}
    for c in cfg.get("classics_pool", []):
        cands[c["id"]] = c
    picks = {p["rank"]: p for p in cfg.get("picks", [])}
    classic_id = cfg.get("classic_comeback", {}).get("id")
    ep = cfg.get("episode", {})
    meta = cfg.get("metadata", {})
    intro_cfg = meta.get("intro", {}) or {}
    outro_cfg = meta.get("outro", {}) or {}
    g_voice = meta.get("global_voice") or "zh-CN-XiaoyiNeural"
    g_rate = meta.get("global_voice_rate") or "+20%"
    tpl = meta.get("vo_template") or "第{rank}名，{dance_type}街舞{title}，来自 {creator}。"
    tpl_classic = meta.get("vo_template_classic") or "特别加映，{dance_type}街舞{title}，来自 {creator}。"

    # 支持 {year} {week} {edition} 变量: 2026-W30-A → year=26 week=30 edition=上
    _wk = ep.get("week", "") or ""
    import re as _re
    _m = _re.match(r"(\d{4})-W(\d{1,2})(?:-([A-Z]))?", _wk)
    if _m:
        _yr = _m.group(1)[-2:]; _wn = int(_m.group(2))
        # edition 支持 A..Z: A=第一 B=第二 C=第三 ...；无 edition→空串
        _ed_letter = _m.group(3) or ""
        _ORD = ["一","二","三","四","五","六","七","八","九","十",
                "十一","十二","十三","十四","十五","十六","十七","十八","十九","二十",
                "二十一","二十二","二十三","二十四","二十五","二十六"]
        _ed = f"第{_ORD[ord(_ed_letter)-65]}篇" if _ed_letter and _ed_letter.isalpha() else ""
    else:
        _yr = ""; _wn = ""; _ed = ""
    _intro_vo_default = f"{_yr}年第{_wn}周热舞榜，{_ed}" if _yr else "本周热舞榜"
    _intro_vo = (intro_cfg.get("vo") or "").format(year=_yr, week=_wn, edition=_ed) if intro_cfg.get("vo") else _intro_vo_default

    segs = [{
        "type": "intro", "cid": None,
        "vo": _intro_vo,
        "title1": intro_cfg.get("title1") or "本周热舞",
        "title2": intro_cfg.get("title2") or "WEEKLY DANCE",
        "sub": clean(ep.get("theme", "")), "week": ep.get("week", ""),
        "foot": intro_cfg.get("foot") or "TOP5 + 特别加映",
    }]

    narration_items = sorted(
        cfg.get("narration", []),
        key=lambda item: -int(item.get("rank") or 0) if item.get("segment") == "top" else -1,
    )
    for item in narration_items:
        top = item.get("segment") == "top"
        rank = item.get("rank")
        if top:
            cid = picks.get(rank, {}).get("id")
            diff = picks.get(rank, {}).get("difficulty", {})
        else:
            cid = classic_id
            diff = cfg.get("classic_comeback", {}).get("difficulty", {})
        cand = cands.get(cid, {})
        osd = item.get("on_screen", {})
        stars = float(osd.get("stars", 0) or 0)
        title = clean(cand.get("song") or cand.get("title", ""))
        tts_title = title.strip("《》")
        fit = diff.get("fit", "")
        moves = osd.get("core_moves", [])
        move0 = moves[0] if moves else "核心动作"
        _dt = cand.get("dance_type", "").strip() or "街舞"
        _creator = cand.get("creator", "").lstrip("@") or "编舞者"
        _title = clean(cand.get("song") or cand.get("title") or "").strip("《》").strip()
        _tpl = tpl if top else tpl_classic
        default_vo = _tpl.format(rank=rank, dance_type=_dt, title=_title, creator=_creator)
        # 全局音色 / 语速覆盖 candidate 级别（除非候选自己写了 vo 且没显式勾选跟随全局；简化：直接用全局）
        _voice = g_voice
        _rate = g_rate
        full_vo = first_sentences(sanitize_tts(item.get("vo", "")), 2) or default_vo
        # 智能同步舞种词：把 vo 里所有旧舞种词/"街舞"替换成当前 dance_type
        if _dt:
            import re as _re
            _pat = _re.compile(r"(Urban(?:\s*[Dd]ance)?|Hip[- ]?[Hh]op|Jazz|K[- ]?pop|Popping|Locking|嘻哈|爵士|韩舞)", _re.IGNORECASE)
            full_vo = _pat.sub(_dt, full_vo)
            full_vo = _re.sub(rf"({_re.escape(_dt)})(\s*{_re.escape(_dt)})+", r"\1", full_vo)
        if top:
            cap = f"{fit} · 重点练{move0}"
        else:
            cap = f"{fit} · 基本功打底必练"
        segs.append({
            "type": item.get("segment"), "cid": cid, "rank": rank, "vo": full_vo,
            "voice": _voice, "voice_rate": _rate,
            "clip_start_sec": float(cand.get("clip_start_sec") or 0),
            "clip_end_sec": float(cand.get("clip_end_sec") or 0),
            "tag": osd.get("tag", ""), "stars": stars, "moves": moves[:3],
            "title": clean(cand.get("song") or cand.get("title") or "").strip("《》") or cand.get("dance_type", "街舞"),
            "creator": clean(cand.get("creator", "")),
            "source": clean(cand.get("source", "")),
            "like": cand.get("like", 0),
            "play": cand.get("play", 0),
            "cap": "",
            "tip": "",
            "subtitles": [],
            "vo_caption": re.sub(r"[，。！？、,\.!\?]+", " ", full_vo).strip(),
        })

    segs.append({"type": "outro", "cid": None,
                 "vo": outro_cfg.get("vo") or "你最喜欢哪一支？评论区见。",
                 "title1": outro_cfg.get("title1") or "关注追更",
                 "sub": outro_cfg.get("sub") or "下周同一时间见"})
    return segs


# ---------- 绘制 ----------
def pill(draw, cx, y, text, font, fg, bg, pad=22, h=None):
    w = draw.textlength(text, font=font)
    h = h or (font.size + 20)
    draw.rounded_rectangle([cx - w / 2 - pad, y, cx + w / 2 + pad, y + h],
                           radius=h / 2, fill=bg)
    draw.text((cx, y + h / 2), text, font=font, fill=fg, anchor="mm")
    return h


def draw_stars(img, d, cx, cy, stars):
    gw = int(d.textlength("★", font=F_STAR))
    space = 8
    n_full = int(math.floor(stars + 1e-6))
    half = (stars - n_full) >= 0.5
    total = gw * 5 + space * 4 + 92
    x = cx - total / 2
    dim = (90, 90, 110)
    th = F_STAR.size + 12
    for i in range(5):
        if i < n_full:
            d.text((x, cy), "★", font=F_STAR, fill=ACCENT, anchor="lm")
        elif i == n_full and half:
            # 真半星：先空心，再叠左半边实心
            d.text((x, cy), "☆", font=F_STAR, fill=dim, anchor="lm")
            tile = Image.new("RGBA", (gw + 8, th), (0, 0, 0, 0))
            ImageDraw.Draw(tile).text((0, th // 2), "★", font=F_STAR, fill=ACCENT, anchor="lm")
            img.alpha_composite(tile.crop((0, 0, gw // 2 + 1, th)), (int(x), int(cy - th // 2)))
        else:
            d.text((x, cy), "☆", font=F_STAR, fill=dim, anchor="lm")
        x += gw + space
    d.text((x + 16, cy), f"{stars:g}", font=F_NUM, fill=ACCENT, anchor="lm")


def chips_row(d, moves, y):
    if not moves:
        return
    widths = [d.textlength(c, font=F_CHIP) + 40 for c in moves]
    tot = sum(widths) + 16 * (len(moves) - 1)
    x = W / 2 - tot / 2
    for c, cw in zip(moves, widths):
        d.rounded_rectangle([x, y, x + cw, y + 46], radius=23, outline=CB, width=2,
                            fill=(15, 25, 35))
        d.text((x + cw / 2, y + 23), c, font=F_CHIP, fill=CB, anchor="mm")
        x += cw + 16


def render_titlecard(seg) -> Image.Image:
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if seg["type"] == "intro":
        d.text((W / 2, 470), seg["title1"], font=F_HUGE, fill=CA, anchor="mm")
        d.text((W / 2, 578), seg["title2"], font=F_MID, fill=CB, anchor="mm")
        for i, ln in enumerate(wrap(d, seg["sub"], F_SUB, W - 2 * MARGIN)):
            d.text((W / 2, 678 + i * 46), ln, font=F_SUB, fill=WHITE, anchor="mm")
        d.text((W / 2, 782), seg["week"], font=F_SMALL, fill=ACCENT, anchor="mm")
        pill(d, W / 2, 858, seg["foot"], F_TAG, BG, ACCENT)
        d.text((W / 2, 946), "每周最火热舞 · 零基础也能跟", font=F_SMALL, fill=CB, anchor="mm")
    else:
        d.text((W / 2, 470), "谢谢观看", font=F_HUGE, fill=CA, anchor="mm")
        d.text((W / 2, 588), "关注追更 · 下周同一时间见", font=F_SUB, fill=WHITE, anchor="mm")
        pill(d, W / 2, 672, "＋ 关注", F_MID, BG, CB, pad=40)
    return img


def render_thanks():
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.text((W / 2, 560), "谢谢观看", font=F_HUGE, fill=CA, anchor="mm")
    d.text((W / 2, 672), "下周同一时间见", font=F_SUB, fill=WHITE, anchor="mm")
    return img


def render_overlay(seg) -> Image.Image:
    """真片/占位通用的紧凑信息条：上标题星级，下动作字幕，中间尽量留给画面。"""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # 顶部面板（压矮 + 降不透明度，露出更多舞蹈）
    d.rounded_rectangle([-40, -40, W + 40, 232], radius=26, fill=(8, 8, 16, 158))
    tag_bg = ACCENT if seg["type"] == "top" else CB
    pill(d, W / 2, 34, seg["tag"], F_TAG, BG, tag_bg)
    for ln in wrap(d, seg["title"], F_TITLE, W - 2 * MARGIN)[:1]:
        d.text((W / 2, 116), ln, font=F_TITLE, fill=CA, anchor="mm")
    # 「平台 空格 @作者」 无标点，符合用户偏好
    # 屏幕字幕只保留 @作者 —— 不出现平台名（用户要求）
    byline = seg.get("creator", "").strip()
    if byline:
        d.text((W / 2, 162), byline, font=F_SMALL, fill=GRAY, anchor="mm")
    # 观看/点赞 用「万」精度；缺失就不写
    def _wan(n):
        try: n = int(n or 0)
        except Exception: return ""
        if n < 10000: return ""
        return f"{n // 10000}万"
    stats = []
    like_s = _wan(seg.get("like"));  play_s = _wan(seg.get("play"))
    if play_s: stats.append(f"播放 {play_s}")
    if like_s: stats.append(f"点赞 {like_s}")
    if stats:
        d.text((W / 2, 186), "  ".join(stats), font=F_SMALL, fill=CA, anchor="mm")
    draw_stars(img, d, W / 2, 220, seg["stars"])
    # 中部 AI 口播大字幕（半透明黑底 + 大白字）
    vo_cap = seg.get("vo_caption", "")
    if vo_cap:
        vo_lines = wrap(d, vo_cap, F_SUB, W - 2 * MARGIN - 40)[:3]
        line_h = F_SUB.size + 14
        box_h = line_h * len(vo_lines) + 30
        box_top = H - box_h - 60
        d.rounded_rectangle([MARGIN - 8, box_top, W - MARGIN + 8, box_top + box_h],
                            radius=18, fill=(0, 0, 0, 190))
        y = box_top + 15 + line_h / 2
        for ln in vo_lines:
            d.text((W / 2, y), ln, font=F_SUB, fill=WHITE, anchor="mm")
            y += line_h
    # 底部面板已移除（用户 2026-07 要求：内容少没必要，让画面更干净）
    return img


def make_glow(radius, color):
    size = radius * 2
    yy, xx = np.mgrid[0:size, 0:size]
    dist = np.sqrt((xx - radius) ** 2 + (yy - radius) ** 2) / radius
    alpha = (np.clip(1 - dist, 0, 1) ** 2 * 150).astype("uint8")
    arr = np.zeros((size, size, 4), "uint8")
    arr[..., 0], arr[..., 1], arr[..., 2] = color
    arr[..., 3] = alpha
    return Image.fromarray(arr, "RGBA")


GLOW_A = make_glow(300, CA)
GLOW_B = make_glow(300, CB)


def render_watermark():
    """个人品牌水印（占位）：圆形头像 logo + BestDancer。"""
    f = load_font(26, True)
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    logo = None
    logo_path = REPO / "brand" / "logo.png"
    if logo_path.exists():
        logo = Image.open(logo_path).convert("RGBA").resize((38, 38), Image.LANCZOS)
    name = "BestDancer"
    nw = probe.textlength(name, font=f)
    lw = (logo.width + 8) if logo else 0
    wm = Image.new("RGBA", (int(lw + nw) + 6, 40), (0, 0, 0, 0))
    if logo:
        wm.alpha_composite(logo, (0, (40 - logo.height) // 2))
    d = ImageDraw.Draw(wm)
    d.text((lw, 20), name, font=f, fill=(245, 245, 250, 210), anchor="lm")
    return wm


WM = render_watermark()


def animated_base(t):
    base = Image.new("RGBA", (W, H), BG + (255,))
    ph = t * 1.2
    base.alpha_composite(GLOW_A, (int(W / 2 + 180 * math.sin(ph)) - 300,
                                  int(360 + 120 * math.cos(ph * 0.7)) - 300))
    base.alpha_composite(GLOW_B, (int(W / 2 - 160 * math.sin(ph * 0.8)) - 300,
                                  int(900 + 140 * math.sin(ph * 0.9)) - 300))
    d = ImageDraw.Draw(base)
    bars = 13
    bw = (W - 2 * MARGIN) / bars
    for i in range(bars):
        hh = 20 + 70 * abs(math.sin(t * 6 + i * 0.6)) * (0.6 + 0.4 * math.sin(t * 2 + i))
        x = MARGIN + i * bw
        d.rounded_rectangle([x + 4, H - 40 - hh, x + bw - 4, H - 40], radius=6,
                            fill=CA if i % 2 == 0 else CB)
    return base


# ---------- 音频 ----------
def wav_dur(p):
    with wave.open(str(p), "rb") as w:
        return w.getnframes() / w.getframerate()


def concat_wavs(files, out, target_durs=None):
    """按顺序拼接 wav；若给 target_durs，把每段 pad 到目标时长再加 GAP 静音。"""
    params, data = None, bytearray()
    for i, f in enumerate(files):
        with wave.open(str(f), "rb") as w:
            if params is None:
                params = w.getparams()
            frames = w.getnframes()
            data += w.readframes(frames)
        cur_sec = frames / params.framerate
        pad_sec = 0.0
        if target_durs is not None and i < len(target_durs):
            pad_sec = max(0.0, target_durs[i] - cur_sec)
        pad_frames = int(params.framerate * (pad_sec + GAP))
        data += b"\x00" * pad_frames * params.sampwidth * params.nchannels
    with wave.open(str(out), "wb") as w:
        w.setparams(params)
        w.writeframes(bytes(data))


def ensure_bgm():
    if BGM_PATH.exists():
        return BGM_PATH
    if not FALLBACK_BGM_PATH.exists():
        import make_bgm
        make_bgm.generate(FALLBACK_BGM_PATH)
    return FALLBACK_BGM_PATH


def seg_bed(entry, i, dur, tmp, ff, bgm):
    """该段背景音：优先真片自带 BGM，无音轨则用自制 BGM。"""
    dst = tmp / f"bed_{i:02d}.wav"
    dst.unlink(missing_ok=True)   # 清残留，避免上一轮的自制 bed 被误判为真片音轨
    if entry.get("bg"):
        subprocess.run([ff, "-y", "-loglevel", "error", "-t", f"{dur:.3f}",
                        "-i", str(entry["bg"]), "-vn", "-ar", "44100", "-ac", "2",
                        "-sample_fmt", "s16", str(dst)], capture_output=True)
        if dst.exists() and dst.stat().st_size > 2000:
            return dst, 1.0
    subprocess.run([ff, "-y", "-loglevel", "error", "-stream_loop", "-1",
                    "-t", f"{dur:.3f}", "-i", str(bgm), "-ar", "44100", "-ac", "2",
                    "-sample_fmt", "s16", str(dst)], check=True)
    return dst, 0.55


def concat_bed(slices, out):
    chunks = []
    for path, gain in slices:
        with wave.open(str(path), "rb") as w:
            data = w.readframes(w.getnframes())
        a = np.frombuffer(data, dtype="<i2").astype(np.float32) * gain
        chunks.append(np.clip(a, -32768, 32767).astype("<i2").tobytes())
    with wave.open(str(out), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(b"".join(chunks))


def edge_tts_synth(text, out_wav, ff, voice=VOICE, rate=RATE):
    mp3 = out_wav.with_suffix(".mp3")
    asyncio.run(__import__("edge_tts").Communicate(text, voice, rate=rate).save(str(mp3)))
    subprocess.run([ff, "-y", "-loglevel", "error", "-i", str(mp3),
                    "-ar", "44100", "-ac", "1", str(out_wav)], check=True)
    mp3.unlink(missing_ok=True)


def find_clip(week, cid):
    if not cid:
        return None
    d = REPO / "assets" / "incoming" / week
    if not d.is_dir():
        return None
    for f in sorted(d.iterdir()):
        if f.suffix.lower() in VIDEO_EXTS and f.stem.split("__")[0] == cid:
            return f
    return None


def normalize_clip(src, dst, dur, ff, start=0.0):
    # 保留原音轨（若有），供“真片自带 BGM”使用
    subprocess.run([ff, "-y", "-loglevel", "error", "-ss", f"{start:.3f}", "-stream_loop", "-1",
                    "-t", f"{dur:.3f}", "-i", str(src), "-vf",
                    f"scale={W}:{H}:force_original_aspect_ratio=increase,"
                    f"crop={W}:{H},fps={FPS}", "-pix_fmt", "yuv420p", str(dst)],
                   check=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("week")
    ap.add_argument("--no-audio", action="store_true")
    args = ap.parse_args()

    import imageio.v2 as imageio
    import imageio_ffmpeg
    ff = imageio_ffmpeg.get_ffmpeg_exe()

    cfg_dir = REPO / "config" / "weekly"
    cfg_path = cfg_dir / f"{args.week}.json"
    if not cfg_path.exists():
        cfg_path = cfg_dir / f"{args.week}.example.json"
    cfg = json.loads(cfg_path.read_text("utf-8"))
    selected_clip_ids = [pick.get("id") for pick in cfg.get("picks", []) if pick.get("id")]
    classic_id = cfg.get("classic_comeback", {}).get("id")
    if classic_id:
        selected_clip_ids.append(classic_id)
    missing_clips = [candidate_id for candidate_id in selected_clip_ids if not find_clip(args.week, candidate_id)]
    if missing_clips:
        raise SystemExit("入选舞段尚未下载，拒绝渲染占位成片: " + ", ".join(missing_clips))
    segs = build_segments(cfg)

    # ---- 文案 review 输出（控制台 + 文本文件）----
    review_lines = ["=" * 60, f"📝 文案 REVIEW ({args.week}) — 共 {len(segs)} 段", "=" * 60]
    for i, s in enumerate(segs):
        t = s.get("type", "?")
        label = f"[{i:02d}][{t}]"
        if t == "top":
            label += f" No.{s.get('rank')}"
        vo = s.get("vo", "")
        review_lines += [f"\n{label}  ({len(vo)}字)", f"  🎙  {vo}"]
        subtitles = s.get("subtitles", [])
        if subtitles:
            review_lines.append("  💬  " + " / ".join(subtitles))
        elif s.get("sub"):
            review_lines.append(f"  💬  {s['sub']}")
    review_lines.append("=" * 60)
    review_text = "\n".join(review_lines) + "\n"
    print("\n" + review_text, flush=True)
    review_path = REPO / "output" / "research" / f"{args.week}_copy_review.txt"
    review_path.parent.mkdir(parents=True, exist_ok=True)
    review_path.write_text(review_text, "utf-8")

    tts_dir = REPO / "output" / "tts" / args.week
    tts_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir = REPO / "output" / "tmp" / args.week
    tmp_dir.mkdir(parents=True, exist_ok=True)

    # 配音：优先 edge-tts 神经女声，失败回退 SAPI
    audio_ok, engine = False, "none"
    if not args.no_audio:
        try:
            for i, s in enumerate(segs):
                edge_tts_synth(s["vo"], tts_dir / f"{i:02d}.wav", ff, s.get("voice", VOICE), s.get("voice_rate", RATE))
            audio_ok, engine = True, f"edge:{VOICE}"
        except Exception as e:  # noqa: BLE001
            print(f"[warn] edge-tts 失败({e})，回退 SAPI 语音")
            manifest = {"items": [{"name": f"{i:02d}.wav", "seg": s["type"], "text": s["vo"]}
                                  for i, s in enumerate(segs)]}
            (tts_dir / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), "utf-8")
            try:
                subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                                "-File", str(REPO / "pipeline" / "tts_sapi.ps1"), args.week],
                               check=True)
                audio_ok = all((tts_dir / it["name"]).exists() for it in manifest["items"])
                engine = "sapi"
            except Exception as e2:  # noqa: BLE001
                print(f"[warn] SAPI 也失败({e2})，出无声样片")

    default_dur = {"intro": 3.5, "top": 15.0, "classic": 15.0, "outro": 6.0}
    min_dur = {"intro": 2.5, "top": 10.0, "classic": 10.0, "outro": 6.0}
    max_dur = {"intro": 5.0, "top": 20.0, "classic": 20.0, "outro": 8.0}
    timeline, t0, wavs = [], 0.0, []
    for i, s in enumerate(segs):
        wp = tts_dir / f"{i:02d}.wav"
        vo_dur = wav_dur(wp) if audio_ok else 0
        # 真片本身多长？拿到就用真片长度 clamp 到 [min,max]
        clip = find_clip(args.week, s.get("cid")) if s.get("cid") else None
        clip_dur = 0.0
        if clip:
            try:
                probe = subprocess.check_output(
                    ["ffprobe", "-v", "error", "-show_entries", "format=duration",
                     "-of", "default=noprint_wrappers=1:nokey=1", str(clip)],
                    text=True).strip()
                clip_dur = float(probe)
            except Exception:
                clip_dur = 0.0
        clip_start = max(0.0, s.get("clip_start_sec", 0.0))
        clip_end = s.get("clip_end_sec", 0.0)
        selected_dur = clip_end - clip_start if clip_end > clip_start else 0.0
        target = selected_dur or clip_dur or default_dur.get(s["type"], 4.0)
        dur = max(target, vo_dur, min_dur.get(s["type"], 3.0))
        dur = min(dur, max_dur.get(s["type"], 30.0))
        if selected_dur:
            dur = min(dur, selected_dur)
        if audio_ok:
            wavs.append(wp)
        timeline.append({"seg": s, "start": t0, "adur": dur})
        t0 += dur + GAP
    total = t0
    total_v = total

    if audio_ok:
        concat_wavs(wavs, tts_dir / "voice.wav",
                    target_durs=[e["adur"] for e in timeline])

    # 背景：有真片就归一化成竖屏，没有则占位
    for i, e in enumerate(timeline):
        clip = find_clip(args.week, e["seg"].get("cid"))
        if clip:
            dst = tmp_dir / f"bg_{i:02d}.mp4"
            try:
                normalize_clip(clip, dst, e["adur"] + GAP, ff, e["seg"].get("clip_start_sec", 0.0))
                e["bg"] = dst
                e["src"] = clip.name
            except Exception as ex:  # noqa: BLE001
                print(f"[warn] 归一化失败 {clip.name}: {ex}")
        e["overlay"] = (render_titlecard(e["seg"]) if e["seg"]["type"] in ("intro", "outro")
                        else render_overlay(e["seg"]))

    # 背景音：逐段优先真片自带 BGM，无则自制；tail 用自制。
    bgm = ensure_bgm()
    bed_slices = [seg_bed(e, i, e["adur"] + GAP, tmp_dir, ff, bgm)
                  for i, e in enumerate(timeline)]
    real_n = sum(1 for _, g in bed_slices if g >= 1.0)
    bed_path = tmp_dir / "bed.wav"
    concat_bed(bed_slices, bed_path)

    out_dir = REPO / "output"
    silent = out_dir / f"{args.week}_demo_silent.mp4"
    final = out_dir / f"{args.week}_demo.mp4"
    writer = imageio.get_writer(silent, fps=FPS, codec="libx264", quality=8,
                                macro_block_size=16, output_params=["-pix_fmt", "yuv420p"])

    n_real = 0
    for e in timeline:
        seg, start, adur = e["seg"], e["start"], e["adur"]
        n = max(1, int(round((adur + GAP) * FPS)))
        overlay = e["overlay"]
        reader = it = last = None
        if e.get("bg"):
            reader = imageio.get_reader(str(e["bg"]))
            it = reader.iter_data()
            n_real += 1
        for k in range(n):
            t = start + k / FPS
            if it is not None:
                try:
                    last = Image.fromarray(next(it)).convert("RGBA")
                except StopIteration:
                    pass
                base = last.copy() if last else animated_base(t)
            else:
                base = animated_base(t)
            d = ImageDraw.Draw(base)
            d.rounded_rectangle([MARGIN, 40, W - MARGIN, 50], radius=5, fill=(45, 45, 60))
            d.rounded_rectangle([MARGIN, 40, MARGIN + (W - 2 * MARGIN) * (t / total_v), 50],
                                radius=5, fill=ACCENT)
            fade = max(0.0, min(min((t - start) / 0.25, 1.0),
                                min((start + adur + GAP - t) / 0.25, 1.0)))
            mask = overlay.split()[3].point(lambda a: int(a * fade))
            base.paste(overlay, (0, 0), mask)
            base.alpha_composite(WM, (W - WM.width - 18, 58))
            writer.append_data(np.asarray(base.convert("RGB")))
        if reader:
            reader.close()
    writer.close()

    if audio_ok:
        subprocess.run([ff, "-y", "-loglevel", "error", "-i", str(silent),
                        "-i", str(tts_dir / "voice.wav"), "-i", str(bed_path),
                        "-filter_complex",
                        "[1:a]aformat=sample_rates=44100:channel_layouts=stereo,"
                        "volume=1.55,asplit=2[vo][sc];"
                        "[2:a]aformat=sample_rates=44100:channel_layouts=stereo,volume=0.32[bg];"
                        "[bg][sc]sidechaincompress=threshold=0.008:ratio=15:"
                        "attack=10:release=350[ducked];"
                        "[vo][ducked]amix=inputs=2:duration=longest:normalize=0,"
                        "alimiter=limit=0.95[a]",
                        "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac",
                        "-b:a", "192k", "-shortest", str(final)], check=True)
    else:
        subprocess.run([ff, "-y", "-loglevel", "error", "-i", str(silent),
                        "-i", str(bed_path), "-c:v", "copy",
                        "-c:a", "aac", "-b:a", "192k", "-shortest", str(final)], check=True)
    silent.unlink(missing_ok=True)

    # OpenClaw 只允许从 workspace 等安全目录发送附件；自动导出一份可点击版本。
    export_dir = Path.home() / ".openclaw" / "workspace" / "exports" / "bestdancer"
    export_dir.mkdir(parents=True, exist_ok=True)
    exported_video = export_dir / final.name
    shutil.copy2(final, exported_video)
    review_path = REPO / "output" / "research" / f"{args.week}_copy_review.txt"
    if review_path.exists():
        shutil.copy2(review_path, export_dir / review_path.name)

    print(f"OK -> {final.relative_to(REPO).as_posix()}  ({total_v:.1f}s, 语音={engine}, "
          f"真片背景={n_real}/{len(timeline)}, 真片BGM={real_n}/{len(timeline)})")
    print(f"ATTACH -> {exported_video}")
    if total_v > 100:
        print(f"[note] 时长 {total_v:.1f}s 偏长，可把 first_sentences 改 2 句。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
