"""窗口捕获层：定位目标窗口、后台/前台截图、区域标定与坐标换算。

两种截图通道
------------------------------------------------------------------
后台截图 (PrintWindow + PW_RENDERFULLCONTENT)
    不要求窗口可见、可以被遮挡，不占用屏幕。实测微信（Qt/WebView2）
    可正常出图。这是首选通道。

前台截图 (mss 屏幕区域拷贝)
    通用兜底，任何窗口都能截，但要求目标窗口未被遮挡。
    如果后台截图全黑就走这条。

坐标系约定
------------------------------------------------------------------
* **图像坐标**：截图上以像素为单位，原点在图像左上角。棋盘的
  ``region`` 与网格 ``grid_rect`` 都用这个坐标系。这样窗口移动、
  缩放变化都不影响标定结果（只要窗口大小不变）。
* **屏幕坐标**：物理像素，原点在主显示器左上角。
* 进程启动时开启 Per-Monitor V2 DPI 感知，因此上面两种坐标都是
  物理像素，与系统缩放无关 —— 不做这一步的话在 125%/150% 缩放下
  坐标会整体偏移。
"""

from __future__ import annotations

import ctypes
from dataclasses import dataclass, field
from typing import Iterable, Sequence

import numpy as np
import win32gui
import win32ui
from PIL import Image

# --------------------------------------------------------------------------
# DPI
# --------------------------------------------------------------------------


def enable_dpi_awareness() -> str:
    """开启 Per-Monitor V2 DPI 感知，返回生效的模式名。幂等。"""
    user32 = ctypes.windll.user32
    try:
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return "Per-Monitor V2"
    except Exception:
        pass
    try:
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
# 窗口
# --------------------------------------------------------------------------


@dataclass
class WindowTarget:
    """一个可用于截图的目标窗口。"""

    hwnd: int
    title: str
    cls: str
    pid: int
    rect: tuple[int, int, int, int]      # 窗口外框（屏幕坐标）
    client: tuple[int, int, int, int]    # 客户区（相对窗口，原点通常为 0,0）

    @property
    def width(self) -> int:
        return self.rect[2] - self.rect[0]

    @property
    def height(self) -> int:
        return self.rect[3] - self.rect[1]

    @property
    def client_size(self) -> tuple[int, int]:
        return self.client[2] - self.client[0], self.client[3] - self.client[1]

    @property
    def is_alive(self) -> bool:
        return bool(win32gui.IsWindow(self.hwnd))

    def describe(self) -> str:
        return (f"0x{self.hwnd:08X} {self.title!r} [{self.cls}] "
                f"{self.width}x{self.height}")

    def client_to_screen(self, x: int, y: int) -> tuple[int, int]:
        """客户区坐标 -> 屏幕坐标。"""
        sx, sy = win32gui.ClientToScreen(self.hwnd, (int(x), int(y)))
        return int(sx), int(sy)

    def screen_to_client(self, x: int, y: int) -> tuple[int, int]:
        sx, sy = win32gui.ScreenToClient(self.hwnd, (int(x), int(y)))
        return int(sx), int(sy)


def _make_target(hwnd: int) -> WindowTarget:
    pid = ctypes.c_ulong()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return WindowTarget(
        hwnd=hwnd,
        title=win32gui.GetWindowText(hwnd),
        cls=win32gui.GetClassName(hwnd),
        pid=int(pid.value),
        rect=win32gui.GetWindowRect(hwnd),
        client=win32gui.GetClientRect(hwnd),
    )


def enum_windows(visible_only: bool = True, min_size: int = 0) -> list[WindowTarget]:
    """枚举顶层窗口。"""
    out: list[WindowTarget] = []

    def cb(hwnd, _):
        if visible_only and not win32gui.IsWindowVisible(hwnd):
            return True
        t = _make_target(hwnd)
        if t.width >= min_size and t.height >= min_size:
            out.append(t)
        return True

    win32gui.EnumWindows(cb, None)
    return out


def find_window(keyword: str = "", cls: str = "", min_size: int = 200,
                title_exact: bool = False) -> WindowTarget | None:
    """按标题关键字 / 窗口类查找窗口，返回面积最大的匹配项。

    多个同名窗口时取最大的那个（通常是真正显示内容的窗口）。
    """
    kw = keyword.lower()
    cl = cls.lower()
    best: WindowTarget | None = None
    best_area = 0
    for t in enum_windows(visible_only=True, min_size=min_size):
        title = t.title.lower()
        if kw:
            hit = (title == kw) if title_exact else (kw in title)
            if not hit:
                continue
        if cl and cl not in t.cls.lower():
            continue
        area = t.width * t.height
        if area > best_area:
            best, best_area = t, area
    return best


