# -*- coding: utf-8 -*-
"""离线验证「自动走棋点击坐标」：识别 + 分析 → 算屏幕坐标，**不点击**。

做三件事：
1. 用 ``ScreenSource`` 抓帧（同时拿到与帧对齐的 ``rect``）；
2. 识别 → 引擎取 bestmove → ``move_screen_points`` 算屏幕物理坐标；
3. 把屏幕坐标减回帧坐标，在**原始截图**上画十字 + 连线，输出 PNG 供人眼核对。

另带 ``--selfcheck``：不需要窗口，纯函数自洽校验
（``move_arrow.src + rect 原点 == move_screen_points``，且减回后落在窗口矩形内）。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from _common import bootstrap, have_engine, have_vision, resolve_hwnd, skip  # noqa: E402

bootstrap()

from app_backend import (ScreenSource, create_engine, create_vision,  # noqa: E402
                         enable_dpi_awareness, move_arrow, move_screen_points,
                         move_to_chinese, validate_fen)


def draw_cross(img, p, color, size=18, width=3):
    """在图像上画一个十字准星（p 为图像坐标）。"""
    x, y = int(round(p[0])), int(round(p[1]))
    cv2.line(img, (x - size, y), (x + size, y), (0, 0, 0), width + 4, cv2.LINE_AA)
    cv2.line(img, (x, y - size), (x, y + size), (0, 0, 0), width + 4, cv2.LINE_AA)
    cv2.line(img, (x - size, y), (x + size, y), color, width, cv2.LINE_AA)
    cv2.line(img, (x, y - size), (x, y + size), color, width, cv2.LINE_AA)
    cv2.circle(img, (x, y), 3, color, -1, cv2.LINE_AA)
    return img


def draw_line(img, p1, p2, color, width=3):
    a = (int(round(p1[0])), int(round(p1[1])))
    b = (int(round(p2[0])), int(round(p2[1])))
    cv2.line(img, a, b, (0, 0, 0), width + 4, cv2.LINE_AA)
    cv2.line(img, a, b, color, width, cv2.LINE_AA)
    return img


def selfcheck() -> int:
    """纯函数自洽：屏幕点 = src + rect 原点；减回后仍在窗口内。"""
    # 一个真实的透视矩阵形状（单位阵也能验证换算恒等式）
    warp_mat = np.array([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]])
    rect = (100, 50, 700, 650)          # l, t, r, b
    move = "h2e2"
    arrow = move_arrow(warp_mat, move)
    pts = move_screen_points(rect, warp_mat, move)
    if not arrow or not pts:
        print("[失败] move_arrow / move_screen_points 返回 None")
        return 1
    l, t, r, b = rect
    ok = True
    for key, skey in (("from", "src_from"), ("to", "src_to")):
        exp = (l + arrow[skey][0], t + arrow[skey][1])
        got = pts[key]
        d = float(np.hypot(got[0] - exp[0], got[1] - exp[1]))
        inb = (l <= got[0] <= r) and (t <= got[1] <= b)
        print(f"  {key}: 屏幕={tuple(round(v,1) for v in got)}  "
              f"期望={tuple(round(v,1) for v in exp)}  误差={d:.3f}px  在窗口内={inb}")
        ok = ok and d < 0.01 and inb
    # 越界保护：把 rect 挪到完全不含棋盘的位置，应当返回 None
    far = move_screen_points((10000, 10000, 10100, 10100), warp_mat, move)
    print(f"  越界保护: rect 远离棋盘 → {far}（应为 None）")
    ok = ok and far is None
    print("[通过] 坐标换算自洽" if ok else "[失败] 自洽校验未通过")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hwnd", default=None, help="目标窗口句柄（默认自动找 JJ象棋）")
    ap.add_argument("--movetime", type=int, default=1500)
    ap.add_argument("--out", default="analysis/click_preview.png")
    ap.add_argument("--selfcheck", action="store_true", help="只跑纯函数自洽校验")
    args = ap.parse_args()

    if args.selfcheck:
        print("=== 坐标换算自洽校验 ===")
        return selfcheck()

    print(f"DPI: {enable_dpi_awareness()}")
    if not have_vision():
        return skip("识别模型不在本机（xq_research/hf_model/... 两个 onnx 缺失）",
                    "按 README「依赖资源：模型与引擎」下载后重跑")
    if not have_engine():
        return skip("Pikafish 引擎不在本机（xq_research/pikafish/ 缺失）",
                    "运行 python tools/setup_engine.py 后重跑")
    src = ScreenSource()
    if args.hwnd:
        hwnd = int(args.hwnd, 0)
    else:
        hwnd = resolve_hwnd(None, "JJ象棋")
        if not hwnd:
            return skip("未找到 JJ象棋 窗口（需要真实对局窗口才能验证）",
                        "先打开对局界面，或用 --hwnd 指定句柄后重跑")
    src.attach(hwnd, "?")
    img = src.grab()
    if img is None:
        return skip("抓帧失败（窗口可能已最小化或被遮挡）",
                    "让对局窗口保持可见后重跑")
    rect = src.rect
    if rect is None:
        return skip("抓帧未记录窗口矩形（PrintWindow 与屏幕抓取都失败）",
                    "让对局窗口保持可见后重跑")
    print(f"抓帧 {img.shape[1]}x{img.shape[0]}  窗口矩形 rect={rect}")

    vision = create_vision(cuda=True)
    res = vision.infer(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    fen = vision.to_fen(res["rows"], "w")
    ok, err, n = validate_fen(fen)
    print(f"识别 FEN: {fen}")
    print(f"棋规: 合法={ok} 着法数={n} {err[:50]}")
    if not ok:
        return 2

    eng = create_engine(threads=8, hash_mb=256)
    r = eng.analyse(fen, movetime_ms=args.movetime)
    best = r.get("bestmove") or ""
    print(f"引擎: bestmove={best} ({move_to_chinese(fen, best)}) "
          f"score={r.get('score_cp')} depth={r.get('depth')}")
    eng.quit()

    arrow = move_arrow(res["warp_mat"], best)
    pts = move_screen_points(rect, res["warp_mat"], best)
    print()
    print("=== 点击坐标 ===")
    if not arrow or not pts:
        print("[失败] 坐标换算失败（move_arrow / move_screen_points 返回 None）")
        return 3
    l, t, r_, b_ = rect
    print(f"  着法 {best}: 拉正图 from={arrow['from']} to={arrow['to']}")
    print(f"       截图坐标 src_from={[round(v,1) for v in arrow['src_from']]}"
          f" src_to={[round(v,1) for v in arrow['src_to']]}")
    print(f"       屏幕坐标 from={tuple(round(v,1) for v in pts['from'])}"
          f" to={tuple(round(v,1) for v in pts['to'])}")

    # 交叉验证：拉正图上起点附近应当有棋子（空白约 150-190）
    lx, ly = int(arrow["from"][0]), int(arrow["from"][1])
    patch = res["warped"][max(0, ly - 6):ly + 6, max(0, lx - 6):lx + 6]
    print(f"  起点在图上的局部像素均值={patch.reshape(-1,3).mean():.1f} "
          f"(棋盘空白处约 150-190)")

    # ---- 画到原始截图（屏幕坐标减回帧坐标） ----
    raw = img.copy()
    p_from = (pts["from"][0] - l, pts["from"][1] - t)
    p_to = (pts["to"][0] - l, pts["to"][1] - t)
    in_frame = (0 <= p_from[0] < img.shape[1] and 0 <= p_from[1] < img.shape[0]
                and 0 <= p_to[0] < img.shape[1] and 0 <= p_to[1] < img.shape[0])
    print(f"  减回帧坐标: from=({p_from[0]:.1f},{p_from[1]:.1f}) "
          f"to=({p_to[0]:.1f},{p_to[1]:.1f})  在画面内={in_frame}")
    draw_line(raw, p_from, p_to, (60, 230, 110), 3)
    draw_cross(raw, p_from, (60, 230, 110), 18, 3)     # 起点：绿
    draw_cross(raw, p_to, (60, 130, 255), 18, 3)       # 终点：橙
    out = HERE / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imencode(".png", raw)[1].tofile(str(out))
    print(f"\n原图+点击点预览: {out}")

    # ---- 也画到拉正图（与 GUI 左栏一致，便于二次确认） ----
    canvas = cv2.cvtColor(res["warped"], cv2.COLOR_RGB2BGR).copy()
    draw_line(canvas, arrow["from"], arrow["to"], (60, 230, 110), 3)
    draw_cross(canvas, arrow["from"], (60, 230, 110), 14, 3)
    draw_cross(canvas, arrow["to"], (60, 130, 255), 14, 3)
    out2 = HERE / "analysis" / "click_preview_warped.png"
    cv2.imencode(".png", canvas)[1].tofile(str(out2))
    print(f"拉正图+点击点预览: {out2}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
