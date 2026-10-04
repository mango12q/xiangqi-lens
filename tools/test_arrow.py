# -*- coding: utf-8 -*-
"""验证走法箭头坐标：对当前棋局窗口识别+分析，把箭头画在图上输出。

箭头画在**拉正棋盘图**上（与 GUI 左栏一致），便于确认坐标是否正确。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from app_backend import (ScreenSource, create_engine, create_vision,
                         enable_dpi_awareness, move_arrow, move_to_chinese,
                         validate_fen)


def draw_arrow(img, p1, p2, color, width=5, head=26):
    """在图像上画一支带三角头的箭头。"""
    p1 = (int(round(p1[0])), int(round(p1[1])))
    p2 = (int(round(p2[0])), int(round(p2[1])))
    dx, dy = p2[0] - p1[0], p2[1] - p1[1]
    dist = float(np.hypot(dx, dy))
    if dist < 1e-6:
        return img
    ux, uy = dx / dist, dy / dist
    m = head * 0.8
    s = (int(p1[0] + ux * m), int(p1[1] + uy * m))
    e = (int(p2[0] - ux * m), int(p2[1] - uy * m))
    cv2.line(img, s, e, color, width, cv2.LINE_AA)
    tip = (int(p2[0] - ux * head * 0.3), int(p2[1] - uy * head * 0.3))
    px, py = -uy, ux
    wing = head * 0.55
    tri = np.array([
        tip,
        (int(e[0] - px * wing), int(e[1] - py * wing)),
        (int(e[0] + px * wing), int(e[1] + py * wing)),
    ], np.int32)
    cv2.fillPoly(img, [tri], color, cv2.LINE_AA)
    return img


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hwnd", default=None)
    ap.add_argument("--movetime", type=int, default=1500)
    ap.add_argument("--out", default="analysis/arrow_preview.png")
    args = ap.parse_args()

    print(f"DPI: {enable_dpi_awareness()}")
    src = ScreenSource()
    if args.hwnd:
        hwnd = int(args.hwnd, 0)
    else:
        cands = [w for w in ScreenSource.list_windows(120) if "JJ象棋" in w[1]]
        if not cands:
            print("[失败] 找不到 JJ象棋 窗口")
            return 1
        hwnd = cands[0][0]
    src.attach(hwnd, "?")
    img = src.grab()
    if img is None:
        print("[失败] 抓帧失败")
        return 1
    print(f"抓帧 {img.shape[1]}x{img.shape[0]}")

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

    # ---- 计算箭头坐标 ----
    mat = res["warp_mat"]
    arrow = move_arrow(mat, best)
    print()
    print("=== 箭头坐标 ===")
    if not arrow:
        print("[失败] move_arrow 返回 None")
        return 3
    print(f"  着法 {best}: from={arrow['from']} to={arrow['to']}  (拉正图坐标)")
    print(f"          src_from={[round(v,1) for v in arrow['src_from']] if arrow['src_from'] else None}")
    print(f"          src_to  ={[round(v,1) for v in arrow['src_to']] if arrow['src_to'] else None}")

    # 交叉验证：拉正图上该位置附近应当有棋子
    lx, ly = int(arrow["from"][0]), int(arrow["from"][1])
    patch = res["warped"][max(0, ly - 6):ly + 6, max(0, lx - 6):lx + 6]
    print(f"  起点在图上的局部像素均值={patch.reshape(-1,3).mean():.1f} "
          f"(棋盘空白处约 150-190，棋子处明显不同)")

    # ---- 画到拉正图 ----
    canvas = cv2.cvtColor(res["warped"], cv2.COLOR_RGB2BGR).copy()
    draw_arrow(canvas, arrow["from"], arrow["to"], (0, 0, 0), 9, 30)      # 描边
    draw_arrow(canvas, arrow["from"], arrow["to"], (110, 230, 60), 5, 26)  # 主体
    out = HERE / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imencode(".png", canvas)[1].tofile(str(out))
    print(f"\n拉正图+箭头: {out}")

    # ---- 也画到原图上（验证反投影） ----
    if arrow["src_from"] and arrow["src_to"]:
        raw = img.copy()
        draw_arrow(raw, arrow["src_from"], arrow["src_to"], (0, 0, 0), 9, 30)
        draw_arrow(raw, arrow["src_from"], arrow["src_to"], (110, 230, 60), 5, 26)
        out2 = HERE / "analysis" / "arrow_on_raw.png"
        cv2.imencode(".png", raw)[1].tofile(str(out2))
        print(f"原图+箭头:   {out2}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
