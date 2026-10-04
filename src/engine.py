"""象棋引擎（UCI 协议）通信层。

设计要点
------------------------------------------------------------------
* 子进程 + 两条常驻泵线程：一条读 stdout、一条读 stderr，避免管道写满
  导致引擎死锁（这是自研引擎封装最常见的坑）。
* 所有输出按行解析成 `EngineInfo`，通过回调推给调用方；主逻辑不必
  轮询引擎。
* `analysis()` 返回一个可取消的上下文：调用方拿到 `bestmove` 后自行
  决定何时停。

已实测的引擎
------------------------------------------------------------------
* Pikafish（通用二进制，UCI，坐标着法如 `h2e2`）
"""

from __future__ import annotations

import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------


@dataclass
class EngineInfo:
    """一次 `info` 输出（引擎思考过程中的一行）。"""

    depth: int = 0
    seldepth: int = 0
    score_cp: int | None = None      # 分数，单位 centipawn
    score_mate: int | None = None    # 若为杀棋，剩余步数（正负表示谁杀）
    lowerbound: bool = False
    upperbound: bool = False
    nodes: int = 0
    nps: int = 0
    time_ms: int = 0
    hashfull: int = 0
    multipv: int = 1
    pv: list[str] = field(default_factory=list)   # 主变例，ICCS 着法序列

    @property
    def score_text(self) -> str:
        """人类可读的分数显示。"""
        if self.score_mate is not None:
            if self.score_mate > 0:
                return f"杀 {self.score_mate}"
            return f"被杀 {-self.score_mate}"
        if self.score_cp is None:
            return "—"
        return f"{self.score_cp / 100:+.2f}"

    @property
    def score_value(self) -> int:
        """归一化到 centipawn 用于排序/绘制评估条；杀棋给一个极大值。"""
        if self.score_mate is not None:
            base = 30000
            sign = 1 if self.score_mate > 0 else -1
            return sign * (base - abs(self.score_mate) * 10)
        return self.score_cp or 0

    def copy(self) -> "EngineInfo":
        return EngineInfo(**{k: getattr(self, k) for k in self.__dataclass_fields__})


@dataclass
class EngineOption:
    """引擎通过 `option name ...` 声明的可调项。"""

    name: str
    type: str = "string"          # check / spin / combo / button / string
    default: str = ""
    min: int | None = None
    max: int | None = None
    var: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# 引擎进程
# --------------------------------------------------------------------------


class EngineError(RuntimeError):
    pass


