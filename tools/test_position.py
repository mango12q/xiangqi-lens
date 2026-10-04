# -*- coding: utf-8 -*-
"""验证严格局面校验 check_position。"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from app_backend import check_position

# (局面, 应否合法, 说明)
CASES = [
    # --- 必须拒绝：识别失败产生的非法局面 ---
    ("4k4/9/9/9/9/9/8P/9/9/4k4", False, "两个黑将、没有红帅（实测遇到的那个）"),
    ("rnbakabnr/9/9/9/9/9/9/9/9/RNBAKABNR", False, "缺少双将"),
    ("rnbakabnr/9/9/9/9/9/9/9/9/RNBAKABN1", False, "缺黑将"),
    ("4k4/9/9/9/9/9/9/9/9/4K4", False, "将帅照面"),
    ("3k5/9/9/9/9/9/9/9/9/3K5", False, "将帅在九宫外（列越界）"),
    ("rnbakabnr/9/9/9/9/9/9/9/9/RNBAKABNR".replace("r", "R"), False, "红车出现 3 个以上"),
    ("9/9/9/9/9/9/9/9/9/4K4", False, "只有红帅缺黑将"),
    ("4k4/9/9/9/9/9/9/9/9/9", False, "只有黑将缺红帅"),
    ("4k4/9/9/9/9/9/9/9/9/RNBAKABN", False, "行长度不足"),
    # --- 必须接受：正常局面 ---
    ("rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR", True, "标准开局"),
    ("r2akab1r/3n5/1c2b1nc1/p1p1p1p1p/9/9/P1P1P1P1P/1CN4C1/9/R1BAKABNR", True, "红马跳出后的局面"),
    # 双将同列但有车阻隔，不构成照面，是合法残局
    ("4k4/9/9/9/4R4/9/9/9/9/4K4", True, "双将同列但有车阻隔"),
    ("3k5/9/9/9/9/9/9/9/9/4K4", True, "黑将偏左但在九宫内"),
    ("4k4/9/9/9/4P4/9/9/9/9/4K4", True, "红兵在中路"),
]


def main() -> int:
    print("=" * 72)
    print("严格局面校验 check_position 验证")
    print("=" * 72)
    fails = 0
    for fen, want, desc in CASES:
        got, why = check_position(fen)
        mark = "OK" if got == want else "!!"
        if got != want:
            fails += 1
        verdict = "合法" if got else "拒绝"
        exp = "合法" if want else "拒绝"
        print(f"  [{mark}] {verdict} (期望{exp})  {desc}")
        if not got:
            print(f"         理由: {why}")
        print(f"         {fen}")
    print()
    if fails:
        print(f"[失败] {fails} 项与预期不符")
        return 1
    print(f"[全部通过] {len(CASES)} 个用例")
    return 0


if __name__ == "__main__":
    sys.exit(main())
