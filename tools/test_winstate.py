# -*- coding: utf-8 -*-
"""验证窗口状态检测：最小化识别、含最小化窗口的枚举。"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from app_backend import list_windows_any, window_state


def main() -> int:
    print("=" * 74)
    print("窗口状态检测验证")
    print("=" * 74)

    print()
    print("[1] 枚举所有窗口（含最小化的）")
    wins = list_windows_any(min_size=150)
    print(f"    共 {len(wins)} 个")
    targets = [w for w in wins if any(k in w["title"] for k in ("象棋", "JJ", "XiangQi"))]
    for w in targets:
        print(f"    {w['title'][:40]:42} 最小化={w['minimized']}  rect={w['rect']}")

    print()
    print("[2] window_state 判定")
    for w in targets:
        ok, mini, note = window_state(w["hwnd"])
        flag = "OK" if (ok != mini) or (ok and not mini) else "?"
        print(f"    {w['title'][:40]:42}")
        print(f"        可用={ok}  最小化={mini}  提示={note!r}  [{flag}]")

    print()
    print("[3] 各情形下的提示文案")
    cases = []
    for w in wins:
        ok, mini, note = window_state(w["hwnd"])
        if note:
            cases.append((w["title"][:34], note))
    if cases:
        for t, n in cases[:6]:
            print(f"    {t:36} → {n}")
    else:
        print("    当前没有异常窗口")

    print()
    print("[4] 不存在的句柄应报错")
    ok, mini, note = window_state(0xDEADBEEF)
    print(f"    伪造句柄: 可用={ok}  提示={note!r}")
    if ok:
        print("    [失败] 无效句柄不应判定为可用")
        return 1

    print()
    print("=" * 74)
    print("[通过] 窗口状态检测正常")
    return 0


if __name__ == "__main__":
    sys.exit(main())
