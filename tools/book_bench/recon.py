# -*- coding: utf-8 -*-
"""开局库对比测试 —— 第 1 步：结构侦察。

对每个 .obk 库只读打开，报告：
  * 文件大小 / SQLite 完整性
  * 表结构、行数
  * vkey 的存储类型分布（INTEGER / REAL 的坑）
  * vvalid 取值分布、vscore 范围
  * 着法能否解码、着法解码后的坐标是否落在棋盘内
"""
from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app_book import obk_decode_move  # noqa: E402

BOOKS = [
    "云霄剑诀7.5.obk",
    "先锋无敌nn库241111-book.obk",
    "最新屠龙商业库.obk",
    "静香库.obk",
]
ROOT = Path(r"D:\opencode\XiangQiLink\开局库")


def open_ro(p: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)


def table_of(conn: sqlite3.Connection) -> str | None:
    for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"):
        cols = {str(r[1]) for r in conn.execute(f'PRAGMA table_info("{name}")')}
        if {"vkey", "vmove"} <= cols:
            return str(name)
    return None


def main() -> int:
    for name in BOOKS:
        p = ROOT / name
        print("=" * 78)
        print(f"库: {name}   ({p.stat().st_size/1048576:.1f} MiB)")
        t0 = time.time()
        try:
            conn = open_ro(p)
        except Exception as exc:
            print("  打开失败:", exc)
            continue

        t = table_of(conn)
        print(f"  表名: {t}")
        cols = [(r[1], r[2]) for r in conn.execute(f'PRAGMA table_info("{t}")')]
        print("  列:", ", ".join(f"{c}:{k}" for c, k in cols))

        n = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        print(f"  行数: {n:,}")

        # 索引
        idx = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index'").fetchall()
        print(f"  索引: {len(idx)} 个")
        for nm, sql in idx[:6]:
            print(f"    - {nm}: {sql}")

        # vkey 存储类型分布
        try:
            dist = conn.execute(
                f'SELECT typeof(vkey), COUNT(*) FROM "{t}" GROUP BY 1').fetchall()
            print("  vkey 类型分布:", dict(dist))
        except Exception as exc:
            print("  vkey 类型分布失败:", exc)

        # vvalid / vscore
        for col in ("vvalid", "vscore", "vwin", "vdraw", "vlost"):
            try:
                d = conn.execute(
                    f'SELECT {col}, COUNT(*) FROM "{t}" GROUP BY 1 '
                    f'ORDER BY 2 DESC LIMIT 5').fetchall()
                print(f"  {col} top5:", d)
            except Exception as exc:
                print(f"  {col} 失败:", exc)

        print(f"  侦察耗时: {time.time()-t0:.2f}s")
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
