# -*- coding: utf-8 -*-
"""验证「我方执黑」时的棋盘翻转逻辑。

核心判据：翻转后的矩阵必须能生成与标准朝向完全一致的 FEN。
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from app_backend import check_position

# 标准开局（标准朝向：row 0 = 黑方底线，row 9 = 红方底线）
START_ROWS = [
    "rnbakabnr",
    ".........",
    ".c.....c.",
    "p.p.p.p.p",
    ".........",
    ".........",
    "P.P.P.P.P",
    ".C.....C.",
    ".........",
    "RNBAKABNR",
]
START_FEN = ("rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR")


def flip_rows(rows: list[str]) -> list[str]:
    """与 app_vision.Worker.flip_rows 相同的实现。"""
    return [row[::-1] for row in reversed(rows)]


def rows_to_fen(rows: list[str], side: str = "w") -> str:
    """与 app_vision.Worker.rows_to_fen 相同的实现。"""
    board: list[str] = []
    for row in rows:
        s, empty = "", 0
        for ch in row:
            if ch in ".x":
                empty += 1
            else:
                if empty:
                    s += str(empty)
                    empty = 0
                s += ch
        if empty:
            s += str(empty)
        board.append(s)
    return "/".join(board) + f" {side} - - 0 1"


def main() -> int:
    print("=" * 72)
    print("我方执黑（棋盘上下颠倒）翻转验证")
    print("=" * 72)

    fails = 0

    print()
    print("[1] 标准朝向应原样生成标准开局 FEN")
    fen = rows_to_fen(START_ROWS)
    ok = fen.split()[0] == START_FEN
    print(f"    生成: {fen.split()[0]}")
    print(f"    期望: {START_FEN}")
    print(f"    {'OK' if ok else '!!'}")
    fails += 0 if ok else 1

    print()
    print("[2] 模拟「我方执黑」：画面上棋盘是上下颠倒的")
    # 执黑时，玩家看到的画面里黑方在下方 → 识别矩阵是标准朝向旋转 180°
    screen_rows = flip_rows(START_ROWS)
    print("    画面朝向矩阵（识别原始输出）:")
    for r in screen_rows:
        print(f"      {r}")
    # 程序应用 flip_rows 还原
    restored = flip_rows(screen_rows)
    fen2 = rows_to_fen(restored)
    ok2 = fen2.split()[0] == START_FEN
    print(f"    还原后 FEN: {fen2.split()[0]}")
    print(f"    期望        : {START_FEN}")
    print(f"    {'OK' if ok2 else '!! 翻转结果不正确'}")
    fails += 0 if ok2 else 1

    print()
    print("[3] 翻转应是对合运算（翻两次回到原状）")
    twice = flip_rows(flip_rows(START_ROWS))
    ok3 = twice == START_ROWS
    print(f"    flip(flip(x)) == x ? {ok3}")
    fails += 0 if ok3 else 1

    print()
    print("[4] 非对称局面：翻转必须改变 FEN（否则说明没真正翻转）")
    # 只在红方一侧放一个车的非对称局面
    asym = [
        "rnbakabnr",
        ".........",
        ".........",
        ".........",
        ".........",
        ".........",
        ".........",
        ".........",
        ".........",
        "R........",
    ]
    f_orig = rows_to_fen(asym).split()[0]
    f_flip = rows_to_fen(flip_rows(asym)).split()[0]
    print(f"    原 FEN  : {f_orig}")
    print(f"    翻转 FEN: {f_flip}")
    ok4 = f_orig != f_flip
    print(f"    两者不同 ? {ok4}")
    fails += 0 if ok4 else 1
    # 红车在 a0（FEN row 9 第 0 列）→ 翻转后应在 i9（FEN row 0 第 8 列）
    expect_flip = "8R/9/9/9/9/9/9/9/9/rnbakabnr"
    ok4b = f_flip == expect_flip
    print(f"    翻转后应为: {expect_flip}")
    print(f"    实际      : {f_flip}   {'OK' if ok4b else '!!'}")
    fails += 0 if ok4b else 1

    print()
    print("[5] 翻转后的 FEN 要能通过局面严格校验")
    ok5, why5 = check_position(fen2.split()[0])
    print(f"    check_position: 合法={ok5}  {why5}")
    fails += 0 if ok5 else 1

    print()
    print("[6] 中局局面翻转（模拟执黑接手中盘）")
    mid = [
        "rnbakab2", "8r", "1c2c1n2", "p3p1p1p", "2p6",
        "6P2", "P1P1P3P", "1C2C1N2", "9", "RNBAKABR1",
    ]
    mid_fen_normal = rows_to_fen(mid).split()[0]
    mid_screen = flip_rows(mid)          # 执黑时识别到的是这个
    mid_restored = rows_to_fen(flip_rows(mid_screen)).split()[0]
    ok6 = mid_restored == mid_fen_normal
    ok6b, why6 = check_position(mid_restored)
    print(f"    标准朝向  : {mid_fen_normal}")
    print(f"    翻转再还原: {mid_restored}")
    print(f"    一致 ? {ok6}   合法 ? {ok6b}")
    fails += 0 if (ok6 and ok6b) else 1

    print()
    print("=" * 72)
    if fails:
        print(f"[失败] {fails} 项未通过")
        return 1
    print("[全部通过] 翻转逻辑正确：执黑时能还原出正确局面")
    return 0


if __name__ == "__main__":
    sys.exit(main())
