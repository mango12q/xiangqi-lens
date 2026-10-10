# -*- coding: utf-8 -*-
"""T1c 索引损坏的实战影响：静香库 / 屠龙库 的 REAL 键查询是否真的可用。

obk 生成工具在键 >= 2^63 时把键按 IEEE double 存成 REAL；由于 64 位整数
超出 double 的 53 位精确表示范围，值被舍入，`WHERE vkey = ?` 走索引查不到，
必须 `CAST(vkey AS REAL) = ?` 全表扫。这里验证：
  * 索引路径命中多少
  * 全表扫兜底路径命中多少 / 是否抛异常
"""
from __future__ import annotations

import sqlite3
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import BOOKS, BOOK_LABEL, START_FEN, book_path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app_book import obk_zhash  # noqa: E402
from common import apply_move  # noqa: E402


def _walk(moves):
    fen = START_FEN
    for mv in moves:
        fen = apply_move(fen, mv)
    return fen


FENS = [
    ("起始局面", START_FEN),
    ("炮二平五后", _walk(["h2e2"])),
    ("马二进三后", _walk(["h0g2"])),
    ("中炮屏风马", _walk(["h2e2", "h9g7", "h0g2", "b9c7"])),
]


def main() -> int:
    for key, fname in BOOKS.items():
        p = book_path(key)
        print("=" * 72)
        print(f"{BOOK_LABEL[key]}  ({fname})")
        conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
        from app_book import OpeningBook
        bk = OpeningBook(p)
        for label, fen in FENS:
            k = obk_zhash(fen)
            signed = k - (1 << 64) if k >= (1 << 63) else k
            dbl = struct.unpack("<d", struct.pack("<Q", k))[0]
            row = {"key": k, "ge_2_63": k >= (1 << 63)}
            t0 = time.time()
            try:
                r1 = conn.execute(
                    "SELECT COUNT(*) FROM bhobk WHERE vkey = ?", (signed,)).fetchone()[0]
                row["idx_hits"] = r1
            except Exception as exc:
                row["idx_hits"] = f"ERR {type(exc).__name__}: {exc}"
            row["idx_ms"] = round(1000 * (time.time() - t0), 1)

            t0 = time.time()
            try:
                r2 = conn.execute(
                    "SELECT COUNT(*) FROM bhobk WHERE CAST(vkey AS REAL) = ?",
                    (dbl,)).fetchone()[0]
                row["scan_hits"] = r2
            except Exception as exc:
                row["scan_hits"] = f"ERR {type(exc).__name__}: {exc}"
            row["scan_ms"] = round(1000 * (time.time() - t0), 1)

            # 通过应用层真实探测（冷查询，无缓存）
            t0 = time.time()
            try:
                ents = bk.probe(fen)
                row["app_moves"] = [e["move"] for e in ents]
            except Exception as exc:
                row["app_moves"] = f"ERR {type(exc).__name__}: {exc}"
            row["app_ms"] = round(1000 * (time.time() - t0), 1)
            print(f"  [{label}] key>2^63={row['ge_2_63']}  idx={row['idx_hits']} "
                  f"({row['idx_ms']}ms)  scan={row['scan_hits']} ({row['scan_ms']}ms)  "
                  f"app={row['app_moves']} ({row['app_ms']}ms)")
        bk.close()
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
