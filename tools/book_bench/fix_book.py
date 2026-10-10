# -*- coding: utf-8 -*-
"""生成「修好的」开局库副本（**不改应用代码，也不动原库**）。

修三件事
--------
1. **字节序**：把 vmove 的两个字节交换，让应用层 ``obk_decode_move`` 能正确解码。
   （只对判定为 ``swapped`` 的库做，例如「先锋无敌nn库241111」。）
2. **索引漏行**：``REINDEX`` 重建 ``idxkey``。实测修好后
   ``integrity_check`` 全部 ``ok``，且原本查不到的 INTEGER 键行全部回来。
3. **REAL 键全表扫**：加表达式索引 ``idx_real(CAST(vkey AS REAL))``，
   让应用层兜底 SQL 也走索引 —— 实测 178ms → 0.1ms。

原库只读打开，绝不修改；输出到独立目录。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from bookio import detect_orientation  # noqa: E402
from common import BOOKS, BOOK_LABEL, book_path  # noqa: E402

OUT = HERE / "out"
OUT.mkdir(exist_ok=True)


def fix_one(key: str, outdir: Path, swap: bool | None = None) -> dict:
    src = book_path(key)
    orient = detect_orientation(key)
    do_swap = (orient == "swapped") if swap is None else swap
    dst = outdir / src.name
    t0 = time.time()
    shutil.copy2(src, dst)
    copy_s = time.time() - t0

    c = sqlite3.connect(str(dst))
    t0 = time.time()
    swapped = 0
    if do_swap:
        # vmove = (from<<8)|to  →  (to<<8)|from
        cur = c.execute(
            'UPDATE bhobk SET vmove = ((vmove & 255) << 8) | ((vmove >> 8) & 255)')
        swapped = cur.rowcount
        c.commit()
    swap_s = time.time() - t0

    t0 = time.time()
    c.execute("REINDEX")
    c.commit()
    reindex_s = time.time() - t0

    t0 = time.time()
    c.execute("CREATE INDEX IF NOT EXISTS idx_real ON bhobk(CAST(vkey AS REAL))")
    c.commit()
    expr_s = time.time() - t0

    integ = [r[0] for r in c.execute("PRAGMA integrity_check")][:3]
    c.close()
    return {"label": BOOK_LABEL[key], "src": str(src), "dst": str(dst),
            "orientation": orient, "swapped_rows": swapped,
            "copy_s": round(copy_s, 1), "swap_s": round(swap_s, 1),
            "reindex_s": round(reindex_s, 1), "expr_index_s": round(expr_s, 1),
            "integrity": integ, "size_mib": round(dst.stat().st_size / 1048576, 1)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", default=str(Path(r"D:\opencode\XiangQiLink\开局库") /
                                           "_fixed"))
    ap.add_argument("--only", default=",".join(BOOKS))
    a = ap.parse_args()
    outdir = Path(a.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    print(f"输出目录: {outdir}\n")

    report = {}
    for key in [x.strip() for x in a.only.split(",") if x.strip()]:
        r = fix_one(key, outdir)
        report[key] = r
        print(f"[{r['label']}]")
        print(f"  字节序 {r['orientation']}  交换行数 {r['swapped_rows']:,}")
        print(f"  复制 {r['copy_s']}s  交换 {r['swap_s']}s  "
              f"REINDEX {r['reindex_s']}s  表达式索引 {r['expr_index_s']}s")
        print(f"  integrity_check {r['integrity']}   输出 {r['size_mib']} MiB")
        print(f"  → {r['dst']}\n")
    (OUT / "fix_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("已写出", OUT / "fix_report.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
