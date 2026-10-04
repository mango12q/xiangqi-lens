# -*- coding: utf-8 -*-
"""验证棋盘几何校验 check_board_geometry。"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from app_backend import check_board_geometry

# 画面尺寸（JJ象棋窗口）
IMG = (1253, 683)

# (标签, 四角 [A0,A8,J0,J8], 期望合法)
CASES = [
    # --- 实测：JJ象棋主界面的 3D 棋盘摆件，pose 输出的退化结果 ---
    ("实测·主界面3D摆件（退化）",
     [[347.6, 577.6], [237.5, 559.2], [528.1, 577.6], [512.8, 577.6]], False),

    # --- 实测：正常对局界面（识别成功的那些帧）---
    ("实测·正常对局",
     [[57.0, 335.9], [632.1, 332.8], [47.8, 963.0], [635.2, 956.9]], True),

    ("实测·另一帧正常对局",
     [[60.1, 338.9], [632.1, 332.8], [44.8, 959.9], [635.2, 956.9]], True),

    # --- 构造的退化情形 ---
    ("四角压成一条横线",
     [[100, 600], [500, 600], [100, 600], [500, 600]], False),
    ("四角压成一条竖线",
     [[300, 100], [300, 100], [300, 800], [300, 800]], False),
    ("四边形太小",
     [[300, 300], [340, 300], [300, 330], [340, 330]], False),
    ("宽高比过扁（宽800高100）",
     [[0, 500], [800, 500], [0, 600], [800, 600]], False),

    # --- 应该接受的正常比例 ---
    ("标准 9:10 棋盘",
     [[50, 300], [650, 300], [50, 970], [650, 970]], True),
    ("略有透视倾斜",
     [[60, 310], [640, 295], [45, 960], [655, 975]], True),
]


def main() -> int:
    print("=" * 72)
    print("棋盘几何校验 check_board_geometry 验证")
    print("=" * 72)
    fails = 0
    for label, kps, want in CASES:
        got, why = check_board_geometry(kps, img_shape=IMG)
        mark = "OK" if got == want else "!!"
        if got != want:
            fails += 1
        print(f"  [{mark}] {'通过' if got else '拒绝'} (期望{'通过' if want else '拒绝'})  {label}")
        if not got:
            print(f"         理由: {why}")
    print()
    if fails:
        print(f"[失败] {fails} 项与预期不符")
        return 1
    print(f"[全部通过] {len(CASES)} 个用例")
    return 0


if __name__ == "__main__":
    sys.exit(main())
