#!/usr/bin/env python3
"""把最终成品 Logo（brand/final_logo.png）导出为各平台尺寸 + 圆形头像。

用法:
    python pipeline/make_logo.py                 # 默认用 brand/final_logo.png
    python pipeline/make_logo.py "<图片路径>"    # 指定其它源图
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

REPO = Path(__file__).resolve().parents[1]


def _square(image: Image.Image) -> Image.Image:
    side = min(image.width, image.height)
    left = (image.width - side) // 2
    top = (image.height - side) // 2
    return image.crop((left, top, left + side, top + side))


def _pad_square(image: Image.Image, size: int, scale: float,
                bg: tuple = (0, 0, 0, 255)) -> Image.Image:
    canvas = Image.new("RGBA", (size, size), bg)
    inner = max(1, int(size * scale))
    resized = image.resize((inner, inner), Image.Resampling.LANCZOS)
    off = (size - inner) // 2
    canvas.alpha_composite(resized, (off, off))
    return canvas


def _circle(image: Image.Image) -> Image.Image:
    size = image.width
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, size - 1, size - 1], fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(image, (0, 0), mask)
    return out


def main() -> int:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO / "brand" / "final_logo.png"
    if not src.exists():
        print(f"找不到图片：{src}")
        return 1

    brand = REPO / "brand"
    brand.mkdir(exist_ok=True)

    with Image.open(src) as image:
        square = _square(image.convert("RGBA"))

    written: list[str] = []

    # 主 Logo（满幅）+ 常用方形尺寸
    square.resize((1024, 1024), Image.Resampling.LANCZOS).save(brand / "logo.png")
    written.append("brand/logo.png")
    for size in (512, 256, 128):
        square.resize((size, size), Image.Resampling.LANCZOS).save(brand / f"logo-{size}.png")
        written.append(f"brand/logo-{size}.png")

    # 圆形头像（抖音/小红书）：留一点黑边，圆裁不切到舞台
    avatar = _circle(_pad_square(square, 512, 0.9))
    avatar.save(brand / "avatar-round.png")
    written.append("brand/avatar-round.png")

    for name in written:
        print(f"logo -> {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
