# -*- coding: utf-8 -*-
"""开局库（opening book）—— 应用层实现。

为什么放在应用层
================
官方 Pikafish 二进制**不带**开局库。实测其 ``uci`` 输出里全部选项只有
``Debug Log File / NumaPolicy / Threads / Hash / Clear Hash / Ponder /
MultiPV / Move Overhead / nodestime / UCI_ShowWDL / EvalFile``，没有任何
``BookFile`` 之类的库选项（Stockfish 官方也没有，只有 Polyfish 等分支才有）。
所以开局库只能在本程序里做：**先查库，命中就用库着，不命中再交给引擎搜索**。

支持的格式
==========
1. ``.obk`` —— **社区实际流通的格式**（如「云霄剑诀」系列）。SQLite 数据库，
   表 ``bhobk(id, vkey, vmove, vscore, vwin, vdraw, vlost, vvalid, vmemo,
   vindex)``；``vkey`` 是 64 位 **Zobrist**（用 cchess 自带的
   ``cchess.zhash_data`` 表算，**含行棋方**），``vmove`` 是
   ``(起点偏移<<8)|终点偏移`` 的 0x88 偏移。按 key 查询，不整库载入内存。

2. ``.xqb`` —— https://github.com/fisherfan/xqbook 。SQLite 数据库，表
   ``book(id, key BLOB, move INTEGER, score, win, draw, lost, valid, memo)``，
   另有 ``information(name, value)``。key 是**位打包的棋盘布局、不含轮次**。

   key 的编码（已用官方样例库两条记录交叉验证）：
   * 按 FEN 顺序（index = row*9 + col，row 0 = 黑方底线/棋盘顶部）**逐格交错**
     打包：每格先 1 bit 标志（0=空位，1=有子），有子再跟 4 bit 棋子编码。
   * 总长 = ceil((90 + 4*棋子数) / 8) 字节；末字节未用到的位必须为 0。
   * 棋子编码：红 1..7 = 车马相仕帅炮兵，黑 9..15 = 车马象士将炮卒。
   * 例：起始局面 = 28 字节。

   着法的编码：16 位 ``(from_square << 8) | to_square``，其中
   ``square = rank*16 + file``，``rank`` 是 **FEN 行号**（0 = 黑方底线，
   与 ICCS 的 rank 相反：ICCS rank = 9 - FEN row），``file`` = 0..8 = a..i。
   * 验证例：``0x7774`` → (7,7)红炮→(7,4) = ``h2e2``「炮二平五」；
     ``0x9776`` → (9,7)红马→(7,6) = ``h0g2``「马二进三」。

3. ``.txt`` / ``.json`` —— 自研线路库，便于手写或从棋谱生成。
   文本每行一条线路（从初始局面开始的 ICCS 着法序列，空格分隔）::

       # 中炮对屏风马（# 后为注释）
       h2e2 h9g7 h0g2 i9h9
       *3 h2e2 h9g7          # 行首 *N 为该线路权重，默认 1

   JSON 支持两种写法::

       {"lines": [["h2e2", "h9g7"], {"moves": ["h2e2"], "weight": 3}]}

   加载时用 cchess 逐着回放，算出每个局面的**棋盘段**并建立
   ``{棋盘段: [(着法, 权重), ...]}`` 映射。

统一约定
========
* **xqb 的 key 不含轮次**（同一布局红黑两轮次命中同一批着法），所以查到的
  着法必须再用**严格**合法性校验过滤一遍，否则轮次判错时会拿对方的着法去
  点鼠标。**obk 的 key 含轮次**，没有这个问题，但为保险仍走同一道校验。
* 本模块不依赖 Qt，可单独 import 做单元测试。
"""
from __future__ import annotations

import json
import random
import sqlite3
import struct
from pathlib import Path

# 标准起始局面（红先）
START_FEN = "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR w - - 0 1"

# obk / XQWLight 的 0x88 棋盘基址：合法格偏移 = 0x33 + (9-FEN行)*16 + 列
OBK_BASE = 0x33

