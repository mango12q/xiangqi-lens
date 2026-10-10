# -*- coding: utf-8 -*-
"""T1b 深度完整性：PRAGMA integrity_check / quick_check + 损坏定位。"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import BOOKS, BOOK_LABEL, book_path  # noqa: E402

OUT = Path(__file__).resolve().parent / "out"
OUT.mkdir(exist_ok=True)


def probe_sql(conn, sql):
    try:
        t0 = time.time()
        rows = conn.execute(sql).fetchall()
        return {"ok": True, "ms": round(1000 * (time.time() - t0), 1),
                "rows": len(rows), "sample": [list(r) for r in rows[:8]]}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    report = {}
    for key, fname in BOOKS.items():
        p = book_path(key)
        print("=" * 70)
        print(f"{BOOK_LABEL[key]}  {fname}  {p.stat().st_size/1048576:.1f} MiB")
        conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
        r = {"file": fname, "label": BOOK_LABEL[key]}
        for name, sql in (("quick_check", "PRAGMA quick_check"),
                          ("integrity_check", "PRAGMA integrity_check")):
            res = probe_sql(conn, sql)
            r[name] = res
            print(f"  {name}: ok={res['ok']} "
                  f"{res.get('sample') if res['ok'] else res.get('error')}")
        r["checks"] = {
            "count": probe_sql(conn, "SELECT COUNT(*) FROM bhobk"),
            "group_key_move": probe_sql(
                conn, "SELECT COUNT(*) FROM (SELECT vkey, vmove FROM bhobk "
                      "GROUP BY vkey, vmove HAVING COUNT(*)>1)"),
            "distinct_keys": probe_sql(conn, "SELECT COUNT(DISTINCT vkey) FROM bhobk"),
            "idx_scan": probe_sql(
                conn, "SELECT COUNT(*) FROM bhobk WHERE vkey = 7101337512282506414"),
        }
        for k, v in r["checks"].items():
            print(f"  {k}: ok={v['ok']} {v.get('sample') or v.get('error')}")
        report[key] = r
        conn.close()

    (OUT / "t1b_integrity.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print("已写出", OUT / "t1b_integrity.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
