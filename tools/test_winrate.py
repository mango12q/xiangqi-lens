# -*- coding: utf-8 -*-
"""验证胜率解析与格式化（WDL）。"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from app_backend import format_winrate, parse_wdl, winrate_from_cp

# 真实的引擎输出行（实测抓取）
REAL_LINES = [
    "info depth 16 seldepth 24 multipv 1 score cp 27 wdl 72 917 11 nodes 1009457 "
    "nps 6192987 hashfull 34 tbhits 0 time 163 pv b2e2 b9c7 c3c4 a9b9",
    "info depth 14 seldepth 32 multipv 1 score cp 26 wdl 70 918 12 nodes 658104 "
    "nps 6327923 hashfull 20 tbhits 0 time 104 pv c3c4 h7e7",
    "info depth 15 seldepth 26 multipv 1 score cp 30 nodes 907941 nps 6218773 "
    "hashfull 28 tbhits 0 time 146 pv g3g4 c7c5",   # 无 wdl
]


def main() -> int:
    print("=" * 72)
    print("胜率（WDL）解析与格式化验证")
    print("=" * 72)
    fails = 0

    print()
    print("[1] parse_wdl 解析")
    for i, line in enumerate(REAL_LINES, 1):
        got = parse_wdl(line)
        exp = None
        if " wdl " in line:
            seg = line.split(" wdl ", 1)[1].split()
            exp = (int(seg[0]), int(seg[1]), int(seg[2]))
        ok = got == exp
        print(f"    行{i}: {got}  期望 {exp}  {'OK' if ok else '!!'}")
        fails += 0 if ok else 1

    print()
    print("[2] format_winrate —— 红方视角（我方执红）")
    wdl = (72, 917, 11)
    s = format_winrate(wdl, 27, my_side="w")
    print(f"    wdl={wdl} 我方执红 → {s}")
    ok = "7%" in s or "6%" in s
    print(f"    红方胜率应约 7%  {'OK' if ok else '!!'}")
    fails += 0 if ok else 1

    print()
    print("[3] format_winrate —— 黑方视角（我方执黑，同一局面）")
    s2 = format_winrate(wdl, 27, my_side="b")
    print(f"    wdl={wdl} 我方执黑 → {s2}")
    # 黑方视角：负率 11/1000 ≈ 1.1%
    ok2 = "1%" in s2
    print(f"    黑方胜率应约 1%  {'OK' if ok2 else '!!'}")
    fails += 0 if ok2 else 1

    print()
    print("[4] 和棋倾向判定")
    s3 = format_winrate((72, 917, 11), 27, "w")
    ok3 = "和棋倾向" in s3
    print(f"    和率 92% 应判为「和棋倾向」 → {'OK' if ok3 else '!!'}")
    fails += 0 if ok3 else 1

    print()
    print("[5] 优劣判定")
    cases = [
        ((800, 150, 50), "我方大优", "w"),
        ((50, 150, 800), "我方劣势", "w"),
        ((300, 400, 300), "均势", "w"),
        ((50, 150, 800), "我方大优", "b"),      # 同一局面，黑方视角
    ]
    for wdl_c, expect, side in cases:
        got = format_winrate(wdl_c, None, side)
        ok_c = expect in got
        print(f"    wdl={wdl_c} 我方={'红' if side=='w' else '黑'} → {got}")
        print(f"      期望含「{expect}」 {'OK' if ok_c else '!!'}")
        fails += 0 if ok_c else 1

    print()
    print("[6] 无 wdl 时用 cp 兜底估算")
    for cp in (0, 100, 300, -300, 1000):
        s_c = format_winrate(None, cp, "w")
        print(f"    cp={cp:+5} → {s_c}")
    ok6 = format_winrate(None, 0, "w").startswith("我方胜率 5")
    print(f"    cp=0 应约 50%  {'OK' if ok6 else '!!'}")
    fails += 0 if ok6 else 1

    print()
    print("[7] winrate_from_cp 单调性（分数越高胜率越高）")
    prev = -1.0
    mono = True
    for cp in range(-500, 501, 100):
        p = winrate_from_cp(cp)
        if p < prev:
            mono = False
        prev = p
    print(f"    单调递增 ? {mono}")
    fails += 0 if mono else 1

    print()
    print("=" * 72)
    if fails:
        print(f"[失败] {fails} 项未通过")
        return 1
    print("[全部通过] 胜率解析与格式化正确")
    return 0


if __name__ == "__main__":
    sys.exit(main())
