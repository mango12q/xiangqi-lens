"""窗口探测 + 截图能力实测工具。

用途
------------------------------------------------------------------
1. 列出所有可见顶层窗口（标题 / 类名 / 进程 / 尺寸），用于找到微信
   小程序的宿主窗口。
2. 对指定窗口同时尝试「后台截图（PrintWindow）」和「前台截图（屏幕
   区域拷贝）」，输出到文件并报告是否全黑 —— 这一步直接决定连线的
   截图模式怎么选。

用法::

    python tools/probe_windows.py                 # 只列窗口
    python tools/probe_windows.py --shot 微信      # 按标题关键字截图测试
"""

from __future__ import annotations

import argparse
import ctypes
import sys
from pathlib import Path

import win32con
import win32gui
import win32ui
from ctypes import wintypes

# --------------------------------------------------------------------------
# DPI 感知：不做这一步，在高缩放屏幕上拿到的坐标是错的
# --------------------------------------------------------------------------


def enable_dpi_awareness() -> str:
    """开启 Per-Monitor V2 DPI 感知，返回实际生效的模式名。"""
    user32 = ctypes.windll.user32
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return "Per-Monitor V2"
    except Exception:
        pass
    try:
        # shcore: PROCESS_PER_MONITOR_DPI_AWARE = 2
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return "Per-Monitor"
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
        return "System"
    except Exception:
        return "None"


# --------------------------------------------------------------------------
# 窗口枚举
# --------------------------------------------------------------------------


class WindowInfo:
    __slots__ = ("hwnd", "title", "cls", "pid", "proc", "rect", "client", "visible")

    def __init__(self, hwnd: int):
        self.hwnd = hwnd
        self.title = win32gui.GetWindowText(hwnd)
        self.cls = win32gui.GetClassName(hwnd)
        self.visible = bool(win32gui.IsWindowVisible(hwnd))
        self.rect = win32gui.GetWindowRect(hwnd)
        try:
            self.client = win32gui.GetClientRect(hwnd)
        except Exception:
            self.client = (0, 0, 0, 0)
        self.pid = self._get_pid()
        self.proc = self._get_proc()

    def _get_pid(self) -> int:
        pid = wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(self.hwnd, ctypes.byref(pid))
        return int(pid.value)

    def _get_proc(self) -> str:
        try:
            import psutil  # 可选依赖
            return psutil.Process(self.pid).name()
        except Exception:
            # 退化方案：用 tasklist 太慢，直接用 pid 表示
            return f"pid:{self.pid}"

    @property
    def size(self) -> tuple[int, int]:
        l, t, r, b = self.rect
        return r - l, b - t

    @property
    def client_size(self) -> tuple[int, int]:
        l, t, r, b = self.client
        return r - l, b - t

    def __repr__(self) -> str:
        w, h = self.size
        cw, ch = self.client_size
        return (f"[0x{self.hwnd:08X}] {self.title!r} cls={self.cls} "
                f"proc={self.proc} win={w}x{h} cli={cw}x{ch}")


def enum_windows(visible_only: bool = True) -> list[WindowInfo]:
    out: list[WindowInfo] = []

    def cb(hwnd, _):
        info = WindowInfo(hwnd)
        if not visible_only or info.visible:
            out.append(info)
        return True

    win32gui.EnumWindows(cb, None)
    return out


def find_windows(*keywords: str, include_hidden: bool = False) -> list[WindowInfo]:
    """按标题或类名关键字模糊查找窗口。"""
    kws = [k.lower() for k in keywords]
    hits = []
    for w in enum_windows(visible_only=not include_hidden):
        hay = f"{w.title} {w.cls} {w.proc}".lower()
        if any(k in hay for k in kws):
            hits.append(w)
    return hits


# --------------------------------------------------------------------------
# 截图
# --------------------------------------------------------------------------

PW_RENDERFULLCONTENT = 0x00000002


