# -*- coding: utf-8 -*-
"""后端：抓帧 + 识别 + 引擎 + 棋规校验。

识别、引擎、棋规全部复用已验证的第三方/已交付组件：

* 识别 : ``xq_vision.XiangqiVision`` —— 两段式 ONNX。
         权重来自 HF Space ``yolo12138/Chinese_Chess_Recognition``（MIT），
         上游工程 ``TheOne1006/chinese-chess-recognition``（Apache-2.0）
* 引擎 : ``engine_client.UciEngine`` —— Pikafish UCI 客户端
* 棋规 : ``cchess`` —— 合法着法校验、中文着法

本模块只做封装与调度，不重复实现上述算法。
"""
from __future__ import annotations

import ctypes
import sys
import time
from pathlib import Path

import cv2
import numpy as np

# 版本号：同时用于窗口标题、运行日志与打包产物命名，
# 便于用户确认自己用的是哪一版。
__version__ = "0.4.0"

HERE = Path(__file__).resolve().parent


def app_base() -> Path:
    """返回"应用根目录"—— 资源应当相对它来查找。

    * 源码运行：脚本所在目录
    * PyInstaller 打包后（``sys.frozen``）：**exe 所在目录**。
      注意不能用 ``sys._MEIPASS``，那是临时解包目录；模型与引擎
      体积大（约 95MB），是作为外部文件放在 exe 同级的，这样
      后续也能单独替换模型而不必重新打包。
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return HERE


def _find_research() -> Path:
    """定位识别/引擎资源目录（xq_research）。

    按顺序尝试若干候选位置，取第一个含 ``hf_model`` 的：
      1. <应用根目录>/xq_research
      2. <应用根目录>/../xq_research
      3. <应用根目录>/resources/xq_research
      4. 当前工作目录及其父目录下的 xq_research

    之所以给这么多候选，是因为它既要支持源码运行，也要支持打包后
    exe 与资源同级或放在 resources 子目录这两种发行方式。
    """
    base = app_base()
    cands = [
        base / "xq_research",
        base.parent / "xq_research",
        base / "resources" / "xq_research",
        Path.cwd() / "xq_research",
        Path.cwd().parent / "xq_research",
    ]
    for cand in cands:
        if (cand / "hf_model").is_dir():
            return cand
    # 兜底：返回首选位置，让后续报错明确指出缺什么
    return base / "xq_research"


RESEARCH = _find_research()
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(RESEARCH))
POSE_ONNX = RESEARCH / "hf_model" / "onnx" / "pose" / "4_v6-0301.onnx"
CLS_ONNX = RESEARCH / "hf_model" / "onnx" / "layout_recognition" / "nano_v3-0319.onnx"
ENGINE_EXE = RESEARCH / "pikafish" / "Pikafish-Windows-x86-64-universal.exe"

PIECE_CN = {
    "K": "帅", "A": "仕", "B": "相", "N": "马", "R": "车", "C": "炮", "P": "兵",
    "k": "将", "a": "士", "b": "象", "n": "马", "r": "车", "c": "炮", "p": "卒",
}


def enable_dpi_awareness() -> str:
    """Per-Monitor V2 DPI 感知。

    不做这一步，高 DPI 缩放下 GetWindowRect / 截图坐标会整体差 1.5 倍。
    """
    try:
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return "Per-Monitor V2"
    except Exception:
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return "Per-Monitor"
    except Exception:
        pass
    return "None"


# ---------------------------------------------------------------------------
# 抓帧
# ---------------------------------------------------------------------------
class ScreenSource:
    """窗口抓帧：PrintWindow 后台截图优先，mss 前台抓屏兜底。

    PrintWindow 必须带 ``PW_RENDERFULLCONTENT (0x2)``，否则 CEF / DirectX
    渲染的窗口（JJ象棋小程序、微信）会截出空白图。
    """

    def __init__(self) -> None:
        self.hwnd: int | None = None
        self.title = ""
        # 抓帧时的窗口矩形 (l, t, r, b)。截图坐标 + 该原点 = 屏幕物理坐标，
        # 自动走棋点击时靠它把 ``src_*`` 换算成屏幕坐标。**必须与返回的那
        # 一帧对齐**，否则窗口移动后会用陈旧坐标点到别处。
        self.rect: tuple[int, int, int, int] | None = None
        self.stats = {"background": 0, "foreground": 0, "failed": 0}

    @staticmethod
    def list_windows(min_size: int = 200):
        """枚举可见且有标题的顶层窗口，按面积降序返回。"""
        import win32gui

        out = []

        def cb(hwnd, _):
            if not win32gui.IsWindowVisible(hwnd):
                return True
            title = win32gui.GetWindowText(hwnd)
            if not title:
                return True
            l, t, r, b = win32gui.GetWindowRect(hwnd)
            if (r - l) >= min_size and (b - t) >= min_size:
                out.append((hwnd, title, win32gui.GetClassName(hwnd), (l, t, r, b)))
            return True

        win32gui.EnumWindows(cb, None)
        out.sort(key=lambda x: -(x[3][2] - x[3][0]) * (x[3][3] - x[3][1]))
        return out

    def attach(self, hwnd: int, title: str) -> None:
        self.hwnd = hwnd
        self.title = title

    def grab(self) -> np.ndarray | None:
        """抓一帧，返回 BGR 数组；失败返回 None。"""
        if self.hwnd is None:
            return None
        import win32gui
        if not win32gui.IsWindow(self.hwnd):
            return None

        img = self._print_window()
        if img is not None:
            a = img.astype(np.float32)
            if float(a.std()) > 3.0 and float((a.max(axis=2) > 12).mean()) > 0.02:
                self.stats["background"] += 1
                return img
        img = self._screen_region()
        if img is not None:
            self.stats["foreground"] += 1
            return img
        self.stats["failed"] += 1
        self.rect = None          # 两路都失败：清掉，避免陈旧矩形被点击逻辑误用
        return None

    def _print_window(self) -> np.ndarray | None:
        import win32gui
        import win32ui

        try:
            l, t, r, b = win32gui.GetWindowRect(self.hwnd)
        except Exception:
            return None
        w, h = r - l, b - t
        if w <= 0 or h <= 0:
            return None

        hwnd_dc = mfc_dc = save_dc = bmp = None
        try:
            hwnd_dc = win32gui.GetWindowDC(self.hwnd)
            mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
            save_dc = mfc_dc.CreateCompatibleDC()
            bmp = win32ui.CreateBitmap()
            bmp.CreateCompatibleBitmap(mfc_dc, w, h)
            save_dc.SelectObject(bmp)
            ok = ctypes.windll.user32.PrintWindow(self.hwnd, save_dc.GetSafeHdc(), 0x2)
            if not ok:
                ok = ctypes.windll.user32.PrintWindow(self.hwnd, save_dc.GetSafeHdc(), 0)
            if not ok:
                return None
            info = bmp.GetInfo()
            bits = bmp.GetBitmapBits(True)
            bw, bh = info["bmWidth"], info["bmHeight"]
            buf = np.frombuffer(bits, dtype=np.uint8)
            if buf.size < bw * bh * 4:
                return None
            arr = buf[: bw * bh * 4].reshape(bh, bw, 4)
            self.rect = (l, t, r, b)                        # 与返回帧对齐
            return np.ascontiguousarray(arr[:, :, :3])      # BGRA -> BGR
        except Exception:
            return None
        finally:
            try:
                if bmp is not None:
                    win32gui.DeleteObject(bmp.GetHandle())
            except Exception:
                pass
            for dc in (save_dc, mfc_dc):
                try:
                    if dc is not None:
                        dc.DeleteDC()
                except Exception:
                    pass
            try:
                if hwnd_dc:
                    win32gui.ReleaseDC(self.hwnd, hwnd_dc)
            except Exception:
                pass

    def _screen_region(self) -> np.ndarray | None:
        import win32gui
        try:
            l, t, r, b = win32gui.GetWindowRect(self.hwnd)
        except Exception:
            return None
        w, h = r - l, b - t
        if w <= 0 or h <= 0:
            return None
        try:
            import mss
            factory = getattr(mss, "MSS", None) or mss.mss
            with factory() as m:
                shot = m.grab({"left": l, "top": t, "width": w, "height": h})
                arr = np.frombuffer(shot.bgra, dtype=np.uint8).reshape(
                    shot.height, shot.width, 4)
                self.rect = (l, t, r, b)                    # 与返回帧对齐
                return np.ascontiguousarray(arr[:, :, :3])
        except Exception:
            return None


# ---------------------------------------------------------------------------
# 识别 / 引擎 / 棋规 薄封装
# ---------------------------------------------------------------------------
def create_vision(cuda: bool = True):
    from xq_vision import XiangqiVision
    return XiangqiVision(str(POSE_ONNX), str(CLS_ONNX), prefer_cuda=cuda)


def window_state(hwnd: int) -> tuple[bool, bool, str]:
    """查询窗口状态，返回 ``(是否有效, 是否最小化, 提示)``。

    为什么要单独判最小化：窗口最小化时 ``GetWindowRect`` 只返回标题栏尺寸
    （实测 JJ象棋 变成 237x39），``PrintWindow`` 也拿不到棋盘内容，于是
    识别结果全是垃圾。这类失败如果只报"局面不合法"，用户根本不知道为什么，
    因此要明确区分出来。
    """
    try:
        import win32gui
        if not win32gui.IsWindow(hwnd):
            return False, False, "窗口已关闭"
        if win32gui.IsIconic(hwnd):
            return False, True, "目标窗口已最小化，请先还原窗口"
        l, t, r, b = win32gui.GetWindowRect(hwnd)
        if (r - l) < 200 or (b - t) < 200:
            return False, False, f"窗口尺寸过小（{r - l}x{b - t}），无法识别棋盘"
        return True, False, ""
    except Exception as exc:
        return False, False, f"窗口状态检查失败: {exc}"


def list_windows_any(min_size: int = 200):
    """枚举所有带标题的顶层窗口（**包含最小化的**）。

    与 ``ScreenSource.list_windows`` 的区别：后者只返回可见窗口，
    用户把对局窗口最小化后就找不到了。
    """
    import win32gui

    out = []

    def cb(hwnd, _):
        title = win32gui.GetWindowText(hwnd)
        if not title:
            return True
        try:
            iconic = win32gui.IsIconic(hwnd)
            # 最小化时 GetWindowRect 不准，用 placement 的还原矩形
            if iconic:
                plc = win32gui.GetWindowPlacement(hwnd)
                l, t, r, b = plc[4]
            else:
                l, t, r, b = win32gui.GetWindowRect(hwnd)
            if (r - l) >= min_size and (b - t) >= min_size:
                out.append({
                    "hwnd": hwnd,
                    "title": title,
                    "class": win32gui.GetClassName(hwnd),
                    "rect": (l, t, r, b),
                    "minimized": bool(iconic),
                })
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    out.sort(key=lambda x: -(x["rect"][2] - x["rect"][0]) * (x["rect"][3] - x["rect"][1]))
    return out


def create_engine(threads: int = 8, hash_mb: int = 256,
                  options: dict | None = None):
    """启动 Pikafish 并下发基础选项。

    ``options`` 是额外的 UCI 选项字典（{name: value}），在基础项之后下发，
    用于「高级」面板里用户手改的参数；下发失败不致命。
    """
    from engine_client import UciEngine
    eng = UciEngine(str(ENGINE_EXE), name="pikafish")
    eng.set_option("Threads", threads)
    eng.set_option("Hash", hash_mb)
    # 让引擎在 info 行里带上 wdl（胜/和/负概率），用于显示人类可读的胜率。
    # ★ 只认小写 "true"：写 Python 的 True 会拼成 "value True" 被引擎忽略
    #   （实测 0 条 info 行带 wdl），历史上这行一直没生效。
    try:
        eng.set_option("UCI_ShowWDL", "true")
    except Exception:
        pass
    apply_engine_options(eng, options)
    eng.isready()
    return eng


# ---------------------------------------------------------------------------
# UCI 选项：解析 / 下发 / 清空哈希
# ---------------------------------------------------------------------------
def parse_engine_options(option_lines: list[str]) -> list[dict]:
    """把 ``UciEngine.option_lines`` 解析成结构化选项列表。

    返回 ``[{'name','type','default','min','max','var':[...]}]``，供 UI 按
    ``type``（spin / check / combo / button / string）动态生成控件。
    解析逻辑与 ``src/engine.py::_parse_option`` 一致，遵循 UCI 规范：

        option name Threads type spin default 1 min 1 max 1024
        option name UCI_ShowWDL type check default false
        option name Clear Hash type button
    """
    out: list[dict] = []
    for text in option_lines:
        text = (text or "").strip()
        if not text.startswith("option name "):
            continue
        rest = text[len("option name "):]
        type_idx = rest.find(" type ")
        if type_idx < 0:
            continue
        name = rest[:type_idx].strip()
        parts = rest[type_idx + len(" type "):].split()
        if not parts:
            continue
        opt = {"name": name, "type": parts[0].lower(),
               "default": "", "min": None, "max": None, "var": []}
        i = 1
        while i < len(parts):
            key = parts[i]
            if key == "default" and i + 1 < len(parts):
                opt["default"] = parts[i + 1]
                i += 2
            elif key == "min" and i + 1 < len(parts):
                try:
                    opt["min"] = int(parts[i + 1])
                except ValueError:
                    pass
                i += 2
            elif key == "max" and i + 1 < len(parts):
                try:
                    opt["max"] = int(parts[i + 1])
                except ValueError:
                    pass
                i += 2
            elif key == "var" and i + 1 < len(parts):
                opt["var"].append(parts[i + 1])
                i += 2
            else:
                i += 1
        out.append(opt)
    return out


def set_engine_option(eng, name: str, value) -> None:
    """下发单个 UCI 选项（bool → 'true'/'false'）。"""
    if isinstance(value, bool):
        value = "true" if value else "false"
    eng.set_option(name, value)


def apply_engine_options(eng, opts: dict | None) -> None:
    """批量下发 UCI 选项；单个失败不影响其余。"""
    if not opts:
        return
    for name, value in opts.items():
        try:
            set_engine_option(eng, name, value)
        except Exception:
            pass


def clear_hash(eng) -> None:
    """清空置换表。

    ``Clear Hash`` 是 button 型选项，不能带 ``value``，而
    ``UciEngine.set_option`` 会强制拼上 " value"，所以必须直接 ``send``。
    """
    try:
        eng.send("setoption name Clear Hash")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# info 行解析：真正的 MultiPV
# ---------------------------------------------------------------------------
def _to_int(s, default: int = 0) -> int:
    try:
        return int(s)
    except Exception:
        return default


def parse_info_line(line: str) -> dict | None:
    """解析单条 ``info ...`` 行。

    返回 ``{'depth','seldepth','multipv','score_cp','score_mate','wdl',
    'nodes','nps','hashfull','time_ms','pv':[...]}``；非 info 行返回 None。

    分数统一到 ``score_cp``：mate N 归一化为 ``30000 - |N|*100``，与
    ``format_score`` 的绝杀判定保持同一量纲（原 ``parse_alternatives`` 同款）。
    """
    if not line.startswith("info"):
        return None
    parts = line.split()
    info = {"depth": None, "seldepth": None, "multipv": 1,
            "score_cp": None, "score_mate": None, "wdl": None,
            "nodes": None, "nps": None, "hashfull": None,
            "time_ms": None, "pv": [], "bound": None}
    i, n = 1, len(parts)
    while i < n:
        key = parts[i]
        if key == "depth" and i + 1 < n:
            info["depth"] = _to_int(parts[i + 1]); i += 2
        elif key == "seldepth" and i + 1 < n:
            info["seldepth"] = _to_int(parts[i + 1]); i += 2
        elif key == "multipv" and i + 1 < n:
            info["multipv"] = _to_int(parts[i + 1], 1) or 1; i += 2
        elif key == "score" and i + 2 < n:
            kind, val = parts[i + 1], _to_int(parts[i + 2])
            if kind == "cp":
                info["score_cp"] = val
            elif kind == "mate":
                info["score_mate"] = val
            i += 3
            if i < n and parts[i] in ("lowerbound", "upperbound"):
                info["bound"] = parts[i]     # 边界行，pv 是截断的中间结果
                i += 1
        elif key == "wdl" and i + 3 < n:
            try:
                info["wdl"] = (int(parts[i + 1]), int(parts[i + 2]), int(parts[i + 3]))
            except ValueError:
                pass
            i += 4
        elif key == "nodes" and i + 1 < n:
            info["nodes"] = _to_int(parts[i + 1]); i += 2
        elif key == "nps" and i + 1 < n:
            info["nps"] = _to_int(parts[i + 1]); i += 2
        elif key == "hashfull" and i + 1 < n:
            info["hashfull"] = _to_int(parts[i + 1]); i += 2
        elif key == "time" and i + 1 < n:
            info["time_ms"] = _to_int(parts[i + 1]); i += 2
        elif key == "pv":
            info["pv"] = parts[i + 1:]
            break
        else:
            i += 1
    if info["score_cp"] is None and info["score_mate"] is not None:
        info["score_cp"] = 30000 - abs(info["score_mate"]) * 100
    return info


class MultiPVBuffer:
    """跨多次 drain 累积引擎 info 行，按 multipv 序号保留最新（最深）的一条。

    引擎在 ``go infinite`` 下会持续输出同一个 ``multipv`` 序号但深度递增的
    行，这里「后到覆盖先到」即得到当前最新主变例。
    """

    def __init__(self) -> None:
        self._by: dict[int, dict] = {}

    def feed(self, lines: list[str]) -> None:
        for line in lines:
            info = parse_info_line(line)
            # 只收带 pv 的行，滤掉 "info currmove ..." 之类的中间行；
            # 同时跳过 lowerbound/upperbound 行——这类行的 pv 是被截断的
            # 中间结果，覆盖上去会把一条完整主变例换成两三步的残段。
            if info is None or not info.get("pv") or info.get("bound"):
                continue
            k = int(info.get("multipv") or 1)
            self._by[k] = info

    def snapshot(self, maxpv: int = 0) -> list[dict]:
        """按 multipv 升序返回；``maxpv>0`` 时过滤掉超出当前档位的陈旧序号。"""
        keys = sorted(self._by)
        if maxpv > 0:
            keys = [k for k in keys if k <= maxpv]
        return [self._by[k] for k in keys]

    def clear(self) -> None:
        self._by.clear()

    def __len__(self) -> int:
        return len(self._by)


def pv_to_chinese(fen: str, pv: list[str], limit: int = 0) -> list[str]:
    """用 cchess 逐着回放整条主变例，返回每一步的中文记谱。

    注意：cchess 的 ``move_player`` **不会自动换边**（``move()`` 会校验行棋方，
    见 cchess/board.py:243），所以每落一子必须 ``next_turn()``，否则从第二步
    起会被判非法而中断。遇到 cchess 不认可的着法（长将/长捉规则差异）即截断。
    """
    if not pv:
        return []
    try:
        from cchess import ChessBoard
    except Exception:
        return []
    try:
        b = ChessBoard(fen)
    except Exception:
        return []
    out: list[str] = []
    for mv in pv:
        try:
            m = b.move_iccs(mv)
        except Exception:
            break
        if m is None:
            break
        try:
            out.append(m.to_text())
        except Exception:
            out.append(mv)
        b.next_turn()
        if limit and len(out) >= limit:
            break
    return out


def short_winrate(wdl: tuple[int, int, int] | None, cp: int | None,
                  my_side: str = "w") -> str:
    """给 PV 表用的紧凑胜率，如 ``'62%'``。

    **视角是「我方」**（按 ``my_side`` 换算）—— 与「当前建议」栏的
    :func:`format_winrate` 不同，后者已改为红/黑方绝对视角。
    """
    if wdl is not None:
        w, d, l = wdl
        total = max(1, w + d + l)
        red = w * 100.0 / total
        mine = red if my_side == "w" else (l * 100.0 / total)
        return f"{mine:.0f}%"
    p = winrate_from_cp(cp)
    if p is None:
        return "—"
    mine = p if my_side == "w" else (100.0 - p)
    return f"{mine:.0f}%"


def parse_wdl(line: str) -> tuple[int, int, int] | None:
    """从一行 info 里解析 wdl（胜/和/负，千分比）。

    注意：引擎给出的 wdl 是**行棋方视角**（实测：红方多一车时，红走
    wdl=(1000,0,0)、黑走 wdl=(0,0,1000)），要按红方视角使用必须先过
    :func:`wdl_to_red`。
    """
    if " wdl " not in line:
        return None
    try:
        seg = line.split(" wdl ", 1)[1].split()
        w, d, l = int(seg[0]), int(seg[1]), int(seg[2])
        return w, d, l
    except Exception:
        return None


def wdl_to_red(wdl: tuple[int, int, int] | None,
               side_to_move: str = "w") -> tuple[int, int, int] | None:
    """把引擎的 wdl（行棋方视角）转成红方视角 (win, draw, loss)。

    ``format_winrate`` / ``short_winrate`` 都按红方视角解释 wdl，因此调用前
    必须先按行棋方翻转，否则黑方走子时胜率会整个反过来。
    """
    if wdl is None:
        return None
    w, d, l = wdl
    return (w, d, l) if side_to_move == "w" else (l, d, w)


def winrate_from_cp(cp: int | None) -> float | None:
    """没有 wdl 时，用经验公式把 centipawn 换算成**红方**胜率（0~100）。

    采用 tanh 形状的经验映射：

        red% = 50 + 50 * tanh(cp / K)      K = 470

    选择理由：
    * **cp = 0 恰好给出 50%**（避免了早期版本用指数式归一化时的偏差）
    * 单调、平滑，大分差自然饱和到 0% / 100%
    * K 取值使 100cp ≈ 60%、300cp ≈ 79%、1000cp ≈ 97%，量级与
      Stockfish/Pikafish 公布的胜率曲线接近

    仅在引擎未输出 wdl 时作为兜底显示。
    """
    if cp is None:
        return None
    import math
    try:
        K = 470.0
        red = 50.0 + 50.0 * math.tanh(float(cp) / K)
        return max(0.0, min(100.0, red))
    except Exception:
        return None


# 优势判定阈值：红/黑胜率相差 ≥ 该百分点才认为一方占优，否则算均势。
WINRATE_EDGE = 10.0


def _winrate_sentence(red: float, black: float, est: bool = False) -> str:
    """按红/黑方胜率拼一句「只报占优方」的评估。

    * 红方占优 → ``红方优势，红方胜率约 62%``
    * 黑方占优 → ``黑方优势，黑方胜率约 58%``
    * 均势     → ``均势（红 50% / 黑 48%）``

    ``est=True`` 表示胜率来自 cp 经验估算（引擎未给 wdl），会附上标记。
    """
    tail = "（估算）" if est else ""
    if red - black >= WINRATE_EDGE:
        return f"红方优势，红方胜率约 {red:.0f}%{tail}"
    if black - red >= WINRATE_EDGE:
        return f"黑方优势，黑方胜率约 {black:.0f}%{tail}"
    inner = f"红 {red:.0f}% / 黑 {black:.0f}%" + ("，估算" if est else "")
    return f"均势（{inner}）"


def format_winrate(wdl: tuple[int, int, int] | None, cp: int | None,
                   my_side: str = "w") -> str:
    """把 wdl 或 cp 转成一句人类可读的局面评估 —— **只报占优的一方**。

    输出以红/黑方**绝对视角**表述（不再用「我方/对方」），例如
    ``红方优势，红方胜率约 62%``；差距不足 :data:`WINRATE_EDGE` 时给
    ``均势（红 50% / 黑 48%）``。

    ``wdl`` 是引擎给的（红方视角，千分比；调用前须过 :func:`wdl_to_red`）。

    注意：``my_side`` 参数仅为兼容旧调用保留，**不再影响输出** ——
    历史版本按「我方视角」换算胜率，现在一律按红/黑方表述。
    """
    if wdl is not None:
        w, _d, l = wdl
        total = max(1, w + _d + l)
        red = w * 100.0 / total
        black = l * 100.0 / total
        return _winrate_sentence(red, black)

    p = winrate_from_cp(cp)
    if p is None:
        return "—"
    return _winrate_sentence(p, 100.0 - p, est=True)


def validate_fen(fen: str) -> tuple[bool, str, int]:
    """用 cchess 做棋规校验 —— 这是过滤识别误判的关键闸门。

    识别模型在棋盘边界或遮挡处可能输出非法局面，Pikafish 遇到非法 FEN
    会打印 CRITICAL ERROR 后直接退出，因此入引擎前必须过这一关。

    注意：``cchess.ChessBoard.create_moves()`` 返回的是**生成器**，
    必须 list() 之后才能取长度。
    """
    try:
        from cchess import ChessBoard
        moves = list(ChessBoard(fen).create_moves())
        return True, "", len(moves)
    except Exception as exc:
        return False, str(exc), 0


def move_to_chinese(fen: str, iccs: str) -> str:
    try:
        from cchess import ChessBoard
        return ChessBoard(fen).move_iccs(iccs).to_text()
    except Exception:
        return ""


def format_score(cp) -> str:
    if cp is None:
        return "—"
    if abs(cp) > 20000:
        return f"绝杀 {max(1, (30000 - abs(cp)) // 100)} 步"
    return f"{cp / 100:+.2f}"


# ---------------------------------------------------------------------------
# 轮次判定
# ---------------------------------------------------------------------------
def is_legal_move(fen: str, iccs: str) -> bool:
    """校验一个 ICCS 着法在当前局面下是否合法。

    用 ``cchess.ChessBoard.is_valid_iccs_move()`` —— 这是库提供的直接
    接口，会完整校验马腿/象眼/九宫/过河兵/炮架/将帅照面/自将等规则。

    重要：不要试图从 ``create_moves()`` 反推 ICCS。实测它的 (row, col)
    编码与 ICCS 并非简单对应（例如元素 ``((1,0),(2,2))`` 在竖线上移动，
    行 1→2 的同时列 0→2 也变），逆向它的坐标语义既脆弱又容易出错。
    """
    if not iccs or len(iccs) != 4:
        return False
    try:
        from cchess import ChessBoard
        return bool(ChessBoard(fen).is_valid_iccs_move(iccs))
    except Exception:
        return False


def enumerate_legal_moves(fen: str, timeout: float = 2.0) -> set[str]:
    """枚举全部合法着法（ICCS 集合）。

    实现方式是对 9x10 的坐标对做全枚举并用库接口校验。实测全量 8100 次
    校验约 0.12s，因此适合在中低频场景使用（例如分析候选着法）。

    高频路径请直接用 :func:`is_legal_move` 校验单个着法。
    """
    from cchess import ChessBoard
    try:
        board = ChessBoard(fen)
    except Exception:
        return set()
    out: set[str] = set()
    files = "abcdefghi"
    t0 = time.perf_counter()
    for f1 in files:
        for r1 in range(10):
            src = f"{f1}{r1}"
            for f2 in files:
                for r2 in range(10):
                    if f1 == f2 and r1 == r2:
                        continue
                    mv = f"{src}{f2}{r2}"
                    try:
                        if board.is_valid_iccs_move(mv):
                            out.add(mv)
                    except Exception:
                        pass
            if time.perf_counter() - t0 > timeout:
                return out
    return out


def legal_moves(fen: str) -> set[str]:
    """兼容旧调用名，等同 :func:`enumerate_legal_moves`。"""
    return enumerate_legal_moves(fen)


def fen_with_side(fen: str, side: str) -> str:
    """替换 FEN 的行棋方字段（'w' 红先 / 'b' 黑先）。"""
    parts = fen.split()
    if len(parts) >= 2:
        parts[1] = side
        return " ".join(parts)
    return f"{fen} {side} - - 0 1"


def _rows_of(fen: str) -> list[str]:
    """FEN 展开成 10 行 x 9 列的短标签行（'.' 表示空位）。"""
    board = fen.split()[0]
    rows: list[str] = []
    for part in board.split("/"):
        line = ""
        for ch in part:
            line += "." * int(ch) if ch.isdigit() else ch
        rows.append(line)
    return rows


def diff_single_move(fen_before: str, fen_after: str,
                     legal: set[str] | None = None) -> str | None:
    """若两局面的差异恰好是一步着法，返回其 ICCS；否则 None。

    判据：
    * 差异恰好 2 处 —— 起点变空、终点由空或有子变为某个棋子
    * 终点新出现的棋子必须等于起点原来的棋子（着法不会改变兵种）
    * 若给了 legal，着法必须落在合法集合内

    注意：这里**只比较格子内容差异**，不要求"终点原本为空"，
    因此吃子着法同样适用（终点原为对方子，现为我方子）。
    """
    ra, rb = _rows_of(fen_before), _rows_of(fen_after)
    if len(ra) != 10 or len(rb) != 10:
        return None
    if any("x" in r for r in ra) or any("x" in r for r in rb):
        return None      # 含识别不确定格，无法可靠差分

    changes = [(r, c, ra[r][c], rb[r][c])
               for r in range(10) for c in range(9)
               if ra[r][c] != rb[r][c]]
    if len(changes) != 2:
        return None

    gone = [(r, c, a) for r, c, a, b in changes if b == "." and a != "."]
    appeared = [(r, c, b) for r, c, a, b in changes if b != "." and a != b]
    if len(gone) != 1 or len(appeared) != 1:
        return None

    (r1, c1, moved_piece) = gone[0]
    (r2, c2, new_piece) = appeared[0]
    if new_piece != moved_piece:
        return None

    # (row, col) 是 FEN 顺序（row 0 = 黑方底线）。
    # ICCS 的 rank 与 FEN 行号相反：rank = 9 - row。
    mv = (f"{chr(97 + c1)}{9 - r1}" f"{chr(97 + c2)}{9 - r2}")
    if legal is not None and mv not in legal:
        return None
    return mv


class TurnManager:
    """轮次管理：局面差分推进轮次 + 引擎着法合法性兜底校正。

    识别的产物只有"局面"，没有"轮到谁走"这一位信息。本类用两个手段补齐：

    1. **差分推进**：两个相邻稳定局面若恰好相差一步合法着法，则走子方
       必然是当时的行棋方，据此翻转到另一方。要求开局轮次正确。
    2. **引擎着法兜底**：Pikafish 只从"当前行棋方"视角给着法。若引擎给出的
       着法只对另一方合法，说明轮次设错了 —— 直接据此校正。

    第 2 条能修正第 1 条的错误累积，因此开局即使猜错也会在首次分析后自纠。
    """

    def __init__(self, side_to_move: str = "w") -> None:
        self.side_to_move = side_to_move
        self.history: list[str] = []
        self.corrections = 0

    def toggle(self) -> str:
        self.side_to_move = "b" if self.side_to_move == "w" else "w"
        return self.side_to_move

    def apply_move(self, iccs: str) -> None:
        self.history.append(iccs)
        self.toggle()

    def try_advance(self, fen_before: str, fen_after: str) -> str | None:
        """若两局面相差一步合法着法，推进轮次并返回该着法；否则 None。"""
        try:
            legal = legal_moves(fen_before)
            mv = diff_single_move(fen_before, fen_after, legal)
            if mv is None:
                return None
            # 动子方必须与当前轮次一致。
            # ICCS rank r 对应的 FEN 行是 9-r（两者行方向相反）。
            r1, c1 = 9 - int(mv[1]), ord(mv[0]) - ord("a")
            piece = _rows_of(fen_before)[r1][c1]
            mover_side = "w" if piece.isupper() else "b"
            if mover_side != self.side_to_move:
                return None
            self.apply_move(mv)
            return mv
        except Exception:
            return None

    def correct_by_engine_move(self, fen: str, bestmove: str) -> bool:
        """用引擎着法的合法性反推轮次。返回 True 表示轮次被修正过。"""
        if not bestmove or bestmove in ("(none)", "0000"):
            return False
        try:
            if bestmove in legal_moves(fen_with_side(fen, self.side_to_move)):
                return False
            other = "b" if self.side_to_move == "w" else "w"
            if bestmove in legal_moves(fen_with_side(fen, other)):
                self.side_to_move = other
                self.corrections += 1
                return True
        except Exception:
            pass
        return False


def parse_alternatives(infos: list[str], fen: str, limit: int = 6) -> list[dict]:
    """从引擎 info 行提取候选着法（取每个 pv 出现的最大深度）。"""
    import re

    cands: dict[str, dict] = {}
    for line in infos:
        m = re.search(
            r"depth (\d+).*?score (cp|mate) (-?\d+).*? pv ([a-i]\d[a-i]\d)", line)
        if not m:
            continue
        depth, kind, val, pv = int(m.group(1)), m.group(2), int(m.group(3)), m.group(4)
        score = val if kind == "cp" else (30000 - abs(val) * 100)
        if pv not in cands or depth > cands[pv]["depth"]:
            cands[pv] = {"depth": depth, "score": score}

    items = sorted(cands.items(), key=lambda kv: -kv[1]["score"])[:limit]
    return [{
        "iccs": pv,
        "chinese": move_to_chinese(fen, pv),
        "score": format_score(meta["score"]),
        "depth": meta["depth"],
    } for pv, meta in items]


# ---------------------------------------------------------------------------
# 着法 → 图像坐标（用于画连线箭头）
# ---------------------------------------------------------------------------
def move_endpoints_iccs(move: str) -> tuple[str, str]:
    """'h2e2' -> ('h2', 'e2')"""
    if not move or len(move) < 4:
        return "", ""
    return move[0:2], move[2:4]


def _grid_xy(row: int, col: int, dst_size: tuple[int, int] = (450, 500),
             padding: int = 50) -> tuple[float, float]:
    """交叉点 (row, col) 在**拉正棋盘图**上的像素坐标。

    与 ``xq_vision`` 的透视目标尺寸保持一致：9 列 / 10 行交叉点分布在
    [PADDING, DST-PADDING] 区间内等分。
    """
    w, h = dst_size
    x = padding + col * (w - 2 * padding) / 8.0
    y = padding + row * (h - 2 * padding) / 9.0
    return float(x), float(y)


def check_board_geometry(kps, min_area_frac: float = 0.04,
                         min_side: float = 40.0,
                         aspect_lo: float = 0.75, aspect_hi: float = 1.30,
                         img_shape: tuple[int, int] | None = None
                         ) -> tuple[bool, str]:
    """校验 pose 输出的棋盘四角是否构成一个合理的四边形。

    必要性：pose 模型**在画面里没有棋盘时也会输出 4 个点**，而且往往退化
    成一条线或一个小四边形（实测在 JJ象棋主界面的 3D 棋盘摆件上，输出四角
    的 y 坐标只差 18px，高度近乎为 0）。这种输出送进分类器会得到完全
    错误的棋盘，因此必须在识别阶段就判掉。

    判据：
    * 上边/下边（A0-A8、J0-J8）与左边/右边（A0-J0、A8-J8）都要有足够长度
    * **水平边均值 / 垂直边均值** 落在合理区间

      注意方向：棋盘是 9 列 × 10 行，即**高略大于宽**，实测正常对局为
      宽 570 / 高 627 ≈ 0.93，因此该比值应 **< 1**，区间取 0.75~1.30
      （容纳倾斜、透视与边界估计误差）。

    * 四边形面积占画面比例不能过小

    ``kps`` 顺序为 ``[A0, A8, J0, J8]``（左上、右上、左下、右下）。
    """
    try:
        import math
        import numpy as np

        pts = np.asarray(kps, dtype=np.float64)
        if pts.shape != (4, 2):
            return False, f"角点形状异常 {pts.shape}"
        a0, a8, j0, j8 = pts

        # 水平边：上边 A0-A8、下边 J0-J8
        top = float(math.dist(a0, a8))
        bottom = float(math.dist(j0, j8))
        # 垂直边：左边 A0-J0、右边 A8-J8
        left = float(math.dist(a0, j0))
        right = float(math.dist(a8, j8))

        if min(top, bottom) < min_side:
            return False, f"棋盘上/下边过短（{top:.0f}, {bottom:.0f}px）"
        if min(left, right) < min_side:
            return False, f"棋盘左/右边过短（{left:.0f}, {right:.0f}px）"

        h_mean = (top + bottom) / 2.0      # 水平边长
        v_mean = (left + right) / 2.0      # 垂直边长
        if v_mean < 1e-6:
            return False, "棋盘高度为 0"
        aspect = h_mean / v_mean
        if not (aspect_lo <= aspect <= aspect_hi):
            return False, (f"棋盘宽高比 {aspect:.2f} 超出合理范围 "
                           f"[{aspect_lo}, {aspect_hi}]")

        # 四边形面积用鞋带公式（不依赖 np.cross —— 新版 numpy 已不接受 2D 向量）
        quad = np.array([a0, a8, j8, j0], dtype=np.float64)
        x, y = quad[:, 0], quad[:, 1]
        area = 0.5 * abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))))
        if img_shape is not None:
            ih, iw = img_shape[:2]
            frac = area / float(ih * iw)
            if frac < min_area_frac:
                return False, f"棋盘面积仅占画面 {frac:.1%}，过小"

        return True, ""
    except Exception as exc:
        return False, f"几何校验异常: {exc}"


def check_position(fen: str) -> tuple[bool, str]:
    """严格检查局面是否合法（比 cchess 的构造校验更严）。

    为什么需要自己查：``cchess.ChessBoard(fen)`` 对**缺子/多子**的局面相当
    宽松 —— 实测它能接受 ``4k4/9/9/9/9/9/8P/9/9/4k4``（两个黑将、没有红帅、
    只有一个兵）。这类局面几乎都来自识别失败（窗口不在对局界面、画面被
    遮挡、棋盘定位错了）。

    而 Pikafish 遇到非法局面会打印 CRITICAL ERROR 后**直接退出进程**，
    之后所有写入都报 OSError。因此这一步必须在送引擎之前把关。

    返回 ``(是否合法, 原因)``。
    """
    board = fen.split()[0] if fen else ""
    rows = board.split("/")
    if len(rows) != 10:
        return False, f"FEN 行数为 {len(rows)}，应为 10"

    grid: list[str] = []
    for r, row in enumerate(rows):
        line = ""
        for ch in row:
            if ch.isdigit():
                line += "." * int(ch)
            else:
                line += ch
        if len(line) != 9:
            return False, f"FEN 第 {r + 1} 行展开为 {len(line)} 列，应为 9"
        grid.append(line)

    flat = "".join(grid)
    bad = set(flat) - set("KABNRCPkabnrcp.")
    if bad:
        return False, f"含非法字符 {sorted(bad)}"

    if flat.count("K") != 1:
        return False, f"红帅数量为 {flat.count('K')}，应为 1"
    if flat.count("k") != 1:
        return False, f"黑将数量为 {flat.count('k')}，应为 1"

    caps = {"A": 2, "B": 2, "N": 2, "R": 2, "C": 2, "P": 5,
            "a": 2, "b": 2, "n": 2, "r": 2, "c": 2, "p": 5}
    for piece, cap in caps.items():
        n = flat.count(piece)
        if n > cap:
            return False, f"{piece} 的数量 {n} 超过上限 {cap}"

    kr = kc = br = bc = None
    for r in range(10):
        for c in range(9):
            if grid[r][c] == "K":
                kr, kc = r, c
            elif grid[r][c] == "k":
                br, bc = r, c
    # FEN 第 0 行是黑方底线，第 9 行是红方底线
    if br is not None and not (0 <= br <= 2 and 3 <= bc <= 5):
        return False, f"黑将在九宫外 ({br},{bc})"
    if kr is not None and not (7 <= kr <= 9 and 3 <= kc <= 5):
        return False, f"红帅在九宫外 ({kr},{kc})"

    if kr is not None and br is not None and kc == bc:
        mid = [grid[r][kc] for r in range(min(kr, br) + 1, max(kr, br))]
        if all(x == "." for x in mid):
            return False, "将帅照面"

    total = sum(flat.count(p) for p in "KABNRCPkabnrcp")
    if total > 32:
        return False, f"棋子总数 {total} 超过 32"

    return True, ""


def move_arrow(warp_mat, move: str,
               dst_size: tuple[int, int] = (450, 500),
               padding: int = 50) -> dict | None:
    """把 ICCS 着法换算成两套坐标，供界面画箭头。

    返回::

        {
          "from": [x, y], "to": [x, y],          # 拉正棋盘图坐标（用于左栏显示）
          "src_from": [x, y], "src_to": [x, y],  # 原截图坐标（可叠加到原图上）
        }

    坐标为 None 表示该着法无法换算（着法格式异常）。
    """
    if not move or len(move) < 4 or move in ("(none)", "0000"):
        return None
    try:
        import numpy as np

        def to_rc(sq: str) -> tuple[int, int]:
            col = ord(sq[0].lower()) - ord("a")
            rank = int(sq[1])
            return 9 - rank, col      # row: 0 = 黑方底线（画面顶部）

        (r1, c1) = to_rc(move[0:2])
        (r2, c2) = to_rc(move[2:4])
        x1, y1 = _grid_xy(r1, c1, dst_size, padding)
        x2, y2 = _grid_xy(r2, c2, dst_size, padding)

        out = {"from": [x1, y1], "to": [x2, y2],
               "src_from": None, "src_to": None}

        if warp_mat is not None:
            inv = np.linalg.inv(np.asarray(warp_mat, dtype=np.float64))
            for key, (gx, gy) in (("src_from", (x1, y1)), ("src_to", (x2, y2))):
                p = inv @ np.array([gx, gy, 1.0])
                if abs(p[2]) > 1e-9:
                    out[key] = [float(p[0] / p[2]), float(p[1] / p[2])]
        return out
    except Exception:
        return None


def move_screen_points(rect, warp_mat, move: str) -> dict | None:
    """把 ICCS 着法换算成**屏幕物理坐标**点对，供自动走棋点击。

    与 ``move_arrow`` 的关系：后者给出 ``src_from``/``src_to``（原截图坐标），
    本函数再叠加抓帧时窗口矩形的原点 ``(l, t)`` 即得屏幕坐标。

    ``rect`` 为 ``ScreenSource.rect``（抓帧时缓存，与该帧对齐），
    ``warp_mat`` 为该帧透视矩阵。任一缺失或点越出窗口矩形都返回 None。

    返回::

        {"from": (sx, sy), "to": (sx, sy)}
    """
    if rect is None or warp_mat is None:
        return None
    arrow = move_arrow(warp_mat, move)
    if not arrow or not arrow.get("src_from") or not arrow.get("src_to"):
        return None
    l, t, r, b = rect
    out: dict[str, tuple[float, float]] = {}
    for key, skey in (("from", "src_from"), ("to", "src_to")):
        sx, sy = arrow[skey]
        x, y = l + float(sx), t + float(sy)
        # 越界保护：点击点必须落在窗口矩形内（留 2px 容差）
        if not (l - 2 <= x <= r + 2 and t - 2 <= y <= b + 2):
            return None
        out[key] = (x, y)
    return out


# ---------------------------------------------------------------------------
# 识别抗错：局面演化校验
# ---------------------------------------------------------------------------
class BoardTracker:
    """过滤单帧误识别，同时维护轮次。

    动机
    ----------------------------------------------------------------
    识别模型偶尔会把一个棋子读错（例如把红傌读成红炮）。这种错误一旦
    被采信，整局分析都会跑偏。但它有个可利用的性质：

        **正确的局面演化，必然能由上一局面走一步合法着法得到。**

    因此对每个新识别出的局面做三重校验，满足其一才采信：

    1. 能由已确认局面**一步合法着法**解释 —— 正常走子，立即采信
    2. 与上一帧识别结果一致（连续两帧）—— 稳定的新局面（例如刚接手
       中盘、或用户跳过了中间几步）
    3. 连续 ``force_after`` 帧都无法解释 —— 兜底采信，避免永久卡死，
       同时给出警告

    不满足任一条的局面只作为"候选"暂存、不送引擎 —— 单帧噪声就挡在这里。

    轮次
    ----------------------------------------------------------------
    校验着法合法性必须结合轮次：同一个 ICCS 着法，红方轮次下可能合法、
    黑方轮次下必然非法。因此内部维护 ``side_to_move``：

    * 首个局面取构造参数 ``first_side``（默认红先）
    * 每采信一步走子后翻转
    * 引擎给出着法后可用 :meth:`correct_by_engine_move` 反推校正 ——
      引擎只从当前行棋方视角给着法，若其着法仅对另一方合法，说明轮次设错
    """

    def __init__(self, force_after: int = 6, sanity_check: bool = True,
                 first_side: str = "w") -> None:
        self.force_after = max(2, force_after)
        self.sanity_check = sanity_check
        self.side_to_move = first_side if first_side in ("w", "b") else "w"
        self.stable_fen: str | None = None      # 已采信局面（仅棋盘段）
        self.pending_fen: str | None = None     # 待确认候选
        self.pending_count = 0
        self.dropped = 0                        # 被挡掉的帧数
        self.forced = 0                         # 兜底采信次数
        self.corrections = 0                    # 轮次被校正次数
        self.moves: list[str] = []              # 已采信的着法序列

    # ---------------- 轮次 ----------------

    def toggle_side(self) -> str:
        self.side_to_move = "b" if self.side_to_move == "w" else "w"
        return self.side_to_move

    @property
    def side_cn(self) -> str:
        return "红" if self.side_to_move == "w" else "黑"

    def full_fen(self) -> str | None:
        """已采信局面 + 当前轮次 的完整 FEN。"""
        if self.stable_fen is None:
            return None
        return fen_with_side(self.stable_fen, self.side_to_move)

    def _explain(self, prev: str, cur: str) -> str | None:
        """cur 能否由 prev 走一步合法着法得到？返回该着法或 None。

        先用纯格子差分拿候选着法，再用 cchess 校验合法性 —— 校验时必须
        带上**当前轮次**，否则黑方着法会被判非法。
        """
        mv = diff_single_move(prev, cur)
        if mv is None:
            return None
        return mv if is_legal_move(fen_with_side(prev, self.side_to_move), mv) else None

    # ---------------- 主入口 ----------------

    def update(self, fen: str) -> tuple[bool, str, str, str]:
        """喂入一帧识别结果（只取棋盘段）。

        返回 ``(是否采信, 原因, 着法, 轮次)``。
        原因：``初始局面 / 走子 / 稳定新局面 / 强制采信 / unchanged / drop``
        """
        board_only = fen.split()[0]

        if self.sanity_check:
            ok, why = check_position(board_only)
            if not ok:
                self.dropped += 1
                return False, "drop", f"局面非法: {why}", self.side_to_move
            # 再用 cchess 走一遍构造校验，兜住 check_position 没覆盖的规则
            try:
                from cchess import ChessBoard
                ChessBoard(board_only)
            except Exception as exc:
                self.dropped += 1
                return False, "drop", f"局面非法: {exc}", self.side_to_move

        if self.stable_fen is None:
            self.stable_fen = board_only
            self.pending_fen = None
            self.pending_count = 0
            return True, "初始局面", "", self.side_to_move

        if board_only == self.stable_fen:
            self.pending_fen = None
            self.pending_count = 0
            return False, "unchanged", "", self.side_to_move

        mv = self._explain(self.stable_fen, board_only)
        if mv:
            self.stable_fen = board_only
            self.pending_fen = None
            self.pending_count = 0
            self.moves.append(mv)
            self.toggle_side()
            return True, "走子", mv, self.side_to_move

        if self.pending_fen is not None and board_only == self.pending_fen:
            self.pending_count += 1
            if self.pending_count >= 2:
                self.stable_fen = board_only
                self.pending_fen = None
                self.pending_count = 0
                return True, "稳定新局面", "", self.side_to_move
        else:
            self.pending_fen = board_only
            self.pending_count = 1

        if self.pending_count >= self.force_after:
            self.stable_fen = board_only
            self.pending_fen = None
            self.pending_count = 0
            self.forced += 1
            return True, "强制采信", "", self.side_to_move

        self.dropped += 1
        return False, "drop", "", self.side_to_move

    def correct_by_engine_move(self, bestmove: str) -> bool:
        """用引擎着法的合法性反推轮次。True 表示轮次被修正过。"""
        if not bestmove or bestmove in ("(none)", "0000") or self.stable_fen is None:
            return False
        try:
            if is_legal_move(fen_with_side(self.stable_fen, self.side_to_move), bestmove):
                return False
            other = "b" if self.side_to_move == "w" else "w"
            if is_legal_move(fen_with_side(self.stable_fen, other), bestmove):
                self.side_to_move = other
                self.corrections += 1
                return True
        except Exception:
            pass
        return False

    def reset(self) -> None:
        self.stable_fen = None
        self.pending_fen = None
        self.pending_count = 0
        self.dropped = 0
        self.forced = 0
        self.corrections = 0
        self.moves.clear()

    @property
    def stats(self) -> str:
        s = f"已挡 {self.dropped} 帧"
        if self.forced:
            s += f" · 强制采信 {self.forced} 次"
        if self.corrections:
            s += f" · 轮次校正 {self.corrections} 次"
        return s

# ---------------------------------------------------------------------------
# 自检
# ---------------------------------------------------------------------------
def _find_test_image() -> Path | None:
    """找一个用于自检的测试图。

    打包发行时不会有 ``shots/`` 目录（那是我开发时的调试产物），
    所以这里按候选顺序找，找不到就返回 None —— 由调用方决定
    是否跳过识别环节，只验证资源与引擎。
    """
    base = app_base()
    cands = []
    if HERE != base:
        cands.append(HERE / "shots" / "bg_002A0A30.png")
    cands += [
        base / "shots" / "bg_002A0A30.png",
        base / "docs" / "arrow-demo.jpg",
        base / "docs" / "screenshot.png",
        Path.cwd() / "shots" / "bg_002A0A30.png",
    ]
    for c in cands:
        if c.is_file():
            return c
    return None


def selftest(image: str | None, movetime: int) -> int:
    """自检。返回 0 表示通过。

    注意：打包成 ``--windowed`` 的 exe 运行时没有控制台，``print`` 会因
    ``sys.stdout is None`` 而失败。因此这里把日志同时写到文件，
    便于打包后排查（日志路径会显示在弹窗/错误信息里）。
    """
    log_lines: list[str] = []

    def say(msg: str = "") -> None:
        log_lines.append(msg)
        try:
            if sys.stdout is not None:
                print(msg)
        except Exception:
            pass

    say("=" * 68)
    say("XiangQiLens 自检")
    say("=" * 68)
    say(f"运行模式 : {'打包 exe' if getattr(sys, 'frozen', False) else '源码'}")
    say(f"DPI 感知 : {enable_dpi_awareness()}")
    say(f"应用根目录: {app_base()}")
    say(f"资源目录 : {RESEARCH}")
    say(f"识别模型 : {POSE_ONNX.name} + {CLS_ONNX.name}")
    say(f"引擎     : {ENGINE_EXE.name}")

    # ---- 资源存在性（打包后最关键的检查项）----
    say()
    say("[0/4] 资源检查")
    missing = []
    for name, p in (("pose 模型", POSE_ONNX), ("分类模型", CLS_ONNX),
                    ("引擎 exe", ENGINE_EXE),
                    ("引擎权重", ENGINE_EXE.parent / "pikafish.nnue")):
        ok = p.is_file()
        size = f"{p.stat().st_size / 1024 / 1024:.1f} MB" if ok else "-"
        say(f"      [{'OK ' if ok else '缺失'}] {name:10} {size:>9}  {p}")
        if not ok:
            missing.append(name)
    if missing:
        say(f"      [失败] 缺少: {', '.join(missing)}")
        say("             请确认 xq_research 与本程序在同一目录")
        _write_selftest_log(log_lines)
        return 1

    img_path = Path(image) if image else _find_test_image()
    if img_path is None or not img_path.is_file():
        say()
        say("[识别环节] 跳过（未找到测试图，发行版不含调试截图，属正常）")
        say("           直接验证棋规与引擎")
        fen = ("rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/"
               "P1P1P1P1P/1C5C1/9/RNBAKABNR w - - 0 1")
        say(f"      用标准开局 FEN: {fen.split()[0]}")
        t_recog = 0.0
    else:
        say(f"测试图   : {img_path.name}")
        bgr = cv2.imdecode(np.fromfile(str(img_path), dtype=np.uint8),
                           cv2.IMREAD_COLOR)
        if bgr is None:
            say(f"[失败] 读图失败: {img_path}")
            _write_selftest_log(log_lines)
            return 1
        say(f"      尺寸: {bgr.shape[1]}x{bgr.shape[0]}")

        say()
        say("[1/4] 加载识别模型")
        t0 = time.perf_counter()
        vision = create_vision(cuda=True)
        say(f"      {time.perf_counter() - t0:.2f}s")

        say("[2/4] 识别棋盘")
        t0 = time.perf_counter()
        res = vision.infer(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
        t_recog = (time.perf_counter() - t0) * 1000
        for i, row in enumerate(res["rows"]):
            say(f"      row{i}: {row}   minConf={res['conf'][i].min():.2f}")
        fen = vision.to_fen(res["rows"], side_to_move="w")
        say(f"      FEN   : {fen}")
        say(f"      耗时  : pose={res['t_pose_ms']:.1f}ms  cls={res['t_cls_ms']:.1f}ms")

    say("[3/4] cchess 棋规校验")
    ok, err, n_moves = validate_fen(fen)
    if not ok:
        say(f"      [失败] {err}")
        _write_selftest_log(log_lines)
        return 2
    say(f"      通过，合法着法 {n_moves} 个")

    say("[4/4] 引擎分析")
    try:
        eng = create_engine(threads=8, hash_mb=256)
    except Exception as exc:
        say(f"      [失败] 引擎启动失败: {exc}")
        _write_selftest_log(log_lines)
        return 3
    t0 = time.perf_counter()
    r = eng.analyse(fen, movetime_ms=movetime)
    best = r.get("bestmove") or ""
    say(f"      bestmove = {best} ({move_to_chinese(fen, best)})")
    say(f"      score    = {format_score(r.get('score_cp'))}   depth = {r.get('depth')}")
    say(f"      耗时     = {time.perf_counter() - t0:.2f}s")
    # 用 is_legal_move 校验（不要用 create_moves 的坐标 —— 实测其
    # (row,col) 编码与 ICCS 并非简单对应，会得出错误结论）
    say(f"      着法合法 : {is_legal_move(fen, best)}")

    eng.quit()
    say()
    if t_recog > 0:
        say(f"[通过] 全链路可用 · 识别 {t_recog:.0f} ms/帧")
    else:
        say("[通过] 资源、棋规与引擎均可用")

    _write_selftest_log(log_lines)
    return 0


def _write_selftest_log(lines: list[str]) -> None:
    """把自检日志写到应用根目录，便于打包后的 exe 排查问题。"""
    try:
        p = app_base() / "selftest.log"
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        pass
