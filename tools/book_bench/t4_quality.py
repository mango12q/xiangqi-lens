# -*- coding: utf-8 -*-
"""T4 着法质量：用 Pikafish 在同一批公共局面上给四个库的库着打分。

方法
----
1. 公共局面集 = T2 公共参考树（四库着法并集的 BFS）按层抽样。
   **同一批局面评所有库** —— 保证公平。
2. 每个局面：
   * 引擎 ``go nodes N`` → 最佳分 best（行棋方视角）
   * 对每个库实际会选的库着（``pick`` = 权重最高）跑
     ``go nodes N searchmoves <mv>`` → 该着分数
   * CPL = max(0, best - score)
3. 汇总：覆盖率、平均/中位 CPL、完全最佳率、CPL>200 漏着率。

尺度说明：象棋分值比国际象棋大（车 ~1000，马炮 ~450，兵 ~100），
所以档位取 0/20/50/100/200/500。
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
from common import BOOKS, BOOK_LABEL, Engine, apply_move  # noqa: E402

OUT = HERE / "out"
OUT.mkdir(exist_ok=True)
BUCKETS = [20, 50, 100, 200, 500]


def build_position_set(per_ply: int, max_ply: int) -> list[tuple[str, int]]:
    d = json.loads((OUT / "t2_replay.json").read_text(encoding="utf-8"))
    by_ply: dict[int, list[str]] = {}
    for ply_s, fens in d.get("reference_samples", {}).items():
        ply = int(ply_s)
        if ply > max_ply:
            continue
        by_ply.setdefault(ply, []).extend(fens)
    rng = random.Random(20260106)
    out = []
    for ply in sorted(by_ply):
        pool = sorted(set(by_ply[ply]))
        if len(pool) > per_ply:
            pool = rng.sample(pool, per_ply)
        out += [(f, ply) for f in pool]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--nodes", type=int, default=80_000)
    ap.add_argument("--per-ply", type=int, default=130)
    ap.add_argument("--max-ply", type=int, default=12)
    ap.add_argument("--tag", default="main")
    a = ap.parse_args()

    positions = build_position_set(a.per_ply, a.max_ply)
    print(f"公共局面集 {len(positions)} 个（ply<= {a.max_ply}，每层<= {a.per_ply}）")
    print(f"引擎节点 {a.nodes}")

    books = {k: Book(k) for k in BOOKS}
    eng = Engine(threads=2, hash_mb=512)
    score_cache: dict[tuple[str, str], int] = {}
    best_cache: dict[str, tuple[int, str]] = {}

    stats = {k: {"pos": 0, "covered": 0, "pick": [], "all": [], "illegal": 0,
                 "ply_pick": {}, "ply_cover": {}, "n_entries": 0}
             for k in BOOKS}
    t0 = time.time()

    for i, (fen, ply) in enumerate(positions):
        if fen not in best_cache:
            r = eng.analyse(fen, nodes=a.nodes)
            best_cache[fen] = (r["score"], r["raw_best"])
            score_cache[(fen, r["raw_best"])] = r["score"]
        best, best_mv = best_cache[fen]

        per_book: dict[str, list[dict]] = {}
        need: set[str] = set()
        for k, bk in books.items():
            s = stats[k]
            s["pos"] += 1
            s["ply_cover"].setdefault(ply, [0, 0])
            s["ply_cover"][ply][1] += 1
            entries = bk.moves(fen, legal_only=True)
            per_book[k] = entries
            s["n_entries"] += len(entries)
            if entries:
                s["covered"] += 1
                s["ply_cover"][ply][0] += 1
                for e in entries:
                    need.add(e["move"])

        for mv in sorted(need):
            if (fen, mv) not in score_cache:
                r = eng.analyse(fen, nodes=a.nodes, searchmoves=mv)
                score_cache[(fen, mv)] = r["score"]

        for k, entries in per_book.items():
            if not entries:
                continue
            s = stats[k]
            pick = max(entries, key=lambda e: e["weight"])
            cpl_pick = max(0, best - score_cache[(fen, pick["move"])])
            s["pick"].append((ply, cpl_pick))
            s["ply_pick"].setdefault(ply, []).append(cpl_pick)
            for e in entries:
                s["all"].append(max(0, best - score_cache[(fen, e["move"])]))

        if (i + 1) % 150 == 0:
            print(f"  {i+1}/{len(positions)}  {time.time()-t0:.0f}s  "
                  f"缓存 {len(score_cache):,}", flush=True)

    eng.close()
    for bk in books.values():
        bk.close()

    report = {"nodes": a.nodes, "positions": len(positions), "buckets": BUCKETS,
              "books": {}}
    for k, s in stats.items():
        picks = [c for _p, c in s["pick"]]
        n = len(picks)
        rep = {"label": BOOK_LABEL[k], "positions": s["pos"],
               "covered": s["covered"],
               "coverage": round(s["covered"] / max(1, s["pos"]), 4),
               "graded": n,
               "mean_entries": round(s["n_entries"] / max(1, s["pos"]), 2)}
        if picks:
            rep.update({
                "mean_cpl": round(statistics.mean(picks), 1),
                "median_cpl": round(statistics.median(picks), 1),
                "p90_cpl": round(sorted(picks)[int(0.9 * (n - 1))], 1),
                "max_cpl": max(picks),
                "exact_best_rate": round(sum(1 for c in picks if c == 0) / n, 4),
                "over_100": round(sum(1 for c in picks if c > 100) / n, 4),
                "over_200": round(sum(1 for c in picks if c > 200) / n, 4),
                "over_500": round(sum(1 for c in picks if c > 500) / n, 4),
                "all_mean_cpl": round(statistics.mean(s["all"]), 1),
            })
            for b in BUCKETS:
                rep[f"within_{b}"] = round(sum(1 for c in picks if c <= b) / n, 4)
        rep["ply_cover"] = {str(p): f"{v[0]}/{v[1]}"
                            for p, v in sorted(s["ply_cover"].items())}
        rep["ply_mean_cpl"] = {str(p): round(statistics.mean(v), 1)
                               for p, v in sorted(s["ply_pick"].items()) if v}
        report["books"][k] = rep
        print(f"[{k}] 覆盖 {rep['coverage']:.1%}  平均CPL {rep.get('mean_cpl')}  "
              f"中位 {rep.get('median_cpl')}  完全最佳 {rep.get('exact_best_rate')}  "
              f"CPL>200 {rep.get('over_200')}", flush=True)

    (OUT / f"t4_quality_{a.tag}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("已写出", OUT / f"t4_quality_{a.tag}.json", f"用时 {time.time()-t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