# xqb 棋子编码：红 1..7 = 车马相仕帅炮兵；黑 9..15 = 车马象士将炮卒
_XQB_CODE = {
    "R": 1, "N": 2, "B": 3, "A": 4, "K": 5, "C": 6, "P": 7,
    "r": 9, "n": 10, "b": 11, "a": 12, "k": 13, "c": 14, "p": 15,
}
_XQB_PIECE = {v: k for k, v in _XQB_CODE.items()}


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def board_of(fen: str) -> str:
    """取 FEN 的棋盘段（去掉轮次及之后的部分）。"""
    return (fen or "").split()[0]


def _board_rows(board: str) -> list[str]:
    """FEN 棋盘段展开成 10 行 x 9 列的字符矩阵（'.' 表示空位）。"""
    rows: list[str] = []
    for part in board.split("/"):
        line = ""
        for ch in part:
            line += "." * int(ch) if ch.isdigit() else ch
        rows.append(line)
    return rows


def _compress(row: str) -> str:
    """把 '.' 连续段压成数字，还原成 FEN 棋盘段的一行。"""
    s, empty = "", 0
    for ch in row:
        if ch == ".":
            empty += 1
        else:
            if empty:
                s += str(empty)
                empty = 0
            s += ch
    if empty:
        s += str(empty)
    return s


def xqb_encode_key(fen_or_board: str) -> bytes:
    """把局面编码成 xqb 的 key（逐格交错位打包）。

    传 FEN 或纯棋盘段都可以。非法/无法编码时抛 :class:`ValueError`。
    """
    board = board_of(fen_or_board)
    rows = _board_rows(board)
    if len(rows) != 10 or any(len(r) != 9 for r in rows):
        raise ValueError(f"非法棋盘段: {board!r}")

    bits: list[int] = []
    for row in rows:
        for ch in row:
            if ch in ".x":                      # 'x' = 识别不确定格，按空处理
                bits.append(0)
                continue
            code = _XQB_CODE.get(ch)
            if code is None:
                raise ValueError(f"未知棋子 {ch!r}")
            bits.append(1)
            for shift in (3, 2, 1, 0):          # 4 bit 棋子编码，高位在前
                bits.append((code >> shift) & 1)

    while len(bits) % 8:                        # 补齐到整字节，未用位为 0
        bits.append(0)

    out = bytearray()
    for i in range(0, len(bits), 8):
        v = 0
        for b in bits[i:i + 8]:
            v = (v << 1) | b
        out.append(v)
    return bytes(out)


def xqb_decode_key(blob: bytes, strict: bool = False) -> str:
    """把 xqb 的 key 还原成 FEN 棋盘段（供调试与测试做往返校验）。

    :param strict: 为 True 时额外校验「末字节未用到的补位必须为 0」
        （xqb 规范如此）。默认 False —— 社区库若由非标准工具生成，
        补位可能没清零，严格校验会把整个库判成损坏。自检/测试用 True。
    """
    bits: list[int] = []
    for byte in blob:
        for shift in (7, 6, 5, 4, 3, 2, 1, 0):
            bits.append((byte >> shift) & 1)

    i, rows = 0, []
    for _ in range(10):
        line = ""
        for _ in range(9):
            if i >= len(bits):
                raise ValueError("key 长度不足")
            flag = bits[i]
            i += 1
            if not flag:
                line += "."
                continue
            if i + 4 > len(bits):
                raise ValueError("key 长度不足（棋子编码被截断）")
            code = 0
            for _k in range(4):
                code = (code << 1) | bits[i]
                i += 1
            piece = _XQB_PIECE.get(code)
            if piece is None:
                raise ValueError(f"未知棋子编码 {code}")
            line += piece
        rows.append(line)

    if strict and any(bits[i:]):
        raise ValueError("key 尾部补位不为 0（不符合 xqb 规范）")
    return "/".join(_compress(r) for r in rows)


def xqb_encode_move(iccs: str) -> int:
    """ICCS 着法（如 ``'h2e2'``）→ xqb 的 16 位整数。"""
    if not iccs or len(iccs) != 4:
        raise ValueError(f"非法 ICCS: {iccs!r}")

    def square(f: str, r: str) -> int:
        file = ord(f.lower()) - ord("a")
        rank = 9 - int(r)                       # ICCS rank → FEN 行号
        if not (0 <= file <= 8 and 0 <= rank <= 9):
            raise ValueError(f"坐标越界: {f}{r}")
        return rank * 16 + file

    return (square(iccs[0], iccs[1]) << 8) | square(iccs[2], iccs[3])


