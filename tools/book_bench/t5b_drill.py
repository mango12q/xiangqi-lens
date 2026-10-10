# -*- coding: utf-8 -*-
"""T5b 开局演练（配对设计，最敏感的「实战」指标）。

为什么这样设计
--------------
完整对局的胜负噪声太大（同等强度引擎几乎全和）。真正决定「库好不好用」的，
是**从同一个真实开局局面出发，库能不能把我带进更好的形势**。

做法（严格配对）
----------------
1. 从 T2 公共参考树里取一批真实开局局面 P（ply 4..10，各层均匀抽样）。
2. 对每个 P、每个库：从 P 继续演练到我方第 40 手（与应用 ``book_max_ply`` 一致）。
   * 我方：库有合法着法就走库着（= 应用 ``play`` 模式），否则走引擎最佳着。
   * 对手：始终走引擎最佳着（同强度）。
3. 演练结束用深度搜索评估，得到「库阶段结束时的形势分」（我方视角）。
4. **同一批 P 评所有库** → 配对比较，方差远小于完整对局。
   对照组 = 我方全程走引擎（不用库），同一批 P。
"""
from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from bookio import Book  # noqa: E402
from common import BOOKS, BOOK_LABEL, Engine, fen_key  # noqa: E402

OUT = HERE / "out"
OUT.mkdir(exist_ok=True)
BOOK_MAX_PLY = 40

_TL = threading.local()


def _engines() -> tuple[Engine, Engine]:
    """每个工作线程复用一对引擎（避免每次演练重载 50MB NNUE）。"""
    e = getattr(_TL, "engines", None)
    if e is None:
        e = (Engine(threads=1, hash_mb=128), Engine(threads=1, hash_mb=128))
        _TL.engines = e
    return e


def build_starts(per_ply: int, lo: int, hi: int) -> list[tuple[str, int]]:
    d = json.loads((OUT / "t2_replay.json").read_text(encoding="utf-8"))
    rng = random.Random(4242)
    out = []
    for ply_s, fens in sorted(d.get("reference_samples", {}).items(),
                              key=lambda kv: int(kv[0])):
        ply = int(ply_s)
        if not (lo <= ply <= hi):
            continue
        pool = sorted(set(fens))
        if len(pool) > per_ply:
            pool = rng.sample(pool, per_ply)
        out += [(f, ply) for f in pool]
    return out


