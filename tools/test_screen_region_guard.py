# -*- coding: utf-8 -*-
"""屏幕区域截图的「遮挡保护」测试。

覆盖 ``ScreenSource._screen_region``：当目标窗口的中心点不属于该窗口
（即窗口此刻被别的窗口盖住）时，必须返回 None 并清空 ``rect`` —— 否则
会静默产出错位画面，而"截屏歪了"正是本次故障的直接表现。

用法::

    python tools/test_screen_region_guard.py
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import app_backend as B

PASS = FAIL = 0


def check(name: str, got, want) -> None:
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}: got={got!r} want={want!r}")


def main() -> int:
    B.enable_dpi_awareness()
    import win32gui

    # 用桌面窗口当靶子：它的 GetWindowRect 永远有效，且尺寸够大
    hwnd = win32gui.GetDesktopWindow()
    src = B.ScreenSource()
    src.attach(hwnd, "desktop")

    print("--- _point_belongs_to_window 基本行为 ---")
    check("无效句柄不崩溃且返回 bool",
          isinstance(B._point_belongs_to_window(0, 0, 0), bool), True)

    orig = B._point_belongs_to_window
    try:
        print("--- 被遮挡：必须放弃抓帧 ---")
        B._point_belongs_to_window = lambda h, x, y: False
        src.rect = (1, 2, 3, 4)                      # 先塞个脏值
        out = src._screen_region()
        check("返回 None", out is None, True)
        check("清空 rect（防陈旧矩形被点击逻辑误用）", src.rect is None, True)

        print("--- 未被遮挡：正常抓帧 ---")
        B._point_belongs_to_window = lambda h, x, y: True
        out = src._screen_region()
        check("抓到图", out is not None, True)
        check("rect 被写入", src.rect is not None, True)
        if out is not None and src.rect is not None:
            l, t, r, b = src.rect
            check("rect 尺寸与图像一致",
                  (out.shape[1], out.shape[0]) == (r - l, b - t), True)
    finally:
        B._point_belongs_to_window = orig

    print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
