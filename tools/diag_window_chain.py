# -*- coding: utf-8 -*-
"""诊断：指定窗口的「抓帧 -> 识别」全链路。

用途
------------------------------------------------------------------
排查"截屏歪了 / 识别失败 / 自动走棋定位不对"这类问题。它会：

1. 列出所有含指定关键字的窗口（默认"象棋"），带进程名与类名；
2. 用真实的 ``ScreenSource`` 抓一帧，报告走的是后台(PrintWindow)还是
   前台(屏幕区域)通道，并落盘原图；
3. 跑一次 ``XiangqiVision.infer``，打印 4 个角点坐标、棋盘宽高比、
   90 格识别结果与 FEN，并落盘透视拉正图。

用法::

    python tools/diag_window_chain.py                     # 自动找含"象棋"的窗口
    python tools/diag_window_chain.py --hwnd 0x0007011E   # 指定窗口
    python tools/diag_window_chain.py --cpu               # 强制 CPU 推理
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import cv2
import numpy as np

from app_backend import (
    ScreenSource,
    create_vision,
    enable_dpi_awareness,
    list_windows_any,
    window_state,
)

BONE = ["A0", "A8", "J0", "J8"]


def board_aspect(kps) -> float:
    """用 4 角点估算棋盘宽高比（上边 / 左边）。标准棋盘约 8/9 = 0.889。"""
    a0, a8, j0, _j8 = [np.asarray(p, dtype=np.float64) for p in kps]
    top = float(np.linalg.norm(a8 - a0))
    left = float(np.linalg.norm(j0 - a0))
    return top / left if left > 1e-6 else 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hwnd", default=None, help="十六进制窗口句柄，如 0x0007011E")
    ap.add_argument("--keyword", default="象棋", help="自动查找的标题关键字")
    ap.add_argument("--out", default="probe_out")
    ap.add_argument("--cpu", action="store_true")
    ap.add_argument("--no-vision", action="store_true")
    args = ap.parse_args()

    print("DPI 感知:", enable_dpi_awareness())
    import win32gui

    wins = list_windows_any(min_size=200)
    hits = [w for w in wins if args.keyword in w["title"]]
    print(f"=== 含「{args.keyword}」的窗口 {len(hits)} 个 ===")
    for w in hits:
        r = w["rect"]
        print(f"  0x{w['hwnd']:08X}  {w['title']!r}  cls={w['class']}  "
              f"{r[2] - r[0]}x{r[3] - r[1]}  minimized={w['minimized']}")

    if args.hwnd:
        hwnd = int(args.hwnd, 16)
        title = win32gui.GetWindowText(hwnd)
    elif hits:
        hwnd, title = hits[0]["hwnd"], hits[0]["title"]
    else:
        print("未找到目标窗口")
        return 1

    print(f"\n>>> 目标: 0x{hwnd:08X} {title!r}")
    print("window_state:", window_state(hwnd))

    src = ScreenSource()
    src.attach(hwnd, title)
    img = src.grab()
    print(f"抓帧 stats={src.stats}  rect={src.rect}")
    if img is None:
        print("抓帧失败（两路都没拿到图）")
        return 1
    print(f"图像 shape={img.shape}")

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    raw = outdir / "diag_grab.png"
    cv2.imwrite(str(raw), img)
    print(f"原图已存: {raw}")

    if args.no_vision:
        return 0

    vis = create_vision(cuda=not args.cpu)
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    res = vis.infer(rgb)

    kps = res["keypoints"]
    print("\n--- pose 4 角点 ---")
    for name, p, s in zip(BONE, kps, res["keypoint_scores"]):
        print(f"  {name}: ({p[0]:8.1f}, {p[1]:8.1f})   score={s:.3f}")
    ar = board_aspect(kps)
    print(f"棋盘宽高比 = {ar:.3f}   (合理区间 0.75~1.3，标准 0.889)")

    warped = res["warped"]
    wp = outdir / "diag_warped.png"
    cv2.imwrite(str(wp), cv2.cvtColor(warped, cv2.COLOR_RGB2BGR))
    print(f"拉正图已存: {wp}")

    print("\n--- 90 格识别 ---")
    for i, row in enumerate(res["rows"]):
        print(f"  {i:2d}  {row}")
    print("\nFEN:", vis.to_fen(res["rows"]))
    print(f"耗时: pose {res['t_pose_ms']:.0f}ms  cls {res['t_cls_ms']:.0f}ms")

    # --- 自动走棋坐标换算验证（只算不点）---
    if src.rect is not None:
        from app_backend import move_screen_points
        from app_input import is_point_on_window

        print("\n--- 走棋坐标换算（ICCS -> 屏幕物理坐标）---")
        for mv in ("a0a1", "e0e1", "i9i8", "b0c2", "h9g7", "c3c4"):
            pts = move_screen_points(src.rect, res["warp_mat"], mv)
            if not pts:
                print(f"  {mv}: 换算失败 / 越出窗口")
                continue
            on_f = is_point_on_window(hwnd, *pts["from"])
            on_t = is_point_on_window(hwnd, *pts["to"])
            print(f"  {mv}: ({pts['from'][0]:.0f},{pts['from'][1]:.0f}) -> "
                  f"({pts['to'][0]:.0f},{pts['to'][1]:.0f})   "
                  f"落点在目标窗口上: from={on_f} to={on_t}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
