# -*- coding: utf-8 -*-
"""带「字节序」处理的开局库封装。

背景一：字节序不统一（T1d 实测）
================================
obk 的 ``vmove`` 约定是 ``(起点偏移 << 8) | 终点偏移``。但社区生成工具不统一：
**「先锋无敌nn库241111」把两个字节反着存**（``(终点 << 8) | 起点``）。
用应用层现有的 ``obk_decode_move`` 解它，得到的着法 **100% 非法**，
会被 ``legal_check`` 全部过滤 → ``pick()`` 永远返回 None → 该库在实战中
**一次都用不上**。

背景二：索引不可信 / REAL 键要全表扫（T1b/T3 实测）
==================================================
obk 表只有 ``idxkey(vkey)`` 一个索引，而部分库的索引是坏的
（``PRAGMA integrity_check`` 报 "row N missing from index" /
"wrong # of entries in index"）。此时 ``WHERE vkey = ?`` 会**静默漏数据**。
另外生成工具在键 >= 2^63 时把键按 IEEE double 存成 REAL，索引查不到，
应用层只能退到全表扫（3.6M 行 ≈ 130ms/次）。

因此本模块提供两种读取路径：
  * :class:`FastBook` —— 整库一次性读进 numpy 排序数组，**完全绕过索引**，
    得到库的**真实内容**（用于测「库本身好不好」）。
  * ``Book(source="app")`` —— 走应用层真实 SQLite 路径（用于测「应用现状」）。
"""
from __future__ import annotations

import json
import sqlite3
import struct
import sys
from collections import deque
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from common import (BOOKS, BOOK_LABEL, START_FEN, apply_move, book_path,  # noqa: E402
                    fen_key)

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app_book import OpeningBook, obk_decode_move  # noqa: E402

OUT = HERE / "out"
OUT.mkdir(exist_ok=True)
ORIENT_CACHE = OUT / "orientation.json"


def swapped_move(vmove) -> str | None:
    """按字节交换解码 vmove。"""
    try:
        v = int(vmove)
    except Exception:
        return None
    return obk_decode_move(((v & 0xFF) << 8) | ((v >> 8) & 0xFF))


_LEGAL_CACHE: dict[tuple[str, str], bool] = {}


def is_legal(fen: str, mv: str) -> bool:
    """带缓存的严格合法性校验（同一 (局面, 着法) 在多个库里会重复问）。"""
    k = (fen, mv)
    v = _LEGAL_CACHE.get(k)
    if v is None:
        v = apply_move(fen, mv) is not None
        if len(_LEGAL_CACHE) > 400_000:
            _LEGAL_CACHE.clear()
        _LEGAL_CACHE[k] = v
    return v