def xqb_decode_move(value: int) -> str | None:
    """xqb 的 16 位着法 → ICCS；编码非法返回 None。"""
    try:
        value = int(value)
    except Exception:
        return None
    if not (0 <= value <= 0xFFFF):
        return None
    frm, to = (value >> 8) & 0xFF, value & 0xFF
    if frm == to:
        return None                       # 起点==终点不是着法，视为损坏数据
    for sq in (frm, to):
        if not (0 <= sq % 16 <= 8 and 0 <= sq // 16 <= 9):
            return None
    return (f"{chr(ord('a') + frm % 16)}{9 - frm // 16}"
            f"{chr(ord('a') + to % 16)}{9 - to // 16}")


def _norm_entries(entries) -> list[dict]:
    """把后端返回的原始条目归一成 ``[{'move','weight'}]``。"""
    out: list[dict] = []
    for e in entries or []:
        if isinstance(e, dict) and e.get("move"):
            out.append({"move": str(e["move"]),
                        "weight": max(1, int(e.get("weight", 1)))})
        elif isinstance(e, (tuple, list)) and len(e) >= 1:
            w = int(e[1]) if len(e) > 1 else 1
            out.append({"move": str(e[0]), "weight": max(1, w)})
    return out


# ---------------------------------------------------------------------------
# 后端 1：xqb（SQLite）
# ---------------------------------------------------------------------------
class XqbBook:
    """xqb 开局库：按 key 精确查询，不整库载入内存。"""

    kind = "xqb"

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        p = Path(self.path)
        if not p.is_file():
            raise FileNotFoundError(self.path)
        # ★ 只读打开（uri=True + mode=ro）：绝不能误写用户的库文件。
        self.conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
        # information 表的 value 是 TEXT；统一按 bytes 取，自己解码，
        # 避免历史库里混入非 UTF-8 字节时直接抛异常。
        self.conn.text_factory = bytes
        self._cache: dict[str, list[dict]] = {}
        self._info = self._read_info()
        if not self._has_book_table():
            raise ValueError(f"不是 xqb 开局库（缺 book 表）: {self.path}")

    def _has_book_table(self) -> bool:
        try:
            row = self.conn.execute(
                "select count(*) from sqlite_master "
                "where type='table' and name='book'").fetchone()
            return bool(row and int(row[0]) > 0)
        except Exception:
            return False

    def _read_info(self) -> dict:
        out: dict[str, str] = {}
        try:
            rows = self.conn.execute("select name, value from information")
        except Exception:
            return out
        for name, value in rows:
            try:
                k = name.decode("utf-8", "replace") if isinstance(name, bytes) else str(name)
                v = value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)
                out[k] = v
            except Exception:
                continue
        return out

    @property
    def info(self) -> dict:
        return dict(self._info)

    def probe(self, fen: str) -> list[dict]:
        board = board_of(fen)
        if board in self._cache:
            return self._cache[board]
        try:
            key = xqb_encode_key(board)
        except Exception:
            return []
        rows = []
        try:
            rows = self.conn.execute(
                "select move, win, draw, lost, score, valid from book "
                "where key = ?", (sqlite3.Binary(key),)).fetchall()
        except Exception:
            rows = []

        out: list[dict] = []
        for row in rows:
            move, win, draw, lost, score, valid = (list(row) + [None] * 6)[:6]
            if valid is not None and int(valid) == 0:
                continue
            mv = xqb_decode_move(int(move))
            if not mv:
                continue
            # 权重：优先「胜局*2 + 和局」（胜率代理），无统计时退到 score，再退到 1
            weight = int(win or 0) * 2 + int(draw or 0)
            if weight <= 0:
                weight = int(score or 0)
            if weight <= 0:
                weight = 1
            out.append({"move": mv, "weight": weight})

        if len(self._cache) > 512:
            self._cache.clear()
        self._cache[board] = out
        return out

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 后端 2：线路库（txt / json）
# ---------------------------------------------------------------------------
class LineBook:
    """自研线路库：加载时用 cchess 回放，算出每个局面的后续着法。"""

    kind = "line"

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        p = Path(self.path)
        if not p.is_file():
            raise FileNotFoundError(self.path)
        self.map: dict[str, list[dict]] = {}
        self.lines = 0
        self.errors = 0
        if p.suffix.lower() == ".json":
            self._load_json(p)
        else:
            self._load_text(p)

    # ---- 加载 ----
    def _load_text(self, p: Path) -> None:
        text = p.read_text(encoding="utf-8", errors="replace")
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()      # 去注释
            if not line:
                continue
            weight = 1
            if line.startswith("*"):                 # *N 权重
                head, _, rest = line.partition(" ")
                try:
                    weight = max(1, int(head[1:]))
                except ValueError:
                    weight = 1
                line = rest.strip()
            if not line:
                continue
            self._add_line(line.split(), weight)

    def _load_json(self, p: Path) -> None:
        data = json.loads(p.read_text(encoding="utf-8", errors="replace"))
        items = data.get("lines", []) if isinstance(data, dict) else data
        for it in items or []:
            if isinstance(it, list):
                self._add_line([str(x) for x in it], 1)
            elif isinstance(it, dict):
                mv = it.get("moves") or it.get("line") or []
                try:
                    w = max(1, int(it.get("weight", 1)))
                except Exception:
                    w = 1
                self._add_line([str(x) for x in mv], w,
                               str(it.get("fen") or START_FEN))

    def _add_line(self, moves, weight: int = 1, fen: str = START_FEN) -> None:
        """把一条线路回放成「局面 → 下一着」映射。"""
        try:
            from cchess import ChessBoard
        except Exception:
            self.errors += 1
            return
        try:
            board = ChessBoard(fen)
        except Exception:
            self.errors += 1
            return

        added = False
        for mv in moves:
            if not mv:
                continue
            board_key = board.to_fen().split()[0]
            try:
                moved = board.move_iccs(mv)
            except Exception:
                moved = None
            if moved is None:                        # 线路里有着法不合法
                self.errors += 1
                break
            bucket = self.map.setdefault(board_key, [])
            for e in bucket:
                if e["move"] == mv:
                    e["weight"] += weight
                    break
            else:
                bucket.append({"move": mv, "weight": weight})
            added = True
            board.next_turn()                        # ★ 必须换边，否则第二步起非法
        if added:
            self.lines += 1

    # ---- 查询 ----
    def probe(self, fen: str) -> list[dict]:
        return list(self.map.get(board_of(fen), []))

    def close(self) -> None:
        self.map.clear()