class Engine:
    """一个 UCI 引擎进程的封装。

    典型用法::

        eng = Engine(r"engines\\pikafish.exe")
        eng.start()
        eng.set_option("Threads", 8)
        eng.new_game()
        eng.start_analysis(fen, on_info=print)
        ...
        eng.stop()
        eng.quit()
    """

    def __init__(self, exe_path: str | os.PathLike[str],
                 workdir: str | os.PathLike[str] | None = None,
                 name: str = "") -> None:
        self.exe_path = str(Path(exe_path))
        if not Path(self.exe_path).is_file():
            raise EngineError(f"引擎文件不存在: {self.exe_path}")
        self.name = name or Path(self.exe_path).stem
        self._workdir = str(workdir) if workdir else str(Path(self.exe_path).parent)

        self._proc: subprocess.Popen[bytes] | None = None
        self._lock = threading.RLock()
        self._alive = False

        self._stdout_q: queue.Queue[str] = queue.Queue()
        self._stderr_lines: list[str] = []
        self._pump_threads: list[threading.Thread] = []
        self._reader_thread: threading.Thread | None = None

        self.options: dict[str, EngineOption] = {}
        self.engine_name = ""
        self.engine_author = ""

        # 当前分析状态
        self._analyzing = False
        self._searching = False
        self._on_info: Callable[[EngineInfo], None] | None = None
        self._multipv: list[EngineInfo] = []
        self._last_info: EngineInfo | None = None
        self._bestmove: str | None = None
        self._bestmove_event = threading.Event()
        self._current_fen = ""

    # ---------------- 生命周期 ----------------

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self, timeout: float = 20.0) -> None:
        """启动进程并完成 uci 握手。"""
        if self.is_running:
            return
        creationflags = 0
        if os.name == "nt":
            # 不弹控制台窗口
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self._proc = subprocess.Popen(
                [self.exe_path],
                cwd=self._workdir,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
                creationflags=creationflags,
            )
        except OSError as exc:
            raise EngineError(f"无法启动引擎 {self.exe_path}: {exc}") from exc

        self._alive = True
        self._stdout_q = queue.Queue()
        self._stderr_lines.clear()
        self._pump_threads = [
            threading.Thread(target=self._pump_stdout, name=f"{self.name}-stdout", daemon=True),
            threading.Thread(target=self._pump_stderr, name=f"{self.name}-stderr", daemon=True),
        ]
        for t in self._pump_threads:
            t.start()

        # 握手
        self._send("uci")
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self._read_line(0.2)
            if line is None:
                if not self.is_running:
                    raise EngineError(self._crash_message("引擎在握手中退出"))
                continue
            self._parse_handshake(line)
            if line.strip() == "uciok":
                self._reader_thread = threading.Thread(
                    target=self._read_loop, name=f"{self.name}-reader", daemon=True)
                self._reader_thread.start()
                return
        raise EngineError(f"引擎 {self.name} 未在 {timeout:.0f}s 内返回 uciok")

    def _crash_message(self, prefix: str) -> str:
        tail = "\n".join(self._stderr_lines[-8:]) if self._stderr_lines else "(无 stderr 输出)"
        return f"{prefix}\n{tail}"

    def quit(self, timeout: float = 5.0) -> None:
        """发送 quit 并等待退出，必要时强制终止。"""
        proc = self._proc
        if proc is None:
            return
        try:
            if proc.poll() is None:
                try:
                    self._send("quit")
                except Exception:
                    pass
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)
        finally:
            self._alive = False
            self._proc = None
            self._searching = False
            self._analyzing = False
            self._bestmove_event.set()

    def restart(self) -> None:
        self.quit()
        self.start()

    # ---------------- 底层收发 ----------------

    def _send(self, cmd: str) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            raise EngineError(f"引擎未运行，无法发送: {cmd}")
        with self._lock:
            try:
                proc.stdin.write((cmd + "\n").encode("utf-8", "replace"))
                proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                raise EngineError(self._crash_message(f"向引擎写入失败 ({cmd})")) from exc

    def _pump_stdout(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            for raw in iter(proc.stdout.readline, b""):
                text = raw.decode("utf-8", "replace").rstrip("\r\n")
                self._stdout_q.put(text)
        except (OSError, ValueError):
            pass

    def _pump_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            for raw in iter(proc.stderr.readline, b""):
                self._stderr_lines.append(raw.decode("utf-8", "replace").rstrip("\r\n"))
                if len(self._stderr_lines) > 400:
                    del self._stderr_lines[:200]
        except (OSError, ValueError):
            pass

    def _read_line(self, timeout: float) -> str | None:
        try:
            return self._stdout_q.get(timeout=timeout)
        except queue.Empty:
            return None

    # ---------------- 握手解析 ----------------

    def _parse_handshake(self, line: str) -> None:
        text = line.strip()
        if text.startswith("id name"):
            self.engine_name = text[len("id name"):].strip()
        elif text.startswith("id author"):
            self.engine_author = text[len("id author"):].strip()
        elif text.startswith("option"):
            opt = self._parse_option(text)
            if opt:
                self.options[opt.name] = opt

    @staticmethod
    def _parse_option(text: str) -> EngineOption | None:
        """解析 `option name X type Y default Z min A max B var C var D`。"""
        if not text.startswith("option name "):
            return None
        rest = text[len("option name "):]
        # 按关键字切分，保留名字中可能出现的同名关键字（用右起查找 type）
        type_idx = rest.find(" type ")
        if type_idx < 0:
            return None
        name = rest[:type_idx].strip()
        rest = rest[type_idx + len(" type "):]

        opt = EngineOption(name=name)
        parts = rest.split()
        if not parts:
            return None
        opt.type = parts[0].lower()
        i = 1
        while i < len(parts):
            key = parts[i]
            if key == "default" and i + 1 < len(parts):
                opt.default = parts[i + 1]
                i += 2
            elif key == "min" and i + 1 < len(parts):
                try:
                    opt.min = int(parts[i + 1])
                except ValueError:
                    pass
                i += 2
            elif key == "max" and i + 1 < len(parts):
                try:
                    opt.max = int(parts[i + 1])
                except ValueError:
                    pass
                i += 2
            elif key == "var" and i + 1 < len(parts):
                opt.var.append(parts[i + 1])
                i += 2
            else:
                i += 1
        return opt

    # ---------------- 配置 ----------------

    def set_option(self, name: str, value: str | int | bool) -> None:
        if isinstance(value, bool):
            value = "true" if value else "false"
        self._send(f"setoption name {name} value {value}")

    def new_game(self) -> None:
        self.stop()
        self._send("ucinewgame")
        self._multipv.clear()
        self._last_info = None
        self._bestmove = None
        self._bestmove_event.clear()

    # ---------------- 分析 ----------------

    def _read_loop(self) -> None:
        """常驻读取引擎输出并分派。"""
        while self._alive and self.is_running:
            line = self._read_line(0.5)
            if line is None:
                if not self.is_running:
                    break
                continue
            text = line.strip()
            if not text:
                continue
            if text.startswith("info "):
                info = self.parse_info(text)
                if info is not None:
                    self._handle_info(info)
            elif text.startswith("bestmove"):
                parts = text.split()
                self._bestmove = parts[1] if len(parts) > 1 else None
                self._searching = False
                self._bestmove_event.set()

    def _handle_info(self, info: EngineInfo) -> None:
        self._last_info = info
        if info.multipv > 1:
            while len(self._multipv) < info.multipv:
                self._multipv.append(EngineInfo())
            self._multipv[info.multipv - 1] = info
        else:
            self._multipv = [info]
        cb = self._on_info
        if cb is not None:
            try:
                cb(info)
            except Exception:
                pass

    @staticmethod
    def parse_info(text: str) -> EngineInfo | None:
        """把一条 `info ...` 解析成 EngineInfo。"""
        if not text.startswith("info"):
            return None
        parts = text.split()
        info = EngineInfo()
        i = 1
        n = len(parts)
        while i < n:
            key = parts[i]
            if key == "depth" and i + 1 < n:
                info.depth = _to_int(parts[i + 1])
                i += 2
            elif key == "seldepth" and i + 1 < n:
                info.seldepth = _to_int(parts[i + 1])
                i += 2
            elif key == "multipv" and i + 1 < n:
                info.multipv = _to_int(parts[i + 1], 1)
                i += 2
            elif key == "score" and i + 2 < n:
                kind = parts[i + 1]
                val = _to_int(parts[i + 2])
                if kind == "cp":
                    info.score_cp = val
                elif kind == "mate":
                    info.score_mate = val
                i += 3
                if i < n and parts[i] in ("lowerbound", "upperbound"):
                    info.lowerbound = parts[i] == "lowerbound"
                    info.upperbound = parts[i] == "upperbound"
                    i += 1
            elif key == "nodes" and i + 1 < n:
                info.nodes = _to_int(parts[i + 1])
                i += 2
            elif key == "nps" and i + 1 < n:
                info.nps = _to_int(parts[i + 1])
                i += 2
            elif key == "hashfull" and i + 1 < n:
                info.hashfull = _to_int(parts[i + 1])
                i += 2
            elif key == "time" and i + 1 < n:
                info.time_ms = _to_int(parts[i + 1])
                i += 2
            elif key == "pv":
                info.pv = parts[i + 1:]
                break
            else:
                i += 1
        return info

    def start_analysis(self, fen: str,
                       on_info: Callable[[EngineInfo], None] | None = None,
                       depth: int = 0, movetime_ms: int = 0,
                       nodes: int = 0, multipv: int = 1,
                       infinite: bool = False,
                       searchmoves: list[str] | None = None) -> None:
        """让引擎开始分析指定局面。

        `on_info` 会在引擎线程被调用，GUI 侧需要自行丢进队列再更新界面。
        不传任何限制参数且 `infinite=False` 时按 depth 兜底为 24，避免
        引擎无限思考。
        """
        self.stop()
        self._on_info = on_info
        self._bestmove = None
        self._bestmove_event.clear()
        self._current_fen = fen
        self._multipv = []

        self._send(f"position fen {fen}")
        if multipv > 1:
            self._send(f"setoption name MultiPV value {multipv}")

        if infinite:
            self._send("go infinite")
        else:
            limits = []
            if searchmoves:
                limits.append("searchmoves " + " ".join(searchmoves))
            if nodes > 0:
                limits.append(f"nodes {nodes}")
            elif movetime_ms > 0:
                limits.append(f"movetime {int(movetime_ms)}")
            elif depth > 0:
                limits.append(f"depth {int(depth)}")
            else:
                limits.append("depth 24")
            self._send("go " + " ".join(limits))
        self._searching = True

    def stop(self) -> str | None:
        """请求停止搜索并等待 bestmove。

        返回引擎给出的最佳着法（ICCS 字符串）或 None。
        未在搜索且已有结果时，直接返回上次结果。
        """
        if not self.is_running:
            return self._bestmove
        if not self._searching:
            return self._bestmove
        try:
            self._send("stop")
        except EngineError:
            self._searching = False
            return self._bestmove
        if self._bestmove_event.wait(timeout=5.0):
            return self._bestmove
        self._searching = False
        return self._bestmove

    def wait_bestmove(self, timeout: float | None = None) -> str | None:
        """等待当前搜索自然结束（例如 movetime 模式）。"""
        if self._bestmove_event.wait(timeout=timeout):
            return self._bestmove
        return None

    @property
    def is_searching(self) -> bool:
        return self._searching

    @property
    def last_info(self) -> EngineInfo | None:
        return self._last_info

    @property
    def multipv_lines(self) -> list[EngineInfo]:
        return list(self._multipv)

    @property
    def current_fen(self) -> str:
        return self._current_fen

    # ---------------- 便捷查询 ----------------

    def think(self, fen: str, depth: int = 0, movetime_ms: int = 0,
              multipv: int = 1, timeout: float = 30.0) -> tuple[str | None, EngineInfo | None]:
        """同步思考一步并返回 (最佳着法, 最后一条 info)。

        适合"点一下要个建议"的场景；连续跟局请用 start_analysis。
        """
        self.start_analysis(fen, depth=depth, movetime_ms=movetime_ms, multipv=multipv)
        best = self.wait_bestmove(timeout=timeout)
        if best is None:
            best = self.stop()
        return best, self._last_info


def _to_int(text: str, default: int = 0) -> int:
    try:
        return int(text)
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------
# 引擎发现
# --------------------------------------------------------------------------

_COMMON_ENGINE_NAMES = ("pikafish", "pikafish-avx2", "alphazero", "elephant",
                        "eleeye", "cyclone", "xqwlight", "fairy-stockfish")


def discover_engines(*dirs: str | os.PathLike[str]) -> list[Path]:
    """在给定目录里找出可能的引擎可执行文件。"""
    found: list[Path] = []
    seen: set[str] = set()
    for d in dirs:
        p = Path(d)
        if not p.is_dir():
            continue
        for f in sorted(p.rglob("*.exe")):
            low = f.name.lower()
            if any(k in low for k in _COMMON_ENGINE_NAMES) or "chess" in low:
                key = str(f.resolve()).lower()
                if key not in seen:
                    seen.add(key)
                    found.append(f)
    return found
