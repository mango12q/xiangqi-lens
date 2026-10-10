# -*- coding: utf-8 -*-
"""交叉验证：FastBook（内存位重解释）与 应用层 SQLite 路径 是否一致。

这是必须做的一致性检查 —— 两条路径对同一个局面必须给出同一批着法，
否则前面所有覆盖率/质量数字都不可信。
"""
from __future__ import annotations

import sqlite3
import struct
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from bookio import Book, FastBook, detect_orientation, swapped_move  # noqa: E402
from common import BOOKS, BOOK_LABEL, START_FEN, apply_move  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app_book import OpeningBook, obk_decode_move, obk_zhash  # noqa: E402


def main() -> int:
    for key in BOOKS:
        fb = FastBook(key)
        orient = detect_orientation(key)
        k0 = obk_zhash(START_FEN)
        vms, ws = fb.probe_key(k0)
        print("=" * 76)
        print(f"{BOOK_LABEL[key]}   起始局面 key={k0}  字节序={orient}")
        print(f"  FastBook 命中行数: {len(vms)}")
        if len(vms):
            dec = [(swapped_move(int(v)) if orient == "swapped"
                    else obk_decode_move(int(v))) for v in vms]
            legal = [m for m in dec if m and apply_move(START_FEN, m)]
            print(f"    解码后: {dec[:8]}{' ...' if len(dec) > 8 else ''}")
            print(f"    其中合法: {len(legal)}/{len(dec)}")

        # 应用层
        ob = OpeningBook(Path(r"D:\opencode\XiangQiLink\开局库") / BOOKS[key])
        ents = ob.probe(START_FEN)
        print(f"  应用层 probe 命中行数: {len(ents)}  {[e['move'] for e in ents][:8]}")
        ob.close()

        # 原始 SQL 三条路径
        conn = sqlite3.connect(
            f"file:{(Path(r'D:\opencode\XiangQiLink\开局库') / BOOKS[key]).as_posix()}?mode=ro",
            uri=True)
        signed = k0 - (1 << 64) if k0 >= (1 << 63) else k0
        dbl = struct.unpack("<d", struct.pack("<Q", k0))[0]
        for label, sql, par in (
                ("vkey = int", "SELECT COUNT(*) FROM bhobk WHERE vkey = ?", (signed,)),
                ("CAST(vkey AS REAL) = dbl",
                 "SELECT COUNT(*) FROM bhobk WHERE CAST(vkey AS REAL) = ?", (dbl,)),
                ("+vkey = int (强制全表扫)",
                 "SELECT COUNT(*) FROM bhobk WHERE +vkey = ?", (signed,)),
                ("typeof 分布",
                 "SELECT typeof(vkey), COUNT(*) FROM bhobk GROUP BY 1", None)):
            try:
                if par is None:
                    r = conn.execute(sql).fetchall()
                else:
                    r = conn.execute(sql, par).fetchall()
                print(f"  SQL {label}: {r}")
            except Exception as exc:
                print(f"  SQL {label}: ERR {type(exc).__name__}: {exc}")
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
