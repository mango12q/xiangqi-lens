# -*- coding: utf-8 -*-
"""「新局 / 自动判断我方执子 / 轮次自纠」测试。

覆盖三件事：
1. ``Worker._detect_my_side`` —— 从棋盘朝向判断我方执子，**含退化关键点的质量门**；
2. ``BoardTracker`` 的**双方合法性自纠** —— 初始轮次猜错也能在第一次走子时纠正；
3. ``apply_newgame`` 的重置，以及 ``_current_window()`` 对下拉框元组的解包
   （后者曾是「框选没反应」的根因）。

不需要真实窗口与引擎（离屏起 Qt）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PASS = 0
FAIL = 0

START_BOARD = "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR"
AFTER_H2E2 = "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C2C4/9/RNBAKABNR"


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [通过] {name}")
    else:
        FAIL += 1
        print(f"  [失败] {name}  {detail}")


def main() -> int:
    print("=== 新局 / 我方执子 / 轮次自纠 测试 ===")

    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    import app_vision as av
    from app_backend import BoardTracker, ScreenSource

    # ---- 1. 我方执子自动判断 ----
    print("\n① 我方执子自动判断（_detect_my_side）")
    # 真实截图 shots/bg_002A0A30.png 的关键点：黑在上、红在下 → 我方执红
    kp_red = np.array([[57, 336], [632, 333], [48, 963], [635, 957]], float)
    side, why = av.Worker._detect_my_side(kp_red, (1253, 683))
    check("黑在上红在下 → 我方执红", side == "w", f"{side} {why}")

    kp_black = np.array([[1749, 1210], [1762, 1205], [1785, 127], [1731, 140]],
                        float)
    side, why = av.Worker._detect_my_side(kp_black, (1256, 1833))
    check("黑在下红在上 → 我方执黑", side == "b", f"{side} {why}")

    # 质量门：退化关键点必须拒绝判断而不是瞎猜
    degenerate = [
        ("同边两角一高一低（A0 在底 A8 在顶）",
         [[1762, 1210], [1749, 122], [1785, 127], [1731, 140]], (1256, 1833)),
        ("四个点跑到画面外",
         [[2155, -642], [2155, -642], [-240, 1754], [-240, -642]], (1117, 1920)),
        ("棋盘太扁（span 不足 40px）",
         [[100, 100], [200, 100], [100, 120], [200, 120]], (600, 800)),
        ("四个点重合",
         [[100, 100], [100, 100], [100, 100], [100, 100]], (600, 800)),
    ]
    for name, kp, shape in degenerate:
        side, why = av.Worker._detect_my_side(np.array(kp, float), shape)
        check(f"退化拒绝：{name}", side is None, f"side={side} {why}")

    check("关键点不足 4 个 → 拒绝",
          av.Worker._detect_my_side(np.zeros((2, 2)), (600, 800))[0] is None)
    check("关键点含 NaN → 拒绝",
          av.Worker._detect_my_side(
              np.array([[0, 0], [np.nan, 1], [2, 2], [3, 3]]), (600, 800))[0]
          is None)
    check("不给 img_shape 也能判（跳过越界检查）",
          av.Worker._detect_my_side(kp_red)[0] == "w")

    # ---- 2. 轮次自纠 ----
    print("\n② BoardTracker 双方合法性自纠")
    t = BoardTracker(first_side="b")            # 故意把初始轮次猜成黑方
    t.update(START_BOARD + " b - - 0 1")
    ok1, reason1, _m1, _s1 = t.update(AFTER_H2E2)
    check("自纠第 1 帧不采信（防单帧噪声）",
          (not ok1) and reason1 == "drop", f"{ok1} {reason1}")
    check("第 1 帧后轮次仍未变", t.side_to_move == "b", t.side_to_move)
    ok, reason, mv, side = t.update(AFTER_H2E2)
    check("第 2 帧（同一着法）被采信", ok, f"{ok} {reason}")
    check("原因标为「走子(轮次自纠)」", reason == "走子(轮次自纠)", reason)
    check("识别出的着法 = h2e2", mv == "h2e2", mv)
    check("自纠后轮次 = 黑方（轮到对方）", side == "b", side)
    check("corrections 计数 +1", t.corrections == 1, str(t.corrections))

    t2 = BoardTracker(first_side="w")           # 猜对时不应触发自纠
    t2.update(START_BOARD + " w - - 0 1")
    ok, reason, mv, side = t2.update(AFTER_H2E2)
    check("猜对时走普通路径", reason == "走子", reason)
    check("猜对时不计数 corrections", t2.corrections == 0, str(t2.corrections))

    # 交替出现的两个不同自纠候选不应被采信（计数要被打断）
    t4 = BoardTracker(first_side="b")
    t4.update(START_BOARD + " b - - 0 1")
    t4.update(AFTER_H2E2)                       # 候选 A，第 1 次
    ok, reason, _mv, _s = t4.update(START_BOARD + " b - - 0 1")   # 换回原局面
    check("局面回退后自纠计数被打断", t4.corrections == 0,
          str(t4.corrections))

    # 无法用任何一方解释的跳变 → 仍然挡下（不能被自纠误放行）
    t3 = BoardTracker(first_side="w")
    t3.update(START_BOARD + " w - - 0 1")
    ok, reason, _mv, _side = t3.update("9/9/9/9/9/9/9/9/9/9")
    check("无法解释的跳变仍被挡下", (not ok) and reason == "drop", reason)

    # ---- 3. apply_newgame ----
    print("\n③ apply_newgame 重置")
    wk = av.Worker(ScreenSource())
    wk.my_side = "b"
    wk._published = "deadbeef"
    wk._last_key = "deadbeef"
    wk._same = 5
    wk._auto_moves_made = 7
    wk._game_over = True
    wk._last_bestmove = "h2e2"
    wk._last_bestmove_fen = "x"
    wk._book_move = {"move": "h2e2"}
    wk._restart_pending_t = 123.0
    wk._cur_fen_base = "old"
    wk.apply_newgame(True)
    check("_published 清空", wk._published == "", wk._published)
    check("_last_key 清空", wk._last_key == "", wk._last_key)
    check("连续计数归零", wk._same == 0, str(wk._same))
    check("累计走子归零", wk._auto_moves_made == 0, str(wk._auto_moves_made))
    check("终局标记复位", wk._game_over is False)
    check("bestmove 清空", wk._last_bestmove == "" and wk._last_bestmove_fen == "")
    check("库着清空", wk._book_move is None)
    check("接盘待校验时间戳清空", wk._restart_pending_t == 0.0)
    check("当前局面清空", wk._cur_fen_base == "", wk._cur_fen_base)
    check("武装了自动判断执子", wk.auto_side_pending is True)
    check("轮次按「红方先手」（象棋红先，新局红方还没动子）",
          wk._cur_side == "w", wk._cur_side)
    check("冷却时间戳一并清零",
          wk._auto_next_click_t == 0.0 and wk._auto_last_click_t == 0.0,
          f"{wk._auto_next_click_t} {wk._auto_last_click_t}")
    wk.apply_newgame(False)
    check("auto_side=False 时不武装", wk.auto_side_pending is False)

    # ---- 3b. 自动走棋的轮次硬闸门 ----
    print("\n③b 自动走棋轮次闸门（_turn_guard_blocks）")
    g = av.Worker(ScreenSource())
    g._tracker = BoardTracker(first_side="b")
    g.my_side = "b"
    g._cur_fen_base = START_BOARD
    g._cur_side = "b"
    check("开局盘面 + 认为轮到黑方 → 拦住", g._turn_guard_blocks() is True)
    g._tracker.moves.append("h2e2")             # 观察到走子后不再拦
    check("观察到走子后放行", g._turn_guard_blocks() is False)
    g._tracker = BoardTracker(first_side="b")
    g._cur_side = "w"
    check("轮次是红方时不拦（本来就轮不到我方）",
          g._turn_guard_blocks() is False)
    g._cur_side = "b"
    g._cur_fen_base = "rnbakab1r/9/1c4nc1/p1p1p1p1p/9/9/P1P1P1P1P/1C2C4/9/RNBAKABNR"
    check("中盘接手（非初始盘面）不拦", g._turn_guard_blocks() is False)
    g._tracker = None
    check("tracker 为空时不拦", g._turn_guard_blocks() is False)

    # ---- 3c. safe_slot 实参裁剪（Qt 信号会把实参全量传进槽）----
    print("\n③c safe_slot（异常可见化 + 实参裁剪）")
    from app_vision import safe_slot

    calls = []

    class _T:
        @safe_slot
        def zero_arg(self):
            calls.append("zero")
            return 1

        @safe_slot
        def one_arg(self, d):
            calls.append(("one", d))
            return 2

        @safe_slot
        def boom(self):
            raise ValueError("故意炸")

    tt = _T()
    check("0 参槽正常调用", tt.zero_arg() == 1)
    # ★ 关键：Qt 的 Signal(bool)/clicked 会把实参传进 0 参槽 —— 必须自动裁掉
    check("0 参槽被传 1 个实参时自动裁剪", tt.zero_arg("多余") == 1,
          str(len(calls)))
    check("1 参槽正常收到参数",
          tt.one_arg({"a": 1}) == 2 and calls[-1] == ("one", {"a": 1}),
          str(calls[-1]))
    check("1 参槽被传 2 个实参时裁剪到 1 个", tt.one_arg({"a": 2}, "多余") == 2)
    check("functools.wraps 保留了函数名", tt.zero_arg.__name__ == "zero_arg",
          tt.zero_arg.__name__)
    check("异常被吞掉并返回 None（不向上抛）", tt.boom() is None)

    # ---- 4. 下拉框元组解包（「框选没反应」的根因）----
    print("\n④ 窗口下拉框解包（_current_window）")
    w = av.MainWindow()
    check("先手下拉已移除", not hasattr(w, "combo_side"))
    check("新局按钮存在", w.btn_newgame.text() == "新局", w.btn_newgame.text())
    check("开始按钮已改名「分析」", w.btn_run.text() == "分析", w.btn_run.text())

    # 模拟 refresh_windows 的存法：itemData 是 (hwnd, title) 元组
    w.combo.clear()
    w.combo.addItem("JJ象棋  [Chrome_WidgetWin_0]  1300x780", (123456, "JJ象棋"))
    cur = w._current_window()
    check("_current_window 正确解出 (hwnd, title)",
          cur == (123456, "JJ象棋"), str(cur))
    check("解出的是 int 而不是元组", isinstance(cur[0], int), str(type(cur[0])))
    w.combo.clear()
    w.combo.addItem("（未找到可用窗口）", None)
    check("无选中项时返回 None", w._current_window() is None)

    # ---- 5. 新局按钮行为 ----
    print("\n⑤ 新局 / 执子回填")
    w2 = av.MainWindow()
    w2._on_new_game()
    check("停止态点新局 → 记下待武装", w2._newgame_armed is True)
    w2.on_side_detected({"side": "b", "why": "黑方底线在画面下方"})
    check("回填下拉框为「我方执黑」",
          w2.combo_my_side.currentData() == "b", w2.combo_my_side.currentText())
    w2.on_side_detected({"side": "x", "why": "非法值"})
    check("非法 side 不改变下拉框",
          w2.combo_my_side.currentData() == "b", w2.combo_my_side.currentText())

    print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
