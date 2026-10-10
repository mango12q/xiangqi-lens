# -*- coding: utf-8 -*-
"""T1 完整性：每个 .obk 库的结构与数据卫生指标。

指标：
  * 总行数 / 有效行数(vvalid=1) / 无效行数
  * vkey 为 NULL 的行数（无法查询）
  * vkey 的存储类型分布（INTEGER vs REAL —— REAL 会绕过索引）
  * (vkey, vmove) 重复条数
  * vmove 无法解码成合法 ICCS 的条数
  * 每个局面的着法条数分布（分支宽度）
  * 唯一 vkey 数
"""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import BOOKS, BOOK_LABEL, book_path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app_book import obk_decode_move  # noqa: E402

OUT = Path(__file__).resolve().parent / "out"
OUT.mkdir(exist_ok=True)


def open_ro(p: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)


def main() -> int:
    report = {}
    for key, fname in BOOKS.items():
        p = book_path(key)
        conn = open_ro(p)
        r = {"file": fname, "label": BOOK_LABEL[key],
             "size_mib": round(p.stat().st_size / 1048576, 1)}
        t = "bhobk"
        n = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        r["rows"] = n
        r["valid_rows"] = conn.execute(
            f'SELECT COUNT(*) FROM "{t}" WHERE vvalid=1').fetchone()[0]
        r["invalid_rows"] = n - r["valid_rows"]
        r["null_key"] = conn.execute(
            f'SELECT COUNT(*) FROM "{t}" WHERE vkey IS NULL').fetchone()[0]
        r["null_move"] = conn.execute(
            f'SELECT COUNT(*) FROM "{t}" WHERE vmove IS NULL').fetchone()[0]
        r["key_types"] = dict(conn.execute(
            f'SELECT typeof(vkey), COUNT(*) FROM "{t}" GROUP BY 1').fetchall())
        r["distinct_keys"] = conn.execute(
            f'SELECT COUNT(DISTINCT vkey) FROM "{t}"').fetchone()[0]
        r["dup_key_move"] = conn.execute(
            f'SELECT COUNT(*) FROM (SELECT vkey, vmove FROM "{t}" '
            f'GROUP BY vkey, vmove HAVING COUNT(*)>1)').fetchone()[0]

        # 着法解码
        bad_move = 0
        moves_per_key = Counter()
        cur_key, cnt = object(), 0
        for vk, vm in conn.execute(
                f'SELECT vkey, vmove FROM "{t}" WHERE vkey IS NOT NULL '
                f'ORDER BY vkey'):
            if vm is None or obk_decode_move(vm) is None:
                bad_move += 1
            if vk != cur_key:
                if cur_key is not object():
                    moves_per_key[cnt] += 1
                cur_key, cnt = vk, 0
            cnt += 1
        moves_per_key[cnt] += 1
        r["undecodable_move"] = bad_move
        r["moves_per_key"] = dict(sorted(moves_per_key.items()))
        r["mean_branch"] = round(
            sum(k * v for k, v in moves_per_key.items()) / max(1, sum(moves_per_key.values())), 2)
        r["max_branch"] = max(moves_per_key) if moves_per_key else 0

        # vscore 分布（去掉极值噪声）
        vs = [x[0] for x in conn.execute(
            f'SELECT vscore FROM "{t}" WHERE vscore IS NOT NULL')]
        vs.sort()
        if vs:
            r["vscore"] = {"min": vs[0], "p50": vs[len(vs) // 2], "max": vs[-1],
                           "distinct": len(set(vs))}
        conn.close()
        report[key] = r
        print(f"[{key}] rows={r['rows']:,} valid={r['valid_rows']:,} "
              f"keys={r['distinct_keys']:,} branch={r['mean_branch']} "
              f"badmove={r['undecodable_move']} dups={r['dup_key_move']:,}")

    (OUT / "t1_integrity.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("已写出", OUT / "t1_integrity.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
