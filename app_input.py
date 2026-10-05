# -*- coding: utf-8 -*-
"""鼠标注入（自动走棋落子用）—— 纯 Win32 用户态，无驱动、无内存注入。

做法与 ``babalae/better-genshin-impact`` 的 ``MouseEventSimulator`` 一致：
用 ``user32!SetCursorPos`` 移动光标，``user32!mouse_event`` 发左键按下/抬起。
**不用** BGI 的 ``MOUSEEVENTF_ABSOLUTE`` 归一化坐标（那只覆盖主显示器），
改用 ``SetCursorPos`` 的虚拟桌面物理坐标，天然支持多显示器与负坐标。

本模块刻意**只用 ctypes**，不碰 ``win32api``/``win32con`` —— 那两个不在
打包脚本的 hiddenimports 里（``tools/build_exe.py:98-104``），用了就得改打包
配置。``win32gui`` 已在包内，前台化/遮挡校验需要它。

对外接口：
* ``MouseClicker``        —— 移动 / 单击 / 落子（两次点击或拖拽）
* ``ensure_foreground``   —— 把目标窗口置前，并回读校验是否真的成了前台
* ``is_point_on_window``  —— 判断屏幕某点是否落在目标窗口上（防点到别的窗口）
"""
from __future__ import annotations

import ctypes
import time

try:
    import win32gui
    _HAS_WIN32GUI = True
except Exception:                                   # pragma: no cover
    win32gui = None
    _HAS_WIN32GUI = False

_IS_WIN = hasattr(ctypes, "windll")

# ---------------------------------------------------------------------------
# Win32 原语
# ---------------------------------------------------------------------------
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_ABSOLUTE = 0x8000

SW_RESTORE = 9
GA_ROOT = 2
INPUT_MOUSE = 0

if _IS_WIN:
    from ctypes import wintypes

    _u32 = ctypes.windll.user32
    _k32 = ctypes.windll.kernel32

    _u32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    _u32.SetCursorPos.restype = wintypes.BOOL
    _u32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
    _u32.GetCursorPos.restype = wintypes.BOOL
    # 末参 dwExtraInfo 是 ULONG_PTR：用 c_void_p 传 None 最稳
    _u32.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                 wintypes.DWORD, ctypes.c_void_p]
    _u32.mouse_event.restype = None

    _u32.GetForegroundWindow.restype = wintypes.HWND
    _u32.GetForegroundWindow.argtypes = []
    _u32.SetForegroundWindow.argtypes = [wintypes.HWND]
    _u32.SetForegroundWindow.restype = wintypes.BOOL
    _u32.BringWindowToTop.argtypes = [wintypes.HWND]
    _u32.BringWindowToTop.restype = wintypes.BOOL
    _u32.IsIconic.argtypes = [wintypes.HWND]
    _u32.IsIconic.restype = wintypes.BOOL
    _u32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    _u32.ShowWindow.restype = wintypes.BOOL
    _u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                              ctypes.POINTER(wintypes.DWORD)]
    _u32.GetWindowThreadProcessId.restype = wintypes.DWORD
    _u32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD,
                                       wintypes.BOOL]
    _u32.AttachThreadInput.restype = wintypes.BOOL
    _u32.WindowFromPoint.argtypes = [wintypes.POINT]
    _u32.WindowFromPoint.restype = wintypes.HWND
    _u32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
    _u32.GetAncestor.restype = wintypes.HWND

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_void_p)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("mi", _MOUSEINPUT)]

    class _INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

    _u32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT),
                               ctypes.c_int]
    _u32.SendInput.restype = wintypes.UINT
else:                                               # pragma: no cover
    _u32 = _k32 = None


def get_cursor_pos() -> tuple[int, int]:
    """返回当前光标屏幕物理坐标。"""
    if not _IS_WIN:
        return (0, 0)
    pt = ctypes.wintypes.POINT()
    _u32.GetCursorPos(ctypes.byref(pt))
    return (int(pt.x), int(pt.y))


def move_to(x: float, y: float) -> bool:
    """把光标移到屏幕物理坐标 (x, y)（支持负值/多显示器）。"""
    if not _IS_WIN:
        return False
    return bool(_u32.SetCursorPos(int(round(x)), int(round(y))))


def _mouse_event(flags: int) -> None:
    _u32.mouse_event(flags, 0, 0, 0, None)


def _send_input(flags: int) -> None:
    inp = _INPUT(type=INPUT_MOUSE,
                 u=_INPUTUNION(mi=_MOUSEINPUT(0, 0, 0, flags, 0, None)))
    _u32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT))


# ---------------------------------------------------------------------------
# 前台化 / 遮挡校验
# ---------------------------------------------------------------------------
def foreground_hwnd() -> int:
    """当前前台窗口句柄。"""
    if not _IS_WIN:
        return 0
    return int(_u32.GetForegroundWindow() or 0)


