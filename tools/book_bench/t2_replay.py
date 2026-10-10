# -*- coding: utf-8 -*-
"""T2 覆盖度与可回放性（字节序已校正）。

两个视角
--------
A. **自树**（每库自己 BFS）：能不能从初始局面连续走子？走多深？有没有非法着法？
B. **公共参考树**（四库着法的并集 BFS）：这才是「实战中会遇到的局面」。
   在该树上算每个库的**命中率**——命中率直接决定实战速度，因为应用里
   「库命中就立刻落子，不等 1200ms 引擎搜索；不命中要等满 1200ms」。
"""
from __future__ import annotations

import json
import sys
import time
from collections import Counter, deque
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from bookio import Book  # noqa: E402
from common import BOOKS, BOOK_LABEL, START_FEN, apply_move, fen_key  # noqa: E402
OUT = HERE / "out"
OUT.mkdir(exist_ok=True)

MAX_NODES = 60_000
MAX_PLY = 30
SAMPLE_PER_PLY = 200

# 公共参考树参数
REF_MAX_PLY = 20
REF_PER_PLY_CAP = 1200
REF_TOTAL_CAP = 30_000


def branch_cap(ply: int) -> int:
    if ply < 4:
        return 8
    if ply < 8:
        return 4
    if ply < 14:
        return 2
    return 1


def self_tree(bk: Book) -> dict:
    """A. 用该库自己的着法 BFS。"""
    t0 = time.time()
    seen = {fen_key(START_FEN)}
    q = deque([(START_FEN, 0)])
    legal = illegal = dead = 0
    nodes_by_ply = Counter()
    cover_by_ply = Counter()
    branch_hist = Counter()
    illegal_samples = []
    samples: dict[int, list[str]] = {}
    max_ply = 0
    probe_s = 0.0

    while q and sum(nodes_by_ply.values()) < MAX_NODES:
        fen, ply = q.popleft()
        nodes_by_ply[ply] += 1
        max_ply = max(max_ply, ply)
        if len(samples.get(ply, [])) < SAMPLE_PER_PLY:
            samples.setdefault(ply, []).append(fen)

        ta = time.time()
        rows = bk.raw_moves(fen)
        probe_s += time.time() - ta

        n_legal = 0
        # 用统一入口取（含字节序校正 + vvalid 过滤）
        for e in bk.moves(fen, legal_only=False):
            mv = e["move"]
            nxt = apply_move(fen, mv)
            if nxt is None:
                illegal += 1
                if len(illegal_samples) < 20:
                    illegal_samples.append({"fen": fen, "move": mv, "ply": ply})
                continue
            legal += 1
            n_legal += 1
            nk = fen_key(nxt)
            if nk not in seen:
                seen.add(nk)
                q.append((nxt, ply + 1))
        if n_legal:
            cover_by_ply[ply] += 1
            branch_hist[n_legal] += 1
        else:
            dead += 1

    return {
        "nodes": sum(nodes_by_ply.values()),
        "unique": len(seen),
        "legal_edges": legal,
        "illegal_edges": illegal,
        "dead_end": dead,
        "max_ply": max_ply,
        "mean_branch": round(sum(k * v for k, v in branch_hist.items())
                             / max(1, sum(branch_hist.values())), 2),
        "nodes_by_ply": dict(sorted(nodes_by_ply.items())),
        "cover_by_ply": dict(sorted(cover_by_ply.items())),
        "illegal_samples": illegal_samples,
        "mean_probe_ms": round(1000 * probe_s / max(1, sum(nodes_by_ply.values())), 2),
        "seconds": round(time.time() - t0, 1),
        "samples": {str(k): v for k, v in sorted(samples.items())},
    }


def union_tree(books: dict[str, Book]) -> dict:
    """B. 公共参考树：着法池 = 四库合法着的并集。"""
    t0 = time.time()
    seen = {fen_key(START_FEN)}
    q = deque([(START_FEN, 0)])
    per_ply: dict[int, list[str]] = {0: [START_FEN]}
    per_ply_count = Counter({0: 1})
    all_positions: dict[int, list[str]] = {0: [START_FEN]}

    while q:
        fen, ply = q.popleft()
        if ply >= REF_MAX_PLY:
            continue
        if per_ply_count[ply] >= REF_PER_PLY_CAP:
            continue
        # 收集着法池：统计有多少个库支持该着法
        pool: Counter = Counter()
        for bk in books.values():
            for e in bk.moves(fen, legal_only=True):
                pool[e["move"]] += 1
        if not pool:
            continue
        ordered = sorted(pool.items(), key=lambda kv: (-kv[1], kv[0]))
        for mv, _n in ordered[:branch_cap(ply)]:
            nxt = apply_move(fen, mv)
            if nxt is None:
                continue
            k = fen_key(nxt)
            if k in seen:
                continue
            seen.add(k)
            per_ply_count[ply + 1] += 1
            all_positions.setdefault(ply + 1, []).append(nxt)
            q.append((nxt, ply + 1))
            if len(seen) > REF_TOTAL_CAP:
                break
        if len(seen) > REF_TOTAL_CAP:
            break

    return {"positions": {str(k): v for k, v in sorted(all_positions.items())},
            "unique": len(seen), "seconds": round(time.time() - t0, 1)}


def main() -> int:
    books = {k: Book(k) for k in BOOKS}
    report = {"orientation": {k: b.orientation for k, b in books.items()}}

    print("=== A. 自树可回放性 ===", flush=True)
    for k, bk in books.items():
        r = self_tree(bk)
        report.setdefault("self_tree", {})[k] = r
        print(f"[{bk.label}] 局面 {r['nodes']:,}  合法边 {r['legal_edges']:,}  "
              f"非法边 {r['illegal_edges']:,}  死路 {r['dead_end']:,}  "
              f"最大层 {r['max_ply']}  平均分支 {r['mean_branch']}  "
              f"探测 {r['mean_probe_ms']}ms  {r['seconds']}s", flush=True)

    print("\n=== B. 公共参考树 ===", flush=True)
    ut = union_tree(books)
    report["union_tree"] = {kk: vv for kk, vv in ut.items() if kk != "positions"}
    print(f"参考树局面数 {ut['unique']:,}  ({ut['seconds']}s)", flush=True)

    cov = {}
    for k, bk in books.items():
        hit = tot = 0
        by_ply_hit = Counter()
        by_ply_tot = Counter()
        t0 = time.time()
        for ply_s, fens in ut["positions"].items():
            ply = int(ply_s)
            for fen in fens:
                tot += 1
                by_ply_tot[ply] += 1
                if bk.moves(fen, legal_only=True):
                    hit += 1
                    by_ply_hit[ply] += 1
        cov[k] = {"label": bk.label, "hit": hit, "total": tot,
                  "coverage": round(hit / max(1, tot), 4),
                  "by_ply": {str(p): f"{by_ply_hit[p]}/{by_ply_tot[p]}"
                             for p in sorted(by_ply_tot)},
                  "seconds": round(time.time() - t0, 1)}
        print(f"[{bk.label}] 命中 {hit:,}/{tot:,} = {cov[k]['coverage']:.1%}  "
              f"({cov[k]['seconds']}s)", flush=True)
    report["reference_coverage"] = cov

    # 参考树局面按层抽样，供 T4 使用
    ref_samples: dict[str, list[str]] = {}
    for ply_s, fens in ut["positions"].items():
        ref_samples[ply_s] = fens[:300]
    report["reference_samples"] = ref_samples

    (OUT / "t2_replay.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for bk in books.values():
        bk.close()
    print("已写出", OUT / "t2_replay.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
