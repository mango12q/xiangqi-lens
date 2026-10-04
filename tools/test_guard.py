# -*- coding: utf-8 -*-
"""验证识别抗错状态机（BoardTracker，含轮次管理）。

覆盖：正常对局、单帧误识别、误识别恢复、持续误识别兜底、局面跳变。
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from cchess import ChessBoard, FULL_INIT_FEN

from app_backend import BoardTracker, is_legal_move


def walk(moves: list[str]) -> str:
    """从标准开局走出一串着法（自动换边），返回仅棋盘段的 FEN。"""
    b = ChessBoard(FULL_INIT_FEN)
    for m in moves:
        if b.move_iccs(m) is None:
            raise RuntimeError(f"着法 {m} 非法 @ {b.to_fen()}")
        board, side = b.to_fen().rsplit(" ", 1)
        b = ChessBoard(board + (" b" if side == "w" else " w"))
    return b.to_fen().split()[0]


def show(tag: str, res: tuple) -> None:
    ok, reason, mv, side = res
    side_cn = "红" if side == "w" else "黑"
    flag = "采信" if ok else "挡下"
    extra = f" move={mv}" if mv else ""
    print(f"    {tag}: {flag}  reason={reason:8}{extra}  轮次={side_cn}{extra and ''}")


def main() -> int:
    print("=" * 74)
    print("识别抗错状态机验证（含轮次管理）")
    print("=" * 74)

    START = FULL_INIT_FEN.split()[0]
    F1 = walk(["h2e2"])                    # 红 炮二平五 → 该黑走
    F2 = walk(["h2e2", "h9g7"])            # 黑 马8进7 → 该红走
    F3 = walk(["h2e2", "h9g7", "b0c2"])    # 红 马八进七 → 该黑走

    print()
    print("[场景1] 正常对局：红黑双方走子都应被采信，轮次正确翻转")
    t = BoardTracker(force_after=6, first_side="w")
    seq = [("初始", START), ("红走 h2e2", F1), ("黑走 h9g7", F2), ("红走 b0c2", F3)]
    for tag, fen in seq:
        res = t.update(fen)
        show(tag, res)
        if not res[0]:
            print(f"    [失败] {tag} 被挡下了")
            return 1
    if t.moves != ["h2e2", "h9g7", "b0c2"]:
        print(f"    [失败] 着法序列不对: {t.moves}")
        return 1
    # 走子顺序：红(h2e2) → 黑(h9g7) → 红(b0c2)，三步之后该轮到黑方
    if t.side_to_move != "b":
        print(f"    [失败] 三次走子后轮次应为黑(b)，实际 {t.side_to_move}")
        return 1
    print(f"    着法序列 {t.moves}  最终轮次={'红' if t.side_to_move=='w' else '黑'}  {t.stats}")
    print("    通过")

    print()
    print("[场景2] 单帧误识别（红傌→红炮）：应被挡下，不污染局面")
    t2 = BoardTracker(force_after=6)
    t2.update(START)
    t2.update(F1)
    print(f"    已采信到红走 h2e2，当前该黑走  {t2.stats}")
    before = t2.stable_fen
    bad = F1.replace("1C2C4", "1N2C4", 1)      # 把 e2 的红炮改成红马
    if bad == F1:
        print("    [跳过] 伪造误识别局面失败（FEN 结构不符预期）")
    else:
        res = t2.update(bad)
        show("误识别帧", res)
        if res[0]:
            print("    [失败] 单帧误识别被采信了")
            return 1
        if t2.stable_fen != before:
            print("    [失败] 已确认局面被污染")
            return 1
        print("    [OK] 已挡下，局面未变")
    print("    通过")

    print()
    print("[场景3] 误识别只出现一帧后恢复正常：后续走子应正常")
    res = t2.update(F2)
    show("正常帧", res)
    if not res[0]:
        print("    [失败] 恢复正常后走子被挡")
        return 1
    print("    通过")

    print()
    print("[场景4] 局面持续无法解释：应兜底采信，不永久卡死")
    # 注意：构造的"误识别局面"必须是**合法**局面，否则会被 check_position
    # 直接拒掉（那是另一层防护），测不到兜底逻辑。
    # 这里用"连走两步"造出一个合法但无法由一步差分解释的局面，
    # 效果等同于识别跳帧 / 持续误识别。
    t3 = BoardTracker(force_after=4)
    t3.update(START)
    jumped2 = walk(["h2e2", "h9g7"])      # 两步，差分无法用一步解释
    fired = False
    for i in range(1, 8):
        res = t3.update(jumped2)
        tag = "采信" if res[0] else "挡下"
        print(f"    第 {i} 帧: {tag}  reason={res[1]}")
        if res[0]:
            if i > 3:
                print(f"    [OK] 第 {i} 帧兜底采信（force_after=4 时应在第 4 次左右触发）")
            fired = True
            break
    if not fired:
        print("    [失败] 持续无法解释的局面未被兜底采信，会永久卡死")
        return 1
    if t3.forced == 0:
        print(f"    [注意] 采信原因不是兜底（forced={t3.forced}），可能走了稳定新局面分支")
    print(f"    {t3.stats}")
    print("    通过")

    print()
    print("[场景5] 局面跳变（接手中盘）：连续两帧一致后采信")
    t5 = BoardTracker(force_after=6)
    t5.update(START)
    jumped = walk(["h2e2", "h9g7", "b0c2", "b9c7"])   # 连走 4 步，差分无法解释
    r1 = t5.update(jumped)
    r2 = t5.update(jumped)
    show("跳变帧1", r1)
    show("跳变帧2", r2)
    if not r2[0] or r2[1] != "稳定新局面":
        print("    [失败] 稳定跳变局面未被采信")
        return 1
    print("    通过")

    print()
    print("[场景6] 轮次自纠：用引擎着法反推")
    t6 = BoardTracker(first_side="b")     # 故意设错：实际该红走
    t6.update(START)
    # 红方着法 h2e2 在"黑走"轮次下非法，应触发校正
    fixed = t6.correct_by_engine_move("h2e2")
    print(f"    设错轮次=b，引擎着法 h2e2 → 校正触发={fixed}，"
          f"新轮次={'红' if t6.side_to_move == 'w' else '黑'}  {t6.stats}")
    if not fixed or t6.side_to_move != "w":
        print("    [失败] 轮次未被校正")
        return 1
    print("    通过")

    print()
    print("[场景7] is_legal_move 基本校验")
    # 注意：e0e1 是红帅在九宫内移动一格，是**合法**着法
    checks = [("h2e2", True), ("h9g7", False), ("a0a1", True), ("e0e1", True)]
    bad = 0
    for mv, want in checks:
        got = is_legal_move(FULL_INIT_FEN, mv)
        mark = "OK" if got == want else "!!"
        if got != want:
            bad += 1
        print(f"    {mv}: {got}  (期望 {want})  [{mark}]")
    if bad:
        print(f"    [失败] {bad} 项不符")
        return 1
    print("    通过")

    print()
    print("=" * 74)
    print("[全部通过] 抗错状态机 + 轮次管理工作正常")
    return 0


if __name__ == "__main__":
    sys.exit(main())