# ---------------------------------------------------------------------------
# 快速精确读取（绕过 SQLite 索引）
# ---------------------------------------------------------------------------
class FastBook:
    """整库载入 numpy 排序数组，按 canonical key 精确查询。

    canonical key = 应用层 ``obk_zhash(fen)`` 得到的 64 位值。
    * 存成 INTEGER 的行：按 int64 补码还原成 uint64。
    * 存成 REAL 的行：double 的 8 字节位模式**就是**那个键，直接按位重解释。
    """

    def __init__(self, key: str, table: str = "bhobk") -> None:
        self.key = key
        self.path = book_path(key)
        self.table = table
        self.keys: np.ndarray
        self.vmoves: np.ndarray
        self.weights: np.ndarray
        self.rows = 0
        self._load()

    def _load(self) -> None:
        conn = sqlite3.connect(
            f"file:{self.path.as_posix()}?mode=ro", uri=True)
        cols = ("vkey, vmove, COALESCE(vwin,0), COALESCE(vdraw,0), "
                "COALESCE(vscore,0), COALESCE(vvalid,1)")
        key_parts, vm_parts, w_parts = [], [], []
        for tname in ("integer", "real"):
            cur = conn.execute(
                f'SELECT {cols} FROM "{self.table}" '
                f"WHERE typeof(vkey)='{tname}'")
            while True:
                rows = cur.fetchmany(300_000)
                if not rows:
                    break
                a = np.array(rows)
                col = np.ascontiguousarray(a[:, 0])
                if tname == "real":
                    keys = col.view(np.uint64)
                else:
                    keys = col.astype(np.int64).view(np.uint64)
                win = np.ascontiguousarray(a[:, 2]).astype(np.int64)
                draw = np.ascontiguousarray(a[:, 3]).astype(np.int64)
                score = np.ascontiguousarray(a[:, 4]).astype(np.int64)
                valid = np.ascontiguousarray(a[:, 5]).astype(np.int64)
                w = win * 2 + draw
                w = np.where(w <= 0, score, w)
                w = np.maximum(w, 1)
                keep = valid != 0
                key_parts.append(keys[keep])
                vm_parts.append(
                    np.ascontiguousarray(a[:, 1]).astype(np.int64)[keep])
                w_parts.append(w[keep])
        conn.close()
        self.keys = np.concatenate(key_parts) if key_parts else np.zeros(0, np.uint64)
        vm = np.concatenate(vm_parts) if vm_parts else np.zeros(0, np.int64)
        self.weights = (np.concatenate(w_parts) if w_parts
                        else np.zeros(0, np.int64)).astype(np.int32)
        self.vmoves = vm.astype(np.int64)
        order = np.argsort(self.keys, kind="stable")
        self.keys = self.keys[order]
        self.vmoves = self.vmoves[order]
        self.weights = self.weights[order]
        self.rows = int(self.keys.size)
        # 边界表：每个唯一键的 [start, end)
        self.uniq, self.starts = np.unique(self.keys, return_index=True)
        self.ends = np.append(self.starts[1:], self.keys.size)

    def probe_key(self, k: int) -> tuple[np.ndarray, np.ndarray]:
        i = int(np.searchsorted(self.uniq, np.uint64(k), side="left"))
        if i >= self.uniq.size or self.uniq[i] != np.uint64(k):
            return (np.zeros(0, np.int64), np.zeros(0, np.int32))
        s, e = int(self.starts[i]), int(self.ends[i])
        return self.vmoves[s:e], self.weights[s:e]


# ---------------------------------------------------------------------------
# 字节序自动判定
# ---------------------------------------------------------------------------
def _probe_positions(limit: int = 400, plies: int = 8) -> list[str]:
    """用已知正确的「云霄剑诀」BFS 出一批公共探测局面。"""
    fb = FastBook("yunxiao")
    seen = {fen_key(START_FEN)}
    q = deque([(START_FEN, 0)])
    out = [START_FEN]
    while q and len(out) < limit:
        fen, ply = q.popleft()
        if ply >= plies:
            continue
        for e in _moves_of(fb, fen, "normal"):
            nxt = apply_move(fen, e)
            if nxt is None:
                continue
            k = fen_key(nxt)
            if k in seen:
                continue
            seen.add(k)
            out.append(nxt)
            q.append((nxt, ply + 1))
    return out


def _moves_of(fb: FastBook, fen: str, orientation: str) -> list[str]:
    from app_book import obk_zhash
    try:
        k = obk_zhash(fen)
    except Exception:
        return []
    vms, _w = fb.probe_key(k)
    out = []
    for vm in vms:
        mv = (swapped_move(int(vm)) if orientation == "swapped"
              else obk_decode_move(int(vm)))
        if mv:
            out.append(mv)
    return out


def detect_orientation(key: str, force: bool = False) -> str:
    """返回 ``'normal'`` 或 ``'swapped'``（带磁盘缓存）。"""
    cache = {}
    if ORIENT_CACHE.exists():
        try:
            cache = json.loads(ORIENT_CACHE.read_text(encoding="utf-8"))
        except Exception:
            cache = {}
    if not force and key in cache:
        return cache[key]["orientation"]

    positions = _probe_positions()
    fb = FastBook(key)
    counts = {"normal": [0, 0], "swapped": [0, 0]}
    for fen in positions:
        for o in ("normal", "swapped"):
            for mv in _moves_of(fb, fen, o):
                if apply_move(fen, mv) is not None:
                    counts[o][0] += 1
                else:
                    counts[o][1] += 1
    rate = {o: (counts[o][0] / max(1, sum(counts[o]))) for o in counts}
    orient = "swapped" if rate["swapped"] > rate["normal"] else "normal"
    cache[key] = {"orientation": orient,
                  "normal_rate": round(rate["normal"], 4),
                  "swapped_rate": round(rate["swapped"], 4),
                  "normal": counts["normal"], "swapped": counts["swapped"]}
    ORIENT_CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    return orient


