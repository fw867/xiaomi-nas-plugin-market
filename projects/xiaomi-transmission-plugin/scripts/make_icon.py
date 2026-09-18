#!/usr/bin/env python3
"""生成插件图标（PNG），只用标准库。

图标是插件的必需资产（`scripts/build_apps.py` 的 `iconSource`），但它是二进制，
不适合手工维护。这里用代码画出来：几何形状固定、结果完全可复现，
改了配色或形状直接重跑即可。

用法：

    python3 scripts/make_icon.py                     # 写 web/assets/transmission.png
    python3 scripts/make_icon.py --size 256 --print-sha256
"""

from __future__ import annotations

import argparse
import hashlib
import math
import struct
import zlib
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT / "web" / "assets" / "transmission.png"

BACKGROUND = (0x0F, 0x8A, 0x8A)      # 深青
GLYPH = (0xFF, 0xFF, 0xFF)           # 白色托盘
ARROW = (0xF5, 0xA5, 0x24)           # 琥珀色箭头
SUPERSAMPLE = 4

# 以下坐标都在 512×512 的参考画布上，渲染时按 --size 缩放采样。
CANVAS = 512.0
SQUARE = (16.0, 16.0, 496.0, 496.0, 104.0)          # 圆角底板
TRAY_OUTER = (132.0, 296.0, 380.0, 404.0, 30.0)     # 托盘外框
TRAY_INNER = (160.0, 250.0, 352.0, 372.0, 0.0)      # 挖空 → U 形
SHAFT = (238.0, 116.0, 274.0, 284.0, 18.0)          # 箭杆
HEAD = ((256.0, 360.0), (188.0, 272.0), (324.0, 272.0))


def rounded_rect_sdf(x: float, y: float, box: tuple[float, ...]) -> float:
    left, top, right, bottom, radius = box
    half_x = (right - left) / 2.0 - radius
    half_y = (bottom - top) / 2.0 - radius
    dx = abs(x - (left + right) / 2.0) - half_x
    dy = abs(y - (top + bottom) / 2.0) - half_y
    return (
        math.hypot(max(dx, 0.0), max(dy, 0.0))
        + min(max(dx, dy), 0.0)
        - radius
    )


def inside_triangle(x: float, y: float, points: tuple[tuple[float, float], ...]) -> bool:
    signs = []
    for index in range(3):
        x0, y0 = points[index]
        x1, y1 = points[(index + 1) % 3]
        signs.append((x1 - x0) * (y - y0) - (y1 - y0) * (x - x0))
    return all(value >= 0 for value in signs) or all(value <= 0 for value in signs)


def sample(x: float, y: float) -> tuple[int, int, int, int]:
    """按 512×512 参考坐标系给一个采样点着色（RGBA）。"""
    if rounded_rect_sdf(x, y, SQUARE) > 0:
        return (0, 0, 0, 0)
    if inside_triangle(x, y, HEAD):
        return (*ARROW, 255)
    if rounded_rect_sdf(x, y, SHAFT) <= 0:
        return (*ARROW, 255)
    outer = rounded_rect_sdf(x, y, TRAY_OUTER)
    if outer <= 0 and rounded_rect_sdf(x, y, TRAY_INNER) > 0:
        return (*GLYPH, 255)
    return (*BACKGROUND, 255)


def render(size: int) -> list[bytes]:
    rows: list[bytes] = []
    step = CANVAS / size / SUPERSAMPLE
    samples = SUPERSAMPLE * SUPERSAMPLE
    for row in range(size):
        pixels = bytearray()
        for column in range(size):
            red = green = blue = alpha = 0
            for sub_y in range(SUPERSAMPLE):
                y = (row * SUPERSAMPLE + sub_y + 0.5) * step
                for sub_x in range(SUPERSAMPLE):
                    x = (column * SUPERSAMPLE + sub_x + 0.5) * step
                    r, g, b, a = sample(x, y)
                    # 预乘 alpha 再平均，避免边缘出现黑边。
                    red += r * a
                    green += g * a
                    blue += b * a
                    alpha += a
            if alpha == 0:
                pixels += bytes((0, 0, 0, 0))
            else:
                pixels += bytes(
                    (
                        round(red / alpha),
                        round(green / alpha),
                        round(blue / alpha),
                        round(alpha / samples),
                    )
                )
        rows.append(bytes(pixels))
    return rows


def write_png(path: Path, size: int) -> bytes:
    raw = b"".join(b"\x00" + row for row in render(size))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)
    return png


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--size", type=int, default=512)
    parser.add_argument("--print-sha256", action="store_true")
    arguments = parser.parse_args()
    if not 16 <= arguments.size <= 2048:
        raise SystemExit("--size 需要在 16 到 2048 之间")
    path = Path(arguments.output)
    png = write_png(path, arguments.size)
    print(f"写入 {path}（{arguments.size}×{arguments.size}，{len(png)} 字节）")
    if arguments.print_sha256:
        print(f"sha256={hashlib.sha256(png).hexdigest()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
