# -*- coding: utf-8 -*-
"""T5 实战演练与对局（最贴近「我实际对局」的测试）。

场景还原（按 app_vision.py 的真实行为）
======================================
* 我方：``book_mode="play"`` —— 前 ``book_max_ply``(默认 40) 手内，只要库命中
  就直接走库着（``pick(fen,"best")``，权重最高），**不等引擎搜索**；
  库不命中或超出深度则走引擎 ``go movetime auto_think_ms``。
* 对手：天天象棋中高段位真人 → 用「同强度引擎 + 拟人噪声」建模
  （MultiPV=3，按分数 softmax 抽样，偶尔不选最佳着）。

两种口径
--------
A. **开局演练（敏感度高）**：只走开局段，记录「我方库阶段结束时」的深度评估分。
   这直接回答「这个库在实战里把我带到好局面还是坏局面」。
B. **完整对局（结论性强）**：走到分出胜负/和棋，统计胜率。
   双方引擎**同强度**，唯一差别就是开局库 —— 这样测的才是库的净贡献。
   另有「不用库」的对照组。
"""
from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from bookio import Book  # noqa: E402
from common import (BOOKS, BOOK_LABEL, Engine, START_FEN, fen_key)  # noqa: E402

OUT = HERE / "out"
OUT.mkdir(exist_ok=True)

MAX_PLIES = 300
NOCAP_DRAW_PLIES = 120      # 象棋 60 回合无吃子判和
BOOK_MAX_PLY = 40           # 与 app_vision.py 默认一致

_TL = threading.local()


def _engines() -> tuple[Engine, Engine]:
    """每个工作线程复用一对引擎（避免每局重载 50MB NNUE）。"""
    e = getattr(_TL, "engines", None)
    if e is None:
        e = (Engine(threads=1, hash_mb=128), Engine(threads=1, hash_mb=128))
        _TL.engines = e
    return e


def _piece_count(board) -> int:
    fen = board.to_fen().split()[0]
    return sum(1 for ch in fen if ch.isalpha())


def _choose_opponent(eng: Engine, fen: str, nodes: int, temp: float,
                     rng: random.Random, ply: int, open_plies: int) -> str:
    """拟人对手。

    开局前 ``open_plies`` 手：MultiPV=3 按分数 softmax 抽样（制造不同开局，
    模拟真人各有各的套路）；
    之后：走引擎最佳着 —— 保持强度，避免对手自己崩盘掩盖库的差异。
    """
    if temp <= 0 or ply >= open_plies:
        return eng.analyse(fen, nodes=nodes)["raw_best"]
    r = eng.analyse(fen, nodes=nodes, multipv=3)
    lines = r.get("lines") or []
    if len(lines) < 2:
        return r["best"] or r["raw_best"]
    scores = [s for _m, s in lines]
    mx = max(scores)
    exps = [math.exp((s - mx) / temp) for s in scores]
    tot = sum(exps)
    r_ = rng.random() * tot
    acc = 0.0
    for (mv, _s), e in zip(lines, exps):
        acc += e
        if r_ <= acc:
            return mv
    return lines[0][0]


def play_game(book_key: str | None, my_side: str, seed: int, *,
              my_nodes: int, opp_nodes: int, opp_temp: float,
              eval_nodes: int, open_plies: int = 10,
              use_book: bool = True) -> dict:
    """book_key=None 表示我方也不用库（对照组）。"""
    from cchess import ChessBoard

    rng = random.Random(seed)
    book = Book(book_key) if (book_key and use_book) else None
    my_eng, opp_eng = _engines()
    my_eng.new_game()
    opp_eng.new_game()
    try:
        board = ChessBoard(START_FEN)
        hist = Counter()
        hist[fen_key(START_FEN)] += 1
        nocap = 0
        ply = 0
        book_hits = book_misses = 0
        my_moves = 0
        book_end_eval = None
        book_end_ply = None
        moves_played: list[str] = []
        result = None
        reason = ""

        while ply < MAX_PLIES:
            if board.no_moves():
                side_now = "w" if board.to_fen().endswith("w") else "b"
                result = 1.0 if side_now != my_side else 0.0
                reason = "checkmate/stalemate"
                break
            fen = board.to_fen()
            side = "w" if fen.endswith("w") else "b"

            if side == my_side:
                my_moves += 1
                mv = None
                if book is not None and ply < BOOK_MAX_PLY:
                    p = book.pick(fen, "best")
                    if p:
                        mv = p["move"]
                        book_hits += 1
                    else:
                        book_misses += 1
                if mv is None:
                    # 库阶段结束（ply 达到 40）→ 用同一次深度搜索同时给出
                    # 「库阶段结束时的形势分」和本手着法，保证各库评估点一致。
                    if book_end_eval is None and ply >= BOOK_MAX_PLY:
                        r = my_eng.analyse(fen, nodes=eval_nodes)
                        book_end_eval = r["score"]
                        book_end_ply = ply
                        mv = r["raw_best"]
                    else:
                        mv = my_eng.analyse(fen, nodes=my_nodes)["raw_best"]
            else:
                mv = _choose_opponent(opp_eng, fen, opp_nodes, opp_temp, rng,
                                      ply, open_plies)

            if not mv or mv in ("(none)", "0000"):
                result = 0.5
                reason = "no move"
                break
            before = _piece_count(board)
            if board.move_iccs(mv) is None:
                result = 0.0 if side == my_side else 1.0
                reason = "illegal move generated"
                break
            board.next_turn()
            after = _piece_count(board)
            nocap = 0 if after < before else nocap + 1
            moves_played.append(mv)
            ply += 1
            k = fen_key(board.to_fen())
            hist[k] += 1
            if hist[k] >= 3:
                result = 0.5
                reason = "threefold"
                break
            if nocap >= NOCAP_DRAW_PLIES:
                result = 0.5
                reason = "60-move rule"
                break
        if result is None:
            result = 0.5
            reason = "ply cap"

        # 若整局在 ply 40 之前就结束，补一个终局评估
        if book_end_eval is None:
            fen = board.to_fen()
            r = my_eng.analyse(fen, nodes=eval_nodes)
            book_end_eval = r["score"] if fen.endswith(my_side) else -r["score"]
            book_end_ply = ply

        return {"book": book_key, "my_side": my_side, "seed": seed,
                "result": result, "reason": reason, "plies": ply,
                "book_hits": book_hits, "book_misses": book_misses,
                "book_end_eval": book_end_eval, "book_end_ply": book_end_ply,
                "my_moves": my_moves,
                "opening": moves_played[:24]}
    finally:
        if book:
            book.close()


