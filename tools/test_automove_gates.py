# -*- coding: utf-8 -*-
"""自动走棋「安全闸门」单元测试 —— 不需要游戏窗口、不点鼠标。

直接构造 Worker（不启动识别/引擎），手动喂状态，逐条验证 _pump_auto_move
的触发与抑制条件。全部为预览模式（auto_dry_run=True），不会产生真实点击。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from app_backend import ScreenSource, enable_dpi_awareness, fen_with_side
from app_vision import Worker

START_FEN = "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR"
START_ROWS = [
    "rnbakabnr", ".........", ".c.....c.", "p.p.p.p.p", ".........",
    ".........", "P.P.P.P.P", ".C.....C.", ".........", "RNBAKABNR",
]

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [通过] {name}")
    else:
        FAIL += 1
        print(f"  [失败] {name}  {detail}")


def make_worker():
    enable_dpi_awareness()
    src = ScreenSource()
    ws = ScreenSource.list_windows(50)
    if not ws:
        raise SystemExit("找不到任何窗口用于测试（需要至少一个可见窗口）")
    hwnd, title = ws[0][0], ws[0][1]
    src.attach(hwnd, title)
    src.rect = _win_rect(hwnd)

    w = Worker(src)
    w.auto_move_enabled = True
    w.auto_dry_run = True
    w.auto_think_ms = 1200
    w.auto_cooldown = 1.5
    w.auto_max_moves = 200
    w.auto_place_wait = 2.5
    w.auto_retry_max = 3
    w.my_side = "w"
    w._cur_warp_mat = np.eye(3, dtype=np.float64)
    w._cur_rows = list(START_ROWS)
    w._cur_fen_base = START_FEN
    w._cur_side = "w"
    w._searching = False
    w._last_bestmove = "h2e2"                       # 红炮：起点是我方棋子
    w._last_bestmove_fen = fen_with_side(START_FEN, "w")
    return w


def _win_rect(hwnd):
    import win32gui
    return win32gui.GetWindowRect(hwnd)


def fired(w: Worker) -> bool:
    return bool(w._auto_done[0]) and bool(w._auto_preview)


def main() -> int:
    print("=== 自动走棋闸门单元测试（预览模式，不点击）===")

    # 本测试需要一个**真实窗口**当 source.hwnd 的替身（门槛逻辑里会调
    # window_state / is_point_on_window）。桌面上一个可用窗口都没有时
    # （例如全部落在窗口黑名单里）明确**跳过**，而不是报失败 —— 这是环境
    # 依赖，不是代码问题。
    enable_dpi_awareness()
    if not ScreenSource.list_windows(50):
        print("  [跳过] 当前桌面没有可用窗口（全被窗口黑名单过滤）。")
        print("         打开任意普通程序窗口（或象棋窗口）后重跑即可。")
        return 0

    # 1. 轮到我方 + 起点是我方棋子 → 应当触发
    w = make_worker()
    w._pump_auto_move()
    check("轮到我方时触发", fired(w), f"_auto_done={w._auto_done}")
    check("预览含起点/终点坐标",
          bool(w._auto_preview.get("from")) and bool(w._auto_preview.get("to")),
          str(w._auto_preview))

    # 2. 轮到对方 → 不触发
    w = make_worker()
    w._cur_side = "b"
    w._last_bestmove_fen = fen_with_side(START_FEN, "b")
    w._pump_auto_move()
    check("轮到对方时不触发", not fired(w))

    # 3. 起点不是我方棋子（黑马 h9g7）→ 不触发
    w = make_worker()
    w._last_bestmove = "h9g7"
    w._pump_auto_move()
    check("起点非我方棋子时不触发", not fired(w))

    # 4. 搜索尚未结束 → 不触发
    w = make_worker()
    w._searching = True
    w._pump_auto_move()
    check("搜索未结束时不触发", not fired(w))

    # 5. bestmove 属于旧局面 → 不触发
    w = make_worker()
    w._last_bestmove_fen = fen_with_side("9/9/9/9/9/9/9/9/9/9", "w")
    w._pump_auto_move()
    check("bestmove 属于旧局面时不触发", not fired(w))

    # 6. 同一局面同一着法只点一次
    w = make_worker()
    w._pump_auto_move()
    first = dict(w._auto_preview)
    w._auto_preview = {}
    w._auto_pending = None
    w._pump_auto_move()
    check("同局面同着法只处理一次", not w._auto_preview, f"第二次={w._auto_preview}")
    check("首次预览内容正确", first.get("best") == "h2e2", str(first))

    # 7. 冷却期内不触发
    w = make_worker()
    w._auto_last_click_t = time.perf_counter()
    w._pump_auto_move()
    check("冷却期内不触发", not fired(w))

    # 8. 达到最大连续走子数 → 自动关闭
    w = make_worker()
    w._auto_moves_made = w.auto_max_moves
    w._pump_auto_move()
    check("达保险丝后自动关闭", not w.auto_move_enabled and not fired(w))

    # 9. pending 在棋盘更新后解除并计数
    w = make_worker()
    w._auto_pending = (START_FEN, "h2e2", time.perf_counter())
    w._cur_fen_base = "different-fen"           # 棋盘已变 → 视为生效
    w._pump_auto_move()
    check("pending 在棋盘更新后解除",
          w._auto_pending is None and w._auto_moves_made == 1,
          f"pending={w._auto_pending} made={w._auto_moves_made}")

    # 10. pending 超时重试到上限 → 自动关闭
    w = make_worker()
    w._auto_pending = (START_FEN, "h2e2", time.perf_counter())
    w._auto_retries = w.auto_retry_max
    w._auto_next_click_t = time.perf_counter() - 1.0
    w._pump_auto_move()
    check("重试超限后自动关闭", not w.auto_move_enabled and w._auto_pending is None)

    # 11. 总开关关闭时完全不动作
    w = make_worker()
    w.auto_move_enabled = False
    w._pump_auto_move()
    check("总开关关闭时不动作", not fired(w))

    print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
