# -*- coding: utf-8 -*-
"""端到端实时验证：对当前棋局窗口连续跑多帧，走完 识别→抗错→轮次→引擎 全链路。

用于确认 GUI 外的完整逻辑链路正常（GUI 只是它的展示层）。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import cv2

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from app_backend import (BoardTracker, ScreenSource, check_board_geometry,
                         check_position, create_engine, create_vision,
                         enable_dpi_awareness, fen_with_side, format_score,
                         move_to_chinese, parse_alternatives, validate_fen)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hwnd", default=None)
    ap.add_argument("--frames", type=int, default=8)
    ap.add_argument("--interval", type=float, default=0.5)
    ap.add_argument("--movetime", type=int, default=1200)
    ap.add_argument("--side", default="w", choices=["w", "b"])
    args = ap.parse_args()

    print("=" * 76)
    print("端到端实时验证：抓帧→识别→抗错→轮次→引擎")
    print("=" * 76)
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
    title = next((t for h, t, c, r in ScreenSource.list_windows(120) if h == hwnd), "?")
    src.attach(hwnd, title)
    print(f"目标: 0x{hwnd:08X} {title!r}")

    vision = create_vision(cuda=True)
    tracker = BoardTracker(force_after=6, first_side=args.side)
    engine = create_engine(threads=8, hash_mb=256)
    print()

    published = ""
    analysed = 0
    for i in range(1, args.frames + 1):
        t0 = time.perf_counter()
        img = src.grab()
        if img is None:
            print(f"--- 帧{i}: 抓帧失败")
            continue
        res = vision.infer(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
        key = "".join(res["rows"])
        fen_base = vision.to_fen(res["rows"], "w")
        t_rec = (time.perf_counter() - t0) * 1000

        # pose 几何校验（与 GUI 一致）：没定位到棋盘就直接跳过
        geo_ok, geo_why = check_board_geometry(res["keypoints"], img_shape=img.shape)
        if not geo_ok:
            print(f"--- 帧{i}: 未定位到棋盘 → {geo_why}")
            time.sleep(args.interval)
            continue

        accept, reason, move, side = tracker.update(fen_base)
        side_cn = "红" if side == "w" else "黑"

        if not accept:
            print(f"--- 帧{i}: 挡下({reason})  {t_rec:.0f}ms  minConf={res['conf'].min():.2f}")
            time.sleep(args.interval)
            continue

        if key == published:
            print(f"--- 帧{i}: 局面未变  {t_rec:.0f}ms")
            time.sleep(args.interval)
            continue

        fen = fen_with_side(fen_base, side)
        # 与 GUI 一致的严格校验：非法局面绝不送引擎
        pos_ok, pos_why = check_position(fen)
        if not pos_ok:
            print(f"--- 帧{i}: 局面非法已拦下 → {pos_why}")
            time.sleep(args.interval)
            continue
        ok, err, n_moves = validate_fen(fen)
        if not ok:
            print(f"--- 帧{i}: 棋规未通过 → {err[:50]}")
            time.sleep(args.interval)
            continue

        published = key
        r = engine.analyse(fen, movetime_ms=args.movetime)
        best = r.get("bestmove") or ""
        analysed += 1

        print(f"--- 帧{i}: 采信({reason}) {t_rec:.0f}ms")
        print(f"    FEN  : {fen}")
        print(f"    轮次 : {side_cn}方走   合法着法 {n_moves}")
        print(f"    建议 : {best}  {move_to_chinese(fen, best)}   "
              f"分数 {format_score(r.get('score_cp'))}  深度 {r.get('depth')}")
        alts = parse_alternatives(r.get("info", []), fen, limit=3)
        for a in alts:
            print(f"           {a['iccs']} {a['chinese']:8} {a['score']:>8} 深度{a['depth']}")
        print(f"    护栏 : {tracker.stats}")
        time.sleep(args.interval)

    engine.quit()
    print()
    print("=" * 76)
    print(f"完成：{args.frames} 帧，分析 {analysed} 次，{tracker.stats}")
    if tracker.moves:
        print(f"检测到的着法序列: {tracker.moves}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