def find_windows_by(title_keywords: Sequence[str] = (),
                    class_keywords: Sequence[str] = (),
                    min_size: int = 200) -> list[WindowTarget]:
    """按一组标题/类名关键字批量匹配。"""
    tk = [k.lower() for k in title_keywords if k]
    ck = [k.lower() for k in class_keywords if k]
    hits: list[WindowTarget] = []
    for t in enum_windows(visible_only=True, min_size=min_size):
        if tk and not any(k in t.title.lower() for k in tk):
            continue
        if ck and not any(k in t.cls.lower() for k in ck):
            continue
        hits.append(t)
    hits.sort(key=lambda x: -x.width * x.height)
    return hits


# --------------------------------------------------------------------------
# 截图后端
# --------------------------------------------------------------------------

PW_RENDERFULLCONTENT = 0x00000002
PW_CLIENTONLY = 0x00000001


def capture_printwindow(hwnd: int, client_only: bool = False) -> Image.Image | None:
    """后台截图。失败返回 None。

    对 DirectX / Qt / WebView2 渲染的窗口必须带 PW_RENDERFULLCONTENT，
    否则结果全黑。
    """
    if not win32gui.IsWindow(hwnd):
        return None

    flags = PW_RENDERFULLCONTENT | (PW_CLIENTONLY if client_only else 0)
    win_rect = win32gui.GetWindowRect(hwnd)
    if client_only:
        # 客户区在窗口内的偏移与尺寸，用于从整窗渲染结果里裁出来
        ox, oy = win32gui.ClientToScreen(hwnd, (0, 0))
        crop_x = ox - win_rect[0]
        crop_y = oy - win_rect[1]
        _, _, cw, ch = win32gui.GetClientRect(hwnd)
    else:
        crop_x = crop_y = 0
        cw, ch = 0, 0

    w = win_rect[2] - win_rect[0]
    h = win_rect[3] - win_rect[1]
    if w <= 0 or h <= 0:
        return None

    hwnd_dc = None
    mfc_dc = None
    save_dc = None
    bmp = None
    try:
        hwnd_dc = win32gui.GetWindowDC(hwnd)
        if not hwnd_dc:
            return None
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        bmp = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(mfc_dc, w, h)
        save_dc.SelectObject(bmp)

        ok = ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), flags)
        if not ok:
            # 退化尝试：不带标志位
            ok = ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), 0)
        if not ok:
            return None

        info = bmp.GetInfo()
        bits = bmp.GetBitmapBits(True)
        bw, bh = info["bmWidth"], info["bmHeight"]
        arr = np.frombuffer(bits, dtype=np.uint8)
        if arr.size < bw * bh * 4:
            return None
        arr = arr[: bw * bh * 4].reshape(bh, bw, 4)
        rgb = arr[:, :, [2, 1, 0]]           # BGRA -> RGB
        img = Image.fromarray(np.ascontiguousarray(rgb))

        if client_only and (crop_x or crop_y):
            box = (crop_x, crop_y, crop_x + cw, crop_y + ch)
            if (box[0] >= 0 and box[1] >= 0
                    and box[2] <= img.width and box[3] <= img.height):
                img = img.crop(box)
        return img
    except Exception:
        return None
    finally:
        if bmp is not None:
            try:
                win32gui.DeleteObject(bmp.GetHandle())
            except Exception:
                pass
        for dc in (save_dc, mfc_dc):
            if dc is not None:
                try:
                    dc.DeleteDC()
                except Exception:
                    pass
        if hwnd_dc:
            try:
                win32gui.ReleaseDC(hwnd, hwnd_dc)
            except Exception:
                pass


def capture_screen_region(bbox: Sequence[int]) -> Image.Image | None:
    """前台截图：抓屏幕上的一块矩形。"""
    l, t, r, b = (int(v) for v in bbox)
    w, h = r - l, b - t
    if w <= 0 or h <= 0:
        return None
    try:
        import mss
        factory = getattr(mss, "MSS", None) or mss.mss
        with factory() as sct:
            shot = sct.grab({"left": l, "top": t, "width": w, "height": h})
            return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    except Exception:
        return None


