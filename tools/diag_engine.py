# -*- coding: utf-8 -*-
"""诊断 Pikafish 引擎启动失败原因：单独捕获 stdout / stderr。"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from _common import bootstrap  # noqa: E402

bootstrap()

# 不再硬编码本机路径：统一从 app_backend 取（它自己会按候选位置找 xq_research）
from app_backend import ENGINE_EXE  # noqa: E402

ENGINE = ENGINE_EXE
CWD = ENGINE.parent

print("=" * 70)
print("Pikafish 引擎启动诊断")
print("=" * 70)
print(f"引擎: {ENGINE}")
print(f"存在: {ENGINE.is_file()}  大小: {ENGINE.stat().st_size / 1024 / 1024:.2f} MB")
print(f"目录: {CWD}")
print()

nnue = CWD / "pikafish.nnue"
print(f"NNUE: {nnue.name}  存在: {nnue.is_file()}  "
      f"大小: {nnue.stat().st_size / 1024 / 1024:.2f} MB" if nnue.is_file() else "NNUE 缺失")
print()

print("[1] 启动引擎，分离 stdout/stderr")
try:
    proc = subprocess.Popen(
        [str(ENGINE)],
        cwd=str(CWD),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,      # 不合并，单独看
        text=True, encoding="utf-8", errors="replace", bufsize=1,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
except OSError as exc:
    print(f"    [失败] 无法启动: {exc}")
    sys.exit(1)

print(f"    PID={proc.pid}")
time.sleep(0.5)
if proc.poll() is not None:
    print(f"    [失败] 启动后立即退出，退出码 {proc.returncode}")
    err = proc.stderr.read() if proc.stderr else ""
    out = proc.stdout.read() if proc.stdout else ""
    print(f"    stderr: {err[:800]}")
    print(f"    stdout: {out[:800]}")
    sys.exit(1)
print("    进程存活")

print()
print("[2] 发送 uci")
try:
    proc.stdin.write("uci\n")
    proc.stdin.flush()
except OSError as exc:
    print(f"    [失败] 写 stdin 出错: {exc}")
    err = proc.stderr.read() if proc.stderr else ""
    print(f"    stderr: {err[:800]}")
    proc.kill()
    sys.exit(1)

lines = []
t0 = time.time()
while time.time() - t0 < 15:
    line = proc.stdout.readline()
    if not line:
        break
    line = line.rstrip()
    lines.append(line)
    if line == "uciok":
        break
print(f"    收到 {len(lines)} 行")
for ln in lines[:12]:
    print(f"      {ln}")

if "uciok" not in lines:
    print("    [失败] 未收到 uciok")
    err = proc.stderr.read() if proc.stderr else ""
    if err:
        print(f"    stderr: {err[:800]}")
    proc.kill()
    sys.exit(1)
print("    uciok 正常")

print()
print("[3] 试一步搜索（验证 NNUE 加载）")
proc.stdin.write("position fen rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR w - - 0 1\n")
proc.stdin.write("go depth 8\n")
proc.stdin.flush()
best = None
t0 = time.time()
while time.time() - t0 < 20:
    line = proc.stdout.readline()
    if not line:
        break
    if line.startswith("bestmove"):
        best = line.strip()
        break
print(f"    {best or '(未取得 bestmove)'}")

print()
print("[4] 检查 stderr 是否有错误")
try:
    proc.stdin.write("quit\n")
    proc.stdin.flush()
    proc.wait(timeout=3)
except Exception:
    proc.kill()
err = proc.stderr.read() if proc.stderr else ""
if err.strip():
    print(f"    stderr 内容:\n{err[:1000]}")
else:
    print("    stderr 为空（正常）")

print()
print("[结论] " + ("引擎工作正常" if best else "引擎存在问题"))
