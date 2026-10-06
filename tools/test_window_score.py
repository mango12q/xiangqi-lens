# -*- coding: utf-8 -*-
"""目标窗口「自动选择」打分测试（纯函数，无需真实窗口）。

覆盖 ``app_vision.App._window_score`` 的权重与 60 分阈值边界。
阈值的关键性质：微信小程序宿主窗口即使**最小化**也刚好 60 分（会被选中），
而微信主窗口 / 记事本等普通窗口必须低于阈值（不被误选）。

用法::

    python tools/test_window_score.py
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from app_vision import MainWindow

S = MainWindow._window_score
PASS = FAIL = 0


def check(name: str, got, want) -> None:
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}: got={got!r} want={want!r}")


def w(title="", cls="", proc="", minimized=False) -> dict:
    return {"title": title, "class": cls, "proc": proc, "minimized": minimized}


def main() -> int:
    print("--- 各窗口得分 ---")
    check("天天象棋（小程序，已还原）",
          S(w("天天象棋", "Chrome_WidgetWin_0", "WeChatAppEx.exe")), 170)
    check("天天象棋（最小化，-10）",
          S(w("天天象棋", "Chrome_WidgetWin_0", "WeChatAppEx.exe", True)), 160)
    check("JJ象棋（PC 端）",
          S(w("JJ象棋", "Chrome_WidgetWin_0", "JJ.exe")), 130)
    check("中国象棋（网页版）",
          S(w("中国象棋", "Chrome_WidgetWin_1", "chrome.exe")), 130)
    check("微信主窗口（不该被选中）",
          S(w("微信", "Qt51514QWindowIcon", "Weixin.exe")), 50)
    check("微信小程序宿主（最小化，标题无象棋）",
          S(w("", "Chrome_WidgetWin_1", "WeChatAppEx.exe", True)), 60)
    check("记事本（不该被选中）",
          S(w("未命名 - 记事本", "Notepad", "notepad.exe")), 10)

    MIN = MainWindow.WINDOW_SCORE_MIN
    print(f"--- 阈值判定（WINDOW_SCORE_MIN={MIN}）---")
    cases = [
        ("天天象棋应被自动选中",
         w("天天象棋", "Chrome_WidgetWin_0", "WeChatAppEx.exe"), True),
        ("JJ象棋应被自动选中",
         w("JJ象棋", "Chrome_WidgetWin_0", "JJ.exe"), True),
        ("中国象棋应被自动选中",
         w("中国象棋", "Chrome_WidgetWin_1", "chrome.exe"), True),
        ("标题不含象棋的微信小程序宿主不应被自动选中",
         w("", "Chrome_WidgetWin_1", "WeChatAppEx.exe", True), False),
        ("微信主窗口不应被自动选中",
         w("微信", "Qt51514QWindowIcon", "Weixin.exe"), False),
        ("记事本不应被自动选中",
         w("未命名 - 记事本", "Notepad", "notepad.exe"), False),
        ("空标题的普通窗口不应被自动选中", w("", "SomeClass", "some.exe"), False),
    ]
    for name, win, want in cases:
        check(name, S(win) >= MIN, want)

    print("--- 排序：天天象棋必须压过微信主窗口 ---")
    a = S(w("天天象棋", "Chrome_WidgetWin_0", "WeChatAppEx.exe"))
    b = S(w("微信", "Qt51514QWindowIcon", "Weixin.exe"))
    check("天天象棋 > 微信主窗口", a > b, True)

    print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
