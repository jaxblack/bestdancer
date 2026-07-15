#!/usr/bin/env python3
"""从个人舞者图的中心截取 1:1 方形 Logo。

用法: python pipeline/make_logo.py "C:\\Users\\jiapengli\\Downloads\\个人.png"
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image

REPO = Path(__file__).resolve().parents[1]


def main() -> int:
    if len(sys.argv) < 2:
        print('用法: python pipeline/make_logo.py "<图片路径>"')
        return 1
    src = Path(sys.argv[1])
    if not src.exists():
        print(f"找不到图片：{src}")
        return 1

    size = 256
    with Image.open(src) as image:
        side = min(image.width, image.height)
        left = (image.width - side) // 2
        top = (image.height - side) // 2
        out = image.crop((left, top, left + side, top + side)).resize((size, size), Image.Resampling.LANCZOS)

    (REPO / "brand").mkdir(exist_ok=True)
    dst = REPO / "brand" / "logo.png"
    out.save(dst)
    print(f"logo -> {dst.relative_to(REPO).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
