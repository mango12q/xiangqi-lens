# -*- coding: utf-8 -*-
"""T6 实战首着清单：常见开局局面下，每个库实际会走哪一手、引擎怎么看。

这是最直观的一张表：用户在实战里第一眼看到的就是这些着法。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from bookio import Book  # noqa: E402
from common import BOOKS, BOOK_LABEL, Engine, START_FEN, apply_move  # noqa: E402

OUT = HERE / "out"
OUT.mkdir(exist_ok=True)


def walk(moves):
    fen = START_FEN
    for mv in moves:
        fen = apply_move(fen, mv)
    return fen


POSITIONS = [
    ("① 起始局面（红先）", START_FEN, []),
    ("② 中炮后（黑先）", walk(["h2e2"]), ["h2e2"]),
    ("③ 起马后（黑先）", walk(["h0g2"]), ["h0g2"]),
    ("④ 飞相后（黑先）", walk(["g0e2"]), ["g0e2"]),
    ("⑤ 仙人指路后（黑先）", walk(["c3c4"]), ["c3c4"]),
    ("⑥ 中炮屏风马后（红先）", walk(["h2e2", "h9g7", "h0g2", "b9c7"]),
     ["h2e2", "h9g7", "h0g2", "b9c7"]),
]


def main() -> int:
    books = {k: Book(k) for k in BOOKS}
    eng = Engine(threads=2, hash_mb=512)
    report = {"positions": []}
    for label, fen, line in POSITIONS:
        best = eng.analyse(fen, nodes=200_000)
        row = {"label": label, "fen": fen, "line": line,
               "engine_best": best["raw_best"], "engine_score": best["score"],
               "books": {}}
        print("=" * 78)
        print(f"{label}")
        print(f"  FEN {fen}")
        print(f"  引擎最佳着 {best['raw_best']}  分数 {best['score']}")
        need = set()
        for k, bk in books.items():
            entries = bk.moves(fen, legal_only=True)
            pick = max(entries, key=lambda e: e["weight"]) if entries else None
            row["books"][k] = {
                "label": bk.label, "n_moves": len(entries),
                "pick": pick["move"] if pick else None,
                "pick_weight": pick["weight"] if pick else None,
                "moves": sorted(e["move"] for e in entries),
            }
            if pick:
                need.add(pick["move"])
        for mv in sorted(need):
            r = eng.analyse(fen, nodes=200_000, searchmoves=mv)
            row["books"] and None
            for k in row["books"]:
                if row["books"][k]["pick"] == mv:
                    row["books"][k]["pick_score"] = r["score"]
                    row["books"][k]["pick_cpl"] = max(0, best["score"] - r["score"])
        for k, v in row["books"].items():
            if v["pick"]:
                mark = "★最佳" if v["pick"] == best["raw_best"] else ""
                print(f"    {v['label']:<20} 候选 {v['n_moves']:>2}  "
                      f"选 {v['pick']} (权重{v['pick_weight']})  "
                      f"分 {v.get('pick_score')}  差 {v.get('pick_cpl')}cp {mark}")
            else:
                print(f"    {v['label']:<20} 候选  0  —— 无可用着法")
        report["positions"].append(row)
    eng.close()
    for bk in books.values():
        bk.close()
    (OUT / "t6_picks.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n已写出", OUT / "t6_picks.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
