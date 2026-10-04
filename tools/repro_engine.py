# -*- coding: utf-8 -*-
"""最小复现：对比 engine_client.UciEngine 与直接 subprocess 的行为差异。

用于定位 analyse() 中 stdin 写入报 OSError 22 的原因。
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

ENGINE = r"D:\opencode\xq_research\pikafish\Pikafish-Windows-x86-64-universal.exe"
CWD = str(Path(ENGINE).parent)
FEN = "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR w - - 0 1"

print("=" * 72)
print("最小复现：UciEngine vs 原始 subprocess")
print("=" * 72)

# ------------------------------------------------------------------
print()
print("[A] 原始 subprocess（已知可用）")
proc = subprocess.Popen(
    [ENGINE], cwd=CWD,
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    text=True, encoding="utf-8", errors="replace", bufsize=1,
)
proc.stdin.write("uci\n")
proc.stdin.flush()
t0 = time.time()
while time.time() - t0 < 10:
    ln = proc.stdout.readline()
    if not ln or ln.strip() == "uciok":
        break
proc.stdin.write(f"position fen {FEN}\n")
proc.stdin.write("go movetime 500\n")
proc.stdin.flush()
while True:
    ln = proc.stdout.readline()
    if not ln or ln.startswith("bestmove"):
        print(f"    bestmove: {ln.strip() if ln else '(EOF)'}")
        break
proc.stdin.write("quit\n")
proc.stdin.flush()
try:
    proc.wait(timeout=5)
except Exception:
    proc.kill()
print("    [OK] 原始 subprocess 正常")

# ------------------------------------------------------------------
print()
print("[B] app_backend.create_engine（生产路径）")
try:
    from app_backend import create_engine, ENGINE_EXE
    print(f"    引擎路径: {ENGINE_EXE}")
    print(f"    存在: {ENGINE_EXE.is_file()}")
    e = create_engine(threads=8, hash_mb=256)
    print(f"    启动 OK，id 行数={len(e.id_lines)}  选项数={len(e.option_lines)}")
    print(f"    proc.poll() = {e.proc.poll()}")
    time.sleep(0.3)
    print(f"    0.3s 后 poll() = {e.proc.poll()}")
    try:
        e.isready()
        print("    isready OK")
    except OSError as exc:
        print(f"    [失败] isready 报错: {exc}")
        print(f"    poll() = {e.proc.poll()}")
        err = ""
        try:
            err = e.proc.stderr.read() if e.proc.stderr else ""
        except Exception:
            pass
        print(f"    stderr: {err[:500]}")
        sys.exit(1)
    r = e.analyse(FEN, movetime_ms=500)
    print(f"    analyse: bestmove={r['bestmove']} score={r['score_cp']} depth={r['depth']}")
    e.quit()
    print("    [OK] create_engine 正常")
except OSError as exc:
    print(f"    [失败] {type(exc).__name__}: {exc}")
    import traceback
    traceback.print_exc()
    sys.exit(1)

print()
print("=" * 72)
print("两者都正常 —— 说明问题不在 UciEngine 本身")
