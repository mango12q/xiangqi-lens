# -*- coding: utf-8 -*-
"""
最小可用的 Pikafish UCI 客户端（zero-dependency，只用 subprocess + threading）。
同时演示如何用 cchess 做棋规校验、把识别结果转成 FEN、把 bestmove 转成屏幕坐标。

依赖：仅标准库 + cchess（可选）
"""
from __future__ import annotations

import os
import queue
import subprocess
import threading
import time


class UciEngine:
    """按 UCI 协议与引擎对话；Pikafish / ElephantEye(UCCI 见 UcciEngine)。"""

    def __init__(self, exe: str, cwd: str | None = None, init_cmd: str = "uci",
                 echo: bool = False, name: str = "engine"):
        self.exe, self.name, self.echo = exe, name, echo
        self.cwd = cwd or os.path.dirname(exe)
        self.q: queue.Queue[str] = queue.Queue()
        self.proc = subprocess.Popen(
            [exe],
            cwd=self.cwd,                      # ★ 必须：Pikafish 从 cwd 找 pikafish.nnue
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self.send(init_cmd)
        hdr = self.wait_for("ucciok" if init_cmd == "ucci" else "uciok", timeout=20)
        self.id_lines = [ln for ln in hdr if ln.startswith("id ")]
        self.option_lines = [ln for ln in hdr if ln.startswith("option ")]

    # ---------- 底层 IO ----------
    def _read_loop(self):
        for line in self.proc.stdout:            # type: ignore[union-attr]
            line = line.rstrip("\r\n")
            if self.echo:
                print(f"[{self.name}] {line}")
            self.q.put(line)
        self.q.put("__EOF__")

    def send(self, cmd: str):
        self.proc.stdin.write(cmd + "\n")        # type: ignore[union-attr]
        self.proc.stdin.flush()                  # type: ignore[union-attr]

    def wait_for(self, prefix: str, timeout: float = 30.0):
        out, t0 = [], time.time()
        while time.time() - t0 < timeout:
            try:
                line = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            if line == "__EOF__":
                break
            out.append(line)
            if line.startswith(prefix):
                return out
        return out

    def drain(self):
        while True:
            try:
                self.q.get_nowait()
            except queue.Empty:
                return

    # ---------- 语义封装 ----------
    def set_option(self, name: str, value):
        self.send(f"setoption name {name} value {value}")

    def isready(self, timeout: float = 60.0):
        self.send("isready")
        return self.wait_for("readyok", timeout)

    def analyse(self, fen: str, movetime_ms: int | None = None, depth: int | None = None,
                multipv: int | None = None, on_info=None, timeout: float = 120.0):
        """
        返回 dict: {'bestmove': 'h2e2', 'ponder': ..., 'info': [...], 'score_cp': int|None, 'depth': int|None}
        on_info: 可选回调，用于实时流式输出（给 GUI 用）
        """
        if multipv:
            self.set_option("MultiPV", multipv)
        self.isready()
        self.drain()
        self.send(f"position fen {fen}")
        if depth:
            self.send(f"go depth {depth}")
        elif movetime_ms:
            self.send(f"go movetime {movetime_ms}")
        else:
            self.send("go depth 16")

        infos, score_cp, depth_seen, best = [], None, None, None
        t0 = time.time()
        while time.time() - t0 < timeout:
            try:
                line = self.q.get(timeout=0.5)
            except queue.Empty:
                continue
            if line == "__EOF__":
                break
            if line.startswith("info"):
                infos.append(line)
                if on_info:
                    on_info(line)
                if " score " in line:
                    try:
                        seg = line.split(" score ", 1)[1].split()
                        if seg[0] == "cp":
                            score_cp = int(seg[1])
                        elif seg[0] == "mate":
                            score_cp = 30000 - abs(int(seg[1])) * 100
                        if "depth" in line:
                            depth_seen = int(line.split("depth ", 1)[1].split()[0])
                    except Exception:
                        pass
            if line.startswith("bestmove"):
                parts = line.split()
                best = parts[1] if len(parts) > 1 else None
                ponder = parts[3] if len(parts) > 3 and parts[2] == "ponder" else None
                return {"bestmove": best, "ponder": ponder, "info": infos,
                        "score_cp": score_cp, "depth": depth_seen}
        return {"bestmove": best, "ponder": None, "info": infos, "score_cp": score_cp, "depth": depth_seen}

    def stop(self):
        self.send("stop")

    def quit(self):
        try:
            self.send("quit")
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


class UcciEngine(UciEngine):
    """UCCI：握手指令是 ucci/ucciok，着法同样是 ICCS(如 h2e2)，但选项名多为小写、时间单位受 usemillisec 影响。"""

    def __init__(self, exe, cwd=None, **kw):
        super().__init__(exe, cwd=cwd, init_cmd="ucci", **kw)


if __name__ == "__main__":
    EXE = r"D:\opencode\xq_research\pikafish\Pikafish-Windows-x86-64-universal.exe"
    e = UciEngine(EXE)
    print("引擎 id:", e.id_lines[:2])
    print("选项数:", len(e.option_lines))
    e.set_option("Threads", 8)
    e.set_option("Hash", 256)
    e.isready()
    FEN = "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR w - - 0 1"
    r = e.analyse(FEN, movetime_ms=1000)
    print("bestmove:", r["bestmove"], "score(cp):", r["score_cp"], "depth:", r["depth"])

    # 与 cchess 联动：把 ICCS 着法转成中文，并做合法性校验
    try:
        from cchess import ChessBoard
        b = ChessBoard(FEN)
        legal = {f"{chr(97+a)}{r_}{chr(97+b)}{r2}" for (a, r_), (b, r2) in b.create_moves()}
        print("引擎着法在合法着法集合内:", r["bestmove"] in legal)
        print("中文着法:", b.copy().move_iccs(r["bestmove"]).to_text())
    except Exception as ex:
        print("cchess 联动失败:", ex)
    e.quit()