def image_stats(img: Image.Image) -> dict:
    """判断截图通道是否有效（全黑/全白视为无效）。"""
    arr = np.asarray(img.convert("RGB"), dtype=np.float32)
    nonblack = float((arr.max(axis=2) > 12).mean())
    return {
        "size": f"{img.width}x{img.height}",
        "mean": round(float(arr.mean()), 2),
        "std": round(float(arr.std()), 2),
        "nonblack_ratio": round(nonblack, 4),
        "usable": nonblack > 0.02 and float(arr.std()) > 2.0,
    }


# --------------------------------------------------------------------------
# 帧源
# --------------------------------------------------------------------------


@dataclass
class CaptureConfig:
    """截图配置。"""

    keyword: str = "微信"              # 目标窗口标题关键字
    window_class: str = ""             # 可选的窗口类过滤
    mode: str = "background"           # background | foreground | auto
    client_only: bool = False          # 只截客户区（去掉标题栏）


class FrameSource:
    """按配置持续获取目标窗口画面。"""

    def __init__(self, config: CaptureConfig | None = None) -> None:
        self.config = config or CaptureConfig()
        self.target: WindowTarget | None = None
        self._last_error: str = ""
        self._active_mode: str = self.config.mode

    # ---------------- 目标窗口 ----------------

    def attach(self, target: WindowTarget | None = None) -> WindowTarget | None:
        """绑定目标窗口。传入 None 则按配置自动查找。"""
        if target is not None:
            self.target = target
            return target
        self.target = find_window(self.config.keyword, self.config.window_class)
        return self.target

    @property
    def attached(self) -> bool:
        return self.target is not None and self.target.is_alive

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def active_mode(self) -> str:
        """实际生效的截图模式（auto 模式下可能会回退到 foreground）。"""
        return self._active_mode

    # ---------------- 抓帧 ----------------

    def grab(self) -> Image.Image | None:
        """抓一帧。窗口不存在或截图失败时返回 None。"""
        if self.target is None:
            self.attach()
        if self.target is None:
            self._last_error = f"未找到窗口: {self.config.keyword!r}"
            return None
        if not self.target.is_alive:
            self._last_error = "目标窗口已关闭"
            self.target = None
            return None

        mode = self.config.mode
        img: Image.Image | None = None

        if mode in ("background", "auto"):
            img = capture_printwindow(self.target.hwnd, self.config.client_only)
            if img is not None:
                st = image_stats(img)
                if st["usable"]:
                    self._active_mode = "background"
                    self._last_error = ""
                    return img
                if mode == "background":
                    # 明确要求后台模式但拿到黑图：如实报错，不静默回退
                    self._last_error = f"后台截图不可用 (nonblack={st['nonblack_ratio']})"
                    return img
                img = None
            elif mode == "background":
                self._last_error = "PrintWindow 调用失败"
                return None

        # 前台兜底
        img = capture_screen_region(self.target.rect)
        if img is None:
            self._last_error = "前台截图失败"
            return None
        self._active_mode = "foreground"
        self._last_error = ""
        return img

    def probe_modes(self) -> dict[str, dict]:
        """同时测试两种通道，返回可用性报告。用于首次配置时的引导。"""
        report: dict[str, dict] = {}
        if self.target is None:
            self.attach()
        if self.target is None:
            return {"error": {"message": f"未找到窗口 {self.config.keyword!r}"}}

        bg = capture_printwindow(self.target.hwnd, self.config.client_only)
        report["background"] = image_stats(bg) if bg is not None else {"usable": False,
                                                                      "message": "调用失败"}
        fg = capture_screen_region(self.target.rect)
        report["foreground"] = image_stats(fg) if fg is not None else {"usable": False,
                                                                      "message": "调用失败"}
        report["target"] = {"title": self.target.title,
                            "class": self.target.cls,
                            "hwnd": f"0x{self.target.hwnd:08X}",
                            "size": f"{self.target.width}x{self.target.height}"}
        return report


# --------------------------------------------------------------------------
# 区域标定
# --------------------------------------------------------------------------


