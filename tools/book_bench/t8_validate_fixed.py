# -*- coding: utf-8 -*-
"""T8 验证修好的副本：用**应用自己的代码路径**跑一遍。

必须确认：
  * 修复后应用查到的着法 **100% 合法**（先锋无敌原本 100% 非法）
  * 修复后应用查到的着法与「库真实内容」**完全一致**（不再丢数据）
  * 查询延迟回到亚毫秒级
"""
from __future__ import annotations

import json
import random
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from bookio import Book, FastBook, detect_orientation, is_legal, swapped_move  # noqa: E402
from common import BOOKS, BOOK_LABEL, book_path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app_book import OpeningBook, obk_decode_move, obk_zhash  # noqa: E402

OUT = HERE / "out"
OUT.mkdir(exist_ok=True)
FIXED_DIR = Path(r"D:\opencode\XiangQiLink\开局库\_fixed")


def load_positions(n: int = 600) -> list[str]:
    d = json.loads((OUT / "t2_replay.json").read_text(encoding="utf-8"))
    pos: list[str] = []
    for _ply, fens in sorted(d.get("reference_samples", {}).items(),
                             key=lambda kv: int(kv[0])):
        pos += fens
    random.Random(5).shuffle(pos)
    return pos[:n]


def evaluate(app_path: Path, key: str, positions: list[str],
             orientation: str, true_orientation: str | None = None) -> dict:
    """``orientation`` 用于解码**被测文件**（经应用层），
    ``true_orientation`` 用于从**原库**算「真实内容」基准。"""
    true_orientation = true_orientation or orientation
    app = OpeningBook(app_path)
    fb = FastBook(key)
    n = illegal = matched = 0
    true_total = app_total = 0
    lat = []
    hits = 0
    mismatch_samples = []
    for fen in positions:
        try:
            k = obk_zhash(fen)
        except Exception:
            continue
        vms, _w = fb.probe_key(k)
        true_moves = []
        for vm in vms:
            mv = (swapped_move(int(vm)) if true_orientation == "swapped"
                  else obk_decode_move(int(vm)))
            if mv and is_legal(fen, mv):
                true_moves.append(mv)
        t0 = time.perf_counter()
        try:
            ents = app.probe(fen)
        except Exception:
            ents = []
        lat.append((time.perf_counter() - t0) * 1000)
        n += 1
        moves = []
        for e in ents:
            if is_legal(fen, e["move"]):
                moves.append(e["move"])
            else:
                illegal += 1
        if ents:
            hits += 1
        true_total += len(true_moves)
        app_total += len(moves)
        if sorted(moves) == sorted(true_moves):
            matched += 1
        elif len(mismatch_samples) < 5:
            mismatch_samples.append({"fen": fen, "app": sorted(moves),
                                     "true": sorted(true_moves)})
    app.close()
    lat.sort()
    return {"positions": n, "hits": hits, "hit_rate": round(hits / max(1, n), 4),
            "illegal_moves_returned": illegal,
            "true_moves": true_total, "app_moves": app_total,
            "match_positions": matched,
            "match_rate": round(matched / max(1, n), 4),
            "mean_ms": round(sum(lat) / max(1, n), 3),
            "p90_ms": round(lat[int(0.9 * (n - 1))], 3),
            "max_ms": round(lat[-1], 3),
            "mismatch_samples": mismatch_samples}


def main() -> int:
    positions = load_positions()
    print(f"验证局面 {len(positions)} 个\n")
    report = {}
    for key in BOOKS:
        fname = BOOKS[key]
        orient = detect_orientation(key)
        print("=" * 76)
        print(f"{BOOK_LABEL[key]}")
        row = {"orientation": orient}
        orig = evaluate(book_path(key), key, positions, orient)
        row["original"] = orig
        print(f"  原库    : 命中 {orig['hits']}/{orig['positions']}   "
              f"非法着法 {orig['illegal_moves_returned']}   "
              f"与真实内容一致 {orig['match_rate']:.1%}   "
              f"延迟 {orig['mean_ms']}ms   "
              f"着法 真实{orig['true_moves']}/应用{orig['app_moves']}")
        fixed_path = FIXED_DIR / fname
        if fixed_path.is_file():
            # 修好的副本里 vmove 已交换 → 应用层按 normal 解码即正确；
            # 真实内容基准仍要用**原库的字节序**算。
            fx = evaluate(fixed_path, key, positions, "normal",
                          true_orientation=orient)
            row["fixed"] = fx
            print(f"  修好副本: 命中 {fx['hits']}/{fx['positions']}   "
                  f"非法着法 {fx['illegal_moves_returned']}   "
                  f"与真实内容一致 {fx['match_rate']:.1%}   "
                  f"延迟 {fx['mean_ms']}ms   "
                  f"着法 真实{fx['true_moves']}/应用{fx['app_moves']}")
        report[key] = row
        print()
    (OUT / "t8_validate_fixed.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("已写出", OUT / "t8_validate_fixed.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