# ---------------------------------------------------------------------------
# 统一封装
# ---------------------------------------------------------------------------
_FASTBOOKS: dict[str, "FastBook"] = {}


def get_fastbook(key: str) -> "FastBook":
    """进程内共享的 FastBook（屠龙 181MB，每次重载代价太大）。"""
    fb = _FASTBOOKS.get(key)
    if fb is None:
        fb = FastBook(key)
        _FASTBOOKS[key] = fb
    return fb


class Book:
    """统一封装。

    ``source='fast'``（默认）：读整库内存索引，得到**库的真实内容**。
    ``source='app'``：走应用层 ``OpeningBook``（真实 SQLite 路径，含索引缺陷）。
    """

    def __init__(self, key: str, orientation: str | None = None,
                 source: str = "fast") -> None:
        self.key = key
        self.label = BOOK_LABEL[key]
        self.file = BOOKS[key]
        self.source = source
        self.orientation = orientation or detect_orientation(key)
        self.fb = get_fastbook(key)
        self._app = None
        self._cache: dict[str, list[dict]] = {}
        if source == "app":
            self._app = OpeningBook(book_path(key))

    def raw_moves(self, fen: str) -> list[dict]:
        """校正字节序后的着法（不过滤合法性）：[{'move','weight','vmove'}]"""
        hit = self._cache.get(fen)
        if hit is not None:
            return hit
        if self._app is not None:
            ents = self._app.probe(fen)
            out = [{"move": e["move"], "weight": max(1, int(e["weight"])),
                    "vmove": None} for e in ents]
        else:
            from app_book import obk_zhash
            try:
                k = obk_zhash(fen)
            except Exception:
                return []
            vms, ws = self.fb.probe_key(k)
            out = []
            for vm, w in zip(vms, ws):
                mv = (swapped_move(int(vm)) if self.orientation == "swapped"
                      else obk_decode_move(int(vm)))
                if not mv:
                    continue
                out.append({"move": mv, "weight": int(w), "vmove": int(vm)})
        if len(self._cache) > 4096:
            self._cache.clear()
        self._cache[fen] = out
        return out

    def moves(self, fen: str, legal_only: bool = True) -> list[dict]:
        """校正字节序 + 合法性过滤后的着法。"""
        out = []
        for e in self.raw_moves(fen):
            legal = is_legal(fen, e["move"])
            if legal_only and not legal:
                continue
            out.append({"move": e["move"], "weight": e["weight"], "legal": legal})
        return out

    def pick(self, fen: str, mode: str = "best") -> dict | None:
        """等价应用层 ``OpeningBook.pick(fen, mode, legal_check)``。"""
        cands = self.moves(fen, legal_only=True)
        if not cands:
            return None
        if mode == "random":
            import random
            total = sum(c["weight"] for c in cands)
            r = random.uniform(0, total)
            acc = 0.0
            for c in cands:
                acc += c["weight"]
                if r <= acc:
                    return c
            return cands[-1]
        return max(cands, key=lambda c: c["weight"])

    def clear_cache(self) -> None:
        self._cache.clear()

    def close(self) -> None:
        if self._app is not None:
            self._app.close()


if __name__ == "__main__":
    for k in BOOKS:
        fb = FastBook(k)
        o = detect_orientation(k, force=True)
        print(f"{BOOK_LABEL[k]:<20} 有效行 {fb.rows:>9,}  唯一键 {fb.uniq.size:>9,}  "
              f"字节序 {o}")
