# -*- coding: utf-8 -*-
"""开局库对比测试 —— 公共脚手架。

包含：
  * 四个被测库的清单
  * Pikafish UCI 封装（支持 searchmoves 单着评分）
  * cchess 局面工具（合法性校验、走子、FEN 规范化）
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app_book import OpeningBook  # noqa: E402

BOOK_DIR = ROOT / "开局库"
ENGINE = ROOT / "engines" / "pikafish.exe"
ENGINE_DIR = ROOT / "engines"

# short key -> 文件名
BOOKS: dict[str, str] = {
    "yunxiao": "云霄剑诀7.5.obk",
    "xianfeng": "先锋无敌nn库241111-book.obk",
    "tulong": "最新屠龙商业库.obk",
    "jingxiang": "静香库.obk",
}

BOOK_LABEL: dict[str, str] = {
    "yunxiao": "云霄剑诀7.5",
    "xianfeng": "先锋无敌nn库241111",
    "tulong": "最新屠龙商业库",
    "jingxiang": "静香库",
}

START_FEN = "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR w - - 0 1"

MATE_CP = 30000


def book_path(key: str) -> Path:
    return BOOK_DIR / BOOKS[key]


def open_books(keys=None) -> dict[str, OpeningBook]:
    out = {}
    for k in (keys or BOOKS):
        out[k] = OpeningBook(book_path(k))
    return out


# ---------------------------------------------------------------------------
# 局面工具
# ---------------------------------------------------------------------------
def fen_key(fen: str) -> str:
    """规范化 FEN 作为局面键：棋盘段 + 轮次。"""
    p = (fen or "").split()
    return f"{p[0]} {p[1] if len(p) > 1 else 'w'}"


def apply_move(fen: str, iccs: str):
    """在 fen 上走着 iccs。合法返回新 FEN（已换边），非法返回 None。"""
    from cchess import ChessBoard
    try:
        b = ChessBoard(fen)
    except Exception:
        return None
    if b.move_iccs(iccs) is None:
        return None
    b.next_turn()
    return b.to_fen()


def side_to_move(fen: str) -> str:
    p = (fen or "").split()
    return p[1] if len(p) > 1 else "w"


# ---------------------------------------------------------------------------
# Pikafish 封装
# ---------------------------------------------------------------------------
class Engine:
    """极简 UCI 封装：一次分析一个局面，可选限定根着法。"""

    def __init__(self, exe: Path = ENGINE, cwd: Path = ENGINE_DIR,
                 threads: int = 1, hash_mb: int = 512) -> None:
        self.proc = subprocess.Popen(
            [str(exe)], cwd=str(cwd),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1)
        self.lock = threading.Lock()
        self._send("uci")
        self._wait("uciok")
        self._send(f"setoption name Threads value {threads}")
        self._send(f"setoption name Hash value {hash_mb}")
        self._ready()

    def _send(self, cmd: str) -> None:
        self.proc.stdin.write(cmd + "\n")
        self.proc.stdin.flush()

    def _wait(self, token: str, timeout: float = 60.0) -> None:
        t0 = time.time()
        while time.time() - t0 < timeout:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("引擎进程结束")
            if line.strip().startswith(token):
                return
        raise TimeoutError(token)

    def _ready(self) -> None:
        self._send("isready")
        self._wait("readyok")

    def new_game(self) -> None:
        """重置到干净状态（清空置换表），保证逐局可复现。"""
        with self.lock:
            self._send("ucinewgame")
            self._send("setoption name Clear Hash")
            self._send("isready")
            self._wait("readyok")

    def analyse(self, fen: str, nodes: int = 100000, movetime: int | None = None,
                searchmoves: str | None = None, multipv: int = 1) -> dict:
        """返回 {'best', 'score', 'mate', 'depth', 'lines'}。

        ``score`` 是从**行棋方**视角的厘兵分（mate 折算成 ±MATE_CP）。
        ``lines`` 是 MultiPV 结果 ``[(move, score), ...]``，按引擎给出顺序。
        """
        with self.lock:
            if multipv > 1:
                self._send(f"setoption name MultiPV value {multipv}")
            self._send(f"position fen {fen}")
            cmd = (f"go movetime {int(movetime)}" if movetime
                   else f"go nodes {int(nodes)}")
            if searchmoves:
                cmd += f" searchmoves {searchmoves}"
            self._send(cmd)
            best, score, mate, depth = None, None, None, 0
            lines: dict[int, tuple[str, int]] = {}
            while True:
                line = self.proc.stdout.readline()
                if not line:
                    raise RuntimeError("引擎进程结束")
                s = line.strip()
                if s.startswith("info ") and " pv " in s and " score " in s:
                    toks = s.split()
                    try:
                        depth = int(toks[toks.index("depth") + 1])
                    except Exception:
                        pass
                    cp = None
                    if "score" in toks:
                        i = toks.index("score")
                        if toks[i + 1] == "cp":
                            try:
                                cp = int(toks[i + 2])
                            except Exception:
                                cp = None
                        elif toks[i + 1] == "mate":
                            try:
                                m = int(toks[i + 2])
                                cp = (MATE_CP - abs(m)) if m > 0 else -(MATE_CP - abs(m))
                                if multipv == 1:
                                    mate = m
                            except Exception:
                                cp = None
                    if "pv" in toks and cp is not None:
                        pv_move = toks[toks.index("pv") + 1]
                        idx = 1
                        if "multipv" in toks:
                            try:
                                idx = int(toks[toks.index("multipv") + 1])
                            except Exception:
                                idx = 1
                        lines[idx] = (pv_move, cp)
                        if multipv == 1:
                            best, score = pv_move, cp
                elif s.startswith("bestmove"):
                    mv = s.split()[1] if len(s.split()) > 1 else None
                    if multipv > 1:
                        self._send("setoption name MultiPV value 1")
                    if mate is not None:
                        cp = (MATE_CP - abs(mate)) if mate > 0 else -(MATE_CP - abs(mate))
                    else:
                        cp = score if score is not None else 0
                    ordered = [lines[i] for i in sorted(lines)]
                    return {"best": best or mv, "raw_best": mv, "score": cp,
                            "mate": mate, "depth": depth,
                            "lines": ordered}

    def close(self) -> None:
        try:
            self._send("quit")
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
