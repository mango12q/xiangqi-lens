# -*- coding: utf-8 -*-
"""T3b 修复验证：``REINDEX`` 能不能修好「索引漏行」造成的静默丢数据。

不改动用户的原库：复制到临时目录再操作。
只读打开原库 → ``VACUUM INTO`` 复制 → 在副本上 ``REINDEX`` → 重新验证。
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
]


def probe_all(conn, fen):
    K = obk_zhash(fen)
    signed = K - (1 << 64) if K >= (1 << 63) else K
    bitdbl = struct.unpack("<d", struct.pack("<Q", K))[0]
    idx = conn.execute("SELECT COUNT(*) FROM bhobk WHERE vkey = ?",
                       (signed,)).fetchone()[0]
    scan = conn.execute("SELECT COUNT(*) FROM bhobk WHERE +vkey = ?",
                        (signed,)).fetchone()[0]
    real = conn.execute("SELECT COUNT(*) FROM bhobk WHERE CAST(vkey AS REAL) = ?",
                        (bitdbl,)).fetchone()[0]
    return idx, scan, real


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="bookbench_"))
    report = {}
    try:
        for key in BOOKS:
            src = book_path(key)
            print("=" * 78)
            print(f"{BOOK_LABEL[key]}  ({src.stat().st_size/1048576:.1f} MiB)")
            dst = tmp / src.name

            # ① 原库（不动它）作为「修复前」基准
            c0 = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
            before = {label: probe_all(c0, fen) for label, fen in POSITIONS}
            integ_before = [r[0] for r in c0.execute("PRAGMA integrity_check")][:2]
            c0.close()

            # ② 逐字节复制 → 在副本上 REINDEX
            t0 = time.time()
            shutil.copy2(src, dst)
            t_copy = time.time() - t0
            c2 = sqlite3.connect(str(dst))
            t0 = time.time()
            c2.execute("REINDEX")
            c2.commit()
            t_reindex = time.time() - t0
            after = {label: probe_all(c2, fen) for label, fen in POSITIONS}
            integ = [r[0] for r in c2.execute("PRAGMA integrity_check")]
            c2.close()

            print(f"  复制 {t_copy:.1f}s   REINDEX {t_reindex:.1f}s")
            print(f"  修复前 integrity_check: {integ_before}")
            print(f"  修复后 integrity_check: {integ[:2]}")
            print(f"  {'局面':<12} {'索引(前)':>9} {'索引(后)':>9} "
                  f"{'全表扫':>8} {'兜底REAL':>9}")
            for label, _ in POSITIONS:
                b, a2 = before[label], after[label]
                print(f"  {label:<12} {b[0]:>9} {a2[0]:>9} {a2[1]:>8} {a2[2]:>9}")
            report[key] = {"copy_s": round(t_copy, 1),
                           "reindex_s": round(t_reindex, 1),
                           "integrity_before": integ_before,
                           "integrity_after": integ[:3],
                           "before": {k: list(v) for k, v in before.items()},
                           "after": {k: list(v) for k, v in after.items()}}
            dst.unlink()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    (OUT / "t3b_reindex.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n已写出", OUT / "t3b_reindex.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
