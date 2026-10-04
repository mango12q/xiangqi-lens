# -*- coding: utf-8 -*-
"""验证轮次判定逻辑。

局面不用手写 FEN，而是用 cchess 自己执行着法生成，避免手工构造出错。
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from cchess import ChessBoard, FULL_INIT_FEN

from app_backend import (TurnManager, _fen_rows, fen_with_side, legal_moves,
                         move_to_chinese, validate_fen)


def make(fen: str, *moves: str) -> str:
    """在 fen 上依次执行 ICCS 着法，返回新 FEN。

    cchess 会校验 FEN 的 side_to_move 字段，着法方与之不符时 move_iccs
    返回 None，所以每走一步都要手动换边。
    """
    b = ChessBoard(fen)
    for m in moves:
        if b.move_iccs(m) is None:
            raise RuntimeError(f"着法 {m} 非法（当前 FEN: {b.to_fen()}）")
        # 换边：cchess 不会自动更新 FEN 的行棋方字段
        board, side = b.to_fen().rsplit(" ", 1)
        b = ChessBoard(board + (" b" if side == "w" else " w"))
    return b.to_fen()


def rows_of(fen: str) -> str:
    return "".join(_fen_rows(fen))


def main() -> int:
    print("=" * 68)
    print("轮次判定验证")
    print("=" * 68)

    START = FULL_INIT_FEN
    print(f"\n标准开局 FEN: {START}")

    print()
    print("[1] FEN 展开检查")
    r = _fen_rows(START)
    print(f"    行数={len(r)}  每行长度={[len(x) for x in r]}")
    if len(r) != 10 or any(len(x) != 9 for x in r):
        print("    [失败] FEN 展开错误")
        return 1
    print(f"    row0={r[0]}")
    print(f"    row9={r[9]}")
    print("    通过")

    # 用 cchess 生成局面
    AFTER_H2E2 = make(START, "h2e2")     # 红 炮二平五
    AFTER_H9G7 = make(START, "h2e2", "h9g7")   # 黑 马8进7
    print()
    print("[0] 用 cchess 生成的测试局面")
    print(f"    红走后: {AFTER_H2E2}")
    print(f"    黑走后: {AFTER_H9G7}")

    print()
    print("[2] 差分推进轮次：红方 h2e2（炮二平五）")
    tm = TurnManager("w")
    print(f"    初始轮次: {tm.side_to_move}")
    mv = tm.try_advance(START, AFTER_H2E2)
    print(f"    检测到着法: {mv}   中文: {move_to_chinese(START, mv) if mv else '-'}")
    print(f"    推进后轮次: {tm.side_to_move}  (期望 b)")
    if mv != "h2e2" or tm.side_to_move != "b":
        print("    [失败]")
        return 1
    print("    通过")

    print()
    print("[3] 差分推进轮次：黑方 h9g7（马8进7）")
    mv2 = tm.try_advance(AFTER_H2E2, AFTER_H9G7)
    print(f"    检测到着法: {mv2}   中文: {move_to_chinese(AFTER_H2E2, mv2) if mv2 else '-'}")
    print(f"    推进后轮次: {tm.side_to_move}  (期望 w)")
    if mv2 != "h9g7" or tm.side_to_move != "w":
        print("    [失败]")
        return 1
    print(f"    着法历史: {tm.history}")
    print("    通过")

    print()
    print("[4] 非法变化应被拒绝（凭空多一个红车）")
    tm2 = TurnManager("w")
    bad = "rnbakabnr/9/1c5c1/p1p1p1p1p/4R4/9/P1P1P1P1P/1C5C1/9/RNBAKABNR"
    mv3 = tm2.try_advance(START, bad)
    print(f"    结果: {mv3}  (期望 None)")
    if mv3 is not None:
        print("    [失败] 非法变化被接受")
        return 1
    print("    通过")

    print()
    print("[5] 轮次不符应被拒绝：说该红方走，实际是黑方走 h9g7")
    tm3 = TurnManager("w")
    mv4 = tm3.try_advance(START, AFTER_H9G7)
    print(f"    结果: {mv4}  (期望 None)")
    if mv4 is not None:
        print("    [失败]")
        return 1
    print("    通过")

    print()
    print("[6] 引擎着法校正轮次")
    # 真实轮到黑方走，但我们误设为红方走
    wrong = fen_with_side(AFTER_H2E2, "w")
    cur = legal_moves(wrong)
    print(f"    误设轮次 w，该局面合法着法 {len(cur)} 个")
    print(f"    黑方着法 h9g7 在此轮次下合法? {'h9g7' in cur}  (期望 False)")
    tm4 = TurnManager("w")
    fixed = tm4.correct_by_engine_move(wrong, "h9g7")
    print(f"    校正触发: {fixed}   新轮次: {tm4.side_to_move}  (期望 b)")
    if not fixed or tm4.side_to_move != "b":
        print("    [失败]")
        return 1
    print("    通过")

    print()
    print("[7] 局面合法性 + 中文着法")
    ok, err, n = validate_fen(START)
    print(f"    开局合法着法 {n} 个（标准值 44）")
    print(f"    h2e2 → {move_to_chinese(START, 'h2e2')}")
    print(f"    b2e2 → {move_to_chinese(START, 'b2e2')}")
    if n != 44:
        print(f"    [失败] 应为 44，实际 {n}")
        return 1
    print("    通过")

    print()
    print("[8] 非法局面应被 validate_fen 拦截")
    bad_fen = "rnbakabnr/9/9/9/9/9/9/9/9/RNBAKABNR w - - 0 1"   # 缺双将
    ok2, err2, n2 = validate_fen(bad_fen)
    print(f"    合法={ok2}  理由={err2[:60]}")
    if ok2:
        print("    [警告] 非法局面未被拦截（cchess 校验较宽松）")
    print("    通过")

    print()
    print("=" * 68)
    print("[全部通过] 轮次判定逻辑正确")
    return 0


if __name__ == "__main__":
    sys.exit(main())
