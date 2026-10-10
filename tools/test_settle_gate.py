# -*- coding: utf-8 -*-
"""识别稳定（动画抑制）闸门测试 —— 针对天天象棋「吃」字动画遮挡盘面的修复。

分两层：
1. 纯函数层：``gray_small`` / ``motion_ratio`` / ``settle_ready``；
2. 真实 Worker 层：离屏起一个 ``Worker``，直接喂合成画面给
   ``_frame_moving``，验证「静止 → 不判定在动」「插入动画块 → 判定在动」
   「动画消失后重新回到静止」。

不需要真实游戏窗口，也不需要引擎（不调用 run()）。
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


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [通过] {name}")
    else:
        FAIL += 1
        print(f"  [失败] {name}  {detail}")


def board_frame(seed: int = 0, glyph: bool = False) -> np.ndarray:
    """造一张 450x500 的「棋盘」图（RGB）。

    ``glyph=True`` 时在正中间画一块高对比色块，模拟天天象棋的「吃」字动画。
    """
    rng = np.random.default_rng(seed)
    img = np.full((500, 450, 3), 210, dtype=np.uint8)
    # 画 10 横 9 竖的网格线，让图有点结构
    for r in range(10):
        y = 25 + r * 50
        img[y:y + 2, :] = 60
    for c in range(9):
        x = 25 + c * 50
        img[:, x:x + 2] = 60
    if glyph:
        img[190:310, 150:300] = np.array([200, 30, 30], dtype=np.uint8)
    img = np.clip(img.astype(np.int16) + rng.integers(-2, 3, img.shape), 0,
                  255).astype(np.uint8)
    return img


def main() -> int:
    print("=== 识别稳定（动画抑制）闸门测试 ===")
    from app_vision import Worker, gray_small, motion_ratio, settle_ready

    # ---- 1. 纯函数 ----
    print("\n① 纯函数")
    a = board_frame(0)
    b = board_frame(1)                      # 同结构、只有随机噪声
    ga, gb = gray_small(a), gray_small(b)
    check("gray_small 输出 160x144", ga.shape == (160, 144), str(ga.shape))
    check("同图差异为 0", motion_ratio(ga, ga) == 0.0,
          str(motion_ratio(ga, ga)))
    r_noise = motion_ratio(ga, gb)
    check("仅噪声时差异极小（<0.005）", r_noise < 0.005, str(r_noise))

    g_glyph = gray_small(board_frame(0, glyph=True))
    r_glyph = motion_ratio(ga, g_glyph)
    check("插入「吃」字块后差异显著（>0.02）", r_glyph > 0.02, str(r_glyph))
    check("首帧（prev=None）按静止处理", motion_ratio(None, ga) == 0.0)
    check("形状不一致按静止处理", motion_ratio(ga, ga[:80]) == 0.0)

    check("settle_ready：未开始静止 → False",
          not settle_ready(0.0, 100.0, 400))
    check("settle_ready：静止不足 → False",
          not settle_ready(100.0, 100.2, 400))
    check("settle_ready：静止足够 → True",
          settle_ready(100.0, 100.5, 400))
    check("settle_ready：settle_ms=0 立即就绪",
          settle_ready(100.0, 100.0, 0))

    from app_vision import suppress_timed_out
    check("suppress_timed_out：未开始计时 → False",
          not suppress_timed_out(0.0, 100.0, 5000))
    check("suppress_timed_out：未超时 → False",
          not suppress_timed_out(100.0, 103.0, 5000))
    check("suppress_timed_out：超时 → True（强制放行一帧）",
          suppress_timed_out(100.0, 105.5, 5000))
    check("suppress_timed_out：max_ms=0 立即超时",
          suppress_timed_out(100.0, 100.0, 0))
    check("suppress_timed_out：边界前一毫秒 → False",
          not suppress_timed_out(100.0, 104.999, 5000))
    check("suppress_timed_out：恰好到点 → True",
          suppress_timed_out(100.0, 105.0, 5000))

    # ---- 2. 真实 Worker ----
    print("\n② Worker._frame_moving（离屏）")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])

    from app_backend import ScreenSource
    w = Worker(ScreenSource())
    check("默认开启动画抑制", w.anim_suppress is True)
    w.anim_suppress = True
    w.motion_thresh = 0.02
    check("默认最长抑制时长为正（防永久不识别）", w.anim_max_ms > 0,
          str(w.anim_max_ms))
    check("默认静置时长为正", w.settle_ms > 0, str(w.settle_ms))

    moving, ratio = w._frame_moving(board_frame(0))
    check("首帧不算在动", not moving, f"ratio={ratio:.4f}")

    moving, ratio = w._frame_moving(board_frame(0))
    check("同一张图不算在动", not moving, f"ratio={ratio:.4f}")

    moving, ratio = w._frame_moving(board_frame(0, glyph=True))
    check("动画帧被判为在动", moving, f"ratio={ratio:.4f}")

    moving, ratio = w._frame_moving(board_frame(0, glyph=True))
    check("动画保持（同帧）不算在动（差异归零）", not moving,
          f"ratio={ratio:.4f}")

    moving, ratio = w._frame_moving(board_frame(0))
    check("动画消失后重新判为在动（相对上一帧有变化）", moving,
          f"ratio={ratio:.4f}")

    moving, ratio = w._frame_moving(board_frame(0))
    check("动画消失后稳定帧 → 恢复静止", not moving, f"ratio={ratio:.4f}")

    # 阈值调大后，同样的动画不应再被判为在动
    w2 = Worker(ScreenSource())
    w2.motion_thresh = 0.5
    w2._frame_moving(board_frame(0))
    moving, ratio = w2._frame_moving(board_frame(0, glyph=True))
    check("阈值调大后动画不再触发", not moving, f"ratio={ratio:.4f}")

    # 阈值边界：先测出实际变化率 r，再把阈值分别设在 r 的两侧
    probe = Worker(ScreenSource())
    probe._frame_moving(board_frame(0))
    _, r = probe._frame_moving(board_frame(0, glyph=True))
    check("动画帧实际变化率在 (0.02, 0.5) 区间", 0.02 < r < 0.5, f"r={r:.4f}")

    w3 = Worker(ScreenSource())
    w3.motion_thresh = max(0.0, r - 0.001)
    w3._frame_moving(board_frame(0))
    moving, _ = w3._frame_moving(board_frame(0, glyph=True))
    check("阈值略低于实际变化率 → 触发", moving, f"thresh={r - 0.001:.4f}")

    w4 = Worker(ScreenSource())
    w4.motion_thresh = min(0.5, r + 0.001)
    w4._frame_moving(board_frame(0))
    moving, _ = w4._frame_moving(board_frame(0, glyph=True))
    check("阈值略高于实际变化率 → 不触发", not moving,
          f"thresh={r + 0.001:.4f}")

    print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
