#!/usr/bin/env python3
"""从个人照裁 1:1 圆形 logo -> brand/logo.png（BestDancer 品牌占位）。

用法: python pipeline/make_logo.py "C:\\Users\\jiapengli\\Downloads\\个人.png"
裁画面中心偏上的正方形（对准人物），做成圆形 + 金色描边的小头像。
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

REPO = Path(__file__).resolve().parents[1]
ACCENT = (255, 225, 77, 255)   # 金色描边


def main() -> int:
    if len(sys.argv) < 2:
        print('用法: python pipeline/make_logo.py "<图片路径>"')
        return 1
    src = Path(sys.argv[1])
    if not src.exists():
        print(f"找不到图片：{src}")
        return 1

    im = Image.open(src).convert("RGBA")
    w, h = im.size
    side = min(w, h)
    cx, cy = int(w * 0.46), int(h * 0.50)     # 人物大致中心
    half = int(side * 0.42)
    box = (max(0, cx - half), max(0, cy - half),
           min(w, cx + half), min(h, cy + half))
    sq = im.crop(box).resize((256, 256), Image.LANCZOS)

    mask = Image.new("L", (256, 256), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, 255, 255), fill=255)
    out = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    out.paste(sq, (0, 0), mask)
    ImageDraw.Draw(out).ellipse((3, 3, 252, 252), outline=ACCENT, width=8)

    (REPO / "brand").mkdir(exist_ok=True)
    dst = REPO / "brand" / "logo.png"
    out.save(dst)
    print(f"logo -> {dst.relative_to(REPO).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
