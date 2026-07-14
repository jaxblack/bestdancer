#!/usr/bin/env python3
"""本周热舞 · 自制 BGM 生成器（纯合成，零版权风险）

用 numpy 合成一段可循环的轻快电子节拍（kick + hats + bass + pad），
写到 assets/bgm/auto_beat.wav。render_demo.py 会自动调用；也可单独运行调参。

用法: python pipeline/make_bgm.py           # 默认 110 BPM
      python pipeline/make_bgm.py --bpm 120
"""
from __future__ import annotations

import argparse
import wave
from pathlib import Path

import numpy as np

SR = 44100
REPO = Path(__file__).resolve().parents[1]


def generate(path: Path, bpm: float = 110.0) -> float:
    np.random.seed(7)
    beat = 60.0 / bpm
    bn = int(beat * SR)
    beats = 16                      # 4 小节
    total = beats * bn
    mix = np.zeros(total + SR)

    bass_roots = [55.0, 43.65, 65.41, 49.0]          # Am F C G (低八度)
    pads = [[220.0, 261.63, 329.63], [174.61, 220.0, 261.63],
            [261.63, 329.63, 392.0], [196.0, 246.94, 293.66]]

    def place(pos, sig):
        e = min(pos + len(sig), len(mix))
        mix[pos:e] += sig[:e - pos]

    for b in range(beats):
        pos = b * bn
        bar = (b // 4) % 4
        # kick（每拍，带音高下坠）
        kn = int(0.32 * SR)
        t = np.arange(kn) / SR
        f = 120 * np.exp(-t * 28) + 48
        place(pos, np.sin(2 * np.pi * np.cumsum(f) / SR) * np.exp(-t * 7) * 0.9)
        # hats（八分音符）
        for h in (0, 1):
            hn = int(0.09 * SR)
            hat = (np.random.rand(hn) * 2 - 1) * np.exp(-np.arange(hn) / SR * 70) * (0.16 if h else 0.11)
            place(pos + h * bn // 2, hat)
        # bass（八分脉冲）
        for e in (0, 1):
            root = bass_roots[bar]
            ln = bn // 2
            t2 = np.arange(ln) / SR
            bs = (np.sin(2 * np.pi * root * t2) * 0.7
                  + np.sign(np.sin(2 * np.pi * root * t2)) * 0.22) * np.exp(-t2 * 4) * 0.5
            place(pos + e * bn // 2, bs)
        # pad（每小节起持续和弦）
        if b % 4 == 0:
            ln = bn * 4
            t3 = np.arange(ln) / SR
            pad = sum(np.sin(2 * np.pi * fq * t3) for fq in pads[bar]) / 3.0
            place(pos, pad * (np.minimum(t3 * 4, 1.0) * np.exp(-t3 * 0.4) * 0.26))

    mix = mix[:total]
    mix = np.convolve(mix, np.ones(8) / 8, mode="same")   # 简易低通，柔化
    mix = np.tanh(mix * 1.15)
    mix = mix / (np.max(np.abs(mix)) + 1e-9) * 0.6
    stereo = np.stack([mix, mix], axis=1)
    data = (stereo * 32767).astype("<i2").tobytes()

    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(data)
    return total / SR


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bpm", type=float, default=110.0)
    ap.add_argument("--out", default=str(REPO / "assets" / "bgm" / "auto_beat.wav"))
    args = ap.parse_args()
    dur = generate(Path(args.out), args.bpm)
    print(f"BGM -> {args.out}  ({dur:.1f}s @ {args.bpm:g}bpm)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