# ---------------------------------------------------------------------------
# 后端 3：obk（社区实际流通的格式）
# ---------------------------------------------------------------------------
def obk_zhash(fen: str) -> int:
    """局面 → obk 的 64 位 Zobrist 键（**含轮次**）。

    直接用 cchess 自带的 Zobrist 表（``cchess.zhash_data``）—— 实测它与社区
    obk 库完全一致：起始局面（红先）的键 = ``7101337512282506414``，正是
    ``云霄剑诀7.5.obk`` 第 1 条记录的 ``vkey``。

    与 xqb 的区别很重要：obk 的键**包含行棋方**（红先时异或 ``z_redKey``），
    所以不存在 xqb 那种「同一布局红黑两个轮次命中同一批着法」的歧义。
    """
    from cchess.zhash_data import z_c90, z_hashTable, z_pieces, z_redKey

    parts = (fen or "").split()
    rows = _board_rows(parts[0] if parts else "")
    if len(rows) != 10 or any(len(r) != 9 for r in rows):
        raise ValueError(f"非法棋盘段: {fen!r}")
    side = parts[1] if len(parts) > 1 else "w"

    key = 0
    for y in range(10):                     # cchess 的 y：0 = 红方底线（底部）
        row = rows[9 - y]                   # FEN 行号 = 9 - y
        for x in range(9):
            ch = row[x]
            if ch in z_pieces:
                key ^= z_hashTable[z_pieces[ch] * 256 + z_c90[x + (9 - y) * 9]]
    if side == "w":                         # 红方行棋 → 异或红方标志
        key ^= z_redKey
    return key & 0xFFFFFFFFFFFFFFFF


