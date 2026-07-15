#!/usr/bin/env python3
"""从最终成品 Logo（brand/final_logo.png）导出各平台资源。

- brand/logo.png       直接复制你的成品原图，逐字节保留，绝不裁切/缩放/重编码
- brand/logo-512/256/128.png  仅等比缩放（原图为方图，构图完全不变）
- brand/avatar-round.png      圆形头像（抖音/小红书），保留完整构图

用法:
    python pipeline/make_logo.py                 # 默认用 brand/final_logo.png
    python pipeline/make_logo.py "<图片路径>"    # 指定其它源图
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

from PIL import Image, ImageDraw

REPO = Path(__file__).resolve().parents[1]


def _pad_square(image: Image.Image, bg: tuple = (0, 0, 0, 255)) -> Image.Image:
    """非方图时用黑底补边成正方形（只补边，绝不裁切）。"""
    w, h = image.size
    if w == h:
        return image
    side = max(w, h)
    canvas = Image.new("RGBA", (side, side), bg)
    canvas.alpha_composite(image.convert("RGBA"), ((side - w) // 2, (side - h) // 2))
    return canvas


def _circle(image: Image.Image) -> Image.Image:
    size = image.width
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, size - 1, size - 1], fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(image.convert("RGBA"), (0, 0), mask)
    return out


def main() -> int:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO / "brand" / "final_logo.png"
    if not src.exists():
        print(f"找不到图片：{src}")
        return 1

    brand = REPO / "brand"
    brand.mkdir(exist_ok=True)
    written: list[str] = []

    # 主 Logo：直接使用你的成品原图，逐字节复制，不做任何处理
    dst = brand / "logo.png"
    if src.resolve() != dst.resolve():
        shutil.copyfile(src, dst)
    written.append("brand/logo.png")

    with Image.open(src) as image:
        master = _pad_square(image.convert("RGBA"))

        # 常用尺寸：等比缩放（方图缩放不改构图）
        for size in (512, 256, 128):
            master.resize((size, size), Image.Resampling.LANCZOS).save(brand / f"logo-{size}.png")
            written.append(f"brand/logo-{size}.png")

        # 圆形头像（抖音/小红书）：圆形遮罩，保留完整构图
        _circle(master.resize((512, 512), Image.Resampling.LANCZOS)).save(brand / "avatar-round.png")
        written.append("brand/avatar-round.png")

    for name in written:
        print(f"logo -> {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
