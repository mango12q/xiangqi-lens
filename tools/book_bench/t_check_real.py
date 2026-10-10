# -*- coding: utf-8 -*-
"""诊断：REAL 键到底存的是「位重解释」还是「数值转换」。

这决定了应用层兜底 SQL ``CAST(vkey AS REAL) = ?``（把键的位重解释成 double）
能不能命中。两种约定：
  * 位重解释：stored = struct.unpack('<d', struct.pack('<Q', K))[0]
  * 数值转换：stored = float(K)   ← 会丢精度（K > 2^53）
"""
from __future__ import annotations

import sqlite3
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from common import BOOKS, BOOK_LABEL, START_FEN, apply_move, book_path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app_book import obk_zhash  # noqa: E402


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
    ("起马对挺卒", walk(["h0g2", "g6g5"])),
    ("飞相局", walk(["g0e2", "h9g7"])),
]


def main() -> int:
    for key in BOOKS:
        p = book_path(key)
        conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
        print("=" * 78)
        print(f"{BOOK_LABEL[key]}")
        for label, fen in POSITIONS:
            K = obk_zhash(fen)
            signed = K - (1 << 64) if K >= (1 << 63) else K
            bitdbl = struct.unpack("<d", struct.pack("<Q", K))[0]
            numdbl = float(K)
            same = (struct.pack("<d", bitdbl) == struct.pack("<d", numdbl))

            def cnt(sql, par):
                try:
                    return conn.execute(sql, par).fetchone()[0]
                except Exception as exc:
                    return f"ERR {type(exc).__name__}"

            idx = cnt("SELECT COUNT(*) FROM bhobk WHERE vkey = ?", (signed,))
            scan = cnt("SELECT COUNT(*) FROM bhobk WHERE +vkey = ?", (signed,))
            real_bit = cnt("SELECT COUNT(*) FROM bhobk WHERE CAST(vkey AS REAL) = ?",
                           (bitdbl,))
            real_num = cnt("SELECT COUNT(*) FROM bhobk WHERE CAST(vkey AS REAL) = ?",
                           (numdbl,))
            real_eq_num = cnt(
                "SELECT COUNT(*) FROM bhobk WHERE typeof(vkey)='real' AND vkey = ?",
                (numdbl,))
            types = conn.execute(
                "SELECT typeof(vkey), COUNT(*) FROM bhobk WHERE +vkey = ? GROUP BY 1",
                (signed,)).fetchall()
            print(f"  [{label}] K={K} bit==num({same})")
            print(f"      索引 vkey=int: {idx}   全表 +vkey=int: {scan}   行类型 {types}")
            print(f"      兜底 CAST=位重解释: {real_bit}   兜底 CAST=数值转换: {real_num}   "
                  f"REAL且=float(K): {real_eq_num}")
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
