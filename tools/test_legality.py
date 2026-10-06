# -*- coding: utf-8 -*-
"""棋规合法性回归测试：**伪合法 vs 真合法**。

本文件原本是 ``test_gameover.py``（终局判定 + 自动接盘）。自动接盘功能已按
用户要求删除，但其中**最关键的一条回归必须保留** —— 它锁死了一个曾导致
设计错误的既有认知：

    ``cchess.is_valid_iccs_move`` 只做「走子规则」的**伪合法**校验，
    **不检查自将 / 将帅照面**（其源码注释原文：
    "只进行最基本的走子规则检查"）。

因此 ``app_backend.is_legal_move`` / ``enumerate_legal_moves`` 都**不能**用来
判定「这步棋走完之后是不是合法局面」。需要真合法请用
``is_strict_legal_move`` / ``has_no_legal_moves``。

不需要窗口，也不需要引擎。
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from app_backend import (enumerate_legal_moves, has_no_legal_moves,
                         is_legal_move, is_strict_legal_move)   # noqa: E402

PASS = 0
FAIL = 0

START_FEN = "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR w - - 0 1"

# 双车杀：黑将在 e9 被红车 a9 与 i9 封锁横线、红车 e8 将军；
# 黑将吃 e8 的车会与 e0 的红帅照面（非法）→ 无任何**真**合法着法。
MATE_FEN = "R3k3R/4R4/9/9/9/9/9/9/9/4K4 b - - 0 1"

# 将帅照面：红帅 e0、黑将 e9，中间全空
FACE_FEN = "4k4/9/9/9/9/9/9/9/9/4K4 w - - 0 1"


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [通过] {name}")
    else:
        FAIL += 1
        print(f"  [失败] {name}  {detail}")


def main() -> int:
    print("=== 棋规合法性回归（伪合法 vs 真合法）===")

    # ---- 1. 起始局面：两种校验都该通过 ----
    print("\n① 起始局面")
    check("伪合法着法数 = 44", len(enumerate_legal_moves(START_FEN)) == 44,
          str(len(enumerate_legal_moves(START_FEN))))
    check("真合法：h2e2（炮二平五）", is_strict_legal_move(START_FEN, "h2e2"))
    check("真合法：c3c4（兵七进一）", is_strict_legal_move(START_FEN, "c3c4"))
    check("真合法：a0a9 非法（车不能跳）",
          not is_strict_legal_move(START_FEN, "a0a9"))
    check("伪合法：a0a9 同样非法", not is_legal_move(START_FEN, "a0a9"))
    check("起始局面没有将死", has_no_legal_moves(START_FEN) is False)

    # ---- 2. ★ 核心回归：杀局里的「送将」着法 ----
    print("\n② ★ 杀局：伪合法全部放行、真合法全部拒绝")
    sent = ("e9d9", "e9f9", "e9e8")
    pseudo = [m for m in sent if is_legal_move(MATE_FEN, m)]
    check("3 个送将着法都通过**伪合法**（证明该 API 不可用于终局/合法判定）",
          len(pseudo) == 3, str(pseudo))
    strict = [m for m in sent if is_strict_legal_move(MATE_FEN, m)]
    check("同样的着法全部被**严格**校验拒绝", strict == [], str(strict))
    check("has_no_legal_moves 判为将死（真合法着法为 0）",
          has_no_legal_moves(MATE_FEN) is True)

    # ---- 3. 将帅照面 ----
    print("\n③ 将帅照面")
    check("e0e1 伪合法通过", is_legal_move(FACE_FEN, "e0e1"))
    check("e0e1 严格拒绝（会让将帅照面）",
          not is_strict_legal_move(FACE_FEN, "e0e1"))
    check("e0d0 严格通过（不照面）", is_strict_legal_move(FACE_FEN, "e0d0"))
    check("e0e1 严格通过（红帅后退一步，不照面）",
          is_strict_legal_move("4k4/9/9/9/9/9/9/9/9/4K4 w - - 0 1", "e0e0") is False)

    # ---- 4. 走子方颜色 ----
    #    这是「轮次自纠」能成立的前提：同一差分着法不可能对红黑双方同时合法
    print("\n④ 走子方颜色（轮次自纠的前提）")
    red = "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR w - - 0 1"
    black = "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR b - - 0 1"
    check("红兵 a3a4：红方轮次合法", is_legal_move(
        "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR w - - 0 1",
        "a3a4"))
    check("红兵 a3a4：黑方轮次非法",
          not is_legal_move(black, "a3a4"))
    check("黑卒 a6a5：黑方轮次合法", is_legal_move(black, "a6a5"))
    check("黑卒 a6a5：红方轮次非法", not is_legal_move(red, "a6a5"))
    check("h2e2（红炮）只对红方轮次合法",
          is_legal_move(red, "h2e2") and not is_legal_move(black, "h2e2"))

    # ---- 5. 失败路径 ----
    print("\n⑤ 受控失败")
    check("空着法返回 False", not is_strict_legal_move(START_FEN, ""))
    check("长度不对返回 False", not is_strict_legal_move(START_FEN, "h2e"))
    check("非法局面返回 False（不抛异常）",
          is_strict_legal_move("bad/fen", "h2e2") is False)
    check("has_no_legal_moves 遇非法局面返回 False（不抛异常）",
          has_no_legal_moves("bad/fen") is False)

    print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