def obk_decode_move(value: int) -> str | None:
    """obk 的 16 位着法 → ICCS。

    编码：``(起点偏移 << 8) | 终点偏移``，偏移是 XQWLight/cchess 的 0x88 棋盘
    偏移（``0x33 + (9 - FEN行) * 16 + 列``）。

    实测：起始局面 ``0xAAA7`` = ``h2e2``（炮二平五）；炮二平五之后（黑先）
    ``0x3A59`` = ``h9g7``（马8进7）。
    """
    try:
        value = int(value)
    except Exception:
        return None
    if not (0 <= value <= 0xFFFF):
        return None

    def square(off: int) -> str | None:
        d = off - OBK_BASE
        col, row = d & 0x0F, d >> 4
        if not (0 <= col <= 8 and 0 <= row <= 9):
            return None
        return f"{chr(ord('a') + col)}{9 - row}"

    frm = square((value >> 8) & 0xFF)
    to = square(value & 0xFF)
    if not frm or not to or frm == to:
        return None
    return frm + to


def obk_encode_move(iccs: str) -> int:
    """ICCS 着法 → obk 的 16 位整数（:func:`obk_decode_move` 的逆）。"""
    if not iccs or len(iccs) != 4:
        raise ValueError(f"非法 ICCS: {iccs!r}")

    def offset(f: str, r: str) -> int:
        col = ord(f.lower()) - ord("a")
        row = 9 - int(r)                    # ICCS rank → FEN 行号
        if not (0 <= col <= 8 and 0 <= row <= 9):
            raise ValueError(f"坐标越界: {f}{r}")
        return OBK_BASE + row * 16 + col

    return (offset(iccs[0], iccs[1]) << 8) | offset(iccs[2], iccs[3])


class ObkBook:
    """obk 开局库（社区实际流通的格式，SQLite）。

    表结构::

        CREATE TABLE bhobk(id INTEGER PRIMARY KEY AUTOINCREMENT, vkey INTEGER,
                           vmove INTEGER, vscore INTEGER, vwin INTEGER,
                           vdraw INTEGER, vlost INTEGER, vvalid INTEGER,
                           vmemo BLOB, vindex INTEGER)

    ``vkey`` 是 64 位 Zobrist（**含轮次**）。

    ★ 兼容一个生成工具的坑：键值高位置 1（≥ 2^63）时，生成工具把它按 IEEE
    double 存进了 ``vkey``（实测：把该 double 的 8 字节原样解包，得到的正是
    正确键）。SQLite 的索引对这类 REAL 值查不到，必须 ``CAST(vkey AS REAL)
    = ?`` 全表扫描才命中。这里两种形式都试，保证不丢数据（实测全表扫 4.9ms）。
    """

    kind = "obk"

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        p = Path(self.path)
        if not p.is_file():
            raise FileNotFoundError(self.path)
        # 只读打开，绝不误写用户的库文件
        self.conn = sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True)
        self.table = self._find_table()
        if not self.table:
            raise ValueError(f"不是 obk 开局库（找不到含 vkey/vmove 的表）: {self.path}")
        self._cache: dict[str, list[dict]] = {}
        try:
            self.rows = int(self.conn.execute(
                f'SELECT COUNT(*) FROM "{self.table}"').fetchone()[0])
        except Exception:
            self.rows = 0

    def _find_table(self) -> str | None:
        """找含 ``vkey`` + ``vmove`` 列的表（标准叫 bhobk，但不硬编码）。"""
        try:
            names = self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        except Exception:
            return None
        for (name,) in names:
            try:
                cols = {str(r[1]) for r in
                        self.conn.execute(f'PRAGMA table_info("{name}")')}
            except Exception:
                continue
            if {"vkey", "vmove"} <= cols:
                return str(name)
        return None

    @property
    def info(self) -> dict:
        return {"version": "obk", "type": "xiangqi",
                "rows": str(getattr(self, "rows", 0))}

    def probe(self, fen: str) -> list[dict]:
        # ★ 缓存键用**完整 FEN**：obk 的 Zobrist 含轮次，同一布局在红/黑轮次
        #   下是不同的键、命中的着法也不同，只用棋盘段做键会串味。
        if fen in self._cache:
            return self._cache[fen]
        try:
            key = obk_zhash(fen)
        except Exception:
            return []

        signed = key - (1 << 64) if key >= (1 << 63) else key
        dbl = struct.unpack("<d", struct.pack("<Q", key))[0]
        sql = (f'SELECT vmove, vwin, vdraw, vlost, vscore, vvalid '
               f'FROM "{self.table}" WHERE {{}}')
        rows: list = []
        try:
            # ① 走索引的正常路径（键存成 INTEGER 时命中）
            rows = self.conn.execute(
                sql.format("vkey = ?"), (signed,)).fetchall()
            if not rows:
                # ② 兜底：键被生成工具按 double 存了 → 必须 CAST 全表扫
                rows = self.conn.execute(
                    sql.format("CAST(vkey AS REAL) = ?"), (dbl,)).fetchall()
        except Exception:
            rows = []

        out: list[dict] = []
        for row in rows:
            move, win, draw, lost, score, valid = (list(row) + [None] * 6)[:6]
            if valid is not None and int(valid) == 0:
                continue
            mv = obk_decode_move(int(move))
            if not mv:
                continue
            weight = int(win or 0) * 2 + int(draw or 0)
            if weight <= 0:
                weight = int(score or 0)
            if weight <= 0:
                weight = 1
            out.append({"move": mv, "weight": weight})

        if len(self._cache) > 512:
            self._cache.clear()
        self._cache[fen] = out
        return out

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------
_SUFFIX = {
    ".xqb": XqbBook,
    ".obk": ObkBook,
    ".txt": LineBook,
    ".json": LineBook,
    ".book": LineBook,
}


