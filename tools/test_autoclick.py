# -*- coding: utf-8 -*-
"""真实点击验证：对当前棋局窗口算坐标并（可选）真的点击一次。

安全默认：不加 ``--yes`` 时只打印将要做的事，**不动鼠标**。
* ``--move-only``  只把光标移到起点，不点击（校准用，最安全）
* ``--dry-run``    连光标都不动，只打印坐标
* ``--yes``        真正执行点击（会抢鼠标，请确保窗口已在前台）

用法示例::

    # 1) 只打印，什么都不做
    python tools/test_autoclick.py --dry-run

    # 2) 只把光标移到「起点」，肉眼确认是否落在正确的棋子上
    python tools/test_autoclick.py --move-only

    # 3) 真点一次引擎推荐的最佳着法
    python tools/test_autoclick.py --yes

    # 4) 指定着法 / 切换拖拽模式 / 分步移动
    python tools/test_autoclick.py --move h2e2 --mode drag --steps 6 --yes
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from app_backend import (ScreenSource, create_engine, create_vision,
                         enable_dpi_awareness, move_screen_points,
                         move_to_chinese, validate_fen, window_state)
from app_input import (MouseClicker, ensure_foreground, foreground_hwnd,
                       get_cursor_pos, is_point_on_window, window_at_point)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hwnd", default=None, help="目标窗口句柄（默认自动找 JJ象棋）")
    ap.add_argument("--move", default=None, help="指定 ICCS 着法，如 h2e2（默认用引擎最佳）")
    ap.add_argument("--movetime", type=int, default=1200)
    ap.add_argument("--mode", default="two_click", choices=["two_click", "drag"])
    ap.add_argument("--backend", default="mouse_event", choices=["mouse_event", "sendinput"])
    ap.add_argument("--steps", type=int, default=1, help="移动插值步数（1=直跳）")
    ap.add_argument("--move-only", action="store_true", help="只移动光标到起点，不点击")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不动鼠标")
    ap.add_argument("--yes", action="store_true", help="跳过确认，直接执行")
    args = ap.parse_args()

    print(f"DPI: {enable_dpi_awareness()}")
    src = ScreenSource()
    if args.hwnd:
        hwnd = int(args.hwnd, 0)
    else:
        cands = [w for w in ScreenSource.list_windows(120) if "JJ象棋" in w[1]]
        if not cands:
            print("[失败] 找不到 JJ象棋 窗口（可用 --hwnd 指定）")
            return 1
        hwnd = cands[0][0]
    print(f"目标窗口 hwnd=0x{hwnd:X}")
    ok, minimized, note = window_state(hwnd)
    print(f"窗口状态: ok={ok} minimized={minimized} {note}")
    if not ok:
        print("[失败] 目标窗口不可用")
        return 1
    src.attach(hwnd, "?")

    # ---- 算坐标 ----
    if args.move:
        best = args.move
        rect = src.grab() and src.rect
        if rect is None:
            print("[失败] 抓帧失败或未记录窗口矩形")
            return 1
        vision = create_vision(cuda=True)
        res = vision.infer(cv2.cvtColor(src.grab(), cv2.COLOR_BGR2RGB))
        fen = vision.to_fen(res["rows"], "w")
        warp_mat = res["warp_mat"]
    else:
        img = src.grab()
        if img is None:
            print("[失败] 抓帧失败")
            return 1
        rect = src.rect
        vision = create_vision(cuda=True)
        res = vision.infer(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
        fen = vision.to_fen(res["rows"], "w")
        vok, err, n = validate_fen(fen)
        print(f"识别 FEN: {fen}  棋规合法={vok} 着法数={n} {err[:40]}")
        if not vok:
            print("[失败] 局面不合法，拒绝点击")
            return 2
        eng = create_engine(threads=8, hash_mb=256)
        r = eng.analyse(fen, movetime_ms=args.movetime)
        best = r.get("bestmove") or ""
        print(f"引擎 bestmove={best} ({move_to_chinese(fen, best)}) depth={r.get('depth')}")
        eng.quit()
        warp_mat = res["warp_mat"]

    pts = move_screen_points(rect, warp_mat, best)
    if not pts:
        print("[失败] 坐标换算失败")
        return 3
    frm, to = pts["from"], pts["to"]
    print()
    print("=== 计划动作 ===")
    print(f"  着法: {best} ({move_to_chinese(fen, best)})")
    print(f"  窗口矩形 rect={rect}")
    print(f"  起点 屏幕坐标 = ({frm[0]:.1f}, {frm[1]:.1f})")
    print(f"  终点 屏幕坐标 = ({to[0]:.1f}, {to[1]:.1f})")
    print(f"  模式={args.mode} 后端={args.backend} 插值步数={args.steps}")
    print(f"  当前光标 = {get_cursor_pos()}")

    if args.dry_run:
        print("\n[dry-run] 未做任何鼠标动作。")
        return 0

    # ---- 前台 + 遮挡校验（硬闸门）----
    fg = foreground_hwnd()
    print(f"\n当前前台 hwnd=0x{fg:X}（目标 0x{hwnd:X}）")
    if fg != hwnd:
        print("目标窗口不在前台，尝试置前…")
        if not ensure_foreground(hwnd):
            print("[失败] 无法将目标窗口置前，放弃（避免点到别的窗口）")
            return 4
        print("置前成功")
    for name, p in (("起点", frm), ("终点", to)):
        on = is_point_on_window(hwnd, *p)
        under = window_at_point(*p)
        print(f"  {name} ({p[0]:.0f},{p[1]:.0f}) 在目标窗口上={on} 该处窗口=0x{under:X}")
        if not on:
            print(f"[失败] {name}被其他窗口遮挡，放弃点击")
            return 5

    clicker = MouseClicker(mode=args.mode, move_steps=args.steps,
                           backend=args.backend)

    if args.move_only:
        if not args.yes:
            print("\n即将把光标移到起点（不点击）。回车继续，Ctrl-C 取消…")
            try:
                input()
            except (EOFError, KeyboardInterrupt):
                print("已取消")
                return 0
        clicker.preview_move(frm, to)
        print(f"光标已移动到 {get_cursor_pos()}，请肉眼确认是否落在正确的棋子上。")
        return 0

    if not args.yes:
        print("\n即将执行**真实点击**（会抢鼠标）。输入 yes 继续：")
        try:
            if input().strip().lower() != "yes":
                print("已取消")
                return 0
        except (EOFError, KeyboardInterrupt):
            print("已取消")
            return 0

    print("执行中…")
    act = clicker.place_move(frm, to)
    print(f"完成: {act}")
    print("请检查棋盘是否已选中起点并落到终点。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
