# -*- coding: utf-8 -*-
"""测试 cchess 的 ICCS 着法校验接口，寻找可靠的合法性判断方式。"""
from __future__ import annotations

import time

from cchess import ChessBoard, FULL_INIT_FEN

START = FULL_INIT_FEN

print("=" * 70)
print("寻找可靠的 ICCS 着法合法性校验方式")
print("=" * 70)

print()
print("[A] is_valid_iccs_move(fen, iccs)")
print(f"    签名探测: {ChessBoard.is_valid_iccs_move.__doc__}")
for m in ["h2e2", "b0c2", "h9g7", "a0a1", "e0e1", "h2h9"]:
    try:
        r = ChessBoard.is_valid_iccs_move(START, m)
        print(f"    {m}: {r}")
    except Exception as exc:
        print(f"    {m}: 异常 {type(exc).__name__}: {exc}")

print()
print("[B] 实例方法 is_valid_iccs_move(iccs)")
b = ChessBoard(START)
for m in ["h2e2", "b0c2", "h9g7", "a0a1"]:
    try:
        print(f"    {m}: {b.is_valid_iccs_move(m)}")
    except Exception as exc:
        print(f"    {m}: 异常 {type(exc).__name__}: {exc}")

print()
print("[C] 暴力枚举法：对每个 ICCS 执行 move_iccs 看返回是否为 None")
t0 = time.perf_counter()
legal = []
files = "abcdefghi"
for f1 in files:
    for r1 in range(10):
        for f2 in files:
            for r2 in range(10):
                iccs = f"{f1}{r1}{f2}{r2}"
                board = ChessBoard(START)
                if board.move_iccs(iccs) is not None:
                    legal.append(iccs)
t1 = time.perf_counter()
print(f"    枚举 8100 个坐标对，耗时 {t1 - t0:.2f}s")
print(f"    合法着法 {len(legal)} 个 (期望 44)")
print(f"    样本: {sorted(legal)[:12]}")
print(f"    h2e2 在内? {'h2e2' in legal}")

print()
print("[D] 用 create_moves 计数验证（只数个数，不用坐标）")
print(f"    len(list(create_moves())) = {len(list(ChessBoard(START).create_moves()))}")

print()
print("[E] 结论")
if len(legal) == 44 and "h2e2" in legal:
    print("    ✓ [C] 暴力枚举法可靠（44 个合法着法，含 h2e2）")
    print("      但 8100 次 ChessBoard 构造太慢，只适合校验单个着法")
print("    → 对于单个着法校验，直接用:")
print("       ChessBoard(fen).move_iccs(mv) is not None   （原地修改，需重新构造）")
print("       或 ChessBoard.is_valid_iccs_move(fen, mv)")
