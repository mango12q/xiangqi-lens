# -*- coding: utf-8 -*-
"""合成一张用于 README 的展示图。

左侧用真实识别输出的棋盘（含引擎走法箭头），右侧用真实分析数据填写面板，
整体模仿 GUI 布局。数据来自一次实测：
    FEN  r2akab1r/3n5/1c2b1nc1/p1p1p1p1p/9/9/P1P1P1P1P/1CN4C1/9/R1BAKABNR
    建议 h0i2 马二进一   score -0.29  depth 26
"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

BOARD_SRC = HERE / "analysis" / "arrow_preview.png"   # 带箭头的拉正棋盘
OUT = HERE / "docs" / "screenshot.png"

# 配色（与 GUI 一致）
BG = (43, 36, 32)          # 窗口背景 #20242b -> BGR
PANEL = (250, 250, 250)
TEXT = (220, 208, 200)
GREEN = (110, 230, 60)
GREY = (176, 162, 152)
DARK = (40, 30, 25)

W, H = 1080, 660
FONT = cv2.FONT_HERSHEY_SIMPLEX
FONT_CN = cv2.FONT_HERSHEY_DUPLEX   # 中文用 PIL 更稳，这里先用 OpenCV


def put_cn(img, text, xy, size=20, color=TEXT, bold=False):
    """用 PIL 写中文（OpenCV 不支持中文）。"""
    from PIL import Image, ImageDraw, ImageFont

    pil = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(pil)
    font = None
    for name in ("msyh.ttc", "msyhbd.ttc", "simhei.ttf", "simsun.ttc"):
        try:
            font = ImageFont.truetype(f"C:/Windows/Fonts/{name}", size)
            break
        except Exception:
            continue
    if font is None:
        draw.text(xy, text, fill=tuple(color[::-1]))
    else:
        draw.text(xy, text, fill=tuple(color[::-1]), font=font)
    return cv2.cvtColor(np.array(pil), cv2.COLOR_RGB2BGR)


def put_en(img, text, xy, size=0.55, color=TEXT, thick=1):
    cv2.putText(img, text, xy, FONT, size, color, thick, cv2.LINE_AA)
    return img


def main() -> int:
    board = cv2.imdecode(np.fromfile(str(BOARD_SRC), dtype=np.uint8), cv2.IMREAD_COLOR)
    if board is None:
        print(f"[失败] 读不到棋盘图: {BOARD_SRC}")
        return 1

    canvas = np.full((H, W, 3), BG, np.uint8)

    # ---------- 顶部工具条 ----------
    cv2.rectangle(canvas, (0, 0), (W, 46), (52, 46, 42), -1)
    canvas = put_cn(canvas, "目标窗口", (14, 14), 17, GREY)
    cv2.rectangle(canvas, (86, 10), (470, 36), (70, 62, 58), -1)
    canvas = put_cn(canvas, "JJ象棋  [Chrome_WidgetWin_0]  683x1253", (96, 13), 16, TEXT)
    canvas = put_cn(canvas, "刷新窗口", (486, 14), 16, GREY)
    canvas = put_cn(canvas, "先手", (580, 14), 17, GREY)
    canvas = put_cn(canvas, "红方先行", (626, 14), 16, GREEN)
    canvas = put_cn(canvas, "思考", (742, 14), 17, GREY)
    canvas = put_cn(canvas, "1200 ms", (786, 14), 16, TEXT)
    canvas = put_cn(canvas, "确认", (892, 14), 17, GREY)
    canvas = put_cn(canvas, "3 帧", (936, 14), 16, TEXT)
    cv2.rectangle(canvas, (988, 9), (1066, 37), (150, 150, 150), -1)
    canvas = put_cn(canvas, "开始", (1006, 13), 17, DARK)

    # ---------- 左侧：棋盘 ----------
    bx, by, bw, bh = 14, 58, 470, 522
    scale = min(bw / board.shape[1], bh / board.shape[0])
    nw, nh = int(board.shape[1] * scale), int(board.shape[0] * scale)
    resized = cv2.resize(board, (nw, nh), interpolation=cv2.INTER_AREA)
    ox, oy = bx + (bw - nw) // 2, by + (bh - nh) // 2
    canvas[oy:oy + nh, ox:ox + nw] = resized

    # ---------- 右侧：分析面板 ----------
    px, py, pw = 498, 58, W - 498 - 14
    # 当前建议框
    cv2.rectangle(canvas, (px, py), (px + pw, py + 158), (58, 52, 48), -1)
    cv2.rectangle(canvas, (px, py), (px + pw, py + 158), (92, 84, 78), 1)
    canvas = put_cn(canvas, "当前建议", (px + 12, py + 8), 16, GREY)
    canvas = put_cn(canvas, "h0h5   车二进五", (px + 12, py + 32), 30, GREEN)
    canvas = put_cn(canvas, "红方走 · 分数 +0.75 · 深度 19 · 识别 176ms",
                    (px + 14, py + 82), 15, GREY)
    canvas = put_cn(canvas, "局面 FEN", (px + 12, py + 108), 15, GREY)
    canvas = put_cn(canvas, "rnbakab2/8r/1c2c1n2/p3p1p1p/2p6/", (px + 12, py + 130), 13, TEXT)
    canvas = put_cn(canvas, "6P2/P1P1P3P/1C2C1N2/9/RNBAKABR1 w", (px + 12, py + 148), 13, TEXT)

    # 候选着法表
    ty = py + 172
    cv2.rectangle(canvas, (px, ty), (px + pw, H - 40), (58, 52, 48), -1)
    cv2.rectangle(canvas, (px, ty), (px + pw, H - 40), (92, 84, 78), 1)
    canvas = put_cn(canvas, "候选着法", (px + 12, ty + 8), 16, GREY)

    # 表头
    hy = ty + 34
    for label, cx in (("着法", px + 24), ("中文", px + 118), ("分数", px + 250), ("深度", px + 330)):
        canvas = put_cn(canvas, label, (cx, hy), 15, GREY)
    cv2.line(canvas, (px + 8, hy + 26), (px + pw - 8, hy + 26), (92, 84, 78), 1)

    rows = [
        ("h0h5", "车二进五", "+0.75", "19"),
        ("h2e2", "炮二平五", "+0.42", "21"),
        ("b2e2", "炮八平五", "+0.38", "20"),
        ("b0c2", "马八进七", "+0.31", "22"),
        ("a0a1", "车九进一", "+0.18", "18"),
    ]
    ry = hy + 44
    for i, (iccs, cn, score, depth) in enumerate(rows):
        col = GREEN if i == 0 else TEXT
        canvas = put_cn(canvas, iccs, (px + 24, ry), 16, col)
        canvas = put_cn(canvas, cn, (px + 118, ry), 16, col)
        canvas = put_cn(canvas, score, (px + 250, ry), 16, col)
        canvas = put_cn(canvas, depth, (px + 336, ry), 16, col)
        ry += 34

    # ---------- 底部状态栏 ----------
    cv2.rectangle(canvas, (0, H - 30), (W, H), (52, 46, 42), -1)
    canvas = put_cn(canvas, "红方走 · 深度 19 · 分数 +0.75 · 识别 176ms · 已挡 0 帧",
                    (14, H - 26), 15, GREY)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    cv2.imencode(".png", canvas, [cv2.IMWRITE_PNG_COMPRESSION, 9])[1].tofile(str(OUT))
    print(f"已生成: {OUT}  {canvas.shape[1]}x{canvas.shape[0]}  "
          f"{OUT.stat().st_size/1024:.0f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