@dataclass
class Region:
    """图像坐标系下的一个矩形区域。"""

    x: int
    y: int
    w: int
    h: int

    @property
    def bbox(self) -> tuple[int, int, int, int]:
        return self.x, self.y, self.x + self.w, self.y + self.h

    @property
    def center(self) -> tuple[int, int]:
        return self.x + self.w // 2, self.y + self.h // 2

    def is_valid(self, min_size: int = 8) -> bool:
        return self.w >= min_size and self.h >= min_size

    def crop(self, img: Image.Image | np.ndarray):
        l, t, r, b = self.bbox
        if isinstance(img, Image.Image):
            return img.crop((l, t, r, b))
        return img[t:b, l:r]

    def to_dict(self) -> dict:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h}

    @classmethod
    def from_dict(cls, d: dict) -> "Region":
        return cls(int(d["x"]), int(d["y"]), int(d["w"]), int(d["h"]))

    @classmethod
    def from_points(cls, p1: tuple[int, int], p2: tuple[int, int]) -> "Region":
        x1, y1 = p1
        x2, y2 = p2
        x, y = min(x1, x2), min(y1, y2)
        return cls(x, y, abs(x2 - x1), abs(y2 - y1))


@dataclass
class BoardGeometry:
    """棋盘在截图中的几何标定。

    ``region`` 是棋盘格的**外接矩形**，包含棋盘的边框和河界，也就是
    9 列 x 10 行交叉点的外框；``grid_rect`` 一般是它在 X 轴上按
    ``cols/rows`` 比例校正后的结果（因为象棋棋盘格子不是正方形，
    9 列 10 行的交叉点间距比值约为 8:9）。

    只存 ``region`` 就能算所有交叉点：交叉点均分区域。
    """

    region: Region
    flipped: bool = False       # True 表示红方在屏幕上方

    def point_of(self, row: int, col: int, rows: int = 10, cols: int = 9) -> tuple[int, int]:
        """返回第 (row, col) 个交叉点的图像坐标。

        row 0 = 顶部交叉点行，col 0 = 最左列。``flipped`` 只影响调用方
        对 row 的解释，这里始终按图像方向计算。
        """
        if cols < 2 or rows < 2:
            raise ValueError("行列数至少为 2")
        x = self.region.x + round(col * self.region.w / (cols - 1))
        y = self.region.y + round(row * self.region.h / (rows - 1))
        return x, y

    def cell_size(self, rows: int = 10, cols: int = 9) -> tuple[float, float]:
        return self.region.w / (cols - 1), self.region.h / (rows - 1)

    def all_points(self, rows: int = 10, cols: int = 9) -> list[list[tuple[int, int]]]:
        return [[self.point_of(r, c, rows, cols) for c in range(cols)] for r in range(rows)]

    def to_dict(self) -> dict:
        return {"region": self.region.to_dict(), "flipped": self.flipped}

    @classmethod
    def from_dict(cls, d: dict) -> "BoardGeometry":
        return cls(Region.from_dict(d["region"]), bool(d.get("flipped", False)))


def auto_locate_board(img: Image.Image, search_ratio: tuple[float, float] = (0.4, 0.95)
                      ) -> Region | None:
    """粗略地在截图中寻找棋盘区域（返回图像坐标）。

    策略：棋盘通常是一块**暖色（木色/棕色）低饱和度**区域，且呈
    近 9:10 的竖向矩形。这里用颜色阈值找最大连通块的包围盒，作为
    自动标定的初值 —— 最终仍建议用户手动微调一次。
    """
    import cv2

    arr = np.asarray(img.convert("RGB"))
    h, w = arr.shape[:2]
    hsv = cv2.cvtColor(arr, cv2.COLOR_RGB2HSV)
    hch, s, v = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]

    # 木色/米黄：色相在橙黄一带，饱和度和亮度都较高
    mask = ((hch >= 5) & (hch <= 35) & (s >= 25) & (s <= 200) & (v >= 60)).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((9, 9), np.uint8))

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    lo, hi = search_ratio
    best: Region | None = None
    best_score = 0.0
    for c in contours:
        x, y, bw, bh = cv2.boundingRect(c)
        if bw < w * 0.15 or bh < h * 0.15:
            continue
        ratio = bh / max(bw, 1)
        if not (lo <= ratio <= hi):
            continue
        score = float(bw) * bh * (1.0 - abs(ratio - 1.11) / 1.11)
        if score > best_score:
            best_score = score
            best = Region(x, y, bw, bh)
    return best
