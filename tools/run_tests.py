# -*- coding: utf-8 -*-
"""跑遍 tools/test_*.py 并汇总退出码 —— 「加 CI」的前置条件。

为什么需要它：25+ 个测试脚本里有一批**必须有真实对局窗口 / 模型 / 引擎**
才能跑。在那之前它们直接 ``return 1``，于是 CI 一上线就是 6 个假红 ——
而假红比没有 CI 更糟，它会训练维护者忽略 CI 失败。

现在的约定（见 tools/_common.py）：
* **通过** = 退出码 0，且输出里没有 ``[跳过]``
* **跳过** = 退出码 0，且输出里有 ``[跳过]``（环境不具备，不是回归）
* **失败** = 退出码非 0

用法::

    python tools/run_tests.py                 # 全部
    python tools/run_tests.py --list          # 只列出会跑哪些脚本
    python tools/run_tests.py -k book         # 只跑名字含 book 的
    python tools/run_tests.py -v              # 打印每个脚本的完整输出
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def discover(pattern: str = "test_*.py") -> list[Path]:
    return sorted(HERE.glob(pattern))


def run_one(path: Path, timeout: float, verbose: bool) -> tuple[str, str, float]:
    """返回 ``(状态, 说明, 耗时秒)``；状态 ∈ {'PASS','SKIP','FAIL','TIMEOUT'}。"""
    t0 = time.perf_counter()
    try:
        p = subprocess.run([sys.executable, str(path)], cwd=str(ROOT),
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return "TIMEOUT", f">{timeout:.0f}s", time.perf_counter() - t0
    dt = time.perf_counter() - t0
    out = (p.stdout or "") + (p.stderr or "")
    if verbose:
        print("-" * 78)
        print(f"$ python {path.name}")
        print(out.rstrip())
        print("-" * 78)

    if p.returncode != 0:
        # 失败时把最后几行贴出来，否则只看到一个 rc 没法排查
        tail = [ln for ln in out.strip().splitlines() if ln.strip()][-4:]
        return "FAIL", f"rc={p.returncode} | " + " / ".join(tail), dt

    if "[跳过]" in out:
        reason = ""
        for ln in out.splitlines():
            if "[跳过]" in ln:
                reason = ln.split("[跳过]", 1)[1].strip()
                break
        return "SKIP", reason, dt

    return "PASS", "", dt


def main() -> int:
    ap = argparse.ArgumentParser(description="跑遍 tools/test_*.py 并汇总")
    ap.add_argument("-k", "--filter", default="", help="只跑文件名含该子串的脚本")
    ap.add_argument("--timeout", type=float, default=600.0, help="单个脚本超时秒数")
    ap.add_argument("-v", "--verbose", action="store_true", help="打印完整输出")
    ap.add_argument("--list", action="store_true", help="只列出脚本，不执行")
    args = ap.parse_args()

    scripts = discover()
    if args.filter:
        scripts = [p for p in scripts if args.filter in p.name]
    if not scripts:
        print("没有匹配的测试脚本")
        return 1

    if args.list:
        for p in scripts:
            print(p.name)
        print(f"\n共 {len(scripts)} 个")
        return 0

    print(f"跑 {len(scripts)} 个测试脚本（每个超时 {args.timeout:.0f}s）")
    print("=" * 78)

    results: list[tuple[str, str, str, float]] = []
    for p in scripts:
        status, note, dt = run_one(p, args.timeout, args.verbose)
        mark = {"PASS": "通过", "SKIP": "跳过",
                "FAIL": "失败", "TIMEOUT": "超时"}[status]
        print(f"  [{mark}] {p.name:<34} {dt:6.1f}s  {note}")
        results.append((status, p.name, note, dt))

    counts = {k: sum(1 for r in results if r[0] == k)
              for k in ("PASS", "SKIP", "FAIL", "TIMEOUT")}
    print("=" * 78)
    print(f"通过 {counts['PASS']} / 跳过 {counts['SKIP']} / "
          f"失败 {counts['FAIL']} / 超时 {counts['TIMEOUT']}  "
          f"（共 {len(results)}，总耗时 {sum(r[3] for r in results):.0f}s）")

    bad = counts["FAIL"] + counts["TIMEOUT"]
    if bad:
        print()
        print("失败清单：")
        for status, name, note, _ in results:
            if status in ("FAIL", "TIMEOUT"):
                print(f"  {name}: {note}")
        return 1

    print("全部通过（跳过项属于环境不具备，不算回归）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
