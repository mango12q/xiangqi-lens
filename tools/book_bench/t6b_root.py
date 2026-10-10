# -*- coding: utf-8 -*-
"""T6b 起始局面的库着全表：权重从哪来、pick(best) 会走哪一手。

重点核查「最新屠龙商业库」在初始局面把 ``pick(best)`` 指向了 f0e1（仕六进五），
这会是用户开局第一眼看到的着法。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from bookio import Book, FastBook, swapped_move  # noqa: E402
from common import BOOKS, BOOK_LABEL, Engine, START_FEN  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app_book import obk_decode_move, obk_zhash  # noqa: E402

OUT = HERE / "out"
OUT.mkdir(exist_ok=True)


def main() -> int:
    eng = Engine(threads=2, hash_mb=512)
    best = eng.analyse(START_FEN, nodes=300_000)
    print(f"初始局面 引擎最佳着 {best['raw_best']} 分数 {best['score']}\n")

    report = {"engine_best": best["raw_best"], "engine_score": best["score"],
              "books": {}}
    for key in BOOKS:
        bk = Book(key)
        fb = FastBook(key)
        k = obk_zhash(START_FEN)
        vms, ws = fb.probe_key(k)
        rows = []
        need = set()
        for vm, w in zip(vms, ws):
            mv = (swapped_move(int(vm)) if bk.orientation == "swapped"
                  else obk_decode_move(int(vm)))
            if not mv:
                continue
            rows.append({"vmove": int(vm), "move": mv, "weight": int(w)})
            need.add(mv)
        scored = {}
        for mv in sorted(need):
            r = eng.analyse(START_FEN, nodes=300_000, searchmoves=mv)
            scored[mv] = r["score"]
        for r in rows:
            r["score"] = scored.get(r["move"])
            r["cpl"] = (max(0, best["score"] - r["score"])
                        if r["score"] is not None else None)
        rows.sort(key=lambda r: -r["weight"])
        pick = rows[0] if rows else None
        report["books"][key] = {"label": bk.label, "orientation": bk.orientation,
                                "n": len(rows), "rows": rows,
                                "pick": pick["move"] if pick else None,
                                "pick_cpl": pick["cpl"] if pick else None}
        print("=" * 74)
        print(f"{bk.label}   候选 {len(rows)} 个   字节序 {bk.orientation}")
        print(f"  pick(best) → {pick['move'] if pick else '无'}  "
              f"CPL {pick['cpl'] if pick else '-'}")
        print(f"  {'着法':<8}{'权重':>10}{'分数':>8}{'CPL':>7}   ")
        for r in rows[:12]:
            star = " ★引擎最佳" if r["move"] == best["raw_best"] else ""
            print(f"  {r['move']:<8}{r['weight']:>10}{r['score']:>8}{r['cpl']:>7}{star}")
        if len(rows) > 12:
            print(f"  … 其余 {len(rows)-12} 个")
        bk.close()
    eng.close()
    (OUT / "t6b_root.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n已写出", OUT / "t6b_root.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
