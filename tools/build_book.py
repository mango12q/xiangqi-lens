# -*- coding: utf-8 -*-
"""用 Pikafish 自动生成 xqb 开局库 —— ``app_book.py`` 的配套工具。

思路
====
从初始局面做「宽度受限 + 深度受限」的树搜索：

1. 对当前局面跑 ``go depth D``（``MultiPV = W``），得到前 W 个候选着法及分数；
2. 保留分数与最优着法相差不超过 ``--margin`` 厘兵的着法，写入库；
3. 对每个保留着法递归到下一层，直到 ``--ply`` 层为止。

产出是**社区标准 xqb**（SQLite），可以直接被 XiangQiLens 的开局库面板加载，
也能被其它支持 xqb 的象棋软件读取。

用法
====
::

    # 生成一份「够用」的小库（约 1~3 分钟）
    python tools/build_book.py --out data/opening.xqb

    # 更深的库（慢）
    python tools/build_book.py --out data/opening-deep.xqb \\
        --depth 16 --ply 8 --width 4 --margin 30 --max-positions 600

    # 只校验已有库
    python tools/build_book.py --verify data/opening.xqb

参数说明
========
``--depth``   每个局面的搜索深度（越大越准，越慢）
``--ply``     树的最大层数（半回合数）
``--width``   MultiPV 宽度（每层最多考虑几个着法）
``--margin``  保留着法的分差上限（厘兵），越小越严格
``--max-positions``  全局局面数上限（防爆，默认 300）
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from app_backend import create_engine, parse_info_line          # noqa: E402
from app_book import (START_FEN, OpeningBook, board_of,          # noqa: E402
                      xqb_encode_key, xqb_encode_move)


def _board_after(fen: str, iccs: str):
    """走一步棋，返回新局面（完整 FEN）或 None。"""
    try:
        from cchess import ChessBoard
        b = ChessBoard(fen)
        if b.move_iccs(iccs) is None:
            return None
        b.next_turn()
        return b.to_full_fen()
    except Exception:
        return None


def search_position(eng, fen: str, depth: int, width: int,
                    margin: int, timeout: float) -> list[tuple[str, int]]:
    """对单个局面搜索，返回 ``[(iccs, cp), ...]``（已按分差过滤）。"""
    eng.drain()
    if width > 1:
        eng.set_option("MultiPV", width)
    else:
        eng.set_option("MultiPV", 1)
    eng.isready()
    eng.send(f"position fen {fen}")
    eng.send(f"go depth {depth}")
    lines = eng.wait_for("bestmove", timeout=timeout)

    # 每个 multipv 只保留**最深**的那一行（lowerbound/upperbound 是中间结果）
    best: dict[int, tuple[str, int, int]] = {}
    for ln in lines:
        info = parse_info_line(ln)
        if not info or info.get("bound"):
            continue
        pv = info.get("pv") or []
        cp = info.get("score_cp")
        if not pv or cp is None:
            continue
        idx = int(info.get("multipv") or 1)
        d = int(info.get("depth") or 0)
        cur = best.get(idx)
        if cur is None or d >= cur[2]:
            best[idx] = (pv[0], int(cp), d)
    if not best:
        return []

    ordered = sorted(best.values(), key=lambda t: -t[1])
    top_cp = ordered[0][1]
    out: list[tuple[str, int]] = []
    for mv, cp, _d in ordered:
        if top_cp - cp > margin:
            continue
        if mv in ("(none)", "0000"):
            continue
        out.append((mv, cp))
    return out


def build(out: str, depth: int, ply: int, width: int, margin: int,
          max_positions: int, threads: int, hash_mb: int,
          movetime_cap: float) -> int:
    """生成库并写盘，返回写入的记录数。"""
    print("=" * 72)
    print("XiangQiLens 开局库生成器")
    print("=" * 72)
    print(f"深度 {depth} · 层数 {ply} · 宽度 {width} · 分差 {margin} "
          f"· 局面上限 {max_positions} · 线程 {threads} · 哈希 {hash_mb}MB")
    print(f"输出 {out}")

    eng = create_engine(threads, hash_mb)
    queue: list[tuple[str, int]] = [(START_FEN, 0)]
    seen: set[str] = set()
    entries: list[tuple[bytes, int, int]] = []
    t0 = time.time()

    try:
        while queue and len(seen) < max_positions:
            fen, cur_ply = queue.pop(0)
            key = board_of(fen)
            if key in seen:
                continue
            seen.add(key)
            try:
                cands = search_position(eng, fen, depth, width, margin,
                                        timeout=max(20.0, movetime_cap))
            except Exception as exc:
                print(f"  [跳过] {key} 搜索失败: {exc}")
                continue
            if not cands:
                continue
            kbytes = xqb_encode_key(key)
            for mv, cp in cands:
                entries.append((kbytes, xqb_encode_move(mv), cp))
                nxt = _board_after(fen, mv)
                if nxt and cur_ply + 1 < ply:
                    queue.append((nxt, cur_ply + 1))
            print(f"  [{len(seen):4d}/{max_positions}] 层{cur_ply} {key[:24]}… "
                  f"→ {len(cands)} 着 · 队列 {len(queue)} · "
                  f"{time.time() - t0:.0f}s")
    finally:
        try:
            eng.send("quit")
            eng.proc.stdin.close()
        except Exception:
            pass

    out_path = Path(out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()
    conn = sqlite3.connect(str(out_path))
    try:
        conn.execute(
            'CREATE TABLE "book" ('
            '"id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL, '
            '"key" BLOB, "move" INTEGER, "score" INTEGER, "win" INTEGER, '
            '"draw" INTEGER, "lost" INTEGER, "valid" INTEGER, "memo" TEXT)')
        conn.execute('CREATE INDEX "idxkey" ON "book" ("key" ASC)')
        conn.execute('CREATE TABLE "information" ("name" TEXT, "value" TEXT)')
        conn.execute('INSERT INTO information VALUES (?, ?)', ("version", "1"))
        conn.execute('INSERT INTO information VALUES (?, ?)', ("type", "xiangqi"))
        conn.execute('INSERT INTO information VALUES (?, ?)',
                     ("generator", f"XiangQiLens build_book depth={depth} "
                                   f"ply={ply} width={width} margin={margin}"))
        conn.executemany(
            "INSERT INTO book (key, move, score, win, draw, lost, valid) "
            "VALUES (?, ?, ?, 0, 0, 0, 1)",
            [(sqlite3.Binary(k), m, cp) for k, m, cp in entries])
        conn.commit()
    finally:
        conn.close()

    print("-" * 72)
    print(f"完成：{len(seen)} 个局面 · {len(entries)} 条记录 · "
          f"用时 {time.time() - t0:.0f}s · {out_path}")
    return len(entries)


def verify(path: str) -> int:
    """读回库文件做一次自洽校验（键/着法解码 + 起始局面能否命中）。"""
    print(f"校验 {path}")
    bk = OpeningBook(path)
    print(f"  类型: {bk.kind} · {bk.describe()}")
    hits = bk.probe(START_FEN)
    print(f"  起始局面命中 {len(hits)} 个着法: "
          f"{[h['move'] for h in hits]}")
    ok = True
    for h in hits:
        if len(h["move"]) != 4 or not h["move"].isalnum():
            print(f"  [FAIL] 非法着法 {h}")
            ok = False
    if not hits:
        print("  [WARN] 起始局面无命中（空库或只收录了中局）")
    print("  校验", "通过" if ok else "失败")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="用 Pikafish 生成 xqb 开局库")
    ap.add_argument("--out", default=str(HERE / "data" / "opening.xqb"),
                    help="输出 .xqb 路径")
    ap.add_argument("--depth", type=int, default=12, help="每个局面的搜索深度")
    ap.add_argument("--ply", type=int, default=6, help="树的最大层数（半回合）")
    ap.add_argument("--width", type=int, default=3, help="MultiPV 宽度")
    ap.add_argument("--margin", type=int, default=40,
                    help="保留着法的分差上限（厘兵）")
    ap.add_argument("--max-positions", type=int, default=300,
                    help="全局局面数上限（防爆）")
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--hash", type=int, default=256, help="哈希 MB")
    ap.add_argument("--timeout", type=float, default=30.0,
                    help="单个局面搜索超时（秒）")
    ap.add_argument("--verify", default=None, help="只校验已有库并退出")
    args = ap.parse_args()

    if args.verify:
        return verify(args.verify)
    n = build(args.out, args.depth, args.ply, args.width, args.margin,
              args.max_positions, args.threads, args.hash, args.timeout)
    if n <= 0:
        print("[失败] 没有生成任何记录")
        return 1
    return verify(args.out)


if __name__ == "__main__":
    raise SystemExit(main())
