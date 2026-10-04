# -*- coding: utf-8 -*-
"""对当前 JJ 象棋窗口做一次完整诊断：抓帧 → 识别 → FEN → 棋规。

用法：
    python tools/diagnose_live.py                # 自动找 JJ象棋窗口
    python tools/diagnose_live.py --hwnd 0x300660
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from app_backend import (ScreenSource, create_vision, enable_dpi_awareness,
                         validate_fen)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hwnd", default=None, help="窗口句柄，如 0x300660")
    ap.add_argument("--keyword", default="象棋", help="按标题关键字自动查找")
    args = ap.parse_args()

    print(f"DPI: {enable_dpi_awareness()}")

    src = ScreenSource()
    if args.hwnd:
        hwnd = int(args.hwnd, 0)
        title = "?"
        for h, t, c, r in ScreenSource.list_windows(min_size=120):
            if h == hwnd:
                title = t
                break
        src.attach(hwnd, title)
    else:
        cands = [w for w in ScreenSource.list_windows(min_size=120)
                 if args.keyword in w[1]]
        if not cands:
            print(f"[失败] 未找到标题含 {args.keyword!r} 的窗口")
            return 1
        hwnd, title, cls, rect = cands[0]
        print(f"目标窗口: 0x{hwnd:08X} {title!r} [{cls}]")
        src.attach(hwnd, title)

    print()
    print("[1] 抓帧")
    img = src.grab()
    if img is None:
        print("    [失败] 抓帧返回 None（窗口可能已关闭或权限不足）")
        return 1
    a = img.astype(np.float32)
    print(f"    尺寸 {img.shape[1]}x{img.shape[0]}")
    print(f"    均值={a.mean():.1f}  标准差={a.std():.1f}  "
          f"非黑占比={(a.max(axis=2) > 12).mean():.3f}")
    print(f"    截图方式: " + ("后台 PrintWindow" if src.stats['background'] else "前台抓屏"))
    out = HERE / "shots"
    out.mkdir(exist_ok=True)
    cv2.imencode(".png", img)[1].tofile(str(out / "live_raw.png"))
    print(f"    已存 {out / 'live_raw.png'}")

    print()
    print("[2] 识别")
    vision = create_vision(cuda=True)
    t0 = time.perf_counter()
    res = vision.infer(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    dt = (time.perf_counter() - t0) * 1000
    kp = np.round(res["keypoints"], 1)
    sc = np.round(res["keypoint_scores"], 3)
    print(f"    耗时 {dt:.0f}ms  (pose={res['t_pose_ms']:.1f} cls={res['t_cls_ms']:.1f})")
    print(f"    棋盘四角: A0={kp[0].tolist()} A8={kp[1].tolist()} "
          f"J0={kp[2].tolist()} J8={kp[3].tolist()}")
    print(f"    角点分数: {sc.tolist()}  (无区分度，仅作参考)")
    print(f"    " + "-" * 52)
    for i, row in enumerate(res["rows"]):
        print(f"    {i}: {row}   minConf={res['conf'][i].min():.2f}")
    print(f"    " + "-" * 52)

    fen = vision.to_fen(res["rows"], "w")
    print(f"    FEN: {fen}")

    print()
    print("[3] 棋规校验")
    ok, err, n = validate_fen(fen)
    print(f"    合法={ok}  合法着法={n}  {err[:80]}")

    print()
    print("[4] 棋子计数")
    flat = "".join(res["rows"])
    red = sum(1 for c in flat if c.isupper())
    black = sum(1 for c in flat if c.islower())
    unknown = flat.count("x")
    empty = flat.count(".")
    print(f"    红 {red} / 黑 {black} / 空 {empty} / 不确定 {unknown}")
    if red + black != 32:
        print(f"    [注意] 棋子总数 {red + black} != 32，可能有误识别或漏识别")

    cv2.imencode(".png", cv2.cvtColor(res["warped"], cv2.COLOR_RGB2BGR))[1].tofile(
        str(out / "live_warped.png"))
    print(f"    拉正棋盘已存 {out / 'live_warped.png'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