class OpeningBook:
    """开局库统一入口：按扩展名选后端，统一做合法性过滤与挑选。"""

    def __init__(self, path: str | Path) -> None:
        p = Path(str(path))
        cls = _SUFFIX.get(p.suffix.lower())
        if cls is None:
            raise ValueError(
                f"不认识的开局库格式 {p.suffix!r}"
                f"（支持 .obk / .xqb / .txt / .json）")
        self.path = str(p)
        self._impl = cls(p)

    # ---- 元信息 ----
    @property
    def kind(self) -> str:
        return self._impl.kind

    @property
    def info(self) -> dict:
        return getattr(self._impl, "info", {})

    def describe(self) -> str:
        """给状态栏用的一句话描述。"""
        extra = ""
        if self.kind == "line":
            extra = f"（{getattr(self._impl, 'lines', 0)} 条线路"
            errs = getattr(self._impl, "errors", 0)
            if errs:
                extra += f"，{errs} 处无法解析"
            extra += "）"
        elif self.kind == "obk":
            n = getattr(self._impl, "rows", 0)
            extra = f"（{n} 条记录）" if n else ""
        else:
            info = self.info
            if info.get("version"):
                extra = f"（xqb v{info['version']}）"
        return f"{Path(self.path).name} · {self.kind}{extra}"

    # ---- 查询 ----
    def probe(self, fen: str) -> list[dict]:
        return _norm_entries(self._impl.probe(fen))

    def pick(self, fen: str, mode: str = "best",
             legal_check=None) -> dict | None:
        """挑一个可用的库着。

        :param mode: ``'best'`` 取权重最高；``'random'`` 按权重随机。
        :param legal_check: ``callable(fen, move) -> bool``，用来按**当前轮次**
            过滤非法着法。强烈建议传入 —— xqb 的 key 不含轮次，同一个局面在
            红/黑轮次下会命中同一批着法，不过滤就可能去点对方的着法。
        :return: ``{'move','weight'}`` 或 ``None``

        ★ 这里**不做结果缓存**：结果依赖 ``legal_check``，而传入的过滤器每次
        都可能不同（不同轮次、不同策略），按 (mode, fen) 缓存会把上一次的
        过滤结果错给下一次。底层 ``probe()`` 已有按局面的缓存，够用。
        """
        cands = self.probe(fen)
        if legal_check is not None:
            kept = []
            for c in cands:
                try:
                    if legal_check(fen, c["move"]):
                        kept.append(c)
                except Exception:
                    continue
            cands = kept
        if not cands:
            return None
        if mode == "random":
            total = sum(max(1, c["weight"]) for c in cands)
            r = random.uniform(0, total)
            acc = 0.0
            for c in cands:
                acc += max(1, c["weight"])
                if r <= acc:
                    return c
            return cands[-1]
        return max(cands, key=lambda c: c["weight"])

    def close(self) -> None:
        try:
            self._impl.close()
        except Exception:
            pass


