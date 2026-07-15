#!/usr/bin/env python3
"""把个人舞者图的人物主体抠出来（去背景），生成 Logo。

用法:
    python scripts/cutout_logo.py [源图路径]

默认源图: <repo>/个人.png
输出到 <repo>/brand/:
    cutout-raw.png        纯人物（裁剪到边界，透明背景）
    logo-cutout.png       透明方形 Logo（人物居中留白）
    logo-cutout-dark.png  品牌深色圆角底 Logo
    logo-cutout-glow.png  深色底 + 粉/青霓虹光晕（成品感）
    logo-cutout-round.png 圆形头像版（深底 + 光晕）
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter
from rembg import remove, new_session

REPO = Path(__file__).resolve().parents[1]

BASE = (11, 11, 20, 255)      # #0B0B14
PINK = (255, 46, 154)         # #FF2E9A
CYAN = (22, 224, 255)         # #16E0FF


def cut_person(src: Path) -> Image.Image:
    """用人像分割模型去背景，返回裁剪到人物边界的 RGBA 图。"""
    session = new_session("u2net_human_seg")
    img = Image.open(src).convert("RGBA")
    cut = remove(img, session=session, post_process_mask=True)
    bbox = cut.getbbox()
    if bbox:
        cut = cut.crop(bbox)
    return cut


def _fit(person: Image.Image, size: int, pad: float) -> tuple[Image.Image, int, int]:
    avail = int(size * (1 - 2 * pad))
    w, h = person.size
    scale = min(avail / w, avail / h)
    nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
    resized = person.resize((nw, nh), Image.Resampling.LANCZOS)
    return resized, (size - nw) // 2, (size - nh) // 2


def _rounded_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius, fill=255)
    return mask


def _circle_mask(size: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, size - 1, size - 1], fill=255)
    return mask


def _glow_layer(size: int) -> Image.Image:
    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(glow)
    r = int(size * 0.42)
    d.ellipse([int(size * 0.06), int(size * 0.08), int(size * 0.06) + r, int(size * 0.08) + r],
              fill=PINK + (150,))
    d.ellipse([size - int(size * 0.06) - r, size - int(size * 0.10) - r,
               size - int(size * 0.06), size - int(size * 0.10)], fill=CYAN + (140,))
    return glow.filter(ImageFilter.GaussianBlur(int(size * 0.14)))


def compose(person: Image.Image, size: int = 512, pad: float = 0.08,
            bg: tuple | None = None, radius: int = 0, glow: bool = False,
            circle: bool = False) -> Image.Image:
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    if bg is not None:
        bg_img = Image.new("RGBA", (size, size), bg)
        if circle:
            canvas.paste(bg_img, (0, 0), _circle_mask(size))
        elif radius:
            canvas.paste(bg_img, (0, 0), _rounded_mask(size, radius))
        else:
            canvas.alpha_composite(bg_img)
    if glow:
        canvas.alpha_composite(_glow_layer(size))
    resized, x, y = _fit(person, size, pad)
    canvas.alpha_composite(resized, (x, y))
    if circle:
        out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        out.paste(canvas, (0, 0), _circle_mask(size))
        return out
    return canvas


def main() -> int:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO / "个人.png"
    if not src.exists():
        print(f"找不到图片：{src}")
        return 1

    out_dir = REPO / "brand"
    out_dir.mkdir(exist_ok=True)

    print("抠图中（首次运行会下载人像分割模型，请稍候）…")
    person = cut_person(src)
    person.save(out_dir / "cutout-raw.png")

    compose(person, bg=None).save(out_dir / "logo-cutout.png")
    compose(person, bg=BASE, radius=116).save(out_dir / "logo-cutout-dark.png")
    compose(person, bg=BASE, radius=116, glow=True).save(out_dir / "logo-cutout-glow.png")
    compose(person, bg=BASE, glow=True, circle=True).save(out_dir / "logo-cutout-round.png")

    print("完成，输出到 brand/：")
    for name in ("cutout-raw", "logo-cutout", "logo-cutout-dark",
                 "logo-cutout-glow", "logo-cutout-round"):
        print(f"  brand/{name}.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
