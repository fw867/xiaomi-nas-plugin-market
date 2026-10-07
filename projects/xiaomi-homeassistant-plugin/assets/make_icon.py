#!/usr/bin/env python3
"""生成「Home Assistant」的应用图标（256×256 PNG，对齐小米 NAS 官方图标的观感）。

只依赖 Pillow；形状：圆角方形蓝色渐变底 + 白色房子轮廓（屋顶 + 屋身 + 门），
屋身里再放三颗 HA 风格的浅色圆点，和 projects/xiaomi-router-center/assets/make_icon.py
是同一套画法（4 倍画布上作画再缩小，边缘更干净）。

用法: python3 assets/make_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

SIZE = 256
SCALE = 4                       # 先在 4 倍画布上画，再缩小，边缘更干净
RADIUS = int(SIZE * 0.22)
# Home Assistant 的品牌蓝（#18BCF2 偏亮，官方标识底色更深一点，这里取中间值）
BG_TOP = (86, 190, 246)
BG_BOTTOM = (24, 116, 210)
ACCENT = (24, 116, 210, 255)


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
    base = gradient(size, BG_TOP, BG_BOTTOM).convert('RGBA')
    mask = rounded_mask(size, RADIUS * SCALE)

    # 顶部极淡高光，模拟官方图标的玻璃感
    gloss = Image.new('L', (size, size), 0)
    ImageDraw.Draw(gloss).ellipse((-size * 0.4, -size * 0.9, size * 1.4, size * 0.55), fill=18)
    base.alpha_composite(Image.merge('RGBA', (Image.new('L', (size, size), 255),
                                               Image.new('L', (size, size), 255),
                                               Image.new('L', (size, size), 255), gloss)))

    unit = size / 256.0
    draw = ImageDraw.Draw(base)
    white = (255, 255, 255, 255)

    def px(value: float) -> int:
        return int(round(value * unit))

    # 房子轮廓的顶点（先画屋顶三角形，再画屋身矩形，两者同色拼接成整体剪影）
    roof = [(px(128), px(48)), (px(232), px(132)), (px(24), px(132))]
    body = [px(52), px(128), px(204), px(216)]
    door = [px(108), px(160), px(148), px(216)]

    # 投影：先画一层模糊的深色剪影，让图形脱离底色
    shadow = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    shadow_draw = ImageDraw.Draw(shadow)
    offset = px(6)
    shadow_draw.polygon([(x, y + offset) for x, y in roof], fill=(8, 44, 96, 120))
    shadow_draw.rounded_rectangle([body[0], body[1] + offset, body[2], body[3] + offset],
                                  radius=px(14), fill=(8, 44, 96, 120))
    base.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(px(7))))

    draw.polygon(roof, fill=white)
    draw.rounded_rectangle(body, radius=px(14), fill=white)
    # 门：挖成底色，看起来像房子的入口
    draw.rounded_rectangle(door, radius=px(8), fill=ACCENT)

    # 门左侧两个 HA 风格的状态点（家庭自动化的小暗示），与门留出间距
    for index in range(2):
        x = px(76 + index * 22)
        y = px(186)
        dot = px(7)
        draw.ellipse([x - dot, y - dot, x + dot, y + dot], fill=(86, 190, 246, 255))

    base.putalpha(mask)
    return base.resize((SIZE, SIZE), Image.LANCZOS)


def main() -> int:
    project = Path(__file__).resolve().parent.parent
    icon = build()
    target = project / 'assets' / 'homeassistant.png'
    target.parent.mkdir(parents=True, exist_ok=True)
    icon.save(target)
    print(f'已生成 {target}（{target.stat().st_size} 字节）')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
