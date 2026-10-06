# -*- coding: utf-8 -*-
"""
最小可用的 Pikafish UCI 客户端（zero-dependency，只用 subprocess + threading）。
同时演示如何用 cchess 做棋规校验、把识别结果转成 FEN、把 bestmove 转成屏幕坐标。

依赖：仅标准库 + cchess（可选）
"""
from __future__ import annotations

import ctypes
import os
import queue
import re
import subprocess
import threading
import time

# ---------------------------------------------------------------------------
# 子进程生命周期：把引擎放进「父进程一退出就一起死」的 Job Object
# ---------------------------------------------------------------------------
# Windows 上**子进程不会随父进程退出而终止**。所以关窗口、崩溃、被任务管理器
# 结束进程时，Pikafish 都可能活下来变成孤儿 —— 它会和当前引擎抢 CPU 与内存，
# 同样 movetime 的搜索要少算好几层。
#
# JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE 让内核在 job 句柄关闭（= 本进程退出）时
# 自动终止 job 内全部进程。这是**不依赖时序**的唯一保证：不靠 GUI 线程在正确
# 的时刻调用 shutdown()，也不靠线程 wait() 是否超时。
#
# 全程 best-effort：拿不到 job 或设置失败都只是失去这层保护，绝不影响启动。
_JOB_HANDLE = None
_JOB_TRIED = False
_JOB_LOCK = threading.Lock()

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JobObjectExtendedLimitInformation = 9


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong)]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32)]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t)]


def _ensure_job():
    """创建（一次）带 KILL_ON_JOB_CLOSE 的 job 并返回其句柄；失败返回 None。"""
    global _JOB_HANDLE, _JOB_TRIED
    with _JOB_LOCK:
        if _JOB_HANDLE is not None or _JOB_TRIED:
            return _JOB_HANDLE
        _JOB_TRIED = True
        if not hasattr(ctypes, "windll"):
            return None
        try:
            k32 = ctypes.windll.kernel32
            k32.CreateJobObjectW.restype = ctypes.c_void_p
            handle = k32.CreateJobObjectW(None, None)
            if not handle:
                return None
            info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
            info.BasicLimitInformation.LimitFlags = \
                _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            ok = k32.SetInformationJobObject(
                ctypes.c_void_p(handle),
                _JobObjectExtendedLimitInformation,
                ctypes.byref(info),
                ctypes.sizeof(info))
            if not ok:
                return None
            _JOB_HANDLE = handle          # 故意不关闭：句柄随进程退出才关闭
            return _JOB_HANDLE
        except Exception:
            return None


def _assign_to_job(proc) -> None:
    """把 ``proc`` 放进 kill-on-close job（best-effort）。"""
    handle = _ensure_job()
    if not handle:
        return
    try:
        ctypes.windll.kernel32.AssignProcessToJobObject(
            ctypes.c_void_p(handle), ctypes.c_void_p(int(proc._handle)))
    except Exception:
        pass