def ensure_foreground(hwnd: int, timeout: float = 0.6) -> bool:
    """尽最大努力把 ``hwnd`` 置前，并**回读校验**是否真的成了前台。

    ``SetForegroundWindow`` 受前台锁限制，可能静默失败。这里用
    ``AttachThreadInput`` 把本线程挂到当前前台线程上绕过限制，最后必须用
    ``GetForegroundWindow()`` 回读确认 —— 校验不通过就返回 False，调用方
    应当放弃本次点击（否则会点到别的窗口）。
    """
    if not _IS_WIN or not hwnd:
        return False
    if _u32.IsIconic(hwnd):
        _u32.ShowWindow(hwnd, SW_RESTORE)
    if int(_u32.GetForegroundWindow() or 0) == int(hwnd):
        return True

    fg = int(_u32.GetForegroundWindow() or 0)
    fg_thread = _u32.GetWindowThreadProcessId(fg, None) if fg else 0
    cur_thread = int(_k32.GetCurrentThreadId())
    attached = False
    try:
        if fg_thread and fg_thread != cur_thread:
            attached = bool(_u32.AttachThreadInput(fg_thread, cur_thread, True))
        _u32.BringWindowToTop(hwnd)
        _u32.SetForegroundWindow(hwnd)
    except Exception:
        pass
    finally:
        if attached:
            try:
                _u32.AttachThreadInput(fg_thread, cur_thread, False)
            except Exception:
                pass

    t0 = time.time()
    while time.time() - t0 < timeout:
        if int(_u32.GetForegroundWindow() or 0) == int(hwnd):
            return True
        time.sleep(0.02)
    return int(_u32.GetForegroundWindow() or 0) == int(hwnd)


def is_point_on_window(hwnd: int, x: float, y: float) -> bool:
    """判断屏幕点 (x, y) 是否落在目标窗口（或其子窗口）上。

    点击前的硬闸门：窗口被别的窗口盖住时 ``WindowFromPoint`` 会返回那个
    窗口，据此放弃点击，避免误点到其他程序。
    """
    if not _IS_WIN or not hwnd:
        return False
    pt = ctypes.wintypes.POINT(int(round(x)), int(round(y)))
    h = int(_u32.WindowFromPoint(pt) or 0)
    if not h:
        return False
    if h == int(hwnd):
        return True
    root = int(_u32.GetAncestor(h, GA_ROOT) or 0)
    return root == int(hwnd) or h == int(hwnd)


def window_at_point(x: float, y: float) -> int:
    """返回屏幕点 (x, y) 处最顶层窗口的根句柄（诊断用）。"""
    if not _IS_WIN:
        return 0
    pt = ctypes.wintypes.POINT(int(round(x)), int(round(y)))
    h = int(_u32.WindowFromPoint(pt) or 0)
    if not h:
        return 0
    return int(_u32.GetAncestor(h, GA_ROOT) or 0) or h


# ---------------------------------------------------------------------------
# 点击器
# ---------------------------------------------------------------------------
class MouseClicker:
    """把「走一步棋」翻译成鼠标动作。

    ``mode``:
      * ``"two_click"``（默认）—— 点起点选子 → 间隔 → 点终点落子。
        中国象棋客户端普遍支持，且不需要在按压态跨帧移动，鲁棒性最高。
      * ``"drag"`` —— 按下起点 → 分步移动到终点 → 抬起。部分只认拖拽的
        棋盘需要它。

    ``backend``: ``"mouse_event"``（默认，对齐 BGI）或 ``"sendinput"``
    （更现代，部分 CEF/WebView 上更可靠）。
    """

    def __init__(self, mode: str = "two_click", move_steps: int = 1,
                 move_step_delay: float = 0.008, down_up_delay: float = 0.030,
                 click_gap: float = 0.120, restore_cursor: bool = False,
                 backend: str = "mouse_event") -> None:
        self.mode = mode
        self.move_steps = max(1, int(move_steps))
        self.move_step_delay = float(move_step_delay)
        self.down_up_delay = float(down_up_delay)
        self.click_gap = float(click_gap)
        self.restore_cursor = bool(restore_cursor)
        self.backend = backend
        self._saved: tuple[int, int] | None = None
        self.last_action: dict = {}

    # ---- 低层 ----
    def _down(self) -> None:
        if self.backend == "sendinput":
            _send_input(MOUSEEVENTF_LEFTDOWN)
        else:
            _mouse_event(MOUSEEVENTF_LEFTDOWN)

    def _up(self) -> None:
        if self.backend == "sendinput":
            _send_input(MOUSEEVENTF_LEFTUP)
        else:
            _mouse_event(MOUSEEVENTF_LEFTUP)

    def move_to(self, x: float, y: float, steps: int | None = None) -> bool:
        """移动光标；``steps>1`` 时线性插值分步移动（触发 hover 高亮）。"""
        steps = self.move_steps if steps is None else max(1, int(steps))
        if steps <= 1:
            return move_to(x, y)
        cx, cy = get_cursor_pos()
        for i in range(1, steps + 1):
            t = i / steps
            move_to(cx + (x - cx) * t, cy + (y - cy) * t)
            time.sleep(self.move_step_delay)
        return True

    # ---- 组合 ----
    def click_at(self, x: float, y: float) -> None:
        """移动到 (x, y) 并单击左键。"""
        self.move_to(x, y)
        time.sleep(self.down_up_delay)          # 让 hover 状态先建立
        self._down()
        time.sleep(self.down_up_delay)
        self._up()

    def preview_move(self, frm, to) -> None:
        """只移动光标到起点、不点击（校准用）。"""
        self.move_to(frm[0], frm[1])

    def place_move(self, frm, to) -> dict:
        """执行一次落子。返回本次动作的描述（供日志/UI）。"""
        if self.restore_cursor:
            self._saved = get_cursor_pos()
        try:
            if self.mode == "drag":
                self.move_to(frm[0], frm[1])
                time.sleep(self.down_up_delay)
                self._down()
                time.sleep(self.down_up_delay)
                self.move_to(to[0], to[1], steps=max(4, self.move_steps))
                time.sleep(self.down_up_delay)
                self._up()
            else:
                self.click_at(frm[0], frm[1])
                time.sleep(self.click_gap)
                self.click_at(to[0], to[1])
        finally:
            if self.restore_cursor and self._saved is not None:
                move_to(*self._saved)
        self.last_action = {"mode": self.mode, "from": tuple(frm), "to": tuple(to),
                            "backend": self.backend}
        return self.last_action
