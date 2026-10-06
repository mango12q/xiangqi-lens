# -*- coding: utf-8 -*-
"""对当前棋局窗口跑一次完整流程（不含 GUI）：抓帧→识别→棋规→轮次→引擎。

用于在没有界面干扰的情况下验证后端链路。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from _common import bootstrap, have_engine, have_vision, resolve_hwnd, skip  # noqa: E402

bootstrap()

from app_backend import (ScreenSource, TurnManager, create_engine, create_vision,  # noqa: E402
                         enable_dpi_awareness, fen_with_side, format_score,
                         move_to_chinese, parse_alternatives, validate_fen)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hwnd", default=None)
    ap.add_argument("--keyword", default="象棋")
    ap.add_argument("--side", default="w", choices=["w", "b"])
    ap.add_argument("--movetime", type=int, default=1500)
    args = ap.parse_args()

    print("=" * 70)
    print("实时链路验证（抓帧 → 识别 → 棋规 → 轮次 → 引擎）")
    print("=" * 70)
    print(f"DPI: {enable_dpi_awareness()}")
    if not have_vision():
        return skip("识别模型不在本机（xq_research/hf_model/... 两个 onnx 缺失）",
                    "按 README「依赖资源：模型与引擎」下载后重跑")
    if not have_engine():
        return skip("Pikafish 引擎不在本机（xq_research/pikafish/ 缺失）",
                    "运行 python tools/setup_engine.py 后重跑")

    # ---- 目标窗口 ----
    src = ScreenSource()
    if args.hwnd:
        hwnd = int(args.hwnd, 0)
        title = next((t for h, t, c, r in ScreenSource.list_windows(120) if h == hwnd), "?")
    else:
        hwnd = resolve_hwnd(None, "JJ象棋") or resolve_hwnd(None, args.keyword)
        if not hwnd:
            return skip(f"未找到含 {args.keyword!r} 的窗口（需要真实对局窗口才能验证）",
                        "先打开对局界面，或用 --hwnd 指定句柄后重跑")
        title = next((t for h, t, c, r in ScreenSource.list_windows(120)
                      if h == hwnd), "?")
    src.attach(hwnd, title)
    print(f"目标窗口: 0x{hwnd:08X}  {title!r}")

    # ---- 抓帧 ----
    print()
    print("[1] 抓帧")
    img = src.grab()
    if img is None:
        return skip("抓不到画面（窗口可能已最小化或被遮挡）",
                    "让对局窗口保持可见后重跑")
    print(f"    {img.shape[1]}x{img.shape[0]}  方式="
          f"{'后台PrintWindow' if src.stats['background'] else '前台抓屏'}")

    # ---- 识别 ----
    print()
    print("[2] 识别")
    vision = create_vision(cuda=True)
    t0 = time.perf_counter()
    res = vision.infer(__import__("cv2").cvtColor(img, __import__("cv2").COLOR_BGR2RGB))
    dt = (time.perf_counter() - t0) * 1000
    print(f"    耗时 {dt:.0f}ms")
    for i, row in enumerate(res["rows"]):
        print(f"    {i}: {row}")
    fen_base = vision.to_fen(res["rows"], "w")
    print(f"    FEN: {fen_base}")

    # ---- 棋规 ----
    print()
    print("[3] 棋规校验")
    tm = TurnManager(side_to_move=args.side)
    fen = fen_with_side(fen_base, tm.side_to_move)
    ok, err, n = validate_fen(fen)
    print(f"    轮次={tm.side_to_move}  合法={ok}  合法着法={n}  {err[:60]}")
    if not ok:
        print("    [失败] 局面非法")
        return 2

    # ---- 引擎 ----
    print()
    print("[4] 引擎分析")
    eng = create_engine(threads=8, hash_mb=256)
    t0 = time.perf_counter()
    r = eng.analyse(fen, movetime_ms=args.movetime)
    best = r.get("bestmove") or ""
    print(f"    bestmove = {best}  ({move_to_chinese(fen, best)})")
    print(f"    score    = {format_score(r.get('score_cp'))}  depth = {r.get('depth')}")
    print(f"    耗时     = {time.perf_counter() - t0:.2f}s")

    # ---- 轮次自纠 ----
    print()
    print("[5] 轮次自纠检查")
    if tm.correct_by_engine_move(fen, best):
        print(f"    轮次被校正为 {tm.side_to_move}")
        fen = fen_with_side(fen_base, tm.side_to_move)
        r = eng.analyse(fen, movetime_ms=args.movetime)
        best = r.get("bestmove") or ""
        print(f"    重算 bestmove = {best} ({move_to_chinese(fen, best)})")
    else:
        print(f"    轮次正确，无需校正（当前 {tm.side_to_move} 方走）")

    # ---- 候选着法 ----
    print()
    print("[6] 候选着法")
    for a in parse_alternatives(r.get("info", []), fen)[:5]:
        print(f"    {a['iccs']}  {a['chinese']:8}  分数 {a['score']:>8}  深度 {a['depth']}")

    eng.quit()
    print()
    print("=" * 70)
    print("[通过] 全链路正常")
    return 0


if __name__ == "__main__":
    sys.exit(main())
