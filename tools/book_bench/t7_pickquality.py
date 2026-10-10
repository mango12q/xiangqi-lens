# -*- coding: utf-8 -*-
"""T7 选取质量：应用的 ``pick(fen,"best")`` 到底选出多好的一手？

为什么这是最关键的一测
----------------------
应用的自动走棋用的是 ``OpeningBook.pick(fen,"best",legal_check)``，
它**只看权重**（``app_book._weight_of`` = ``vwin*2+vdraw``，都为 0 时退到 ``vscore``），
不看引擎分数。所以：

* 若某个库的权重列语义不对（例如 ``vwin`` 是脏数据、或全库权重相同），
  ``pick(best)`` 就会挑到一个**很差**的着法 —— 而用户是直接自动落子的。
* 本测试量化「选取遗憾」：库自己给出的最好着法（按引擎分）与 ``pick`` 实际
  选出的着法之间的分差。

同时报告：候选数、权重是否退化（全部相同 → 等于随机挑）。
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from bookio import Book  # noqa: E402
from common import BOOKS, BOOK_LABEL, Engine  # noqa: E402

OUT = HERE / "out"
OUT.mkdir(exist_ok=True)
MAX_CANDS = 14


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--nodes", type=int, default=120_000)
    ap.add_argument("--positions", type=int, default=400)
    ap.add_argument("--max-ply", type=int, default=14)
    ap.add_argument("--tag", default="main")
    a = ap.parse_args()

    d = json.loads((OUT / "t2_replay.json").read_text(encoding="utf-8"))
    pool: list[tuple[str, int]] = []
    for ply_s, fens in d.get("reference_samples", {}).items():
        if int(ply_s) <= a.max_ply:
            pool += [(f, int(ply_s)) for f in fens]
    rng = random.Random(999)
    rng.shuffle(pool)

    books = {k: Book(k) for k in BOOKS}
    eng = Engine(threads=2, hash_mb=512)
    cache: dict[tuple[str, str], int] = {}
    stats = {k: {"pos": 0, "multi": 0, "degenerate": 0, "regrets": [],
                 "pick_worst": 0, "ply_regret": {}, "weight_sets": 0,
                 "cand_counts": []} for k in BOOKS}
    used = 0
    t0 = time.time()
    for fen, ply in pool:
        if used >= a.positions:
            break
        cands: dict[str, list[dict]] = {}
        any_multi = False
        for k, bk in books.items():
            e = bk.moves(fen, legal_only=True)
            cands[k] = e
            if len(e) >= 2:
                any_multi = True
        if not any_multi:
            continue
        used += 1
        need = set()
        for k, e in cands.items():
            for x in e[:MAX_CANDS]:
                need.add(x["move"])
        for mv in sorted(need):
            if (fen, mv) not in cache:
                cache[(fen, mv)] = eng.analyse(fen, nodes=a.nodes,
                                               searchmoves=mv)["score"]
        for k, e in cands.items():
            s = stats[k]
            s["pos"] += 1
            if len(e) < 2:
                continue
            s["multi"] += 1
            s["cand_counts"].append(len(e))
            top = e[:MAX_CANDS]
            weights = {x["weight"] for x in top}
            if len(weights) == 1:
                s["degenerate"] += 1
            s["weight_sets"] += len(weights)
            scores = {x["move"]: cache[(fen, x["move"])] for x in top}
            best_cand = max(scores.values())
            pick = max(e, key=lambda x: x["weight"])
            pick_score = cache.get((fen, pick["move"]))
            if pick_score is None:
                pick_score = eng.analyse(fen, nodes=a.nodes,
                                         searchmoves=pick["move"])["score"]
                cache[(fen, pick["move"])] = pick_score
            regret = max(0, best_cand - pick_score)
            s["regrets"].append(regret)
            s["ply_regret"].setdefault(ply, []).append(regret)
            if regret >= max(1, 0) and pick_score == min(scores.values()):
                s["pick_worst"] += 1
        if used % 50 == 0:
            print(f"  {used} 个多候选局面  {time.time()-t0:.0f}s", flush=True)

    eng.close()
    for bk in books.values():
        bk.close()

    report = {"nodes": a.nodes, "positions_used": used, "books": {}}
    for k, s in stats.items():
        regs = s["regrets"]
        n = len(regs)
        rep = {"label": BOOK_LABEL[k], "positions": s["pos"],
               "multi_candidate": s["multi"],
               "mean_candidates": (round(statistics.mean(s["cand_counts"]), 2)
                                   if s["cand_counts"] else None),
               "degenerate_weight_rate": round(s["degenerate"] / max(1, n), 4),
               "mean_distinct_weights": (round(s["weight_sets"] / max(1, n), 2)
                                         if n else None)}
        if n:
            rep.update({
                "mean_regret": round(statistics.mean(regs), 1),
                "median_regret": round(statistics.median(regs), 1),
                "p90_regret": round(sorted(regs)[int(0.9 * (n - 1))], 1),
                "max_regret": max(regs),
                "zero_regret": round(sum(1 for r in regs if r == 0) / n, 4),
                "over_30": round(sum(1 for r in regs if r > 30) / n, 4),
                "over_100": round(sum(1 for r in regs if r > 100) / n, 4),
                "picked_worst": round(s["pick_worst"] / n, 4),
            })
        rep["ply_mean_regret"] = {str(p): round(statistics.mean(v), 1)
                                  for p, v in sorted(s["ply_regret"].items()) if v}
        report["books"][k] = rep
        print(f"[{k}] 多候选 {rep['multi_candidate']}  平均遗憾 {rep.get('mean_regret')}cp  "
              f"中位 {rep.get('median_regret')}  0遗憾 {rep.get('zero_regret')}  "
              f"退化权重 {rep['degenerate_weight_rate']}", flush=True)

    (OUT / f"t7_pickquality_{a.tag}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("已写出", OUT / f"t7_pickquality_{a.tag}.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