def drill(book_key: str | None, fen: str, start_ply: int, *,
          nodes: int, eval_nodes: int, end_ply: int) -> dict:
    from cchess import ChessBoard

    book = Book(book_key) if book_key else None
    my_eng, opp_eng = _engines()
    my_eng.new_game()
    opp_eng.new_game()
    try:
        board = ChessBoard(fen)
        my_side = "w" if fen.endswith("w") else "b"
        ply = start_ply
        hits = misses = 0
        my_moves = 0
        moves: list[str] = []
        while ply < end_ply:
            if board.no_moves():
                break
            cur = board.to_fen()
            side = "w" if cur.endswith("w") else "b"
            if side == my_side:
                my_moves += 1
                mv = None
                if book is not None and ply < BOOK_MAX_PLY:
                    p = book.pick(cur, "best")
                    if p:
                        mv = p["move"]
                        hits += 1
                    else:
                        misses += 1
                if mv is None:
                    mv = my_eng.analyse(cur, nodes=nodes)["raw_best"]
            else:
                mv = opp_eng.analyse(cur, nodes=nodes)["raw_best"]
            if not mv or mv in ("(none)", "0000"):
                break
            if board.move_iccs(mv) is None:
                break
            board.next_turn()
            moves.append(mv)
            ply += 1

        end_fen = board.to_fen()
        r = my_eng.analyse(end_fen, nodes=eval_nodes)
        score = r["score"] if end_fen.endswith(my_side) else -r["score"]
        return {"book": book_key, "start": fen, "start_ply": start_ply,
                "my_side": my_side, "eval": score, "hits": hits,
                "misses": misses, "my_moves": my_moves, "end_ply": ply,
                "moves": moves}
    finally:
        if book:
            book.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-ply", type=int, default=12)
    ap.add_argument("--lo", type=int, default=4)
    ap.add_argument("--hi", type=int, default=10)
    ap.add_argument("--nodes", type=int, default=60_000)
    ap.add_argument("--eval-nodes", type=int, default=600_000)
    ap.add_argument("--end-ply", type=int, default=BOOK_MAX_PLY)
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--tag", default="main")
    a = ap.parse_args()

    starts = build_starts(a.per_ply, a.lo, a.hi)
    print(f"配对开局局面 {len(starts)} 个（ply {a.lo}..{a.hi}），"
          f"每组演练到 ply {a.end_ply}")
    groups: list[str | None] = list(BOOKS) + [None]
    jobs = [(bk, fen, ply) for bk in groups for fen, ply in starts]
    print(f"共 {len(jobs)} 次演练，{a.workers} 并发", flush=True)

    res: list[dict] = []
    t0 = time.time()
    done = 0
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(drill, bk, fen, ply, nodes=a.nodes,
                          eval_nodes=a.eval_nodes, end_ply=a.end_ply): (bk, fen, ply)
                for bk, fen, ply in jobs}
        for f in as_completed(futs):
            try:
                res.append(f.result())
            except Exception as exc:
                bk, fen, ply = futs[f]
                res.append({"book": bk, "start": fen, "start_ply": ply,
                            "error": f"{type(exc).__name__}: {exc}"})
            done += 1
            if done % 50 == 0 or done == len(jobs):
                print(f"  {done}/{len(jobs)}  {time.time()-t0:.0f}s", flush=True)

    # 配对比较：以 (start) 为配对键
    by_start: dict[str, dict[str, dict]] = {}
    for r in res:
        if "error" in r:
            continue
        by_start.setdefault(r["start"], {})[r["book"] or "control"] = r

    summary = {}
    for bk in groups:
        key = bk or "control"
        rows = [v[key] for v in by_start.values() if key in v]
        if not rows:
            continue
        evals = [r["eval"] for r in rows]
        paired = []
        for v in by_start.values():
            if key in v and "control" in v:
                paired.append(v[key]["eval"] - v["control"]["eval"])
        hits = sum(r["hits"] for r in rows)
        misses = sum(r["misses"] for r in rows)
        summary[key] = {
            "label": BOOK_LABEL.get(bk, "不用库（对照）") if bk else "不用库（对照）",
            "drills": len(rows),
            "mean_eval": round(statistics.mean(evals), 1),
            "median_eval": round(statistics.median(evals), 1),
            "stdev_eval": round(statistics.pstdev(evals), 1) if len(evals) > 1 else 0.0,
            "worse_than_control": (round(sum(1 for d in paired if d < 0) / len(paired), 4)
                                   if paired else None),
            "mean_delta_vs_control": (round(statistics.mean(paired), 1)
                                      if paired else None),
            "book_hits": hits, "book_misses": misses,
            "hit_rate": round(hits / max(1, hits + misses), 4),
        }
    report = {"params": {"per_ply": a.per_ply, "lo": a.lo, "hi": a.hi,
                         "nodes": a.nodes, "eval_nodes": a.eval_nodes,
                         "end_ply": a.end_ply, "starts": len(starts)},
              "summary": summary, "drills": res,
              "seconds": round(time.time() - t0, 1)}
    (OUT / f"t5b_drill_{a.tag}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print("\n=== 汇总（同一批开局局面配对）===")
    print(f"{'库':<20} {'演练':>5} {'均势分':>8} {'中位':>7} {'标准差':>8} "
          f"{'vs对照':>8} {'命中率':>7}")
    for k, s in summary.items():
        print(f"{s['label']:<20} {s['drills']:>5} {s['mean_eval']:>8} "
              f"{s['median_eval']:>7} {s['stdev_eval']:>8} "
              f"{str(s['mean_delta_vs_control']):>8} {s['hit_rate']:>7.1%}")
    print(f"用时 {report['seconds']}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