def capture_printwindow(hwnd: int) -> "object | None":
    """后台截图：PrintWindow（带 PW_RENDERFULLCONTENT）。

    对 DirectX / WebView2 渲染的窗口，必须带这个标志位，否则拿到全黑图。
    """
    import numpy as np
    from PIL import Image

    try:
        l, t, r, b = win32gui.GetWindowRect(hwnd)
    except Exception:
        return None
    w, h = r - l, b - t
    if w <= 0 or h <= 0:
        return None

    hwnd_dc = win32gui.GetWindowDC(hwnd)
    if not hwnd_dc:
        return None
    mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = mfc_dc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    try:
        bmp.CreateCompatibleBitmap(mfc_dc, w, h)
        save_dc.SelectObject(bmp)
        ok = ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), PW_RENDERFULLCONTENT)
        if not ok:
            ok = ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 0)
        if not ok:
            return None
        info = bmp.GetInfo()
        bits = bmp.GetBitmapBits(True)
        arr = np.frombuffer(bits, dtype=np.uint8).reshape(info["bmHeight"], info["bmWidth"], 4)
        # PrintWindow 给的是 BGRA，转成 RGB
        return Image.fromarray(arr[:, :, [2, 1, 0]].copy())
    finally:
        try:
            win32gui.DeleteObject(bmp.GetHandle())
        except Exception:
            pass
        try:
            save_dc.DeleteDC()
        except Exception:
            pass
        try:
            mfc_dc.DeleteDC()
        except Exception:
            pass
        try:
            win32gui.ReleaseDC(hwnd, hwnd_dc)
        except Exception:
            pass


def capture_screen_region(bbox: tuple[int, int, int, int]) -> "object | None":
    """前台截图：从屏幕抓一块矩形区域（通用，但要求窗口不被遮挡）。"""
    import mss
    from PIL import Image

    l, t, r, b = bbox
    if r - l <= 0 or b - t <= 0:
        return None
    with mss.mss() as sct:
        shot = sct.grab({"left": l, "top": t, "width": r - l, "height": b - t})
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


def image_stats(img) -> dict:
    """统计图像亮度与"是否几乎全黑"，用于判断截图通道是否有效。"""
    import numpy as np

    arr = np.asarray(img.convert("RGB"), dtype=np.float32)
    mean = float(arr.mean())
    std = float(arr.std())
    # 非黑像素占比（任一道 > 12）
    nonblack = float((arr.max(axis=2) > 12).mean())
    return {
        "size": f"{img.width}x{img.height}",
        "mean": round(mean, 2),
        "std": round(std, 2),
        "nonblack_ratio": round(nonblack, 4),
        "looks_black": nonblack < 0.01,
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="窗口探测与截图实测")
    parser.add_argument("--shot", nargs="*", default=None,
                        help="按标题/类名/进程关键字截图测试，可多个关键字")
    parser.add_argument("--all", action="store_true", help="列出全部窗口（含无标题）")
    parser.add_argument("--out", default="probe_out", help="输出目录")
    args = parser.parse_args()

    mode = enable_dpi_awareness()
    print(f"DPI 感知模式: {mode}")
    try:
        import psutil  # noqa: F401
        print("psutil: 已安装（进程名可用）")
    except ImportError:
        print("psutil: 未安装（将用 pid 代替进程名，可 pip install psutil）")
    print()

    wins = enum_windows(visible_only=not args.all)
    print(f"=== 可见顶层窗口 {len(wins)} 个 ===")
    for w in sorted(wins, key=lambda x: -x.size[0] * x.size[1]):
        if not args.all and w.size[0] < 80 and w.size[1] < 80 and not w.title:
            continue
        print("  " + repr(w))

    if args.shot is None:
        print()
        print("提示: 加 --shot 微信 测试该窗口的后台/前台截图能力")
        return 0

    keywords = args.shot or ["微信"]
    print()
    print(f"=== 截图测试: 关键字 {keywords} ===")
    targets = find_windows(*keywords, include_hidden=True)
    if not targets:
        print("未找到匹配窗口")
        return 1

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    for w in targets:
        if w.size[0] < 100 or w.size[1] < 100:
            continue
        print()
        print(f"--- {w.title!r} cls={w.cls} proc={w.proc} hwnd=0x{w.hwnd:08X} ---")

        back = capture_printwindow(w.hwnd)
        if back is not None:
            st = image_stats(back)
            name = outdir / f"bg_{w.hwnd:08X}.png"
            back.save(name)
            print(f"  后台截图(PrintWindow): {st}  -> {name}")
        else:
            print("  后台截图(PrintWindow): 失败")

        fore = capture_screen_region(w.rect)
        if fore is not None:
            st = image_stats(fore)
            name = outdir / f"fg_{w.hwnd:08X}.png"
            fore.save(name)
            print(f"  前台截图(屏幕区域):    {st}  -> {name}")
        else:
            print("  前台截图(屏幕区域): 失败")

    return 0


if __name__ == "__main__":
    sys.exit(main())
