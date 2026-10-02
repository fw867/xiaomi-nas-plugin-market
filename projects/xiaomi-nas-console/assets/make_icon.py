#!/usr/bin/env python3
"""生成「控制台」的小米客户端图标（256×256 PNG）。

小米客户端读的是 /data/plugin/www/icon/nas-console.icon，
内容其实就是 PNG（与其它社区插件一致）。

用法：python3 assets/make_icon.py assets/xiaomi-nas-console.png
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 256
BG_TOP = (36, 39, 44)
BG_BOTTOM = (20, 22, 26)
ACCENT = (255, 122, 26)
WHITE = (240, 243, 247)


def vertical_gradient(size: int, top: tuple[int, int, int], bottom: tuple[int, int, int]) -> Image.Image:
    canvas = Image.new('RGB', (size, size))
    for y in range(size):
        ratio = y / max(1, size - 1)
        color = tuple(round(top[i] + (bottom[i] - top[i]) * ratio) for i in range(3))
        for x in range(size):
            canvas.putpixel((x, y), color)
    return canvas


def rounded_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new('L', (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
    return mask


def main() -> int:
    target = Path(sys.argv[1] if len(sys.argv) > 1 else 'assets/xiaomi-nas-console.png')
    target.parent.mkdir(parents=True, exist_ok=True)

    base = vertical_gradient(SIZE, BG_TOP, BG_BOTTOM).convert('RGBA')
    draw = ImageDraw.Draw(base)

    # 仪表环（概览）
    center = (SIZE // 2, 104)
    radius = 52
    draw.ellipse((center[0] - radius, center[1] - radius, center[0] + radius, center[1] + radius),
                 outline=(255, 255, 255, 38), width=14)
    draw.arc((center[0] - radius, center[1] - radius, center[0] + radius, center[1] + radius),
             start=205, end=335, fill=ACCENT, width=14)
    draw.ellipse((center[0] - 9, center[1] - 9, center[0] + 9, center[1] + 9), fill=WHITE)

    # 三条指标条（状态列表）
    bars = ((64, 168, 128, 20), (64, 196, 176, 20), (64, 224, 104, 20))
    for x0, y0, width, height in bars:
        draw.rounded_rectangle((x0, y0, x0 + width, y0 + height), radius=height // 2,
                               fill=(255, 255, 255, 210))

    base.putalpha(rounded_mask(SIZE, 56))
    base.save(target, format='PNG', optimize=True)
    print(f'已生成 {target} ({target.stat().st_size} 字节)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