class UciEngine:
    """按 UCI 协议与引擎对话；Pikafish / ElephantEye(UCCI 见 UcciEngine)。"""

    def __init__(self, exe: str, cwd: str | None = None, init_cmd: str = "uci",
                 echo: bool = False, name: str = "engine"):
        self.exe, self.name, self.echo = exe, name, echo
        self.cwd = cwd or os.path.dirname(exe)
        self.q: queue.Queue[str] = queue.Queue()
        # ★ 引擎已死的**权威**信号。不能用队列里的 "__EOF__" 哨兵代替它 ——
        #   哨兵只能被消费一次，一旦被 wait_for / drain 吃掉，之后所有等待都
        #   只能空转满超时，且无法区分「引擎已死」和「引擎没响应」。
        self._dead = threading.Event()
        self.proc = subprocess.Popen(
            [exe],
            cwd=self.cwd,                      # ★ 必须：Pikafish 从 cwd 找 pikafish.nnue
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        # 让引擎进程跟随本进程一起结束（见模块顶部 Job Object 说明）
        _assign_to_job(self.proc)
        self.send(init_cmd)
        hdr = self.wait_for("ucciok" if init_cmd == "ucci" else "uciok", timeout=20)
        self.id_lines = [ln for ln in hdr if ln.startswith("id ")]
        self.option_lines = [ln for ln in hdr if ln.startswith("option ")]

    # ---------- 底层 IO ----------
    def _read_loop(self):
        try:
            for line in self.proc.stdout:        # type: ignore[union-attr]
                line = line.rstrip("\r\n")
                if self.echo:
                    print(f"[{self.name}] {line}")
                self.q.put(line)
        finally:
            # 无论正常 EOF 还是读异常，都要宣告「引擎没了」。
            self._dead.set()
            self.q.put("__EOF__")                # 兼容既有调用方

    @property
    def dead(self) -> bool:
        """引擎进程是否已结束（reader 线程已退出）。"""
        return self._dead.is_set()

    def send(self, cmd: str):
        # ★ 引擎已死时不要写管道：那会抛裸的 ``OSError: [Errno 22]``，
        #   既看不出原因，也无法与「管道恰好满」区分。改成明确的异常。
        if self._dead.is_set():
            raise RuntimeError(f"引擎进程已退出，指令未发送: {cmd!r}")
        try:
            self.proc.stdin.write(cmd + "\n")    # type: ignore[union-attr]
            self.proc.stdin.flush()              # type: ignore[union-attr]
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"引擎管道已关闭（{exc}），指令未发送: {cmd!r}") from exc

    def wait_for(self, prefix: str, timeout: float = 30.0):
        out, t0 = [], time.time()
        while time.time() - t0 < timeout:
            # ★ 引擎已死且队列已排空 → 立刻返回，不要再空转满 timeout。
            #   没有这一条时，引擎死后第二次 wait_for 会白等整个超时
            #   （实测 1.01s），isready 也无法区分「死了」和「没响应」。
            if self._dead.is_set() and self.q.empty():
                break
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
        # 引擎已死就直接返回空列表（调用方可用 ``dead`` 区分「死了」与「没响应」），
        # 不再尝试写管道、也不空转满超时。
        if self._dead.is_set():
            return []
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

        infos, score_cp, depth_seen = [], None, None
        best = ponder = None
        got_bestmove = False
        t0 = time.time()
        try:
            while time.time() - t0 < timeout:
                if self._dead.is_set() and self.q.empty():
                    break
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
                        # ★ 只接受 multipv 1（或没写 multipv）的分数。MultiPV>1
                        #   时引擎按 multipv 升序逐条打印，无条件覆盖会让返回值
                        #   变成**最差那条**主变例的分数（实测 multipv 1 cp300 /
                        #   2 cp40 / 3 cp-1200 → 旧代码返回 -1200，应为 300）。
                        m = re.search(r"\bmultipv (\d+)", line)
                        if m is None or int(m.group(1)) == 1:
                            try:
                                seg = line.split(" score ", 1)[1].split()
                                if seg[0] == "cp":
                                    score_cp = int(seg[1])
                                elif seg[0] == "mate":
                                    score_cp = 30000 - abs(int(seg[1])) * 100
                                if "depth" in line:
                                    depth_seen = int(
                                        line.split("depth ", 1)[1].split()[0])
                            except Exception:
                                pass
                if line.startswith("bestmove"):
                    parts = line.split()
                    best = parts[1] if len(parts) > 1 else None
                    ponder = (parts[3]
                              if len(parts) > 3 and parts[2] == "ponder" else None)
                    got_bestmove = True
                    break
        finally:
            # ★ 超时/异常都必须显式停搜索。否则引擎仍在跑，后续 info 行无人
            #   消费地灌进无界队列，而下一次 analyse() 会把这段残留的
            #   bestmove 当成**新局面的**结果 —— 结果串味。
            if not got_bestmove:
                try:
                    self.stop()
                    self.wait_for("bestmove", timeout=1.0)
                except Exception:
                    pass
                self.drain()
        return {"bestmove": best, "ponder": ponder, "info": infos,
                "score_cp": score_cp, "depth": depth_seen}

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
