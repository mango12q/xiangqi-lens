# -*- coding: utf-8 -*-
"""连续多帧稳定性压力测试：抓帧→识别循环，检查成功率、耗时、内存增长。

用于确认长时间跟局不会泄漏或卡顿。
"""
from __future__ import annotations

import argparse
import gc
import sys
import time
from pathlib import Path

import cv2

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from app_backend import (ScreenSource, create_vision, enable_dpi_awareness,
                         validate_fen)


def rss_mb() -> float:
    try:
        import psutil
        return psutil.Process().memory_info().rss / 1024 / 1024
    except Exception:
        return 0.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hwnd", default=None)
    ap.add_argument("--frames", type=int, default=30)
    ap.add_argument("--interval", type=float, default=0.3)
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
    title = next((t for h, t, c, r in ScreenSource.list_windows(120) if h == hwnd), "?")
    src.attach(hwnd, title)
    print(f"目标: 0x{hwnd:08X} {title!r}")
    print()

    vision = create_vision(cuda=True)
    print(f"起始内存: {rss_mb():.1f} MB")
    print(f"{'帧':>4} {'抓帧':>6} {'识别':>7} {'总':>7} {'置信':>6} {'棋子':>5} {'合法':>5}  棋盘指纹")
    print("-" * 78)

    ok = fail = 0
    times: list[float] = []
    keys: dict[str, int] = {}
    mem_start = rss_mb()

    for i in range(1, args.frames + 1):
        t0 = time.perf_counter()
        img = src.grab()
        if img is None:
            print(f"{i:>4}  抓帧失败")
            fail += 1
            time.sleep(args.interval)
            continue
        t_grab = (time.perf_counter() - t0) * 1000

        t1 = time.perf_counter()
        try:
            res = vision.infer(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
        except Exception as exc:
            print(f"{i:>4}  识别异常: {exc}")
            fail += 1
            time.sleep(args.interval)
            continue
        t_rec = (time.perf_counter() - t1) * 1000
        t_all = (time.perf_counter() - t0) * 1000
        times.append(t_all)

        flat = "".join(res["rows"])
        n_pieces = sum(1 for c in flat if c not in ".x")
        fen = vision.to_fen(res["rows"], "w")
        legal_ok, _, n_moves = validate_fen(fen)
        conf = float(res["conf"].min())
        fp = flat[:27] + "..." + flat[-9:]
        keys[flat] = keys.get(flat, 0) + 1
        ok += 1
        print(f"{i:>4} {t_grab:>6.0f} {t_rec:>7.0f} {t_all:>7.0f} {conf:>6.2f} "
              f"{n_pieces:>5} {'✓' if legal_ok else '×':>5}  {fp}")
        time.sleep(args.interval)

    gc.collect()
    mem_end = rss_mb()

    print()
    print("=" * 78)
    print(f"成功 {ok} / 失败 {fail}  ({ok / max(ok + fail, 1):.0%})")
    if times:
        times.sort()
        print(f"单帧总耗时: 中位 {times[len(times) // 2]:.0f}ms  "
              f"最小 {times[0]:.0f}ms  最大 {times[-1]:.0f}ms  "
              f"→ 约 {1000 / times[len(times) // 2]:.1f} FPS")
    print(f"内存: {mem_start:.1f} → {mem_end:.1f} MB  (增长 {mem_end - mem_start:+.1f} MB)")
    print(f"不同局面数: {len(keys)}")
    for k, v in sorted(keys.items(), key=lambda kv: -kv[1])[:3]:
        print(f"  {v:>3} 次  {k[:27]}...{k[-9:]}")
    print("=" * 78)
    if fail == 0 and (mem_end - mem_start) < 80:
        print("[通过] 连续识别稳定，无内存泄漏迹象")
        return 0
    print("[注意] 存在失败帧或内存增长偏大")
    return 1


if __name__ == "__main__":
    sys.exit(main())
