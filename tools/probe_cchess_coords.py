# -*- coding: utf-8 -*-
"""确定 cchess 的坐标映射关系。"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from cchess import ChessBoard, FULL_INIT_FEN

from app_backend import _rows_of, legal_moves


def main() -> int:
    START = FULL_INIT_FEN.split()[0]
    rows = _rows_of(START)
    print("FEN 行展开（row0 = FEN 第 1 行 = 黑方底线）:")
    for i, r in enumerate(rows):
        print(f"  {i}: {r}")

    print()
    lm = legal_moves(START)
    print(f"legal_moves 数量 = {len(lm)}   (标准开局应为 44)")
    print(f"样本: {sorted(lm)[:10]}")
    for m in ["h2e2", "b0c2", "b2e2", "a0a1", "c3c4"]:
        print(f"  {m} 在内? {m in lm}")

    print()
    print("--- 用 cchess 逐个验证这些着法是否合法 ---")
    for m in ["h2e2", "b0c2", "b2e2", "a0a1", "c3c4", "h0g2"]:
        b = ChessBoard(FULL_INIT_FEN)
        r = b.move_iccs(m)
        txt = r.to_text() if r else "非法/None"
        print(f"  {m}: {txt}")

    print()
    print("--- 原始 create_moves 元组 ---")
    b = ChessBoard(FULL_INIT_FEN)
    tups = list(b.create_moves())
    print(f"总数: {len(tups)}")
    print(f"前 12 个: {tups[:12]}")

    print()
    print("--- 反推：执行每个元组着法，看它对应哪个 ICCS ---")
    for (r1, c1), (r2, c2) in tups[:6]:
        iccs = f"{chr(97 + c1)}{r1}{chr(97 + c2)}{r2}"
        b2 = ChessBoard(FULL_INIT_FEN)
        res = b2.move_iccs(iccs)
        print(f"  元组(({r1},{c1}),({r2},{c2})) -> 推测 {iccs} -> "
              f"{'合法: ' + res.to_text() if res else '非法'}")

    print()
    print("--- 反向：执行 ICCS，看它对应哪个元组 ---")
    for iccs in ["h2e2", "b0c2", "b2e2"]:
        b3 = ChessBoard(FULL_INIT_FEN)
        res = b3.move_iccs(iccs)
        print(f"  {iccs} -> {'合法: ' + res.to_text() if res else '非法'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
