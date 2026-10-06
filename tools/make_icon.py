# -*- coding: utf-8 -*-
"""生成 assets/icon.ico —— 仓库自有的应用图标。

为什么要有这个脚本（而不是直接塞一个 .ico 进仓库）：
* 原来 `tools/build_exe.py` 的图标指向**另一个仓库**
  （``../refs/chessboard/server/icons/icon.ico``）—— 换台机器就静默丢图标，
  而且那个文件与本项目无依赖关系、许可也不明；
* 图标是构建产物，把生成方式一起入库才能重现与微调，也避免版权来源不清。

本图标为**原创绘制**（几何图形 + 系统字体里的一个汉字），不使用任何第三方素材。

用法::

    python tools/make_icon.py            # 写入 assets/icon.ico
    python tools/make_icon.py --preview  # 另外导出一张 PNG 便于肉眼检查
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent.parent
OUT = HERE / "assets" / "icon.ico"

# 配色与应用内左栏棋盘底色保持一致（app_vision.BoardView 的 #1b1f26）
BG = (20, 24, 31, 255)          # 底色
BG_EDGE = (48, 57, 70, 255)     # 圆角描边
GRID = (58, 69, 83, 255)        # 棋盘格线
PIECE = (192, 57, 43, 255)      # 棋子（红）
PIECE_RIM = (240, 230, 210, 255)  # 棋子外圈
GLYPH = (245, 239, 224, 255)    # 汉字

SIZES = [16, 24, 32, 48, 64, 128, 256]
SUPERSAMPLE = 4                 # 先在 4 倍尺寸上画，再缩下来做抗锯齿

# 汉字：象棋的「象」。用系统自带的雅黑粗体，取不到就退回黑体。
_FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
)


def _load_font(px: int) -> ImageFont.FreeTypeFont:
    for p in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(p, px)
        except Exception:
            continue
    # 没有任何中文字体时用默认位图字体（字形会退化，但不会崩）
    return ImageFont.load_default()


def build(size: int = 256) -> Image.Image:
    """画一张 size×size 的图标。"""
    s = size * SUPERSAMPLE
    im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)

    # ---- 圆角底板 ----
    pad = s * 0.045
    radius = s * 0.20
    d.rounded_rectangle([pad, pad, s - pad, s - pad], radius=radius,
                        fill=BG, outline=BG_EDGE, width=max(1, int(s * 0.012)))

    # ---- 棋盘格线（做成"能看到棋盘"的暗示，别太抢）----
    m = s * 0.175                       # 棋盘区域外边距
    x0, y0, x1, y1 = m, m, s - m, s - m
    cols, rows = 4, 5                   # 5 竖线 6 横线 → 读起来像象棋盘
    lw = max(1, int(s * 0.009))
    for i in range(cols + 1):
        x = x0 + (x1 - x0) * i / cols
        d.line([(x, y0), (x, y1)], fill=GRID, width=lw)
    for j in range(rows + 1):
        y = y0 + (y1 - y0) * j / rows
        d.line([(x0, y), (x1, y)], fill=GRID, width=lw)

    # ---- 棋子：外圈 + 实体 + 内圈 ----
    cx = cy = s / 2
    r = s * 0.295
    d.ellipse([cx - r, cy - r, cx + r, cy + r],
              fill=PIECE, outline=PIECE_RIM,
              width=max(1, int(s * 0.028)))
    r2 = r * 0.80
    d.ellipse([cx - r2, cy - r2, cx + r2, cy + r2],
              outline=PIECE_RIM, width=max(1, int(s * 0.014)))

    # ---- 汉字 ----
    font = _load_font(int(s * 0.40))
    text = "象"
    try:
        box = d.textbbox((0, 0), text, font=font)
        tw, th = box[2] - box[0], box[3] - box[1]
        d.text((cx - tw / 2 - box[0], cy - th / 2 - box[1]), text,
               font=font, fill=GLYPH)
    except Exception:
        d.text((cx, cy), text, font=font, fill=GLYPH, anchor="mm")

    return im.resize((size, size), Image.LANCZOS)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--preview", action="store_true",
                    help="额外导出 assets/icon-preview.png 便于肉眼检查")
    args = ap.parse_args()

    big = build(256)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    big.save(OUT, format="ICO",
             sizes=[(n, n) for n in SIZES if n <= 256])
    print(f"已写入 {OUT}  ({OUT.stat().st_size / 1024:.1f} KB)")

    # 校验：真的能按各尺寸读回来
    with Image.open(OUT) as im:
        got = sorted(im.info.get("sizes", []))
    print(f"  内含尺寸: {got}")
    missing = [n for n in SIZES if n <= 256 and (n, n) not in got]
    if missing:
        print(f"  [失败] 缺少尺寸: {missing}")
        return 1

    if args.preview:
        p = OUT.parent / "icon-preview.png"
        # 一行排开各尺寸，方便看小尺寸下是否糊成一团
        strip = Image.new("RGBA", (sum(SIZES) + 8 * (len(SIZES) + 1), 272),
                          (255, 255, 255, 255))
        x = 8
        for n in SIZES:
            strip.paste(build(n), (x, 264 - n), build(n))
            x += n + 8
        strip.save(p)
        print(f"  预览已写入 {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
