# -*- coding: utf-8 -*-
"""测试 Pikafish 的 UCI_ShowWDL 选项，看能否拿到胜/和/负概率。

如果可用，界面就能显示人类可读的胜率（如"红方 58% 胜"），
比裸的 centipawn 分数直观得多。
"""
from __future__ import annotations

import subprocess
import time

EXE = r"D:\opencode\xq_research\pikafish\Pikafish-Windows-x86-64-universal.exe"
CWD = r"D:\opencode\xq_research\pikafish"
FEN = "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR w - - 0 1"


def run(show_wdl: bool, label: str, depth: int = 16) -> list[str]:
    p = subprocess.Popen(
        [EXE], cwd=CWD,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    p.stdin.write("uci\n")
    p.stdin.flush()
    t0 = time.time()
    while time.time() - t0 < 15:
        ln = p.stdout.readline()
        if not ln or ln.strip() == "uciok":
            break

    p.stdin.write("setoption name Threads value 8\n")
    p.stdin.write("setoption name Hash value 128\n")
    if show_wdl:
        p.stdin.write("setoption name UCI_ShowWDL value true\n")
    p.stdin.write("isready\n")
    p.stdin.flush()
    t0 = time.time()
    while time.time() - t0 < 15:
        ln = p.stdout.readline()
        if not ln or ln.strip() == "readyok":
            break

    print(f"=== {label} (UCI_ShowWDL={show_wdl}) ===")
    p.stdin.write(f"position fen {FEN}\n")
    p.stdin.write(f"go depth {depth}\n")
    p.stdin.flush()

    infos: list[str] = []
    t0 = time.time()
    while time.time() - t0 < 60:
        ln = p.stdout.readline()
        if not ln:
            break
        ln = ln.strip()
        if ln.startswith("info") and " pv " in ln:
            infos.append(ln)
        if ln.startswith("bestmove"):
            print(f"  bestmove: {ln}")
            break

    # 只看最后几条（最深）
    for line in infos[-3:]:
        print(f"  {line[:200]}")
    has_wdl = any(" wdl " in line for line in infos)
    print(f"  → 输出中包含 wdl 字段: {has_wdl}")
    print()

    p.stdin.write("quit\n")
    p.stdin.flush()
    try:
        p.wait(timeout=5)
    except Exception:
        p.kill()
    return infos


def main() -> int:
    print("=" * 74)
    print("Pikafish UCI_ShowWDL 测试")
    print("=" * 74)
    print()

    a = run(False, "默认（不开启）")
    b = run(True, "开启 WDL")

    print("=" * 74)
    if any(" wdl " in x for x in b):
        print("[结论] UCI_ShowWDL 有效，可以拿到胜/和/负概率")
        # 提取一条示例
        for line in b:
            if " wdl " in line:
                seg = line.split(" wdl ", 1)[1].split()
                w, d, l = int(seg[0]), int(seg[1]), int(seg[2])
                total = w + d + l
                print(f"       示例: 胜 {w} / 和 {d} / 负 {l}  (共 {total})")
                print(f"       → 胜率 {w/total:.1%}  和率 {d/total:.1%}  负率 {l/total:.1%}")
                break
        return 0
    print("[结论] UCI_ShowWDL 无效或未生效")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
