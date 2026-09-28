#!/usr/bin/env python3
"""为自建插件生成统一风格的应用图标（对齐小米 NAS「基础应用」官方视觉）。

用法：
  python3 scripts/make_plugin_icons.py
  python3 scripts/make_plugin_icons.py --preview /tmp/plugin_icons_preview.png

输出：
  projects/<project>/<iconSource>   —— 与 scripts/build_apps.py 的 iconSource 一致，
  CI 会把它原样复制成市场图标 apps/icons/<id>.png。

规格（全部来自对官方基础应用图标的实测归纳）：
  * 画布 256x256 RGBA；squircle 圆角方形铺满画布，圆角半径 56px（22%），圆角外全透明；
  * 竖向线性渐变底色（上亮下暗）+ 极淡顶部高光（alpha <= 18）；
  * 图形纯白 #FFFFFF，笔画宽 14–18px，圆角端点，图形外接框约 118–150px；
  * 图形下方柔和投影（偏移 (0,5)、模糊 ~8px、黑色峰值 alpha 42）。

只使用 Pillow：无网络、无字体依赖、确定性输出；全部图形先在 4 倍画布上绘制，
再 LANCZOS 缩小，保证边缘干净。
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[1]

# --- 统一规格 ---------------------------------------------------------------
SIZE = 256          # 输出边长
SS = 4              # 超采样倍数
RADIUS = 56         # 圆角半径（22%）
STROKE_MIN, STROKE_MAX = 14, 18
GLYPH_MIN_SIDE, GLYPH_MAX_SIDE = 118, 158   # 图形外接框目标区间
PAD_MIN = 28        # 自检要求的四周最小留白
PAD_TARGET = 40     # 规格建议的四周留白
CORNER_CLEAR = 0    # 圆角外 alpha 必须为 0

SHADOW_OFFSET = (0, 5)
SHADOW_BLUR = 8.0
SHADOW_PEAK_ALPHA = 42
HILIGHT_PEAK_ALPHA = 16
HILIGHT_HEIGHT = 96

GREEN = (52, 199, 89)     # #34C759
AMBER = (255, 179, 0)     # #FFB300


# ---------------------------------------------------------------------------
# 绘图工具
# ---------------------------------------------------------------------------
def hex_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def lerp_rgb(top: tuple[int, int, int], bottom: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(int(round(lerp(top[i], bottom[i], t))) for i in range(3))  # type: ignore[return-value]


class Pen:
    """在超采样画布上作画；所有坐标都用 1x（0–256）单位给出。"""

    def __init__(self, draw: ImageDraw.ImageDraw, scale: int = SS) -> None:
        self.d = draw
        self.s = scale

    # -- 基本形状 ---------------------------------------------------------
    def _box(self, x0, y0, x1, y1):
        s = self.s
        return [x0 * s, y0 * s, x1 * s, y1 * s]

    def rrect(self, x0, y0, x1, y1, r, fill=255):
        self.d.rounded_rectangle(self._box(x0, y0, x1, y1), radius=r * self.s, fill=fill)

    def circle(self, cx, cy, r, fill=255):
        self.d.ellipse(self._box(cx - r, cy - r, cx + r, cy + r), fill=fill)

    def ring(self, cx, cy, r, w, fill=255):
        """r 是圆环中心线半径，w 是笔画宽度。"""
        rr = r + w / 2.0
        self.d.ellipse(self._box(cx - rr, cy - rr, cx + rr, cy + rr),
                       outline=fill, width=max(1, int(round(w * self.s))))

    def bar(self, p0, p1, w, fill=255):
        """粗线段 + 圆角端点。"""
        s = self.s
        self.d.line([(p0[0] * s, p0[1] * s), (p1[0] * s, p1[1] * s)],
                    fill=fill, width=max(1, int(round(w * s))))
        self.circle(p0[0], p0[1], w / 2.0, fill=fill)
        self.circle(p1[0], p1[1], w / 2.0, fill=fill)

    def polyline(self, pts, w, fill=255):
        s = self.s
        self.d.line([(x * s, y * s) for x, y in pts], fill=fill,
                    width=max(1, int(round(w * s))), joint="curve")
        for point in (pts[0], pts[-1]):
            self.circle(point[0], point[1], w / 2.0, fill=fill)

    def arc(self, cx, cy, r, a0, a1, w, fill=255):
        """角度按 PIL 约定：0° 在右，顺时针增长（y 轴向下）。带圆角端点。"""
        rr = r + w / 2.0
        self.d.arc(self._box(cx - rr, cy - rr, cx + rr, cy + rr), a0, a1,
                   fill=fill, width=max(1, int(round(w * self.s))))
        for angle in (a0, a1):
            rad = math.radians(angle)
            self.circle(cx + r * math.cos(rad), cy + r * math.sin(rad), w / 2.0, fill=fill)

    def round_poly(self, pts, r, fill=255):
        """圆角多边形（箭头之类）：每个顶点用相切圆弧替代尖角。"""
        s = self.s
        n = len(pts)
        out: list[tuple[float, float]] = []
        for i in range(n):
            prev = pts[(i - 1) % n]
            cur = pts[i]
            nxt = pts[(i + 1) % n]
            u1 = _unit(prev[0] - cur[0], prev[1] - cur[1])
            u2 = _unit(nxt[0] - cur[0], nxt[1] - cur[1])
            if u1 is None or u2 is None:
                out.append(cur)
                continue
            dot = max(-1.0, min(1.0, u1[0] * u2[0] + u1[1] * u2[1]))
            theta = math.acos(dot)                      # 顶点的内角
            if theta <= 1e-6 or theta >= math.pi - 1e-6 or r <= 0:
                out.append(cur)
                continue
            t = r / math.tan(theta / 2.0)
            t1 = (cur[0] + u1[0] * t, cur[1] + u1[1] * t)
            t2 = (cur[0] + u2[0] * t, cur[1] + u2[1] * t)
            bis = _unit(u1[0] + u2[0], u1[1] + u2[1]) or u1
            centre = (cur[0] + bis[0] * (r / math.sin(theta / 2.0)),
                      cur[1] + bis[1] * (r / math.sin(theta / 2.0)))
            a1 = math.atan2(t1[1] - centre[1], t1[0] - centre[0])
            a2 = math.atan2(t2[1] - centre[1], t2[0] - centre[0])
            sweep = a2 - a1
            while sweep > math.pi:
                sweep -= 2 * math.pi
            while sweep < -math.pi:
                sweep += 2 * math.pi
            steps = max(3, int(abs(sweep) * r * s / 1.5))
            out.append(t1)
            for k in range(1, steps):
                angle = a1 + sweep * k / steps
                out.append((centre[0] + r * math.cos(angle), centre[1] + r * math.sin(angle)))
            out.append(t2)
        self.d.polygon([(x * s, y * s) for x, y in out], fill=fill)

    def arrow_head(self, tip, direction, length, half_width, r=4.0, fill=255):
        """实心圆角三角箭头：tip 尖端，direction 指向，length/half_width 决定大小。"""
        ux, uy = _unit(*direction)
        base = (tip[0] - ux * length, tip[1] - uy * length)
        px, py = -uy, ux
        p1 = (base[0] + px * half_width, base[1] + py * half_width)
        p2 = (base[0] - px * half_width, base[1] - py * half_width)
        self.round_poly([tip, p1, p2], r, fill=fill)


def _unit(x: float, y: float):
    length = math.hypot(x, y)
    if length < 1e-9:
        return None
    return (x / length, y / length)


def inset_poly(pts, w):
    """把闭合多边形每条边向内平移 w 后的新顶点（用来做等宽描边环）。"""
    out = []
    n = len(pts)
    for i in range(n):
        prev = pts[(i - 1) % n]
        cur = pts[i]
        nxt = pts[(i + 1) % n]
        u1 = _unit(prev[0] - cur[0], prev[1] - cur[1])
        u2 = _unit(nxt[0] - cur[0], nxt[1] - cur[1])
        if u1 is None or u2 is None:
            out.append(cur)
            continue
        dot = max(-1.0, min(1.0, u1[0] * u2[0] + u1[1] * u2[1]))
        theta = math.acos(dot)
        bis = _unit(u1[0] + u2[0], u1[1] + u2[1]) or u1
        d = w / math.sin(theta / 2.0)
        out.append((cur[0] + bis[0] * d, cur[1] + bis[1] * d))
    return out


# ---------------------------------------------------------------------------
# 图形绘制（每个函数只画白色图形 + 可选小彩点）
# ---------------------------------------------------------------------------
def draw_netneighbor(w: Pen, c: Pen) -> None:
    """主机 + 两个节点（网络里可见、共享目录）。"""
    w.bar((78, 76), (100, 138), 16)          # 左节点连线
    w.bar((178, 76), (156, 138), 16)         # 右节点连线
    w.circle(78, 76, 16)                     # 左节点
    w.circle(178, 76, 16)                    # 右节点
    w.rrect(74, 132, 182, 196, 16)           # 主机


def draw_disksleep(w: Pen, c: Pen) -> None:
    """硬盘 + 月牙。"""
    w.rrect(56, 128, 194, 202, 18)           # 盘体
    w.circle(98, 165, 21, fill=0)            # 盘片（挖空露出底色）
    w.circle(98, 165, 8.5)                   # 中心轴
    w.circle(176, 80, 31)                    # 月牙外圆
    w.circle(164, 67, 25, fill=0)            # 月牙缺口（咬出细月牙）


def draw_ssh(w: Pen, c: Pen) -> None:
    """终端窗口 + 在线指示灯。"""
    _outline_rrect(w, 56, 58, 188, 182, 22, 14)
    w.polyline([(86, 96), (108, 120), (86, 144)], 15)   # >
    w.bar((122, 144), (154, 144), 15)                   # 光标
    c.circle(192, 192, 13, fill=GREEN + (255,))         # 开关 / 在线


def _outline_rrect(pen: Pen, x0, y0, x1, y1, r, width, fill=255) -> None:
    pen.d.rounded_rectangle(pen._box(x0, y0, x1, y1), radius=r * pen.s,
                            outline=fill, width=max(1, int(round(width * pen.s))))


def draw_fwclient(w: Pen, c: Pen) -> None:
    """穿透：带缺口的墙 + 穿过缺口的箭头。"""
    w.bar((118, 64), (118, 90), 24)          # 墙上段（含圆角端点后 52–102）
    w.bar((118, 166), (118, 192), 24)        # 墙下段（154–204）
    w.bar((62, 128), (164, 128), 18)         # 箭头杆（从墙左侧穿到右侧）
    w.arrow_head((204, 128), (1, 0), 39, 18, r=5)


def draw_qbittorrent(w: Pen, c: Pen) -> None:
    """几何笔画的 qb 字母。"""
    w.ring(84, 113, 23.5, 15)                # q 的字碗
    w.bar((104, 105), (104, 176), 15)        # q 的右竖尾
    w.bar((140, 78), (140, 176), 15)         # b 的竖笔
    w.ring(170, 139, 23.5, 15)               # b 的字碗


def draw_transmission(w: Pen, c: Pen) -> None:
    """字母 T + 向下箭头（下载）。"""
    w.bar((70, 72), (186, 72), 17)           # T 横
    w.bar((128, 72), (128, 118), 17)         # T 竖
    w.bar((128, 145), (128, 165), 17)        # 箭头杆
    w.arrow_head((128, 199), (0, 1), 30, 21, r=5)


def draw_webdav(w: Pen, c: Pen) -> None:
    """文件夹 + 链环徽标。"""
    w.rrect(54, 66, 124, 96, 10)             # 文件夹提手
    w.rrect(54, 82, 202, 190, 18)            # 文件夹主体
    w.circle(156, 143, 41, fill=0)           # 徽标底（挖空，白链环才看得见）
    w.ring(147, 134, 12, 10)                 # 链环 1
    w.ring(165, 152, 12, 10)                 # 链环 2（沿对角扣成链）


def draw_115sync(w: Pen, c: Pen) -> None:
    """云 + 圆形循环箭头（环形箭头包住云朵，一眼看出「同步」）。"""
    _small_cloud(w)
    w.arc(128, 128, 56, 130, 410, 15)        # 环形箭头，底部留 80° 缺口
    # 箭头尖 = 弧线末端沿顺时针切线再外推一个头长
    w.arrow_head((141.0, 190.2), (-0.766, 0.643), 30, 21, r=6)


def _small_cloud(w: Pen) -> None:
    """居中的小云朵（外接框约 80x52，四周与环留白）。"""
    w.circle(128, 114, 23)
    w.circle(106, 124, 17)
    w.circle(154, 124, 15)
    w.rrect(88, 123, 168, 143, 10)


def draw_aliyundrive(w: Pen, c: Pen) -> None:
    """云 + 上下双向直箭头。"""
    _cloud(w, top=52)
    w.bar((104, 144), (104, 168), 15)        # 下箭头杆
    w.arrow_head((104, 198), (0, 1), 28, 15, r=4)
    w.bar((152, 196), (152, 172), 15)        # 上箭头杆
    w.arrow_head((152, 140), (0, -1), 28, 15, r=4)


def _cloud(w: Pen, top: float) -> None:
    """实心云朵：三个圆 + 圆角底边。top 是最高点 y。"""
    big = 34
    cy_big = top + big
    w.circle(128, cy_big, big)
    w.circle(92, cy_big + 14, 28)
    w.circle(168, cy_big + 16, 25)
    w.rrect(64, cy_big + 14, 193, cy_big + 42, 14)


def draw_dpanel(w: Pen, c: Pen) -> None:
    """容器：吊臂 + 三个方块（上 1 下 2）。"""
    w.bar((70, 62), (186, 62), 14)           # 吊臂
    w.rrect(100, 78, 156, 134, 12)           # 上
    w.rrect(66, 146, 122, 202, 12)           # 左下
    w.rrect(134, 146, 190, 202, 12)          # 右下


def draw_devicemanager(w: Pen, c: Pen) -> None:
    """机架：两条横槽 + 两个指示灯。"""
    w.rrect(62, 58, 194, 198, 22)
    w.rrect(78, 84, 178, 108, 12, fill=0)    # 横槽 1
    w.rrect(78, 126, 178, 150, 12, fill=0)   # 横槽 2
    c.circle(95, 96, 9, fill=GREEN + (255,))
    c.circle(95, 138, 9, fill=AMBER + (255,))


def draw_store(w: Pen, c: Pen) -> None:
    """应用市场：下载箭头入托盘（沿用旧图标语义，老用户仍认得出是商店/下载）。"""
    w.rrect(54, 168, 202, 204, 16)           # 托盘外框
    w.rrect(69, 158, 187, 189, 8, fill=0)    # 挖出内腔 → 上开口托盘（壁厚 15）
    w.bar((128, 62), (128, 128), 18)         # 箭头杆
    w.arrow_head((128, 178), (0, 1), 34, 23, r=7)   # 箭头尖探进托盘开口


def draw_emby(w: Pen, c: Pen) -> None:
    """Emby：单个白色圆角播放三角，外接框约 124x129（占画布约 50%）。"""
    w.round_poly([(66, 52), (66, 204), (208, 128)], 16)


def draw_jellyfin(w: Pen, c: Pen) -> None:
    """Jellyfin：外圈圆角三角描边（15px）+ 内部小实心圆角三角。"""
    ring = 15
    outer = [(60, 40), (60, 216), (216, 128)]
    w.round_poly(outer, 22)                                   # 外三角
    w.round_poly(inset_poly(outer, ring), 22 - ring, fill=0)  # 挖空 → 等宽描边环
    w.round_poly([(100, 88), (100, 168), (164, 128)], 10)     # 内部小实心三角


# ---------------------------------------------------------------------------
# 图标清单（project / 图标路径以 scripts/build_apps.py 的 iconSource 为准）
# ---------------------------------------------------------------------------
ICON_SPECS: list[dict] = [
    {
        "id": "netneighbor", "name": "网络邻居",
        "project": "xiaomi-netneighbor-plugin", "rel": "web/assets/netneighbor-icon.png",
        "top": "#6EC6FF", "bottom": "#1565D8", "draw": draw_netneighbor,
        "purpose": "主机 + 两个节点：网络里可见并共享",
        "label_ascii": "netneighbor",
    },
    {
        "id": "disksleep", "name": "硬盘休眠",
        "project": "xiaomi-disk-sleep-plugin", "rel": "web/assets/disk-sleep-icon.png",
        "top": "#9B9BF5", "bottom": "#4B49C8", "draw": draw_disksleep,
        "purpose": "硬盘 + 月牙：硬盘休眠",
        "label_ascii": "disksleep",
    },
    {
        "id": "sshcontrol", "name": "SSH 开关",
        "project": "xiaomi-ssh-control-plugin", "rel": "web/assets/ssh-control-icon.png",
        "top": "#5A6B85", "bottom": "#1F2A3C", "draw": draw_ssh,
        "purpose": "终端窗口 + 绿灯：SSH 开关/在线",
        "label_ascii": "sshcontrol",
    },
    {
        "id": "fwclient", "name": "内网穿透",
        "project": "xiaomi-fwclient-plugin", "rel": "web/assets/fwclient.png",
        "top": "#5BE0D2", "bottom": "#0E8C82", "draw": draw_fwclient,
        "purpose": "箭头穿过带缺口的墙：内网穿透",
        "label_ascii": "fwclient",
    },
    {
        "id": "qbittorrent", "name": "qB 下载",
        "project": "xiaomi-qbittorrent-plugin", "rel": "web/assets/qb.png",
        "top": "#5AC8FA", "bottom": "#0A6BE0", "draw": draw_qbittorrent,
        "purpose": "几何笔画 qb 字母",
        "label_ascii": "qbittorrent",
    },
    {
        "id": "transmission", "name": "Transmission",
        "project": "xiaomi-transmission-plugin", "rel": "web/assets/transmission.png",
        "top": "#FF8A75", "bottom": "#D32F2F", "draw": draw_transmission,
        "purpose": "字母 T + 向下箭头：BT 下载",
        "label_ascii": "transmission",
    },
    {
        "id": "webdav", "name": "WebDAV 文件桥",
        "project": "xiaomi-webdav-plugin", "rel": "web/assets/webdav.png",
        "top": "#7BE0A8", "bottom": "#1E9E55", "draw": draw_webdav,
        "purpose": "文件夹 + 链环：文件共享/挂载",
        "label_ascii": "webdav",
    },
    {
        "id": "115sync", "name": "115 云备份",
        "project": "xiaomi-115-sync-plugin", "rel": "web/assets/115-sync-icon.png",
        "top": "#7FB3FF", "bottom": "#2456C8", "draw": draw_115sync,
        "purpose": "云 + 循环箭头：云端同步",
        "label_ascii": "115sync",
    },
    {
        "id": "aliyundrivesync", "name": "阿里云盘备份",
        "project": "xiaomi-aliyundrive-sync-plugin", "rel": "web/assets/aliyundrive-icon.png",
        "top": "#B69BFF", "bottom": "#6A2FD0", "draw": draw_aliyundrive,
        "purpose": "云 + 上下双向箭头：上传下载同步",
        "label_ascii": "aliyundrive",
    },
    {
        "id": "dpanel", "name": "DPanel 容器",
        "project": "xiaomi-dpanel-plugin", "rel": "web/assets/dpanel.png",
        "top": "#58B6F0", "bottom": "#1A63C8", "draw": draw_dpanel,
        "purpose": "吊臂 + 三个容器方块：Docker 面板",
        "label_ascii": "dpanel",
    },
    {
        "id": "devicemanager", "name": "设备管家",
        "project": "xiaomi-device-manager-prototype", "rel": "assets/xiaomi-device-manager-v2.png",
        "top": "#9FB0C4", "bottom": "#47566B", "draw": draw_devicemanager,
        "purpose": "机架 + 指示灯：设备状态监控",
        "label_ascii": "devicemanager",
    },
    # 下面三个不在 build_apps.py 的 PACKAGE_SPECS 里，路径单独写死。
    {
        "id": "store", "name": "应用市场",
        "project": "xiaomi-community-app-store", "rel": "web/assets/community-store-v4.png",
        "top": "#FFC168", "bottom": "#EE7B00", "draw": draw_store,
        "purpose": "下载箭头入托盘：应用市场/商店",
        "label_ascii": "store",
    },
    {
        "id": "emby", "name": "Emby",
        "project": "xiaomi-emby-plugin", "rel": "web/assets/emby.png",
        "top": "#5BC85B", "bottom": "#2E9B33", "draw": draw_emby,
        "purpose": "单个白色播放三角：媒体串流",
        "label_ascii": "emby",
    },
    {
        "id": "jellyfin", "name": "Jellyfin",
        "project": "xiaomi-jellyfin-plugin", "rel": "web/assets/jellyfin.png",
        "top": "#A56BF0", "bottom": "#5B2BC4", "draw": draw_jellyfin,
        "purpose": "三角套三角（描边环 + 内心）：开源媒体服务",
        "label_ascii": "jellyfin",
    },
]


# ---------------------------------------------------------------------------
# 合成
# ---------------------------------------------------------------------------
def make_gradient(top: str, bottom: str) -> Image.Image:
    """竖向线性渐变（上亮下暗），1x 尺寸。"""
    a = hex_rgb(top)
    b = hex_rgb(bottom)
    strip = Image.new("RGB", (1, SIZE))
    for y in range(SIZE):
        strip.putpixel((0, y), lerp_rgb(a, b, y / (SIZE - 1)))
    return strip.resize((SIZE, SIZE), Image.NEAREST)


def make_hilight() -> Image.Image:
    """极淡顶部高光（alpha <= 18）L 蒙版。"""
    mask = Image.new("L", (1, SIZE), 0)
    for y in range(SIZE):
        if y >= HILIGHT_HEIGHT:
            mask.putpixel((0, y), 0)
        else:
            t = 1.0 - y / HILIGHT_HEIGHT
            mask.putpixel((0, y), int(round(HILIGHT_PEAK_ALPHA * t ** 1.7)))
    return mask.resize((SIZE, SIZE), Image.NEAREST)


def tile_mask() -> Image.Image:
    """1x 的 squircle 蒙版。"""
    big = Image.new("L", (SIZE * SS, SIZE * SS), 0)
    ImageDraw.Draw(big).rounded_rectangle(
        [0, 0, SIZE * SS - 1, SIZE * SS - 1], radius=RADIUS * SS, fill=255)
    return big.resize((SIZE, SIZE), Image.LANCZOS)


def build_icon(spec: dict) -> tuple[Image.Image, dict]:
    white_big = Image.new("L", (SIZE * SS, SIZE * SS), 0)
    color_big = Image.new("RGBA", (SIZE * SS, SIZE * SS), (0, 0, 0, 0))
    spec["draw"](Pen(ImageDraw.Draw(white_big)), Pen(ImageDraw.Draw(color_big)))

    glyph = white_big.resize((SIZE, SIZE), Image.LANCZOS)
    color = color_big.resize((SIZE, SIZE), Image.LANCZOS)
    mask = tile_mask()

    # 底色 + 高光
    base = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    tint = make_gradient(spec["top"], spec["bottom"]).convert("RGBA")
    hilight = Image.new("RGBA", (SIZE, SIZE), (255, 255, 255, 0))
    hilight.putalpha(ImageChops.multiply(make_hilight(), mask))
    tint.alpha_composite(hilight)
    tint.putalpha(mask)
    base.alpha_composite(tint)

    # 投影：偏移 (0,5)、模糊 ~8px、黑色峰值 alpha ~42
    shadow = glyph.filter(ImageFilter.GaussianBlur(SHADOW_BLUR))
    peak = shadow.getextrema()[1] or 1
    scale = SHADOW_PEAK_ALPHA / peak
    shadow = shadow.point(lambda v: min(255, int(round(v * scale))))
    shifted = Image.new("L", (SIZE, SIZE), 0)
    shifted.paste(shadow, SHADOW_OFFSET)
    shade = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    shade.putalpha(ImageChops.multiply(shifted, mask))
    base.alpha_composite(shade)

    # 白色图形
    white = Image.new("RGBA", (SIZE, SIZE), (255, 255, 255, 0))
    white.putalpha(glyph)
    base.alpha_composite(white)

    # 小彩点（唯一允许的第三色）
    color.putalpha(ImageChops.multiply(color.getchannel("A"), mask))
    base.alpha_composite(color)

    info = {"glyph_mask": ImageChops.lighter(glyph, color.getchannel("A"))}
    return base, info


# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------
def glyph_bbox(mask: Image.Image) -> tuple[int, int, int, int] | None:
    return mask.point(lambda v: 255 if v >= 100 else 0).getbbox()


def first_opaque_x(image: Image.Image, row: int = 0, threshold: int = 255) -> int | None:
    alpha = image.getchannel("A")
    for x in range(SIZE):
        if alpha.getpixel((x, row)) >= threshold:
            return x
    return None


def check_icon(spec: dict, image: Image.Image, mask: Image.Image) -> tuple[list[str], dict]:
    """返回 (失败项, 指标)。"""
    fails: list[str] = []
    metrics: dict = {}
    if image.size != (SIZE, SIZE):
        fails.append(f"尺寸 {image.size} != {(SIZE, SIZE)}")
    if image.mode != "RGBA":
        fails.append(f"模式 {image.mode} != RGBA")

    alpha = image.getchannel("A")
    corners = [(0, 0), (SIZE - 1, 0), (0, SIZE - 1), (SIZE - 1, SIZE - 1)]
    bad = [p for p in corners if alpha.getpixel(p) != CORNER_CLEAR]
    if bad:
        fails.append(f"角像素 alpha != 0: {bad}")
    metrics["corners_alpha"] = [alpha.getpixel(p) for p in corners]

    start = first_opaque_x(image, 0, 255)
    metrics["first_opaque_x_row0"] = start
    if start is None or not (50 <= start <= 58):
        fails.append(f"第一行不透明起点 x={start} 不在 50–58")

    soft = first_opaque_x(image, 0, 128)
    metrics["first_visible_x_row0"] = soft

    box = glyph_bbox(mask)
    metrics["glyph_bbox"] = box
    if box is None:
        fails.append("图形为空")
    else:
        x0, y0, x1, y1 = box
        metrics["glyph_size"] = (x1 - x0, y1 - y0)
        metrics["padding"] = (x0, y0, SIZE - x1, SIZE - y1)
        if x0 < PAD_MIN or y0 < PAD_MIN or x1 > SIZE - PAD_MIN or y1 > SIZE - PAD_MIN:
            fails.append(f"图形外接框 {box} 超出 [{PAD_MIN},{SIZE - PAD_MIN}]")
        for side, value in zip(("左", "上", "右", "下"), metrics["padding"]):
            if value < PAD_TARGET:
                metrics.setdefault("padding_warn", []).append(f"{side}={value}")
    return fails, metrics


def check_uniform(metrics: dict[str, dict]) -> tuple[list[str], dict]:
    """相邻图标风格一致性：同圆角/同笔画区间 + 图形尺寸与居中在同一档。"""
    fails: list[str] = []
    widths, heights, cxs, cys = [], [], [], []
    for key, m in metrics.items():
        box = m["glyph_bbox"]
        x0, y0, x1, y1 = box
        widths.append(x1 - x0)
        heights.append(y1 - y0)
        cxs.append((x0 + x1) / 2)
        cys.append((y0 + y1) / 2)
    summary = {
        "glyph_width_range": (min(widths), max(widths)),
        "glyph_height_range": (min(heights), max(heights)),
        "centre_x_range": (min(cxs), max(cxs)),
        "centre_y_range": (min(cys), max(cys)),
        "radius": RADIUS, "canvas": SIZE, "stroke_range": (STROKE_MIN, STROKE_MAX),
    }
    if min(widths) < GLYPH_MIN_SIDE or max(widths) > GLYPH_MAX_SIDE:
        fails.append(f"图形宽度 {min(widths)}–{max(widths)} 超出 {GLYPH_MIN_SIDE}–{GLYPH_MAX_SIDE}")
    if min(heights) < 110 or max(heights) > GLYPH_MAX_SIDE:
        fails.append(f"图形高度 {min(heights)}–{max(heights)} 超出 110–{GLYPH_MAX_SIDE}")
    if max(abs(v - SIZE / 2) for v in cxs) > 10:
        fails.append(f"横向居中偏离过大 {min(cxs)}–{max(cxs)}")
    if max(abs(v - SIZE / 2) for v in cys) > 10:
        fails.append(f"纵向居中偏离过大 {min(cys)}–{max(cys)}")
    if max(widths) / min(widths) > 1.4 or max(heights) / min(heights) > 1.4:
        fails.append("图形尺寸差异过大，视觉重量不一致")
    return fails, summary


# ---------------------------------------------------------------------------
# 预览图
# ---------------------------------------------------------------------------
CJK_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhl.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/System/Library/Fonts/PingFang.ttc",
]


def load_label_font(size: int):
    for candidate in CJK_FONT_CANDIDATES:
        if Path(candidate).is_file():
            try:
                return ImageFont.truetype(candidate, size), Path(candidate).name
            except OSError:
                continue
    try:
        return ImageFont.load_default(size=size), "Pillow default"
    except TypeError:  # 老版本 Pillow
        return ImageFont.load_default(), "Pillow default"


def make_preview(path: Path, icons: list[tuple[dict, Image.Image]]) -> None:
    bg = hex_rgb("#EEF1F6")
    cell = 96
    gap = 24
    caption_h = 18
    row_pitch = cell + 6 + caption_h + gap
    title_h = 36
    cols = 5                       # 4 列图标 + 1 列官方参考
    rows = math.ceil(len(icons) / 4)

    width = gap + cols * cell + (cols - 1) * gap + gap
    height = gap + title_h + rows * row_pitch + gap
    canvas = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(canvas)
    font, font_name = load_label_font(13)
    cjk_ok = font_name != "Pillow default"      # 默认位图字体没有中文字形
    title_font, _ = load_label_font(18)
    cell_font, _ = load_label_font(12)

    draw.text((gap, gap), "小米 NAS 插件图标预览（统一风格，96px）", font=title_font, fill=(51, 58, 69),
              anchor="la")

    for index, (spec, image) in enumerate(icons):
        col = index % 4
        row = index // 4
        x = gap + col * (cell + gap)
        y = gap + title_h + row * row_pitch
        thumb = image.resize((cell, cell), Image.LANCZOS)
        canvas.paste(thumb, (x, y), thumb)
        label = spec["name"] if cjk_ok else spec["label_ascii"]
        draw.text((x + cell / 2, y + cell + 4), label, font=font, fill=(51, 58, 69), anchor="ma")

    # 最后一列：两格官方风格参考（占位，不画真图）
    ref_text = "官方风格参考" if cjk_ok else "official style"
    ref_x = gap + 4 * (cell + gap)
    for row in range(2):
        y = gap + title_h + row * row_pitch
        draw.rounded_rectangle([ref_x, y, ref_x + cell, y + cell], radius=21,
                               fill=hex_rgb("#DDE3EC"), outline=hex_rgb("#C3CCD9"), width=1)
        draw.text((ref_x + cell / 2, y + cell / 2), ref_text, font=cell_font,
                  fill=(122, 134, 152), anchor="mm")
        draw.text((ref_x + cell / 2, y + cell + 4), ref_text, font=font, fill=(122, 134, 152),
                  anchor="ma")

    path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(path, "PNG", optimize=True)
    print(f"preview  {path}  {canvas.size[0]}x{canvas.size[1]}  {path.stat().st_size} B  "
          f"字体={font_name}")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="为自建插件生成统一风格图标")
    parser.add_argument("--preview", help="额外输出一张预览图（浅灰底 + 网格 + 中文名）")
    args = parser.parse_args()

    results: list[tuple[dict, Image.Image]] = []
    metrics: dict[str, dict] = {}
    failures: dict[str, list[str]] = {}
    print(f"canvas {SIZE}x{SIZE}  radius {RADIUS}  supersample {SS}x\n")

    for spec in ICON_SPECS:
        target = ROOT / "projects" / spec["project"] / spec["rel"]
        image, info = build_icon(spec)
        target.parent.mkdir(parents=True, exist_ok=True)
        image.save(target, "PNG", optimize=True)
        fails, m = check_icon(spec, image, info["glyph_mask"])
        failures[spec["id"]] = fails
        metrics[spec["id"]] = m
        results.append((spec, image))
        print(f"[{spec['id']}] {target.relative_to(ROOT)}")
        print(f"    {image.size[0]}x{image.size[1]} {image.mode}  {target.stat().st_size} B  "
              f"glyph={m.get('glyph_size')}  padding={m.get('padding')}")

    uniform_fails, summary = check_uniform(metrics)

    print("\n=== 自检 ===")
    ok = True
    for spec in ICON_SPECS:
        key = spec["id"]
        m = metrics[key]
        fails = list(failures[key])
        note = ""
        if m.get("padding_warn"):
            note = f"  (留白 <40px: {', '.join(m['padding_warn'])})"
        if fails:
            ok = False
            print(f"FAIL {key}: {'; '.join(fails)}{note}")
        else:
            print(f"PASS {key}: 256x256 RGBA, 角 alpha={m['corners_alpha']}, "
                  f"首行不透明 x={m['first_opaque_x_row0']} (可见边缘 x={m['first_visible_x_row0']}), "
                  f"图形 {m['glyph_size'][0]}x{m['glyph_size'][1]}, "
                  f"留白 {m['padding']}{note}")
    if uniform_fails:
        ok = False
        print(f"FAIL 一致性: {'; '.join(uniform_fails)}")
    else:
        print(f"PASS 一致性: 画布 {summary['canvas']}, 圆角 {summary['radius']}, "
              f"笔画 {summary['stroke_range']}, 图形宽 {summary['glyph_width_range']}, "
              f"高 {summary['glyph_height_range']}, 中心x {summary['centre_x_range']}, "
              f"中心y {summary['centre_y_range']}")

    if args.preview:
        make_preview(Path(args.preview), results)

    print("\n全部自检通过" if ok else "\n存在未通过的自检项")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
