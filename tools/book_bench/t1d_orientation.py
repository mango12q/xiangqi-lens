# -*- coding: utf-8 -*-
"""T1d 着法字节序（重大发现）。

obk 的 vmove = ``(起点偏移 << 8) | 终点偏移``。但社区库的生成工具不统一：
有的库把两个字节**反着存**（``(终点 << 8) | 起点``）。此时应用层解出来的
着法全部非法，会被 ``legal_check`` 过滤干净 → **整库不可用**。

做法
----
用已知正确的「云霄剑诀」从初始局面 BFS 出一批**公共探测局面**，
再让四个库在完全相同的局面上作答，分别统计：
  * 正常解码下的合法着法数 / 覆盖局面数
  * 字节交换解码下的合法着法数 / 覆盖局面数
"""
from __future__ import annotations

import json
import sys
from collections import Counter, deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (BOOKS, BOOK_LABEL, START_FEN, apply_move, book_path,  # noqa: E402
                    fen_key)

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app_book import OpeningBook, obk_decode_move  # noqa: E402

OUT = Path(__file__).resolve().parent / "out"
OUT.mkdir(exist_ok=True)

PROBE_PLIES = 8
PROBE_CAP = 400


def swap_bytes(vmove: int) -> str | None:
    """按字节交换重新解码 vmove。"""
    try:
        v = int(vmove)
    except Exception:
        return None
    return obk_decode_move(((v & 0xFF) << 8) | ((v >> 8) & 0xFF))


def build_probe_positions() -> list[tuple[str, int]]:
    """用云霄剑诀（已验证正确）BFS 出公共探测局面。"""
    bk = OpeningBook(book_path("yunxiao"))
    seen = {fen_key(START_FEN)}
    q = deque([(START_FEN, 0)])
    out = [(START_FEN, 0)]
    while q and len(out) < PROBE_CAP:
        fen, ply = q.popleft()
        if ply >= PROBE_PLIES:
            continue
        for e in bk.probe(fen):
            nxt = apply_move(fen, e["move"])
            if nxt is None:
                continue
            k = fen_key(nxt)
            if k in seen:
                continue
            seen.add(k)
            out.append((nxt, ply + 1))
            q.append((nxt, ply + 1))
    bk.close()
    return out


def main() -> int:
    positions = build_probe_positions()
    print(f"公共探测局面: {len(positions)} 个（ply<= {PROBE_PLIES}）")
    print(f"其中初始局面 1 个，其余由云霄剑诀 BFS 得到\n")

    report = {"probe_positions": len(positions), "books": {}}
    for key, fname in BOOKS.items():
        bk = OpeningBook(book_path(key))
        raw = bk._impl.conn  # 直接拿原始 vmove，绕过解码
        table = bk._impl.table
        from app_book import obk_zhash
        import sqlite3
        import struct

        c = {"label": BOOK_LABEL[key], "file": fname,
             "pos_total": len(positions), "pos_hit": 0,
             "normal_legal": 0, "normal_illegal": 0,
             "swap_legal": 0, "swap_illegal": 0,
             "pos_with_normal_legal": 0, "pos_with_swap_legal": 0,
             "pos_with_any_entry": 0, "root_entries": 0}

        for fen, ply in positions:
            k = obk_zhash(fen)
            signed = k - (1 << 64) if k >= (1 << 63) else k
            dbl = struct.unpack("<d", struct.pack("<Q", k))[0]
            sql = (f'SELECT vmove FROM "{table}" WHERE {{}}')
            try:
                rows = raw.execute(sql.format("vkey = ?"), (signed,)).fetchall()
                if not rows:
                    rows = raw.execute(sql.format("CAST(vkey AS REAL) = ?"),
                                       (dbl,)).fetchall()
            except Exception:
                rows = []
            if rows:
                c["pos_with_any_entry"] += 1
            if ply == 0:
                c["root_entries"] = len(rows)

            n_ok = n_bad = s_ok = s_bad = 0
            for (vm,) in rows:
                if apply_move(fen, obk_decode_move(vm) or "") is not None:
                    n_ok += 1
                else:
                    n_bad += 1
                sm = swap_bytes(vm)
                if sm and apply_move(fen, sm) is not None:
                    s_ok += 1
                else:
                    s_bad += 1
            c["normal_legal"] += n_ok
            c["normal_illegal"] += n_bad
            c["swap_legal"] += s_ok
            c["swap_illegal"] += s_bad
            if n_ok:
                c["pos_with_normal_legal"] += 1
            if s_ok:
                c["pos_with_swap_legal"] += 1
        bk.close()

        tot = c["normal_legal"] + c["normal_illegal"]
        c["normal_legal_rate"] = round(c["normal_legal"] / tot, 4) if tot else None
        tot2 = c["swap_legal"] + c["swap_illegal"]
        c["swap_legal_rate"] = round(c["swap_legal"] / tot2, 4) if tot2 else None
        report["books"][key] = c
        print(f"[{BOOK_LABEL[key]}]")
        print(f"   有记录的探测局面: {c['pos_with_any_entry']}/{len(positions)}"
              f"   初始局面记录数: {c['root_entries']}")
        print(f"   正常解码: 合法 {c['normal_legal']} / 非法 {c['normal_illegal']}"
              f"  → 合法率 {c['normal_legal_rate']}"
              f"  有合法着的局面 {c['pos_with_normal_legal']}")
        print(f"   字节交换: 合法 {c['swap_legal']} / 非法 {c['swap_illegal']}"
              f"  → 合法率 {c['swap_legal_rate']}"
              f"  有合法着的局面 {c['pos_with_swap_legal']}")
        print()

    (OUT / "t1d_orientation.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("已写出", OUT / "t1d_orientation.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
