# -*- coding: utf-8 -*-
"""T3 应用现状可用性：索引缺陷造成的静默丢数据 + 查询延迟。

发现（T1b/T1c/一致性检查）
==========================
* obk 表只有 ``idxkey(vkey)`` 一个索引。多个库的索引**不完整**
  （``PRAGMA integrity_check`` 报 "row N missing from index"），
  且「最新屠龙商业库」的 INTEGER 键行**系统性查不到**。
* 应用层 ``ObkBook.probe`` 的顺序是：
      ① ``WHERE vkey = ?``（走索引）
      ② 索引返回 0 行时，``WHERE CAST(vkey AS REAL) = ?``（全表扫）
  兜底的 ``?`` 是**键的位重解释**（``struct.unpack('<d', struct.pack('<Q', K))``），
  它只能命中「按位重解释存成 REAL」的行；对 INTEGER 行永远不命中。
  → 索引漏掉的行 **永远找不回来**（静默丢数据）。

本脚本量化：同一批局面下，应用路径 vs 库真实内容（内存精确索引）。
"""
from __future__ import annotations

import json
import sqlite3
import statistics
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from bookio import Book, FastBook, detect_orientation  # noqa: E402
from common import BOOKS, BOOK_LABEL, START_FEN, book_path, fen_key  # noqa: E402

OUT = HERE / "out"
OUT.mkdir(exist_ok=True)

SAMPLE_N = 1200


def integrity_error_count(path: Path) -> dict:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        rows = [r[0] for r in conn.execute("PRAGMA integrity_check")]
    except Exception as exc:
        rows = [f"ERR {exc}"]
    conn.close()
    missing = sum(1 for r in rows if "missing from index" in str(r))
    other = [r for r in rows if "missing from index" not in str(r) and r != "ok"]
    return {"total_errors": len(rows) if rows != ["ok"] else 0,
            "missing_from_index": missing, "other": other[:5]}


def build_positions() -> list[str]:
    p = OUT / "t2_replay.json"
    pos: list[str] = []
    if p.exists():
        d = json.loads(p.read_text(encoding="utf-8"))
        for _ply, fens in sorted(d.get("reference_samples", {}).items(),
                                 key=lambda kv: int(kv[0])):
            pos += fens
    if not pos:
        pos = [START_FEN]
    import random
    random.Random(11).shuffle(pos)
    return pos[:SAMPLE_N]


def app_vs_true(key: str, positions: list[str]) -> dict:
    """应用路径 vs 库真实内容。"""
    book_app = Book(key, source="app")     # 走真实 SQLite 路径
    fb = FastBook(key)
    orient = book_app.orientation
    from app_book import obk_decode_move, obk_zhash
    from bookio import is_legal, swapped_move

    lost_pos = 0
    lost_entries = 0
    app_empty_true_not = 0
    app_has_true_none = 0
    total_true = total_app = 0
    checked = 0
    samples = []
    for fen in positions:
        try:
            k = obk_zhash(fen)
        except Exception:
            continue
        vms, _ws = fb.probe_key(k)
        true_moves = []
        for vm in vms:
            mv = (swapped_move(int(vm)) if orient == "swapped"
                  else obk_decode_move(int(vm)))
            if mv and is_legal(fen, mv):
                true_moves.append(mv)
        app_moves = [e["move"] for e in book_app._app.probe(fen)
                     if is_legal(fen, e["move"])]
        checked += 1
        total_true += len(true_moves)
        total_app += len(app_moves)
        if len(app_moves) < len(true_moves):
            lost_pos += 1
            lost_entries += len(true_moves) - len(app_moves)
            if len(samples) < 8:
                samples.append({"fen": fen, "true": sorted(true_moves),
                                "app": sorted(app_moves)})
        if not app_moves and true_moves:
            app_empty_true_not += 1
        if app_moves and not true_moves:
            app_has_true_none += 1
    book_app.close()
    return {"checked": checked, "true_moves": total_true, "app_moves": total_app,
            "lost_positions": lost_pos, "lost_entries": lost_entries,
            "lost_position_rate": round(lost_pos / max(1, checked), 4),
            "lost_entry_rate": round(lost_entries / max(1, total_true), 4),
            "app_empty_but_true_not": app_empty_true_not,
            "app_has_but_true_none": app_has_true_none,
            "samples": samples}


def latency(key: str, positions: list[str]) -> dict:
    book_app = Book(key, source="app")
    lat = []
    hits = 0
    for fen in positions:
        book_app._app._impl._cache.clear()
        t0 = time.perf_counter()
        rows = book_app._app.probe(fen)
        lat.append((time.perf_counter() - t0) * 1000)
        if rows:
            hits += 1
    book_app.close()
    lat.sort()
    n = len(lat)
    return {"n": n, "hits": hits, "hit_rate": round(hits / max(1, n), 4),
            "mean_ms": round(sum(lat) / max(1, n), 2),
            "median_ms": round(lat[n // 2], 2),
            "p90_ms": round(lat[int(0.9 * (n - 1))], 2),
            "p99_ms": round(lat[int(0.99 * (n - 1))], 2),
            "max_ms": round(lat[-1], 2),
            "total_s": round(sum(lat) / 1000, 1)}


def main() -> int:
    positions = build_positions()
    print(f"样本局面 {len(positions)} 个（来自 T2 公共参考树）\n")
    report = {"sample": len(positions)}
    for key in BOOKS:
        p = book_path(key)
        print("=" * 78)
        print(f"{BOOK_LABEL[key]}  ({p.stat().st_size/1048576:.1f} MiB)")
        ie = integrity_error_count(p)
        report.setdefault("integrity", {})[key] = ie
        print(f"  integrity_check: 错误 {ie['total_errors']} 条，"
              f"其中 'missing from index' {ie['missing_from_index']} 条，"
              f"其他 {ie['other']}")

        av = app_vs_true(key, positions)
        report.setdefault("app_vs_true", {})[key] = av
        print(f"  应用路径 vs 库真实内容（{av['checked']} 个局面）:")
        print(f"    真实合法着法总数 {av['true_moves']:,}，应用查到 {av['app_moves']:,}"
              f"  → 丢失 {av['lost_entries']:,} 条 ({av['lost_entry_rate']:.1%})")
        print(f"    受影响局面 {av['lost_positions']:,}/{av['checked']:,} "
              f"({av['lost_position_rate']:.1%})；"
              f"库有内容但应用查不到的局面 {av['app_empty_but_true_not']:,}")

        la = latency(key, positions)
        report.setdefault("latency", {})[key] = la
        print(f"  查询延迟: 平均 {la['mean_ms']}ms 中位 {la['median_ms']}ms "
              f"p90 {la['p90_ms']}ms p99 {la['p99_ms']}ms 最大 {la['max_ms']}ms  "
              f"应用命中率 {la['hit_rate']:.1%}")
        print()

    (OUT / "t3_latency.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("已写出", OUT / "t3_latency.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