def run(books: list[str | None], games_per_side: int, workers: int,
        my_nodes: int, opp_nodes: int, opp_temp: float,
        eval_nodes: int, tag: str, open_plies: int = 10) -> dict:
    jobs = []
    seed = 1000
    for bk in books:
        for side in ("w", "b"):
            for i in range(games_per_side):
                seed += 1
                jobs.append((bk, side, seed))
    print(f"共 {len(jobs)} 局，{workers} 并发", flush=True)

    out = []
    done = 0
    t0 = time.time()
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(play_game, bk, side, sd, my_nodes=my_nodes,
                          opp_nodes=opp_nodes, opp_temp=opp_temp,
                          eval_nodes=eval_nodes, open_plies=open_plies): (bk, side, sd)
                for bk, side, sd in jobs}
        for f in as_completed(futs):
            try:
                out.append(f.result())
            except Exception as exc:
                bk, side, sd = futs[f]
                out.append({"book": bk, "my_side": side, "seed": sd,
                            "error": f"{type(exc).__name__}: {exc}"})
            with lock:
                done += 1
                if done % 5 == 0 or done == len(jobs):
                    print(f"  {done}/{len(jobs)}  {time.time()-t0:.0f}s", flush=True)

    summary = {}
    for bk in books:
        rows = [r for r in out if r.get("book") == bk and "error" not in r]
        if not rows:
            continue
        key = bk or "control"
        score = sum(r["result"] for r in rows) / len(rows)
        evals = [r["book_end_eval"] for r in rows if r.get("book_end_eval") is not None]
        hits = sum(r["book_hits"] for r in rows)
        misses = sum(r["book_misses"] for r in rows)
        summary[key] = {
            "label": BOOK_LABEL.get(bk, "不用库（对照）") if bk else "不用库（对照）",
            "games": len(rows),
            "score": round(score, 4),
            "wins": sum(1 for r in rows if r["result"] == 1.0),
            "draws": sum(1 for r in rows if r["result"] == 0.5),
            "losses": sum(1 for r in rows if r["result"] == 0.0),
            "as_red": round(sum(r["result"] for r in rows if r["my_side"] == "w")
                            / max(1, sum(1 for r in rows if r["my_side"] == "w")), 4),
            "as_black": round(sum(r["result"] for r in rows if r["my_side"] == "b")
                              / max(1, sum(1 for r in rows if r["my_side"] == "b")), 4),
            "book_hits": hits, "book_misses": misses,
            "book_hit_rate": round(hits / max(1, hits + misses), 4),
            "mean_book_end_eval": (round(statistics.mean(evals), 1) if evals else None),
            "median_book_end_eval": (round(statistics.median(evals), 1) if evals else None),
            "mean_book_end_ply": round(statistics.mean(
                [r["book_end_ply"] for r in rows if r.get("book_end_ply") is not None]), 1),
            "errors": sum(1 for r in out if r.get("book") == bk and "error" in r),
        }
    res = {"tag": tag, "params": {"my_nodes": my_nodes, "opp_nodes": opp_nodes,
                                  "opp_temp": opp_temp, "eval_nodes": eval_nodes,
                                  "open_plies": open_plies,
                                  "games_per_side": games_per_side,
                                  "workers": workers,
                                  "book_max_ply": BOOK_MAX_PLY},
           "summary": summary, "games": out,
           "seconds": round(time.time() - t0, 1)}
    (OUT / f"t5_{tag}.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--games", type=int, default=10, help="每库每方局数")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--my-nodes", type=int, default=50_000)
    ap.add_argument("--opp-nodes", type=int, default=50_000)
    ap.add_argument("--opp-temp", type=float, default=60.0)
    ap.add_argument("--open-plies", type=int, default=10,
                    help="对手在第几手之前用拟人随机开局，之后走最佳着")
    ap.add_argument("--eval-nodes", type=int, default=400_000)
    ap.add_argument("--tag", default="main")
    ap.add_argument("--books", default=",".join(BOOKS) + ",control")
    a = ap.parse_args()

    books: list[str | None] = []
    for x in a.books.split(","):
        x = x.strip()
        if x == "control":
            books.append(None)
        elif x in BOOKS:
            books.append(x)
    res = run(books, a.games, a.workers, a.my_nodes, a.opp_nodes,
              a.opp_temp, a.eval_nodes, a.tag, a.open_plies)
    print("\n=== 汇总 ===")
    for k, s in res["summary"].items():
        print(f"{s['label']:<20} 得分 {s['score']:.3f} "
              f"({s['wins']}胜 {s['draws']}和 {s['losses']}负)  "
              f"库命中率 {s['book_hit_rate']:.1%}  "
              f"库末均势 {s['mean_book_end_eval']}")
    print(f"用时 {res['seconds']}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