def open_book(path: str | Path) -> OpeningBook:
    """便捷入口（等价于 ``OpeningBook(path)``）。"""
    return OpeningBook(path)


# ---------------------------------------------------------------------------
# 自检：python app_book.py
# ---------------------------------------------------------------------------
def _selftest() -> int:
    ok = True

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        print(("  [OK]   " if cond else "  [FAIL] ") + name +
              (f"  {detail}" if detail else ""))
        if not cond:
            ok = False

    print("app_book 自检")
    print("① key 编解码往返")
    key = xqb_encode_key(START_FEN)
    check("起始局面 key = 28 字节", len(key) == 28, f"实得 {len(key)}")
    check("起始局面 key = 官方样例字节",
          key.hex() == "ceb7cef37ac801e079f7df7df00002ebaebaeb02c004653a56939440",
          key.hex())
    check("往返还原", xqb_decode_key(key) == board_of(START_FEN))

    print("② 着法编解码")
    for iccs, val in (("h2e2", 0x7774), ("h0g2", 0x9776)):
        check(f"{iccs} → 0x{val:04X}", xqb_encode_move(iccs) == val,
              f"实得 0x{xqb_encode_move(iccs):04X}")
        check(f"0x{val:04X} → {iccs}", xqb_decode_move(val) == iccs,
              str(xqb_decode_move(val)))
    check("越界着法返回 None", xqb_decode_move(0xFFFF) is None)

    print("③ 少子局面 key 长度")
    few = "4k4/9/9/9/9/9/9/9/9/4K4 w - - 0 1"      # 2 子
    k2 = xqb_encode_key(few)
    check("2 子局面 key = 13 字节", len(k2) == 13, f"实得 {len(k2)}")
    check("2 子局面往返", xqb_decode_key(k2) == board_of(few))

    print("④ obk 编解码")
    k = obk_zhash(START_FEN)
    check("起始局面(红先) obk 键 = 7101337512282506414",
          k == 7101337512282506414, str(k))
    k_black = obk_zhash(board_of(START_FEN) + " b - - 0 1")
    check("obk 键含轮次（红先 ≠ 黑先）", k != k_black,
          f"{k} vs {k_black}")
    try:
        from cchess import ChessBoard
        ref = ChessBoard(START_FEN).zhash() & 0xFFFFFFFFFFFFFFFF
        check("自算 obk 键与 cchess.zhash 一致", k == ref, f"{k} vs {ref}")
    except Exception as exc:
        check("与 cchess.zhash 交叉验证", False, str(exc))
    for iccs, val in (("h2e2", 0xAAA7), ("h9g7", 0x3A59), ("c3c4", 0x9585)):
        check(f"obk {val:#06x} → {iccs}", obk_decode_move(val) == iccs,
              str(obk_decode_move(val)))
        check(f"obk {iccs} → {val:#06x}", obk_encode_move(iccs) == val,
              hex(obk_encode_move(iccs)))
    check("obk 非法偏移返回 None", obk_decode_move(0x3C3D) is None)
    check("obk 起点==终点返回 None", obk_decode_move(0xAAAA) is None)

    print("⑤ 线路库")
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "t.txt"
        p.write_text("# 中炮\nh2e2 h9g7 h0g2\n*2 h2e2 b9c7\n", encoding="utf-8")
        bk = OpeningBook(p)
        # 两条线路首着相同（都是 h2e2），权重应累加为 1+2=3
        hit = bk.probe(START_FEN)
        check("起始局面只命中 1 个着法", len(hit) == 1, str(hit))
        check("同着法权重累加为 3",
              hit and hit[0]["move"] == "h2e2" and hit[0]["weight"] == 3, str(hit))
        picked = bk.pick(START_FEN, "best")
        check("best 取权重最高", picked and picked["move"] == "h2e2"
              and picked["weight"] == 3, str(picked))
        # 中炮之后的分支：h2e2 后黑应 h9g7 或 b9c7
        after = "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C2C4/9/RNBAKABNR b - - 0 1"
        nxt = bk.probe(after)
        check("中炮后命中 2 个着法", len(nxt) == 2, str(nxt))
        check("描述串含线路数", "2 条线路" in bk.describe(), bk.describe())

    print("\n结果:", "全部通过" if ok else "存在失败")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
