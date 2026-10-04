#!/usr/bin/env python3
"""生成「路由器软件中心」的应用图标（256×256 PNG，对齐小米 NAS 官方图标的观感）。

只依赖 Pillow；形状：圆角方形渐变底 + 白色路由器图形（机身 + 两根天线 + 信号弧）。
用法: python3 assets/make_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

SIZE = 256
SCALE = 4                       # 先在 4 倍画布上画，再缩小，边缘更干净
RADIUS = int(SIZE * 0.22)


def rounded_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new('L', (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=radius, fill=255)
    return mask


def gradient(size: int, top: tuple[int, int, int], bottom: tuple[int, int, int]) -> Image.Image:
    image = Image.new('RGB', (1, size))
    for y in range(size):
        ratio = y / max(1, size - 1)
        image.putpixel((0, y), tuple(
            int(top[i] + (bottom[i] - top[i]) * ratio) for i in range(3)))
    return image.resize((size, size))


def build() -> Image.Image:
    size = SIZE * SCALE
    base = gradient(size, (86, 132, 255), (40, 82, 208)).convert('RGBA')
    mask = rounded_mask(size, RADIUS * SCALE)

    # 顶部极淡高光，模拟官方图标的玻璃感
    gloss = Image.new('L', (size, size), 0)
    ImageDraw.Draw(gloss).ellipse((-size * 0.4, -size * 0.9, size * 1.4, size * 0.55), fill=16)
    base.alpha_composite(Image.merge('RGBA', (Image.new('L', (size, size), 255),
                                               Image.new('L', (size, size), 255),
                                               Image.new('L', (size, size), 255), gloss)))

    draw = ImageDraw.Draw(base)
    white = (255, 255, 255, 255)
    unit = size / 256.0

    # 投影（先画一层模糊的深色，让图形脱离底色）
    shadow = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    body = [int(60 * unit), int(122 * unit), int(196 * unit), int(186 * unit)]
    shadow_draw.rounded_rectangle([body[0], body[1] + 5 * unit, body[2], body[3] + 5 * unit],
                                  radius=int(18 * unit), fill=(10, 30, 80, 110))
    base.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(7 * unit)))

    # 机身
    draw.rounded_rectangle(body, radius=int(18 * unit), fill=white)

    # 两根天线
    draw.line([(int(88 * unit), int(122 * unit)), (int(74 * unit), int(72 * unit))],
              fill=white, width=int(11 * unit))
    draw.line([(int(168 * unit), int(122 * unit)), (int(182 * unit), int(72 * unit))],
              fill=white, width=int(11 * unit))
    for x, y in ((int(74 * unit), int(71 * unit)), (int(182 * unit), int(71 * unit))):
        draw.ellipse([x - int(7 * unit), y - int(7 * unit), x + int(7 * unit), y + int(7 * unit)], fill=white)

    # 机身里的三个指示灯
    for index in range(3):
        x = int((104 + index * 24) * unit)
        draw.rounded_rectangle([x, int(140 * unit), x + int(12 * unit), int(164 * unit)],
                               radius=int(6 * unit), fill=(64, 104, 214, 255))

    # 顶部信号弧
    for radius, width in ((34, 9), (22, 9)):
        box = [int(128 * unit) - radius * int(unit * 10) // 10, int(52 * unit) - radius * int(unit * 10) // 10,
               0, 0]
        box[2] = int(128 * unit) + radius * int(unit * 10) // 10
        box[3] = int(52 * unit) + radius * int(unit * 10) // 10
        draw.arc(box, start=205, end=335, fill=white, width=int(width * unit))

    base.putalpha(mask)
    return base.resize((SIZE, SIZE), Image.LANCZOS)


def main() -> int:
    project = Path(__file__).resolve().parent.parent
    icon = build()
    targets = [project / 'assets' / 'xiaomi-router-center.png', project / 'web' / 'icon.png']
    for target in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        icon.save(target)
        print(f'已生成 {target}（{target.stat().st_size} 字节）')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
