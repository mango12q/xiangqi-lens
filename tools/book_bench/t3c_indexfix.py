# -*- coding: utf-8 -*-
"""T3c 性能修复验证：表达式索引能不能消掉「REAL 键全表扫」的 170ms。

「最新屠龙商业库」约 50% 的行把键按位重解释存成 REAL，索引查不到，
应用层兜底 ``CAST(vkey AS REAL) = ?`` 是**全表扫 3.6M 行**（实测 ~170ms/次）。

修复思路：在副本上加一个**表达式索引**
    CREATE INDEX idx_real ON bhobk(CAST(vkey AS REAL));
这样兜底查询也能走索引。另外先 REINDEX 修好 idxkey 的漏行。

不改用户原库：复制到临时目录再操作。
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import struct
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from common import BOOKS, BOOK_LABEL, START_FEN, apply_move, book_path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app_book import obk_zhash  # noqa: E402

OUT = HERE / "out"
OUT.mkdir(exist_ok=True)


def walk(moves):
    fen = START_FEN
    for mv in moves:
        fen = apply_move(fen, mv)
    return fen


POSITIONS = [
    ("起始局面", START_FEN),
    ("炮二平五后", walk(["h2e2"])),
    ("马二进三后", walk(["h0g2"])),
    ("中炮屏风马", walk(["h2e2", "h9g7", "h0g2", "b9c7"])),
    ("飞相局", walk(["g0e2", "h9g7"])),
    ("起马对挺卒", walk(["h0g2", "g6g5"])),
    ("仙人指路", walk(["c3c4", "h9g7"])),
    ("过宫炮", walk(["b2e2", "h9g7"])),
]


def timed_probe(conn, fen, reps=5):
    K = obk_zhash(fen)
    signed = K - (1 << 64) if K >= (1 << 63) else K
    bitdbl = struct.unpack("<d", struct.pack("<Q", K))[0]
    # 模拟应用层 probe 的两条路径
    t0 = time.perf_counter()
    hits = 0
    for _ in range(reps):
        try:
            rows = conn.execute(
                "SELECT vmove, vwin, vdraw, vlost, vscore, vvalid FROM bhobk "
                "WHERE vkey = ?", (signed,)).fetchall()
        except Exception:
            rows = []
        if not rows:
            try:
                rows = conn.execute(
                    "SELECT vmove, vwin, vdraw, vlost, vscore, vvalid FROM bhobk "
                    "WHERE CAST(vkey AS REAL) = ?", (bitdbl,)).fetchall()
            except Exception:
                rows = []
        hits = len(rows)
    return round(1000 * (time.perf_counter() - t0) / reps, 1), hits


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="bookbench_idx_"))
    report = {}
    try:
        for key in BOOKS:
            src = book_path(key)
            print("=" * 78)
            print(f"{BOOK_LABEL[key]}  ({src.stat().st_size/1048576:.1f} MiB)")
            dst = tmp / src.name
            shutil.copy2(src, dst)

            c0 = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
            before = {lab: timed_probe(c0, fen) for lab, fen in POSITIONS}
            c0.close()

            c = sqlite3.connect(str(dst))
            t0 = time.time()
            c.execute("REINDEX")
            c.commit()
            t_re = time.time() - t0
            after_re = {lab: timed_probe(c, fen) for lab, fen in POSITIONS}
            t0 = time.time()
            try:
                c.execute("CREATE INDEX IF NOT EXISTS idx_real "
                          "ON bhobk(CAST(vkey AS REAL))")
                c.commit()
                t_expr = time.time() - t0
                expr_ok = True
            except Exception as exc:
                t_expr = time.time() - t0
                expr_ok = f"{type(exc).__name__}: {exc}"
            after_expr = {lab: timed_probe(c, fen) for lab, fen in POSITIONS}
            integ = [r[0] for r in c.execute("PRAGMA integrity_check")][:2]
            c.close()

            print(f"  REINDEX {t_re:.1f}s   表达式索引 {t_expr:.1f}s  "
                  f"(ok={expr_ok})  integrity {integ}")
            print(f"  {'局面':<12} {'原始 ms':>9} {'命中':>5} "
                  f"{'REINDEX ms':>11} {'命中':>5} {'+表达式 ms':>11} {'命中':>5}")
            for lab, _ in POSITIONS:
                b, r1, r2 = before[lab], after_re[lab], after_expr[lab]
                print(f"  {lab:<12} {b[0]:>9} {b[1]:>5} "
                      f"{r1[0]:>11} {r1[1]:>5} {r2[0]:>11} {r2[1]:>5}")
            report[key] = {"reindex_s": round(t_re, 1),
                           "expr_index_s": round(t_expr, 1),
                           "expr_ok": expr_ok,
                           "integrity_after": integ,
                           "before": {k: list(v) for k, v in before.items()},
                           "after_reindex": {k: list(v) for k, v in after_re.items()},
                           "after_expr": {k: list(v) for k, v in after_expr.items()}}
            dst.unlink()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    (OUT / "t3c_indexfix.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n已写出", OUT / "t3c_indexfix.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
