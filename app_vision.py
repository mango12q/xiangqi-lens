# -*- coding: utf-8 -*-
"""XiangQiLens GUI —— 界面与调度层（识别/引擎逻辑见 app_backend.py）。

默认只给走法建议，落子由用户自行操作；可选「自动走棋」（默认关闭 +
预览模式）会模拟鼠标点击替我方落子 —— 见 Worker._pump_auto_move 与
app_input.MouseClicker。
"""
from __future__ import annotations

import argparse
import json
import queue
import sys
import time
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent

# 标准起始局面的**棋盘段**（不含轮次）。用来判断「盘面还是开局第一手」——
# 象棋红先，所以此时行棋方必然是红方；自动走棋靠它挡掉「轮次被猜成黑方」的
# 危险情形（见 Worker._pump_auto_move）。
START_BOARD = "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR"

# 切到「应用根目录」：模型与引擎用相对路径查找，从桌面快捷方式或
# 打包后的 exe 启动时，当前目录可能是任意位置，不切换会找不到资源。
# app_backend.app_base() 会区分源码运行（脚本目录）与打包运行（exe 目录）。
import os as _os
import sys as _sys
try:
    _base = Path(_sys.executable).resolve().parent if getattr(_sys, "frozen", False) else HERE
    _os.chdir(_base)
except OSError:
    pass

sys.path.insert(0, str(HERE))

from app_backend import (HERE as BACKEND_DIR, MultiPVBuffer, ScreenSource,
                         __version__, app_base, apply_engine_options,
                         check_board_geometry, check_position, clear_hash,
                         create_engine, create_vision, enable_dpi_awareness,
                         fen_with_side, format_score, format_winrate,
                         is_strict_legal_move,
                         list_windows_any, move_arrow,
                         move_screen_points, move_to_chinese,
                         parse_engine_options, pv_to_chinese, selftest,
                         short_winrate, validate_fen, wdl_to_red, window_state)

from app_book import OpeningBook
from app_input import MouseClicker, ensure_foreground, is_point_on_window

from PySide6.QtCore import QObject, QRect, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen
from PySide6.QtWidgets import (QApplication, QButtonGroup, QCheckBox, QComboBox,
                               QDoubleSpinBox, QFileDialog,
                               QFormLayout, QGridLayout, QGroupBox,
                               QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QMainWindow, QMessageBox, QPushButton,
                               QRadioButton, QScrollArea, QSpinBox, QSplitter,
                               QStatusBar, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)


# ---------------------------------------------------------------------------
# 纯函数：识别稳定（动画抑制）与终局判定
# ---------------------------------------------------------------------------
# 抽成模块级纯函数是为了能脱离 Qt/Worker 单独做单元测试（见
# tools/test_settle_gate.py 与 tools/test_gameover.py）。
def gray_small(img, size: tuple[int, int] = (144, 160)):
    """转灰度并缩放，供帧间差异比较用。"""
    g = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    return cv2.resize(g, size, interpolation=cv2.INTER_AREA)


def motion_ratio(prev, cur, pixel_delta: int = 25) -> float:
    """两帧灰度图的「变化像素占比」（0.0 ~ 1.0）。

    用占比而不是平均绝对差，是为了对**局部**动画敏感 —— 天天象棋的「吃」
    字只覆盖盘面中间一小块，平均差会被大片静止区域稀释掉，占比不会。
    首帧（prev 为 None）或形状不一致时返回 0.0（按静止处理，避免开局卡住）。
    """
    if prev is None or cur is None or prev.shape != cur.shape:
        return 0.0
    diff = cv2.absdiff(prev, cur)
    return float((diff > pixel_delta).mean())


def settle_ready(still_since: float, now: float, settle_ms: int) -> bool:
    """画面是否已持续静止达到 ``settle_ms``。"""
    if still_since <= 0.0:
        return False
    return (now - still_since) * 1000.0 >= max(0, settle_ms)


def suppress_timed_out(settle_start: float, now: float, max_ms: int) -> bool:
    """「画面一直在动」是否已超过最长抑制时长（该强制放行一帧了）。

    没有这个兜底，盘面上若有**持续**动画/闪烁（将军提示、倒计时环、平台特效），
    变化率会一直超阈值 → 永远 continue → **永不识别**。
    """
    if settle_start <= 0.0:
        return False
    return (now - settle_start) * 1000.0 >= max(0, max_ms)


# ---------------------------------------------------------------------------
# 异常可见化
# ---------------------------------------------------------------------------
_last_report_t = 0.0


def report_exception(exc: BaseException, where: str = "") -> None:
    """把未捕获异常写进日志并弹窗提示。

    **为什么必须有**：程序用 ``pythonw`` 启动**没有控制台**，Qt 槽函数里抛出
    的异常会被 PySide 打到 stderr 后吞掉 —— 用户看到的就是「点了按钮没反应」，
    完全无从排查。（本次「框选没反应」就是因为 ``int((hwnd, title))`` 抛了
    TypeError 被静默吞掉，一直没人发现。）

    ★ 弹窗用**非阻塞 + 限频**：槽函数可能被高频调用（例如每帧一次），
    ``QMessageBox.critical`` 是模态的，会卡死 GUI 线程；反复弹也会堆一屏。
    """
    global _last_report_t
    import traceback
    try:
        txt = "".join(traceback.format_exception(
            type(exc), exc, exc.__traceback__))
    except Exception:
        txt = f"{type(exc).__name__}: {exc}"
    head = f"{type(exc).__name__}: {exc}"
    if where:
        head = f"[{where}] {head}"
    try:
        with open(app_base() / "XiangQiLens_crash.log", "a",
                  encoding="utf-8") as fh:
            fh.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n{txt}")
    except Exception:
        pass

    now = time.time()
    if now - _last_report_t < 3.0:              # 限频，避免刷屏/卡死
        return
    _last_report_t = now
    try:
        box = QMessageBox(QMessageBox.Critical, "程序内部错误",
                          f"{head}\n\n详细信息已写入 XiangQiLens_crash.log")
        box.setAttribute(Qt.WA_DeleteOnClose, True)
        box.show()                              # 非阻塞：不卡 GUI 线程
    except Exception:
        pass


def safe_slot(fn):
    """装饰 Qt 槽函数：异常不再被静默吞掉，而是写日志 + 弹窗。

    ★ **必须按被包函数的签名裁剪多余的实参**：Qt 信号可能带参数
    （``Signal(bool)`` / ``Signal(dict)``…），而槽函数可能一个都不收；
    PySide 会把信号实参**全量**传给槽，包装器里的 ``fn(*a)`` 就会抛
    ``TypeError: takes 1 positional argument but 2 were given``。
    ``functools.wraps`` 只影响 ``inspect.signature`` 的观感，**不会**让
    PySide 少传参数（已实测），所以这里必须自己裁。
    """
    import functools
    import inspect

    try:
        _sig = inspect.signature(fn)
        _params = list(_sig.parameters.values())
        _n_pos = sum(1 for p in _params
                     if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD))
        _varargs = any(p.kind == p.VAR_POSITIONAL for p in _params)
    except Exception:
        _n_pos, _varargs = 0, True

    @functools.wraps(fn)
    def _wrapper(*args, **kwargs):
        if not _varargs and len(args) > _n_pos:
            args = args[:_n_pos]
        try:
            return fn(*args, **kwargs)
        except Exception as exc:                     # noqa: BLE001
            report_exception(exc, where=fn.__name__)
            return None
    return _wrapper


# ---------------------------------------------------------------------------
# 后台工作线程
# ---------------------------------------------------------------------------
class Worker(QObject):
    """抓帧 → 识别 → 棋规校验 → 引擎分析。"""

    frame_ready = Signal(object, object, str, dict, str)
    status = Signal(str)
    error = Signal(str)
    done = Signal()
    # 流式分析刷新（无图像）：引擎每加深一层就发一次，避免反复转换图像。
    analysis_update = Signal(str, dict)
    # 引擎握手时声明的可调选项，用于动态构建「高级」面板。
    engine_options = Signal(list)
    # 自动走棋开关变化（驱动主界面的「运行中」指示）
    auto_state_changed = Signal(bool)
    # 自动走棋每次动作的预览/执行信息 {best, chinese, from, to, dry}
    auto_preview = Signal(dict)
    # 开局库状态变化 {enabled, path, kind, ok, note}
    book_state = Signal(dict)
    # 识别稳定（动画抑制）状态变化 {suppress, settling, ratio}
    settle_state = Signal(dict)
    # 「新局」自动判断出的我方执子 {side, why}
    side_detected = Signal(dict)

    def __init__(self, source: ScreenSource) -> None:
        super().__init__()
        self.source = source
        self.threads = 2
        self.hash_mb = 256
        self.movetime = 1200
        self.confirm = 3
        # ---- 引擎参数（UI 可调） ----
        self.multipv = 3               # 多主变例条数
        self.infinite = False          # 持续加深（go infinite）
        self.move_overhead = 0
        self.show_wdl = True
        self.engine_opts: dict = {}    # 「高级」面板里手改的选项 {name: value}
        # first_side：首帧轮次（红先/黑先），只影响"该谁走"
        self.first_side = "w"
        # my_side：我方执子方（红/黑）。**只影响左栏显示图的朝向**
        # （执黑时把显示图转 180°，让我方在下）；「当前建议」栏的胜率已改为
        # 红/黑方绝对视角表述（见 format_winrate），不再随本项变化。
        # 矩阵/FEN 永远是标准朝向，因为识别模型的拉正流程已按内容把黑方底线
        # 摆到 row 0（见 run() 里的注释）。这是与 first_side 完全独立的一维。
        self.my_side = "w"
        self._run = False
        self._vision = None
        self._engine = None
        self._last_key = ""
        self._same = 0
        self._published = ""
        self._tracker = None
        self._last_fen = ""
        # ---- 非阻塞引擎驱动状态 ----
        self._searching = False        # 当前是否有搜索在进行
        self._search_fen = ""          # 正在分析的 FEN
        self._multi = 1                # 引擎当前已下发的 MultiPV
        self._pv = MultiPVBuffer()     # 累积的 MultiPV 结果
        self._turn_checked = False     # 本局面是否已做过轮次校正
        self._cur_fen_base = ""        # 当前局面的棋盘段（不含轮次）
        self._cur_side = "w"
        self._cur_warp_mat = None      # 画箭头用的透视矩阵
        self._cur_ctx: dict = {}       # 每帧识别上下文（conf/t_recog/moves/note…）
        self._last_emit = 0.0          # analysis_update 节流时间戳
        self._last_rebuild = 0.0       # 引擎重建重试节流时间戳
        self._cn_cache: dict = {}      # (fen, pv) → 中文序列，避免重复回放
        # ---- 自动走棋（模拟鼠标点击落子，仅走我方）----
        # 可调项（经 _cmds 队列下发，见 post_auto / apply_auto_params）
        self.auto_move_enabled = False  # 总开关（默认关）
        self.auto_think_ms = 1200       # 自动走棋专用思考时间（强制有限搜索）
        self.auto_dry_run = True        # 预览模式：只算不点（默认开，安全）
        self.auto_click_mode = "two_click"   # "two_click" | "drag"
        self.auto_cooldown = 1.5        # 两次落子最小间隔 s
        self.auto_max_moves = 200       # 连续走子保险丝
        self.auto_place_wait = 2.5      # 落子后等待棋盘更新的超时 s
        self.auto_retry_max = 3         # 点击未生效的最大重试次数
        self.auto_restore_cursor = False
        self.auto_move_steps = 1        # 光标移动插值步数（1=直跳）
        # 运行时状态
        self._clicker = None            # MouseClicker（首次真点时惰性创建）
        self._last_bestmove = ""        # 最近一次 bestmove（ICCS）
        self._last_bestmove_fen = ""    # 该 bestmove 对应的 FEN
        self._auto_done: tuple = ("", "")   # 已处理过的 (fen, best)，防重复
        self._auto_pending = None       # (fen, best, t_click) 等待棋盘更新
        self._auto_retries = 0
        self._auto_next_click_t = 0.0
        self._auto_last_click_t = 0.0
        self._auto_moves_made = 0
        self._cur_rows: list[str] | None = None   # 当前已采信矩阵（校验起点棋子）
        self._auto_preview: dict = {}   # 最近一次预览/落子信息（给 UI）
        # ---- 开局库（应用层实现，见 app_book.py）----
        self.book_enabled = False
        self.book_path = ""
        self.book_mode = "hint"         # "hint" 仅提示 | "play" 库着优先
        self.book_max_ply = 40          # 只在前 N 手内用库（0 = 不限）
        self._book: OpeningBook | None = None
        self._book_note = ""            # 库加载结果描述（给 UI）
        self._book_move: dict | None = None   # 当前局面的库着 {move,weight,chinese}
        # ---- 识别稳定（动画抑制）----
        # 天天象棋吃子时棋盘中间会弹「吃」字动画，遮住盘面导致误识别。
        # 用帧间像素差异检测「画面正在变化」，变化期间一律不进入演化校验，
        # 直到画面静止达到 settle_ms 才恢复 —— 即「等动画消失后再识别」。
        self.anim_suppress = False      # 动画抑制总开关（默认关）
        self.settle_ms = 400            # 画面需静止这么久才恢复识别
        self.motion_thresh = 0.02       # 变化像素比例阈值（2%）
        # 最长抑制时长：超过就强制放行一帧，防止持续动画把识别永久卡死
        self.anim_max_ms = 5000
        self._prev_gray = None          # 上一帧的拉正灰度小图
        self._still_since = 0.0         # 画面开始静止的时刻
        self._settle_start = 0.0        # 本轮「画面在动」的起始时刻（超时兜底用）
        self._settling = False          # 是否正处于「等待稳定」状态
        # ---- 「新局」自动判断我方执子 ----
        self.auto_side_pending = False  # 待自动判断（棋盘一出现就判）
        self._side_note = ""            # 上次的失败原因（避免刷屏）
        # 主线程 → Worker 的参数变更命令队列。
        # 不能用 Qt 信号：run() 是个长驻阻塞循环，永远不会把控制权交回该线程
        # 的事件循环，queued 连接的槽函数根本不会被派发。
        self._cmds: queue.Queue = queue.Queue()
        self._log_path, self._log_fh = self._open_log()

    @staticmethod
    def _open_log():
        """打开运行日志，写在应用根目录旁边。

        打包后没有控制台，出问题时全靠这个日志定位；源码运行也一并写，
        便于对照。写入失败不影响主流程。
        """
        try:
            base = Path(_sys.executable).resolve().parent if getattr(_sys, "frozen", False) else HERE
            p = base / "XiangQiLens.log"
            fh = open(p, "w", encoding="utf-8", buffering=1)
            fh.write(f"XiangQiLens 运行日志  {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            fh.write(f"版本: v{__version__}\n")
            fh.write(f"模式: {'打包 exe' if getattr(_sys, 'frozen', False) else '源码'}\n")
            fh.write("=" * 60 + "\n")
            return p, fh
        except Exception:
            return None, None

    def _log(self, msg: str) -> None:
        if self._log_fh is not None:
            try:
                self._log_fh.write(f"{time.strftime('%H:%M:%S')}  {msg}\n")
            except Exception:
                pass

    @staticmethod
    def flip_rows(rows: list[str]) -> list[str]:
        """把识别出的 10x9 矩阵旋转 180°，得到标准朝向（row 0 = 黑方底线）。

        模型训练时假设画面里红方在下方，因此：
        * 我方执红（红在下）→ 原样使用
        * 我方执黑（黑在下）→ 画面上下颠倒，需 180° 旋转
          （上下翻转 + 每行左右翻转；只上下翻会得到镜像的非法国度）

        返回的每一行也一并左右翻转，保证与 FEN 的 a..i 列序一致。
        """
        return [row[::-1] for row in reversed(rows)]

    @staticmethod
    def rows_to_fen(rows: list[str], side_to_move: str) -> str:
        """把 10x9 短标签矩阵转成 FEN（不依赖 xq_vision，便于翻转后使用）。"""
        board: list[str] = []
        for row in rows:
            s, empty = "", 0
            for ch in row:
                if ch in ".x":
                    empty += 1
                else:
                    if empty:
                        s += str(empty)
                        empty = 0
                    s += ch
            if empty:
                s += str(empty)
            board.append(s)
        return "/".join(board) + f" {side_to_move} - - 0 1"

    # ---- 识别稳定（动画抑制）----
    def _frame_moving(self, warped) -> tuple[bool, float]:
        """比较相邻两帧的拉正棋盘，判断「画面是否正在变化」。

        返回 ``(是否在动, 变化像素比例)``。

        实现：转灰度 → 缩到 144x160（够用且快）→ 与上一帧做绝对差 →
        统计差值 > 25 的像素占比。天天象棋的「吃」字动画是画在盘面中间
        的，会落在拉正图里，因此能被这一步抓到。

        注意首帧没有参照物，一律按「静止」返回，避免开局就一直卡住。
        """
        try:
            g = gray_small(warped)
        except Exception:
            return False, 0.0
        prev, self._prev_gray = self._prev_gray, g
        ratio = motion_ratio(prev, g)
        return ratio > self.motion_thresh, ratio

    # ---- 「新局」自动判断我方执子 ----
    @staticmethod
    def _detect_my_side(keypoints, img_shape=None) -> tuple[str | None, str]:
        """从棋盘朝向判断我方执子，返回 ``(side, 说明)``；判不了返回 ``(None, 原因)``。

        原理：识别模型给的 4 个角是**内容语义**的 —— ``BONE_NAMES = [A0, A8,
        J0, J8]``，前两个是黑方两角、后两个是红方两角（见 xq_vision.py）。
        而象棋客户端一律把**己方**摆在画面下方 ⇒ 谁的两角在下面（y 更大），
        谁就是我方。跟「红方先手」无关，只看朝向，所以开局红方还没动子也能判。

        质量门（很重要）：关键点检测偶尔会退化（例如 A0 跑到画面底部、A8 在
        顶部，或整组点跑到画面外）。这里要求「同一条边的两角大致等高」+
        「棋盘有足够高度」+「四点都在画面内」，否则拒绝判断而不是瞎猜。
        """
        try:
            kp = np.asarray(keypoints, dtype=float)
        except Exception:
            return None, "关键点无法解析"
        if kp.shape[0] < 4 or not np.all(np.isfinite(kp[:4])):
            return None, "关键点数量不足"

        a0, a8, j0, j8 = kp[0], kp[1], kp[2], kp[3]
        a_y = (a0[1] + a8[1]) / 2.0
        j_y = (j0[1] + j8[1]) / 2.0
        span = abs(a_y - j_y)
        if span < 40:
            return None, "棋盘高度太小"
        # 同一条边的两个角应当大致等高（横边），否则说明关键点跑偏了
        if abs(a0[1] - a8[1]) > 0.35 * span or abs(j0[1] - j8[1]) > 0.35 * span:
            return None, "关键点不成四边形"
        if img_shape is not None:
            h, w = img_shape[:2]
            pts = kp[:4]
            if (pts[:, 0].min() < -0.05 * w or pts[:, 0].max() > 1.05 * w
                    or pts[:, 1].min() < -0.05 * h or pts[:, 1].max() > 1.05 * h):
                return None, "关键点落在画面外"

        if a_y > j_y:
            return "b", f"黑方底线在画面下方（y {a_y:.0f} > {j_y:.0f}）"
        return "w", f"红方底线在画面下方（y {j_y:.0f} > {a_y:.0f}）"

    @Slot()
    def run(self) -> None:
        self._run = True

        # 初始化分步反馈：模型加载 + 首次预热 + 引擎启动合计可能 20~40 秒
        # （DirectML 首次要编译着色器），期间界面是全黑的，必须给出提示，
        # 否则用户会以为程序卡死而关掉它。
        self.status.emit("① 加载识别模型…（首次约 10~30 秒，请稍候）")
        try:
            self._vision = create_vision(cuda=True)
        except Exception as exc:
            self.error.emit(f"识别模型加载失败: {exc}")
            self.done.emit()
            return

        # 主动预热：第一次推理会编译 DirectML 着色器，耗时明显长于后续。
        # 放在这里做掉，进入主循环后每一帧的耗时才是真实水平。
        self.status.emit("② 预热识别模型…（首次推理较慢，属正常）")
        try:
            import numpy as _np
            dummy = _np.zeros((256, 256, 3), dtype=_np.uint8)
            t0 = time.perf_counter()
            self._vision.infer(dummy)
            self.status.emit(f"   预热完成，用时 {time.perf_counter() - t0:.1f}s")
        except Exception as exc:
            # 预热失败不算致命（可能这张假图触发异常），继续走主流程
            self.status.emit(f"   预热跳过：{str(exc)[:60]}")

        self.status.emit("③ 启动 Pikafish 引擎…")
        try:
            self._engine = create_engine(self.threads, self.hash_mb,
                                         options=self._extra_options())
            # MultiPV 单独下发（create_engine 只处理 Threads/Hash）
            self._multi = 1
            if self.multipv != 1:
                self._engine.set_option("MultiPV", self.multipv)
                self._multi = self.multipv
            # 把引擎声明的可调项发给 UI，用于构建「高级」面板
            self.engine_options.emit(parse_engine_options(self._engine.option_lines))
        except Exception as exc:
            self.error.emit(f"引擎启动失败: {exc}")
            self.done.emit()
            return

        from app_backend import BoardTracker
        # BoardTracker 同时负责「演化校验」和「轮次管理」
        self._tracker = BoardTracker(force_after=6, first_side=self.first_side)
        self._log("初始化完成，进入识别循环")

        self.status.emit("就绪，开始识别…")
        _last_state_note = ""
        while self._run:
            t0 = time.perf_counter()
            # 每轮循环顶部先消费引擎输出：引擎在独立进程里持续加深，
            # 这里只是非阻塞取结果并流式刷新 PV（放在顶部是因为下面有大量
            # continue 分支会跳过循环末尾）。
            self._process_commands()
            self._pump_engine()
            # 自动走棋：拿到确定 bestmove 且轮到我方时点击落子。
            # 放在 _pump_engine 之后、其余 continue 分支之前，保证每轮都评估一次。
            self._pump_auto_move()

            # 先查窗口状态：最小化时截图拿不到棋盘，必须明确告知用户，
            # 否则只会表现为"局面不合法"，让人摸不着头脑。
            if self.source.hwnd:
                win_ok, minimized, note = window_state(self.source.hwnd)
                if not win_ok:
                    if note != _last_state_note:
                        self.status.emit(note)
                        self._log(f"窗口状态异常: {note}")
                        _last_state_note = note
                    time.sleep(1.0)
                    continue
                _last_state_note = ""

            bgr = self.source.grab()
            if bgr is None:
                self.status.emit("抓帧失败（窗口已关闭？）")
                self._log("抓帧失败")
                time.sleep(0.5)
                continue

            try:
                res = self._vision.infer(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
            except Exception as exc:
                self.status.emit(f"识别异常: {exc}")
                self._log(f"识别异常: {exc}")
                time.sleep(0.3)
                continue

            # 朝向：识别模型的关键点是「黑方两角 A0/A8、红方两角 J0/J8」
            # （见 xq_vision.BONE_NAMES），拉正流程本身就会把黑方底线摆到
            # row 0 —— 无论哪一方在画面下方，模型输出都已经是标准朝向。
            # 所以这里**不能**按「我方执子」再翻一次：那会把已经摆正的矩阵
            # 翻反，黑将跑到 row 9，被 check_position 判「黑将在九宫外」，
            # 每帧都挡下（表现为状态栏一直刷「局面变化无法解释，已挡下」）。
            # 只做兜底自检：万一模型真把黑将摆到了下方，才翻转。
            rows = res["rows"]
            if (any("k" in rows[i] for i in (7, 8, 9))
                    and not any("k" in rows[i] for i in (0, 1, 2))):
                rows = self.flip_rows(rows)
                self._log("朝向自检：黑将在下方，已翻转 180°（模型输出异常）")
            key = "".join(rows)

            # pose 几何校验：画面里没有棋盘时，模型会输出退化的四边形
            # （例如被 JJ象棋 主界面的 3D 棋盘摆件误导），这时直接跳过，
            # 否则会拿一个扭曲的图去分类，产出完全错误的局面。
            geo_ok, geo_why = check_board_geometry(
                res["keypoints"], img_shape=bgr.shape)
            if not geo_ok:
                self.status.emit(f"未定位到棋盘：{geo_why}（请确认对局界面已打开）")
                self._log(f"几何校验失败: {geo_why}")
                time.sleep(0.5)
                continue

            # ---- 动画/不稳定期抑制（天天象棋「吃」字动画等）----
            # 拉正图是固定尺寸的，帧间像素差异能直接反映「盘面正在变化」。
            # 变化期间一律不进入演化校验，直到画面静止达到 settle_ms 才恢复，
            # 这就是需求里的「等动画消失后再识别盘面变化，有变化了再移动棋子」。
            #
            # ★ 必须有「最长抑制时长」兜底：若盘面区域存在**持续**动画/闪烁
            #   （将军提示、倒计时环、平台自己的特效），ratio 会一直超阈值，
            #   没有兜底就永远 continue、**永不识别**。超时后强制放行一帧，
            #   放行进来的错帧由 BoardTracker 的演化校验挡下，代价可控。
            if self.anim_suppress:
                moving, ratio = self._frame_moving(res["warped"])
                now_m = time.perf_counter()
                if moving:
                    self._still_since = 0.0
                    if self._settle_start == 0.0:
                        self._settle_start = now_m
                    if not self._settling:
                        self._settling = True
                        self._log(f"动画抑制：画面变化 {ratio * 100:.1f}%，暂停识别")
                        try:
                            self.settle_state.emit(
                                {"suppress": True, "settling": True, "ratio": ratio})
                        except Exception:
                            pass
                    if not suppress_timed_out(self._settle_start, now_m,
                                              self.anim_max_ms):
                        self.status.emit(
                            f"画面变化中（{ratio * 100:.1f}%），等待稳定后再识别…")
                        time.sleep(0.05)
                        continue
                    # 超时兜底：强制放行一帧，并把抑制计时重新起算
                    self._settle_start = now_m
                    self._log(f"动画抑制超时（>{self.anim_max_ms:.0f}ms），强制放行一帧")
                else:
                    self._settle_start = 0.0
                    if self._still_since == 0.0:
                        self._still_since = now_m
                    if not settle_ready(self._still_since, now_m, self.settle_ms):
                        time.sleep(0.05)      # 已静止但还没到静置时长
                        continue
                    if self._settling:
                        self._settling = False
                        self._log("画面已稳定，恢复识别")
                        try:
                            self.settle_state.emit(
                                {"suppress": True, "settling": False, "ratio": ratio})
                        except Exception:
                            pass

            # ---- 「新局」自动判断我方执子 ----
            # 放在动画抑制之后：拿到的是一帧**稳定**画面，关键点更可信。
            # 开局红方还没动子 / 对局还在匹配中时也能判（只看棋盘朝向）。
            if self.auto_side_pending:
                _side, _why = self._detect_my_side(res["keypoints"], bgr.shape)
                if _side:
                    self.auto_side_pending = False
                    self._side_note = ""
                    self.my_side = _side
                    self._log(f"自动判断我方执子：{_side}（{_why}）")
                    try:
                        self.side_detected.emit({"side": _side, "why": _why})
                    except Exception:
                        pass
                elif _why != self._side_note:
                    self._side_note = _why
                    self.status.emit(
                        f"自动判断我方执子：暂时判不了（{_why}），棋盘出现后会自动重试…")

            # 连续 N 帧一致才进入演化校验（先滤掉动画/过渡帧）
            if key == self._last_key:
                self._same += 1
            else:
                self._last_key, self._same = key, 1
            if self._same < self.confirm or key == self._published:
                time.sleep(0.10)
                continue

            # 矩阵已是标准朝向（row 0 = 黑方底线），直接生成 FEN
            fen_base = self.rows_to_fen(rows, "w")

            # 演化校验 + 轮次推进（BoardTracker 内部处理）
            accept, reason, move, side = self._tracker.update(fen_base)
            if not accept:
                if reason == "drop":
                    self.status.emit(
                        f"局面变化无法解释，已挡下（{self._tracker.stats}）"
                        f" · minConf={res['conf'].min():.2f}")
                    self._log(f"演化校验挡下: {move}  minConf={res['conf'].min():.2f}")
                time.sleep(0.10)
                continue

            move_note = ""
            if reason == "走子" and move:
                side_cn = "红" if side == "w" else "黑"
                move_note = f"检测到 {move} → {side_cn}方走"
            elif reason == "强制采信":
                move_note = "局面跳变且持续存在，已强制采信（识别可能有误）"
            elif reason == "稳定新局面":
                move_note = "已接手中盘局面"

            fen = fen_with_side(fen_base, side)
            # 送引擎前做严格校验：Pikafish 遇到非法局面会直接退出进程，
            # 一旦崩掉后续所有写入都报 OSError，所以这里必须拦住。
            pos_ok, pos_why = check_position(fen)
            if not pos_ok:
                self.status.emit(f"局面不合法已跳过：{pos_why}")
                self._log(f"局面校验失败: {pos_why}  FEN={fen}")
                time.sleep(0.25)
                continue
            ok, err, n_moves = validate_fen(fen)
            if not ok:
                self.status.emit(f"棋规校验未通过：{err[:60]}")
                time.sleep(0.25)
                continue

            self._published = key
            t_recog = (time.perf_counter() - t0) * 1000
            self._log(f"采纳局面({reason}) {fen}  识别 {t_recog:.0f}ms")
            # 暂存本帧识别上下文；diag 由 _build_diag 从引擎 PV 快照组装
            self._cur_fen_base, self._cur_side = fen_base, side
            self._cur_warp_mat = res["warp_mat"]
            self._cur_rows = rows
            self._cur_ctx = {
                "t_recog": t_recog,
                "conf": float(res["conf"].min()),
                "moves": n_moves,
                "side": side,
                "note": move_note,
                # 仅表示「左栏显示图是否转 180° 让画面朝向与我方一致」，
                # 与矩阵/FEN 无关（矩阵永远保持标准朝向）
                "flipped": self.my_side == "b",
            }
            # 开局库：按当前局面查库，命中就把库着挂到 diag 上（并供自动走棋取用）
            self._refresh_book_move(fen)
            self.status.emit(move_note or "引擎分析中…")
            try:
                # 非阻塞发起搜索：立即返回，结果由 _pump_engine 流式补齐
                self._start_search(fen)
            except Exception as exc:
                # 引擎可能已经崩溃退出（非法局面 / 资源问题），重建它，
                # 否则后续每一帧都会在同一个死进程上重复失败。
                # 注意这里**不能 continue**：key 已写入 _published，本帧若跳过就
                # 不会再被采纳，棋盘图会一直停在上一局面上。继续往下发 frame_ready。
                self.error.emit(f"引擎异常，正在重启: {exc}")
                self._log(f"引擎异常: {exc}")
                self._rebuild_engine()

            diag = self._build_diag()
            warped = cv2.cvtColor(res["warped"], cv2.COLOR_RGB2BGR)
            if self.my_side == "b":
                # 只转「拿来显示」的那张图：我方执黑时让盘面在左栏里也是
                # 我方在下，看着顺手。矩阵不动，FEN 仍然正确。
                warped = cv2.rotate(warped, cv2.ROTATE_180)
            self.frame_ready.emit(bgr, warped, fen, diag, key)
            side_cn = "红" if side == "w" else "黑"
            self.status.emit(f"{side_cn}方走 · 深度 {diag['depth'] or '—'} · "
                             f"分数 {diag['score']} · 识别 {t_recog:.0f}ms · "
                             f"{self._tracker.stats}")

        # 在 Worker 线程里关掉开局库连接：sqlite3 连接有线程亲和性，
        # 若留给主线程的 shutdown() 去关，会抛
        # "SQLite objects created in a thread can only be used in that same thread"
        # （被 except 吞掉，表现为连接泄漏）。
        self._close_book()
        self.done.emit()

    def stop(self) -> None:
        self._run = False

    def shutdown(self) -> None:
        self._run = False
        if self._engine is not None:
            # 若正处于 go infinite 搜索中，先 stop 再 quit，避免引擎等待搜索结束
            try:
                if self._searching:
                    self._engine.send("stop")
                    self._searching = False
            except Exception:
                pass
            self._dispose_engine(self._engine)
            self._engine = None
        self._close_book()
        if self._log_fh is not None:
            try:
                self._log("已停止")
                self._log_fh.close()
            except Exception:
                pass
            self._log_fh = None

    # ---------------- 引擎驱动（非阻塞 + 流式 MultiPV） ----------------
    def _extra_options(self) -> dict:
        """组装除 Threads/Hash 外的引擎选项（用于 create_engine / 重建）。"""
        opts = dict(self.engine_opts)
        if self.move_overhead:
            opts["Move Overhead"] = self.move_overhead
        opts["UCI_ShowWDL"] = bool(self.show_wdl)
        return opts

    def _drain_lines(self) -> list[str]:
        """非阻塞取空引擎输出队列（reader 线程在另一端持续灌入）。"""
        out: list[str] = []
        if self._engine is None:
            return out
        q = self._engine.q
        while True:
            try:
                out.append(q.get_nowait())
            except Exception:
                break
        return out

    def _start_search(self, fen: str) -> None:
        """停掉旧搜索，按当前参数对 fen 发起新搜索（不阻塞，立即返回）。"""
        eng = self._engine
        if eng is None:
            return
        if self._searching:
            self._stop_search()
        # MultiPV 跨搜索保持，只在变化时下发
        if self.multipv != self._multi:
            eng.set_option("MultiPV", self.multipv)
            self._multi = self.multipv
        self._pv.clear()
        self._turn_checked = False
        self._search_fen = fen
        eng.drain()
        eng.send(f"position fen {fen}")
        if self.auto_move_enabled:
            # 自动走棋必须等一个确定的 bestmove 才能落子，而 go infinite 永不
            # 返回 bestmove —— 所以开启自动走棋时强制有限搜索（"持续加深"置灰）。
            eng.send(f"go movetime {int(self.auto_think_ms)}")
        elif self.infinite:
            eng.send("go infinite")
        else:
            eng.send(f"go movetime {int(self.movetime)}")
        self._searching = True
        self._last_emit = 0.0

    def _stop_search(self, timeout: float = 0.5) -> str | None:
        """停止当前搜索并取回 bestmove（仅在有搜索时才发 stop）。"""
        eng = self._engine
        if eng is None or not self._searching:
            return None
        best = None
        try:
            eng.send("stop")
            lines = eng.wait_for("bestmove", timeout=timeout)
            for ln in lines:
                if ln.startswith("bestmove"):
                    parts = ln.split()
                    best = parts[1] if len(parts) > 1 else None
        except Exception:
            pass
        self._searching = False
        return best

    @staticmethod
    def _dispose_engine(eng) -> None:
        """关闭引擎进程，并显式关掉 stdin 管道。

        ``UciEngine.quit()`` 之后 stdin 仍是打开状态；对象被 GC 时 Python 会
        尝试向（可能已死的）进程 flush，打印
        ``Exception ignored ... OSError: [Errno 22] Invalid argument``。
        显式关闭即可消除这类噪音。

        **只关 stdin，不能关 stdout** —— reader 线程正阻塞在读它，
        关掉会抛 ValueError 把线程打死。
        """
        if eng is None:
            return
        try:
            eng.quit()
        except Exception:
            pass
        try:
            if eng.proc is not None and eng.proc.stdin is not None:
                eng.proc.stdin.close()
        except Exception:
            pass

    def _rebuild_engine(self) -> None:
        """引擎进程崩溃/退出后重建，恢复参数，并重新分析当前局面。"""
        self._searching = False
        self._pv.clear()
        self._cn_cache.clear()
        self._last_rebuild = time.perf_counter()
        self._dispose_engine(self._engine)
        self._engine = None
        try:
            self._engine = create_engine(self.threads, self.hash_mb,
                                         options=self._extra_options())
            self._multi = 1
            if self.multipv != 1:
                self._engine.set_option("MultiPV", self.multipv)
                self._multi = self.multipv
            self.engine_options.emit(parse_engine_options(self._engine.option_lines))
            self.status.emit("引擎已重启")
        except Exception as exc:
            self.error.emit(f"引擎重启失败: {exc}")
            self._engine = None
            return
        # 重建成功后立刻恢复对当前局面的分析，否则面板会一直空着
        # 直到棋盘下一次变化。
        if self._cur_fen_base:
            try:
                self._start_search(fen_with_side(self._cur_fen_base, self._cur_side))
            except Exception as exc:
                self.error.emit(f"重建后重算失败: {exc}")

    def _pump_engine(self) -> None:
        """每轮循环顶部调用：非阻塞消费引擎输出并流式刷新 PV。"""
        if self._engine is None:
            # 引擎不可用（重建失败）：限频重试，避免每轮都去拉起进程
            now = time.perf_counter()
            if now - self._last_rebuild >= 3.0:
                self._log("引擎不可用，尝试重建")
                self._rebuild_engine()
            return
        try:
            lines = self._drain_lines()
        except Exception:
            lines = []
        # 引擎进程结束（崩溃/退出）→ 重建
        try:
            dead = self._engine.proc.poll() is not None
        except Exception:
            dead = True
        if any(l == "__EOF__" for l in lines) or dead:
            self._log("检测到引擎进程结束，正在重建")
            self._rebuild_engine()
            return
        if not lines:
            return

        best = None
        for ln in lines:
            if ln.startswith("bestmove"):
                parts = ln.split()
                best = parts[1] if len(parts) > 1 else None
                self._searching = False
        # 记录本次 bestmove 及其所属局面，供自动走棋取用（_pump_auto_move）。
        # 必须在下面任何 return 之前记录，否则 movetime 结束的那一轮会被漏掉。
        if best:
            self._last_bestmove = best
            self._last_bestmove_fen = self._search_fen

        infos = [ln for ln in lines if ln.startswith("info")]
        if infos:
            self._pv.feed(infos)
            snap = self._pv.snapshot(self.multipv)
            # 首次拿到引擎着法时做轮次校正。
            # 注：go infinite 下没有 bestmove，用 multipv=1 行的首着等价替代。
            if snap and not self._turn_checked and self._tracker is not None:
                pv0 = snap[0]["pv"][0] if snap[0].get("pv") else None
                if pv0:
                    try:
                        corrected = self._tracker.correct_by_engine_move(pv0)
                    except Exception:
                        corrected = False
                    self._turn_checked = True
                    if corrected:
                        self._cur_side = self._tracker.side_to_move
                        note = f"轮次已校正为{'红' if self._cur_side == 'w' else '黑'}方走"
                        self._cur_ctx["side"] = self._cur_side
                        self._cur_ctx["note"] = note
                        self._log(note)
                        self._start_search(
                            fen_with_side(self._cur_fen_base, self._cur_side))
                        return
            now = time.perf_counter()
            if now - self._last_emit >= 0.05:      # 20Hz 节流
                self._last_emit = now
                self.analysis_update.emit(self._search_fen, self._build_diag(best))
        elif best is not None and self._cur_ctx:
            # 没有新 info 但拿到了 bestmove（movetime 结束），刷新一次终值
            self.analysis_update.emit(self._search_fen, self._build_diag(best))

    def _build_diag(self, bestmove: str | None = None) -> dict:
        """由当前 PV 快照 + 识别上下文组装给 UI 的 diag。"""
        ctx = self._cur_ctx or {}
        fen = self._search_fen
        my_side = self.my_side
        snap = self._pv.snapshot(self.multipv)
        # 引擎 wdl 是行棋方视角，先取出行棋方再换算成红方视角
        _fparts = fen.split()
        stm = _fparts[1] if len(_fparts) > 1 else "w"

        lines: list[dict] = []
        for info in snap:
            pv = info.get("pv") or []
            wdl = wdl_to_red(info.get("wdl"), stm)
            cp = info.get("score_cp")
            # 中文回放较贵（每次 ~1.4ms），按 (fen, pv) 缓存：
            # 流式刷新时同一条主变例常常连续多轮不变，可直接命中。
            ck = (fen, tuple(pv))
            cn = self._cn_cache.get(ck)
            if cn is None:
                try:
                    cn = pv_to_chinese(fen, pv) if pv else []
                except Exception:
                    cn = []
                if len(self._cn_cache) > 128:
                    self._cn_cache.clear()
                self._cn_cache[ck] = cn
            lines.append({
                "multipv": info.get("multipv", 1),
                "depth": info.get("depth"),
                "seldepth": info.get("seldepth"),
                "score_cp": cp,
                "score": format_score(cp),
                "wdl": wdl,
                "winrate": short_winrate(wdl, cp, my_side),
                "pv": pv,
                "pv_iccs": " ".join(pv),
                "chinese_seq": " ".join(cn),
                "nodes": info.get("nodes"),
                "nps": info.get("nps"),
                "hashfull": info.get("hashfull"),
                "time_ms": info.get("time_ms"),
            })

        top = lines[0] if lines else None
        best = bestmove or (top["pv"][0] if top and top.get("pv") else "")
        wdl = top["wdl"] if top else None
        cp = top["score_cp"] if top else None

        # 开局库：命中且为「库着优先」时，用库着覆盖建议与箭头
        book = self._book_move
        book_used = bool(book and self.book_playable())
        if book_used:
            best = book["move"]

        # alts：BoardView 需要每条线首着的箭头坐标
        alts: list[dict] = []
        for ln in lines:
            first = ln["pv"][0] if ln.get("pv") else ""
            if not first:
                continue
            alts.append({
                "iccs": first,
                "chinese": ln["chinese_seq"].split(" ")[0] if ln["chinese_seq"] else "",
                "score": ln["score"],
                "depth": ln["depth"],
                "arrow": (move_arrow(self._cur_warp_mat, first)
                          if self._cur_warp_mat is not None else None),
            })

        return {
            "score": format_score(cp),
            "depth": top["depth"] if top else None,
            "seldepth": top["seldepth"] if top else None,
            "best": best,
            "chinese": move_to_chinese(fen, best) if best else "",
            "t_recog": ctx.get("t_recog", 0.0),
            "conf": ctx.get("conf", 0.0),
            "moves": ctx.get("moves", 0),
            "side": ctx.get("side", self._cur_side),
            "note": ctx.get("note", ""),
            "flipped": ctx.get("flipped", self.my_side == "b"),
            "winrate": format_winrate(wdl, cp, my_side) if top else "分析中…",
            "wdl": wdl,
            "arrow": (move_arrow(self._cur_warp_mat, best)
                      if (best and self._cur_warp_mat is not None) else None),
            "alts": alts,
            "pv": lines,
            "nodes": top["nodes"] if top else None,
            "nps": top["nps"] if top else None,
            "hashfull": top["hashfull"] if top else None,
            "time_ms": top["time_ms"] if top else None,
            "engine_mode": "持续加深" if self.infinite else "限时",
            "guard": self._tracker.stats if self._tracker is not None else "",
            # 开局库
            "book": book,
            "book_used": book_used,
            "book_mode": self.book_mode,
            "book_enabled": bool(self.book_enabled and self._book is not None),
            "settling": self._settling,
        }

    # ---- 自动走棋（模拟鼠标点击落子）----
    def _from_is_my_piece(self, iccs: str) -> bool:
        """起点格必须是我方棋子 —— 防止轮次误判时动到对方的子。"""
        try:
            if not self._cur_rows or len(iccs) < 2:
                return False
            col = ord(iccs[0].lower()) - ord("a")
            row = 9 - int(iccs[1])
            if not (0 <= row <= 9 and 0 <= col <= 8):
                return False
            p = self._cur_rows[row][col]
            if p in (".", "x"):
                return False
            return p.isupper() if self.my_side == "w" else p.islower()
        except Exception:
            return False

    def _disable_auto_move(self, reason: str = "") -> None:
        """关闭自动走棋并通知 UI（不终止识别线程）。"""
        self.auto_move_enabled = False
        self._auto_pending = None
        if reason:
            self._log(f"自动走棋已停止: {reason}")
        try:
            self.auto_state_changed.emit(False)
        except Exception:
            pass

    def _turn_guard_blocks(self) -> bool:
        """轮次**未经验证**时是否该拦住自动走棋。

        若 tracker 认为是**黑方**走、又还没观察到任何走子、且盘面还是**初始
        局面** —— 这个轮次几乎必然是猜错的（象棋红先，新局第一手一定是红方）。
        此时落子会走出不该走的着法，所以直接不点。

        为什么只挡「初始局面」：真·黑方先走的局面不存在；而中盘接手时盘面
        本来就不是初始局面，不会误伤（那种情况轮次确实可能是我方）。
        """
        return (self._tracker is not None
                and not self._tracker.moves
                and self._cur_side == "b"
                and self._cur_fen_base == START_BOARD)

    def _pump_auto_move(self) -> None:
        """每轮循环调用：轮到我方且拿到确定 bestmove 时，模拟鼠标点击落子。

        所有闸门不满足就静默返回；只有全部通过才会真正点击。
        """
        if not self.auto_move_enabled:
            return
        now = time.perf_counter()

        # ---- 1) 上一次点击的生效判定 / 超时重试 ----
        if self._auto_pending is not None:
            pfen, pbest, _t = self._auto_pending
            if self._cur_fen_base and self._cur_fen_base != pfen:
                # 棋盘已更新 → 上次点击生效
                self._auto_pending = None
                self._auto_retries = 0
                self._auto_moves_made += 1
                self._log(f"自动走棋已生效（累计 {self._auto_moves_made} 手）")
                return
            if now < self._auto_next_click_t:
                return
            if self._auto_retries >= self.auto_retry_max:
                self.status.emit("自动走棋未生效，已暂停（请检查坐标/遮挡）")
                self._disable_auto_move("多次点击未生效")
                return
            self._auto_retries += 1
            self._auto_next_click_t = now + self.auto_place_wait
            self._log(f"自动走棋重试第 {self._auto_retries} 次")
            self._do_auto_click(pfen, pbest)
            return

        # ---- 2) 触发闸门 ----
        # 画面正在变化（动画/落子中）→ 一律不点，等识别稳定了再说。
        # 这正是需求里的「有变化了再移动棋子」：只在稳定盘面上动作。
        if self.anim_suppress and self._settling:
            return
        best = self._last_bestmove
        fen = self._last_bestmove_fen
        fen_cur = (fen_with_side(self._cur_fen_base, self._cur_side)
                   if self._cur_fen_base else "")
        # 开局库优先：库着不依赖引擎搜索结果，局面一稳定就能走，无需等搜索。
        book_best = ""
        if (self.book_playable() and self._book_move and fen_cur
                and self._cur_side == self.my_side):
            cand = str(self._book_move.get("move") or "")
            # ★ 复核一遍：库着是按「查库那一刻」的 fen 过滤的，之后轮次可能被
            #   引擎校正（_cur_side 翻转）→ 同一个库着在新轮次下可能非法。
            if cand and is_strict_legal_move(fen_cur, cand):
                book_best = cand
            elif cand:
                self._log(f"开局库着法在最新局面下已非法，丢弃: {cand}")
        if book_best:
            best, fen = book_best, fen_cur
        else:
            if not best or best in ("(none)", "0000"):
                return
            if self._searching:
                return                              # 搜索未结束，bestmove 未定
            if not fen or not fen_cur:
                return
            if fen != fen_cur:
                return                              # bestmove 属于旧局面
        if self._cur_side != self.my_side:
            return                                  # ★ 只走我方
        if self._turn_guard_blocks():
            self._log("轮次闸门：盘面还是开局却认为轮到黑方，暂不落子")
            return
        if (fen, best) == self._auto_done:
            return                                  # 同局面同着法只点一次
        if now - self._auto_last_click_t < self.auto_cooldown:
            return
        if self._auto_moves_made >= self.auto_max_moves:
            self.status.emit("自动走棋已达最大连续走子数，已停止")
            self._disable_auto_move("达到最大连续走子数")
            return
        if not self._from_is_my_piece(best):
            return
        if book_best:
            self._log(f"开局库命中，走库着 {best}")
        if self.source.hwnd:
            ok, minimized, _ = window_state(self.source.hwnd)
            if not ok or minimized:
                return

        self._do_auto_click(fen, best)

    def _do_auto_click(self, fen: str, best: str) -> None:
        """把 bestmove 换算成屏幕坐标并执行点击（或预览）。"""
        hwnd = self.source.hwnd
        if not hwnd:
            return
        cap = self.source.rect
        if cap is None:
            return
        # 陈旧坐标防护：窗口在抓帧后被移动/缩放 → warp_mat 失效，放弃本次
        try:
            import win32gui
            cur = win32gui.GetWindowRect(hwnd)
        except Exception:
            return
        if (cur[2] - cur[0], cur[3] - cur[1]) != (cap[2] - cap[0], cap[3] - cap[1]):
            self._log("窗口尺寸已变化，放弃本次自动走棋")
            return
        if abs(cur[0] - cap[0]) > 4 or abs(cur[1] - cap[1]) > 4:
            self._log("窗口已移动，放弃本次自动走棋")
            return

        pts = move_screen_points(cap, self._cur_warp_mat, best)
        if not pts:
            self._log(f"自动走棋坐标换算失败: {best}")
            return
        chinese = move_to_chinese(fen, best)
        self._auto_preview = {"best": best, "chinese": chinese,
                              "from": pts["from"], "to": pts["to"],
                              "dry": bool(self.auto_dry_run)}

        if self.auto_dry_run:
            self._auto_done = (fen, best)
            self._auto_last_click_t = time.perf_counter()
            try:
                self.auto_preview.emit(self._auto_preview)
            except Exception:
                pass
            self.status.emit(f"[预览] 自动走棋将点击 {chinese}（{best}）"
                             f" {pts['from']} → {pts['to']}")
            return

        if self._clicker is None:
            self._clicker = MouseClicker(mode=self.auto_click_mode,
                                         move_steps=self.auto_move_steps,
                                         restore_cursor=self.auto_restore_cursor)
        # 硬闸门：目标窗口必须在前台，且两个落点都在该窗口上
        if not ensure_foreground(hwnd):
            self.status.emit("无法将目标窗口置前，跳过本次自动走棋")
            self._log("ensure_foreground 失败，跳过")
            return
        if not (is_point_on_window(hwnd, *pts["from"])
                and is_point_on_window(hwnd, *pts["to"])):
            self.status.emit("点击点被其他窗口遮挡，跳过本次自动走棋")
            self._log("点击点被遮挡，跳过")
            return

        try:
            self._clicker.place_move(pts["from"], pts["to"])
        except Exception as exc:
            self.error.emit(f"自动点击失败: {exc}")
            return

        self._auto_done = (fen, best)
        self._auto_last_click_t = time.perf_counter()
        self._auto_pending = (fen, best, self._auto_last_click_t)
        self._auto_next_click_t = self._auto_last_click_t + self.auto_place_wait
        try:
            self.auto_preview.emit(self._auto_preview)
        except Exception:
            pass
        self._log(f"自动落子 {chinese} {best} @ {pts['from']}→{pts['to']}")
        self.status.emit(f"自动落子：{chinese}（{best}）")

    def apply_auto_params(self, p: dict) -> None:
        """应用自动走棋参数（Worker 线程内调用）。"""
        prev = self.auto_move_enabled
        self.auto_move_enabled = bool(p.get("enabled", self.auto_move_enabled))
        self.auto_think_ms = max(100, int(p.get("think_ms", self.auto_think_ms)))
        self.auto_dry_run = bool(p.get("dry_run", self.auto_dry_run))
        self.auto_click_mode = p.get("click_mode", self.auto_click_mode)
        self.auto_cooldown = float(p.get("cooldown", self.auto_cooldown))
        self.auto_max_moves = max(1, int(p.get("max_moves", self.auto_max_moves)))
        self.auto_restore_cursor = bool(
            p.get("restore_cursor", self.auto_restore_cursor))
        self.auto_move_steps = max(1, int(p.get("move_steps", self.auto_move_steps)))
        if "place_wait" in p:
            self.auto_place_wait = max(0.3, float(p["place_wait"]))

        if self.auto_move_enabled and self._clicker is None:
            self._clicker = MouseClicker(mode=self.auto_click_mode,
                                         move_steps=self.auto_move_steps,
                                         restore_cursor=self.auto_restore_cursor)
        elif self._clicker is not None:
            self._clicker.mode = self.auto_click_mode
            self._clicker.move_steps = self.auto_move_steps
            self._clicker.restore_cursor = self.auto_restore_cursor

        if self.auto_move_enabled and not prev:
            # 刚开启：清掉上一局的计数/状态
            self._auto_moves_made = 0
            self._auto_done = ("", "")
            self._auto_pending = None
            self._auto_retries = 0
            self.status.emit("自动走棋已启用" +
                             ("（预览模式，不真点）" if self.auto_dry_run else ""))
            # 当前若在分析中，用有限搜索重开一次，确保拿得到 bestmove
            if self._cur_fen_base:
                try:
                    self._start_search(
                        fen_with_side(self._cur_fen_base, self._cur_side))
                except Exception:
                    pass
        if not self.auto_move_enabled:
            self._auto_pending = None
        try:
            self.auto_state_changed.emit(self.auto_move_enabled)
        except Exception:
            pass

    # ---- 开局库 ----
    def book_playable(self) -> bool:
        """开局库是否参与「库着优先」（自动走棋取库着）。"""
        return (self.book_enabled and self._book is not None
                and self.book_mode == "play")

    def _book_legal(self, fen: str, mv: str) -> bool:
        """按**当前轮次**校验库着是否合法。

        xqb 的 key 只含棋盘布局、不含轮次，同一个布局在红/黑轮次下会命中
        同一批着法。不过滤就可能拿对方的着法去点鼠标，所以必须校验。

        用**严格**校验（含自将/照面），不是 ``is_legal_move`` 那种伪合法 ——
        库里的着法若会让自己的将暴露，点出去就是一歩非法棋。
        """
        return is_strict_legal_move(fen, mv)

    def _refresh_book_move(self, fen: str) -> None:
        """按当前局面查一次库，结果存到 ``self._book_move``。"""
        self._book_move = None
        if not self.book_enabled or self._book is None or not fen:
            return
        if self.book_max_ply and self._tracker is not None \
                and len(self._tracker.moves) >= self.book_max_ply:
            return                                  # 已超出开局库深度
        try:
            hit = self._book.pick(fen, "best", legal_check=self._book_legal)
        except Exception as exc:
            self._log(f"开局库查询失败: {exc}")
            return
        if not hit:
            return
        self._book_move = {
            "move": hit["move"],
            "weight": int(hit.get("weight", 1)),
            "chinese": move_to_chinese(fen, hit["move"]),
            "ply": len(self._tracker.moves) if self._tracker else 0,
        }

    def apply_book_params(self, p: dict) -> None:
        """应用开局库参数（Worker 线程内调用）。

        ★ 必须在 Worker 线程里打开库：sqlite3 连接有线程亲和性，
        在主线程建好再拿到这里用会抛 "SQLite objects created in a thread
        can only be used in that same thread"。
        """
        self.book_enabled = bool(p.get("enabled", self.book_enabled))
        self.book_mode = str(p.get("mode", self.book_mode))
        if self.book_mode not in ("hint", "play"):
            self.book_mode = "hint"
        try:
            self.book_max_ply = max(0, int(p.get("max_ply", self.book_max_ply)))
        except Exception:
            pass

        new_path = str(p.get("path", self.book_path) or "")
        if new_path != self.book_path or (self._book is None and new_path):
            self.book_path = new_path
            self._close_book()
            self._book_note = ""
            if new_path:
                try:
                    self._book = OpeningBook(new_path)
                    self._book_note = self._book.describe()
                    self._log(f"开局库已加载: {self._book_note}")
                except Exception as exc:
                    self._book = None
                    self._book_note = f"加载失败: {exc}"
                    self._log(f"开局库加载失败: {exc}")

        # 开关或库变化后立刻按当前局面重查一次，UI 能马上看到效果
        if self._cur_fen_base:
            self._refresh_book_move(
                fen_with_side(self._cur_fen_base, self._cur_side))
        try:
            self.book_state.emit({
                "enabled": self.book_enabled, "path": self.book_path,
                "kind": self._book.kind if self._book else "",
                "ok": self._book is not None, "note": self._book_note,
                "mode": self.book_mode, "max_ply": self.book_max_ply,
                "hit": self._book_move,
            })
        except Exception:
            pass

    def _close_book(self) -> None:
        if self._book is not None:
            try:
                self._book.close()
            except Exception:
                pass
        self._book = None
        self._book_move = None

    # ---- 自动接盘 ----
    def post_params(self, params: dict) -> None:
        """提交引擎参数变更（主线程调用）。"""
        self._cmds.put(("params", params))

    def post_clear_hash(self) -> None:
        """请求清空置换表（主线程调用）。"""
        self._cmds.put(("clear", None))

    def post_auto(self, params: dict) -> None:
        """提交自动走棋参数变更（主线程调用）。"""
        self._cmds.put(("auto", params))

    def post_book(self, params: dict) -> None:
        """提交开局库参数变更（主线程调用）。"""
        self._cmds.put(("book", params))

    def post_settle(self, params: dict) -> None:
        """提交识别稳定（动画抑制）参数变更（主线程调用）。"""
        self._cmds.put(("settle", params))

    def post_my_side(self, side: str) -> None:
        """提交「我方执子」变更（主线程调用）。"""
        self._cmds.put(("myside", side))

    def _process_commands(self) -> None:
        """在 Worker 线程内消费命令队列（循环顶部调用）。

        会**合并**积压的命令：连续改多个参数只应用最后一次，避免拖拽
        SpinBox 时反复「停搜索→下发→重启」把引擎抖崩。
        """
        last_params = None
        last_auto = None
        last_book = None
        last_settle = None
        do_clear = False
        last_myside = None
        drained = False
        while True:
            try:
                kind, payload = self._cmds.get_nowait()
            except queue.Empty:
                break
            drained = True
            if kind == "params":
                last_params = payload
            elif kind == "auto":
                last_auto = payload
            elif kind == "book":
                last_book = payload
            elif kind == "settle":
                last_settle = payload
            elif kind == "myside":
                last_myside = payload
            elif kind == "clear":
                do_clear = True
        if not drained:
            return
        if do_clear:
            try:
                self.clear_hash_requested()
            except Exception as exc:
                self.error.emit(f"清空置换表失败: {exc}")
        if last_myside in ("w", "b"):
            self.my_side = last_myside
            self._log(f"我方执子改为：{last_myside}")
        if last_settle is not None:
            try:
                self.apply_settle_params(last_settle)
            except Exception as exc:
                self.error.emit(f"识别稳定参数失败: {exc}")
        if last_book is not None:
            try:
                self.apply_book_params(last_book)
            except Exception as exc:
                self.error.emit(f"开局库参数失败: {exc}")
        if last_params is not None:
            try:
                self.apply_engine_params(last_params)
            except Exception as exc:
                self.error.emit(f"参数应用失败: {exc}")
        if last_auto is not None:
            try:
                self.apply_auto_params(last_auto)
            except Exception as exc:
                self.error.emit(f"自动走棋参数失败: {exc}")

    def apply_settle_params(self, p: dict) -> None:
        """应用识别稳定（动画抑制）参数（Worker 线程内调用）。"""
        self.anim_suppress = bool(p.get("suppress", self.anim_suppress))
        try:
            self.settle_ms = max(0, int(p.get("settle_ms", self.settle_ms)))
        except Exception:
            pass
        try:
            self.motion_thresh = min(0.5, max(0.0,
                                              float(p.get("thresh",
                                                          self.motion_thresh))))
        except Exception:
            pass
        if not self.anim_suppress:
            self._settling = False
            self._still_since = 0.0
            self._settle_start = 0.0
        # ★ 关闭期间 _prev_gray 不再更新，重新开启时它已陈旧 —— 不清掉的话
        #   第一帧会拿「很久以前的画面」做比较，必然误判成「在动」。
        self._prev_gray = None
        try:
            self.settle_state.emit({"suppress": self.anim_suppress,
                                    "settling": False,
                                    "ratio": 0.0,
                                    "settle_ms": self.settle_ms,
                                    "thresh": self.motion_thresh})
        except Exception:
            pass

    def apply_engine_params(self, params: dict) -> None:
        """运行中热改引擎参数：停当前搜索 → 下发 → 用新参数重跑当前局面。"""
        eng = self._engine
        if eng is None:
            return
        try:
            self.threads = int(params.get("threads", self.threads))
            self.hash_mb = int(params.get("hash_mb", self.hash_mb))
            self.multipv = max(1, int(params.get("multipv", self.multipv)))
            self.move_overhead = int(params.get("move_overhead", self.move_overhead))
            self.show_wdl = bool(params.get("show_wdl", self.show_wdl))
            self.infinite = bool(params.get("infinite", self.infinite))
            if "advanced" in params:
                self.engine_opts = dict(params.get("advanced") or {})
        except Exception:
            pass

        if self._searching:
            self._stop_search()
        try:
            eng.set_option("Threads", self.threads)
            eng.set_option("Hash", self.hash_mb)
            try:
                eng.set_option("UCI_ShowWDL", self.show_wdl)
            except Exception:
                pass
            if self.move_overhead:
                eng.set_option("Move Overhead", self.move_overhead)
            apply_engine_options(eng, self.engine_opts)
            self._multi = 1
            if self.multipv != 1:
                eng.set_option("MultiPV", self.multipv)
                self._multi = self.multipv
        except Exception as exc:
            self.error.emit(f"参数下发失败: {exc}")
            return

        if self._cur_fen_base:
            try:
                self._start_search(fen_with_side(self._cur_fen_base, self._cur_side))
            except Exception as exc:
                self.error.emit(f"重算失败: {exc}")
        self.status.emit("引擎参数已应用")

    def clear_hash_requested(self) -> None:
        if self._engine is not None:
            clear_hash(self._engine)
            self.status.emit("已清空置换表")


# ---------------------------------------------------------------------------
# 棋盘显示
# ---------------------------------------------------------------------------
class BoardView(QLabel):
    """显示识别模块输出的透视拉正棋盘，并叠加走法箭头与不确定格标记。

    棋盘图像直接取自识别管线（已过角点检测 + 透视变换），因此显示的
    画面与识别所依据的图像完全一致，便于排查识别问题。

    箭头坐标由 ``app_backend.move_arrow`` 依据着法 ICCS 反算，使用与
    透视目标同尺寸的坐标系，因此可以直接画在这张拉正图上。
    """

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumSize(360, 400)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet("background:#1b1f26; color:#7c8698;")
        self.setText("等待识别…\n\n选择目标窗口后点击「开始」")
        self._warped: np.ndarray | None = None
        self._rows: list[str] | None = None
        self._arrow: dict | None = None       # 最佳着法箭头
        self._alts: list[dict] = []           # 候选着法（含 arrow）
        self._flipped = False                 # 我方执黑：坐标需 180° 映射
        self._qimg: QImage | None = None      # 缓存的棋盘 QImage，避免每帧重复转换

    def update_board(self, warped: np.ndarray, rows: list[str],
                     arrow: dict | None = None, alts: list[dict] | None = None,
                     flipped: bool = False) -> None:
        self._warped = warped
        self._rows = rows
        self._arrow = arrow
        self._alts = alts or []
        self._flipped = flipped
        # 预先转好 QImage（cv2→RGB + copy），paintEvent 直接复用；
        # 这样引擎持续加深时的高频 set_arrows 不会再重复转换图像。
        try:
            h, w = warped.shape[:2]
            rgb = cv2.cvtColor(warped, cv2.COLOR_BGR2RGB)
            self._qimg = QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888).copy()
        except Exception:
            self._qimg = None
        self.update()

    def set_arrows(self, arrow: dict | None,
                   alts: list[dict] | None = None) -> None:
        """只更新走法箭头（引擎流式刷新时用），复用已缓存的棋盘图。"""
        self._arrow = arrow
        self._alts = alts or []
        self.update()

    def _map(self, x: float, y: float, w: int, h: int) -> tuple[float, float]:
        """把我方执黑时的坐标做 180° 映射，使其与显示的画面一致。

        识别出的矩阵在执黑时被翻转了 180°（得到标准 FEN），但画面上棋子的
        实际位置没变，所以绘制用的坐标要跟着翻转。
        """
        if not self._flipped:
            return x, y
        return float(w) - float(x), float(h) - float(y)

    @staticmethod
    def _draw_arrow(p: QPainter, x1: float, y1: float, x2: float, y2: float,
                    scale: float, ox: int, oy: int,
                    color: QColor, width: int, head: float) -> None:
        """在 (ox,oy) 为原点、scale 缩放的坐标系里画一支箭头。"""
        import math

        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QPolygonF

        ax, ay = ox + x1 * scale, oy + y1 * scale
        bx, by = ox + x2 * scale, oy + y2 * scale
        dx, dy = bx - ax, by - ay
        dist = math.hypot(dx, dy)
        if dist < 1e-6:
            return
        ux, uy = dx / dist, dy / dist

        # 缩短两端，避免箭头压住棋子本身
        margin = head * 0.85
        sx, sy = ax + ux * margin, ay + uy * margin
        ex, ey = bx - ux * margin, by - uy * margin

        pen = QPen(color, width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        p.setPen(pen)
        p.drawLine(QPointF(sx, sy), QPointF(ex, ey))

        # 箭头三角
        tipx, tipy = bx - ux * head * 0.35, by - uy * head * 0.35
        px, py = -uy, ux
        wing = head * 0.52
        poly = QPolygonF([
            QPointF(tipx, tipy),
            QPointF(ex - px * wing, ey - py * wing),
            QPointF(ex, ey),
            QPointF(ex + px * wing, ey + py * wing),
        ])
        p.setBrush(color)
        p.setPen(Qt.NoPen)
        p.drawPolygon(poly)

    def paintEvent(self, ev) -> None:
        super().paintEvent(ev)
        if self._warped is None:
            return

        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.SmoothPixmapTransform)
            p.setRenderHint(QPainter.Antialiasing)
            h, w = self._warped.shape[:2]
            aw, ah = self.width(), self.height()
            scale = min(aw / w, ah / h)
            dw, dh = int(w * scale), int(h * scale)
            ox, oy = (aw - dw) // 2, (ah - dh) // 2

            if self._qimg is not None:
                # PySide6 的 drawImage 没有 (x, y, w, h, img) 重载，必须传 QRect
                p.drawImage(QRect(ox, oy, dw, dh), self._qimg)

            # ---- 走法箭头 ----
            head = max(9.0, 17.0 * scale)
            base_w = max(3, int(5 * scale))

            # 候选着法用淡色细箭头（先画，让最佳着法压在上面）
            for i, a in enumerate(self._alts):
                ar = a.get("arrow")
                if not ar or i == 0:
                    continue
                f, t = ar.get("from"), ar.get("to")
                if not f or not t:
                    continue
                fx, fy = self._map(f[0], f[1], w, h)
                tx, ty = self._map(t[0], t[1], w, h)
                self._draw_arrow(p, fx, fy, tx, ty, scale, ox, oy,
                                 QColor(90, 170, 255, 110), max(2, base_w - 2), head)

            # 最佳着法：先描深色边，再画亮绿主体，保证在任何底色上都看得清
            ar = self._arrow
            if ar and ar.get("from") and ar.get("to"):
                f, t = ar["from"], ar["to"]
                fx, fy = self._map(f[0], f[1], w, h)
                tx, ty = self._map(t[0], t[1], w, h)
                self._draw_arrow(p, fx, fy, tx, ty, scale, ox, oy,
                                 QColor(0, 0, 0, 170), base_w + 4, head * 1.15)
                self._draw_arrow(p, fx, fy, tx, ty, scale, ox, oy,
                                 QColor(60, 230, 110), base_w, head)

            # ---- 识别不确定的格子 ----
            if self._rows:
                from xq_vision import DST_SIZE, PADDING
                p.setPen(QPen(QColor(255, 92, 92), 2))
                p.setBrush(Qt.NoBrush)
                for r in range(10):
                    for c in range(9):
                        if self._rows[r][c] != "x":
                            continue
                        cx = (PADDING + c * (DST_SIZE[0] - 2 * PADDING) / 8) * scale + ox
                        cy = (PADDING + r * (DST_SIZE[1] - 2 * PADDING) / 9) * scale + oy
                        p.drawEllipse(int(cx) - 11, int(cy) - 11, 22, 22)
        finally:
            p.end()


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"XiangQiLens v{__version__} · 象棋识别分析")
        self.resize(1200, 800)
        self.source = ScreenSource()
        self.worker: Worker | None = None
        self.thread: QThread | None = None
        # 高级面板：控件引用 {name: (widget, type, default)}
        self.adv_controls: dict = {}
        # 未开始分析时点了「新局」→ 记住，等 start() 时立刻自动判断
        self._newgame_armed = False
        dpi = enable_dpi_awareness()

        # ---- 左：棋盘 ----
        self.board = BoardView()
        left = QVBoxLayout()
        left.setContentsMargins(6, 6, 6, 6)
        left.addWidget(self.board, 1)
        left_box = QWidget()
        left_box.setLayout(left)

        # ---- 右：分析 ----
        self.lbl_fen = QLabel("—")
        self.lbl_fen.setWordWrap(True)
        self.lbl_fen.setStyleSheet(
            "font-family:Consolas,'Courier New',monospace; color:#c8d0dc; font-size:11px;")

        self.lbl_best = QLabel("—")
        self.lbl_best.setStyleSheet(
            "font-family:'Microsoft YaHei'; font-size:25px; "
            "color:#5ee08a; font-weight:600;")
        # 胜率单独一行、字号大一些 —— 这是最直观的局面判断
        self.lbl_winrate = QLabel("—")
        self.lbl_winrate.setStyleSheet(
            "font-family:'Microsoft YaHei'; font-size:15px; color:#e8dcc8;")
        self.lbl_winrate.setWordWrap(True)
        self.lbl_best_sub = QLabel("—")
        self.lbl_best_sub.setStyleSheet("color:#98a2b0; font-size:12px;")
        self.lbl_best_sub.setWordWrap(True)
        # 轮次提示（差分推进 / 引擎校正时显示）
        self.lbl_side_note = QLabel("")
        self.lbl_side_note.setStyleSheet("color:#e8b93c; font-size:12px;")
        self.lbl_side_note.hide()

        engine_grp = self._build_engine_panel()

        info = QGroupBox("当前建议")
        iv = QVBoxLayout(info)
        iv.addWidget(self.lbl_best)
        iv.addWidget(self.lbl_winrate)
        iv.addWidget(self.lbl_best_sub)
        iv.addWidget(self.lbl_side_note)
        iv.addSpacing(8)
        iv.addWidget(QLabel("局面 FEN"))
        iv.addWidget(self.lbl_fen)

        # 多主变例（PV）：引擎真实 MultiPV 输出（中文 + ICCS 双列）
        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ["#", "分数", "胜率", "深度", "中文主变例", "ICCS"])
        hh = self.table.horizontalHeader()
        hh.setStretchLastSection(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setVisible(False)
        for col in (0, 1, 2, 3, 5):
            hh.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.Stretch)

        alt = QGroupBox("多主变例 (MultiPV)")
        av = QVBoxLayout(alt)
        av.addWidget(self.table)

        right = QVBoxLayout()
        right.addWidget(self._build_automove_panel())   # 自动走棋（置顶，安全相关）
        right.addWidget(self._build_book_panel())       # 开局库
        right.addWidget(self._build_settle_panel())     # 识别稳定（动画抑制）
        right.addWidget(engine_grp)
        right.addWidget(info)
        right.addWidget(alt, 1)
        right_box = QWidget()
        right_box.setLayout(right)

        split = QSplitter(Qt.Horizontal)
        split.addWidget(left_box)
        split.addWidget(right_box)
        split.setStretchFactor(0, 5)
        split.setStretchFactor(1, 4)
        split.setSizes([680, 500])

        # ---- 顶部控制 ----
        self.combo = QComboBox()
        self.combo.setMinimumWidth(340)
        btn_refresh = QPushButton("刷新窗口")
        btn_refresh.clicked.connect(self.refresh_windows)

        self.spin_mt = QSpinBox()
        self.spin_mt.setRange(200, 10000)
        self.spin_mt.setSingleStep(100)
        self.spin_mt.setValue(1200)
        self.spin_mt.setSuffix(" ms")

        self.spin_conf = QSpinBox()
        self.spin_conf.setRange(1, 10)
        self.spin_conf.setValue(3)
        self.spin_conf.setSuffix(" 帧")

        # 持续加深：锁定局面后让引擎一直加深（go infinite），主变例实时刷新；
        # 不勾选则每次识别到新局面按「思考」时间分析一次。
        self.chk_infinite = QCheckBox("持续加深")
        self.chk_infinite.setToolTip(
            "勾选后引擎会锁定当前局面持续加深，主变例随深度实时刷新；\n"
            "不勾选则每次识别到新局面，按「思考」时间分析一次。")
        self.chk_infinite.toggled.connect(self._on_infinite_toggled)

        # 我方执子：决定「谁是我方」—— 影响左栏显示朝向、自动走棋「只走我方」
        # 的闸门，以及胜率视角。默认由「新局」自动判断。
        # ★ 原来的「先手」下拉已移除：分析时默认按「轮到我方」处理，
        #   而且 BoardTracker 会在观察到第一次走子时自动纠正轮次（双方合法性自纠），
        #   所以开局猜错也能自愈。
        self.combo_my_side = QComboBox()
        self.combo_my_side.addItem("我方执红", "w")
        self.combo_my_side.addItem("我方执黑", "b")
        self.combo_my_side.setToolTip(
            "你在这局里执哪一方。\n"
            "识别模型会自动把棋盘摆正（谁在下都能正确识别），\n"
            "此项决定：左栏盘面朝向我方、自动走棋只走我方。\n"
            "点「新局」会自动判断（象棋客户端都把己方摆在画面下方）。")
        self.combo_my_side.currentIndexChanged.connect(self._on_my_side_changed)

        self.btn_newgame = QPushButton("新局")
        self.btn_newgame.setMinimumWidth(64)
        self.btn_newgame.setToolTip(
            "开新一局：**停掉当前分析**并清空局面跟踪（避免上一局的局面/轮次\n"
            "残留把新局挡下），然后由你点「分析」重新开始。\n"
            "点「分析」时会**自动判断我方执子** —— 按「己方棋子摆在画面下方」\n"
            "这个所有象棋客户端通用的约定，开局红方还没动子也能判。")
        self.btn_newgame.clicked.connect(self._on_new_game)

        self.btn_run = QPushButton("分析")
        self.btn_run.setMinimumWidth(96)
        self.btn_run.clicked.connect(self.toggle)

        top = QHBoxLayout()
        top.addWidget(QLabel("目标窗口"))
        top.addWidget(self.combo, 1)
        top.addWidget(btn_refresh)
        top.addSpacing(10)
        top.addWidget(QLabel("我方执子"))
        top.addWidget(self.combo_my_side)
        top.addWidget(self.btn_newgame)
        top.addSpacing(6)
        top.addWidget(QLabel("思考"))
        top.addWidget(self.spin_mt)
        top.addWidget(self.chk_infinite)
        top.addWidget(QLabel("确认"))
        top.addWidget(self.spin_conf)
        top.addWidget(self.btn_run)
        top_box = QWidget()
        top_box.setLayout(top)

        root = QVBoxLayout()
        root.setContentsMargins(8, 8, 8, 8)
        root.addWidget(top_box)
        root.addWidget(split, 1)
        central = QWidget()
        central.setLayout(root)
        self.setCentralWidget(central)

        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage(f"DPI: {dpi} · 就绪")
        self.refresh_windows()
        self._load_auto_config()
        self._update_auto_indicator(self._auto_mode() != "off")

    # ---------------- 引擎参数面板 ----------------
    def _build_engine_panel(self) -> QGroupBox:
        """构建「引擎参数」面板（默认折叠，节省右栏空间）。"""
        grp = QGroupBox("引擎参数")
        grp.setCheckable(True)
        grp.setChecked(False)                 # 默认折叠

        outer = QVBoxLayout(grp)
        outer.setContentsMargins(6, 6, 6, 6)
        body = QWidget()
        grid = QGridLayout(body)
        grid.setContentsMargins(0, 0, 0, 0)

        self.spin_threads = QSpinBox()
        self.spin_threads.setRange(1, max(1, _os.cpu_count() or 4))
        self.spin_threads.setValue(2)
        self.spin_hash = QSpinBox()
        self.spin_hash.setRange(16, 4096)
        self.spin_hash.setSingleStep(64)
        self.spin_hash.setSuffix(" MB")
        self.spin_hash.setValue(256)
        self.spin_multipv = QSpinBox()
        self.spin_multipv.setRange(1, 8)
        self.spin_multipv.setValue(3)
        self.spin_overhead = QSpinBox()
        self.spin_overhead.setRange(0, 5000)
        self.spin_overhead.setSingleStep(10)
        self.spin_overhead.setSuffix(" ms")
        self.chk_wdl = QCheckBox("显示胜/和/负 (WDL)")
        self.chk_wdl.setChecked(True)
        self.btn_clear_hash = QPushButton("清空置换表")

        grid.addWidget(QLabel("线程 Threads"), 0, 0)
        grid.addWidget(self.spin_threads, 0, 1)
        grid.addWidget(QLabel("哈希 Hash"), 1, 0)
        grid.addWidget(self.spin_hash, 1, 1)
        grid.addWidget(QLabel("多主变例 MultiPV"), 2, 0)
        grid.addWidget(self.spin_multipv, 2, 1)
        grid.addWidget(QLabel("落子延时 Move Overhead"), 3, 0)
        grid.addWidget(self.spin_overhead, 3, 1)
        grid.addWidget(self.chk_wdl, 4, 0, 1, 2)
        grid.addWidget(self.btn_clear_hash, 5, 0, 1, 2)
        outer.addWidget(body)

        # 高级：运行时由引擎 option_lines 动态生成
        self.adv_grp = QGroupBox("高级（引擎全部可调项）")
        self.adv_grp.setCheckable(True)
        self.adv_grp.setChecked(False)
        adv_outer = QVBoxLayout(self.adv_grp)
        self.adv_scroll = QScrollArea()
        self.adv_scroll.setWidgetResizable(True)
        self.adv_scroll.setMaximumHeight(220)
        self.adv_body = QWidget()
        self.adv_form = QFormLayout(self.adv_body)
        self.adv_scroll.setWidget(self.adv_body)
        adv_outer.addWidget(self.adv_scroll)
        outer.addWidget(self.adv_grp)
        self.adv_grp.setVisible(False)        # 拿到引擎选项前不显示
        self.adv_scroll.setVisible(False)
        self.adv_grp.toggled.connect(self.adv_scroll.setVisible)

        # 折叠行为：未勾选时隐藏参数体
        body.setVisible(False)
        grp.toggled.connect(body.setVisible)

        # 控件变化 → 通知 Worker 热改
        for w in (self.spin_threads, self.spin_hash, self.spin_multipv,
                  self.spin_overhead):
            w.valueChanged.connect(self._on_param_changed)
        self.chk_wdl.toggled.connect(self._on_param_changed)
        self.btn_clear_hash.clicked.connect(self._on_clear_hash)
        return grp

    # ---------------- 开局库面板 ----------------
    def _build_book_panel(self) -> QGroupBox:
        """构建「开局库」面板（默认折叠）。

        官方 Pikafish 二进制不带开局库（实测其 UCI 选项里没有任何库选项），
        所以库在应用层做：查库命中就用库着，库外仍由引擎计算。
        """
        grp = QGroupBox("开局库（应用层实现）")
        grp.setCheckable(True)
        grp.setChecked(False)

        outer = QVBoxLayout(grp)
        outer.setContentsMargins(6, 6, 6, 6)

        self.lbl_book_state = QLabel("未启用")
        self.lbl_book_state.setWordWrap(True)
        self.lbl_book_state.setStyleSheet("color:#98a2b0; font-size:11px;")
        outer.addWidget(self.lbl_book_state)

        body = QWidget()
        grid = QGridLayout(body)
        grid.setContentsMargins(0, 0, 0, 0)

        self.chk_book = QCheckBox("启用开局库")
        self.chk_book.setToolTip(
            "查库命中时用库着（开局更稳更快），库外仍由引擎计算。\n"
            "支持 .obk（社区实际流通的格式，如「云霄剑诀」）与 .xqb、\n"
            "以及 .txt/.json 线路库。")

        self.edit_book = QLineEdit()
        self.edit_book.setPlaceholderText("选择开局库文件…")
        self.edit_book.setReadOnly(True)
        btn_browse = QPushButton("浏览…")
        btn_browse.clicked.connect(self._on_pick_book)

        self.combo_book_mode = QComboBox()
        self.combo_book_mode.addItem("库着优先（自动走棋走库着）", "play")
        self.combo_book_mode.addItem("仅提示（不影响走棋）", "hint")
        self.combo_book_mode.setToolTip(
            "库着优先：命中时「当前建议」与自动走棋都用库着。\n"
            "仅提示：只在面板上显示库着，不影响引擎与自动走棋。")

        self.spin_book_ply = QSpinBox()
        self.spin_book_ply.setRange(0, 200)
        self.spin_book_ply.setValue(40)
        self.spin_book_ply.setSuffix(" 手")
        self.spin_book_ply.setToolTip("只在前 N 手内用库；0 = 不限制。")

        self.lbl_book_hit = QLabel("—")
        self.lbl_book_hit.setWordWrap(True)
        self.lbl_book_hit.setStyleSheet("color:#2f6f4f; font-size:11px;")

        grid.addWidget(self.chk_book, 0, 0, 1, 2)
        grid.addWidget(self.edit_book, 1, 0)
        grid.addWidget(btn_browse, 1, 1)
        grid.addWidget(QLabel("库着策略"), 2, 0)
        grid.addWidget(self.combo_book_mode, 2, 1)
        grid.addWidget(QLabel("库深度"), 3, 0)
        grid.addWidget(self.spin_book_ply, 3, 1)
        grid.addWidget(self.lbl_book_hit, 4, 0, 1, 2)
        outer.addWidget(body)

        body.setVisible(False)
        grp.toggled.connect(body.setVisible)

        self.chk_book.toggled.connect(self._on_book_changed)
        self.combo_book_mode.currentIndexChanged.connect(self._on_book_changed)
        self.spin_book_ply.valueChanged.connect(self._on_book_changed)
        return grp

    def _collect_book_params(self) -> dict:
        return {
            "enabled": self.chk_book.isChecked(),
            "path": self.edit_book.text().strip(),
            "mode": self.combo_book_mode.currentData() or "play",
            "max_ply": self.spin_book_ply.value(),
        }

    @safe_slot
    def _on_pick_book(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择开局库文件", str(app_base()),
            "开局库 (*.obk *.xqb *.txt *.json);;obk 开局库 (*.obk);;"
            "xqb 开局库 (*.xqb);;线路库 (*.txt *.json);;所有文件 (*)")
        if not path:
            return
        self.edit_book.setText(path)
        self.chk_book.setChecked(True)          # 选完文件即启用
        self._on_book_changed()

    def _on_book_changed(self, *_args) -> None:
        if self.worker is not None:
            self.worker.post_book(self._collect_book_params())

    @Slot(dict)
    def on_book_state(self, d: dict) -> None:
        if not hasattr(self, "lbl_book_state"):
            return
        if not d.get("enabled"):
            self.lbl_book_state.setText("未启用")
        elif d.get("ok"):
            self.lbl_book_state.setText(f"已加载：{d.get('note', '')}")
        else:
            self.lbl_book_state.setText(f"⚠ {d.get('note', '加载失败')}")
        self._show_book_hit(d.get("hit"))

    def _show_book_hit(self, hit) -> None:
        if not hasattr(self, "lbl_book_hit"):
            return
        if hit:
            self.lbl_book_hit.setText(
                f"当前命中库着：{hit.get('chinese', '')}"
                f"（{hit.get('move', '')}）权重 {hit.get('weight', 1)}")
        else:
            self.lbl_book_hit.setText("当前局面未命中开局库")

    # ---------------- 识别稳定（动画抑制）面板 ----------------
    def _build_settle_panel(self) -> QGroupBox:
        """构建「识别稳定」面板（默认折叠、**功能默认关闭**）。

        天天象棋吃子时棋盘中间会弹「吃」字动画，遮住盘面导致识别出错。
        开启后：检测到画面正在变化就暂停识别，直到画面静止达到设定时长才
        继续 —— 即「等动画消失后再识别盘面变化，有变化了再移动棋子」。
        """
        grp = QGroupBox("识别稳定（动画抑制）")
        grp.setCheckable(True)
        grp.setChecked(False)

        outer = QVBoxLayout(grp)
        outer.setContentsMargins(6, 6, 6, 6)

        self.lbl_settle_state = QLabel("—")
        self.lbl_settle_state.setWordWrap(True)
        self.lbl_settle_state.setStyleSheet("color:#98a2b0; font-size:11px;")
        outer.addWidget(self.lbl_settle_state)

        body = QWidget()
        grid = QGridLayout(body)
        grid.setContentsMargins(0, 0, 0, 0)

        self.chk_anim = QCheckBox("等画面静止后再识别")
        self.chk_anim.setChecked(False)
        self.chk_anim.setToolTip(
            "开启后：帧间像素差异超过阈值即视为「画面在动」（吃子动画、\n"
            "落子动画、窗口刷新等），暂停识别；画面静止达到设定时长才恢复。\n"
            "这是针对天天象棋「吃」字动画遮挡盘面的核心修复。")

        self.spin_settle = QSpinBox()
        self.spin_settle.setRange(0, 3000)
        self.spin_settle.setSingleStep(100)
        self.spin_settle.setValue(400)
        self.spin_settle.setSuffix(" ms")
        self.spin_settle.setToolTip(
            "画面必须持续静止这么久才恢复识别。\n"
            "动画较长时可加大；觉得反应慢可减小。")

        self.spin_thresh = QDoubleSpinBox()
        self.spin_thresh.setRange(0.0, 0.5)
        self.spin_thresh.setSingleStep(0.005)
        self.spin_thresh.setDecimals(3)
        self.spin_thresh.setValue(0.02)
        self.spin_thresh.setToolTip(
            "帧间「变化像素」占比超过此值就算画面在动。\n"
            "默认 0.02（2%）。误报多就调大，漏检动画就调小。")

        grid.addWidget(self.chk_anim, 0, 0, 1, 2)
        grid.addWidget(QLabel("静置时长"), 1, 0)
        grid.addWidget(self.spin_settle, 1, 1)
        grid.addWidget(QLabel("变化阈值"), 2, 0)
        grid.addWidget(self.spin_thresh, 2, 1)
        outer.addWidget(body)

        body.setVisible(False)
        grp.toggled.connect(body.setVisible)

        self.chk_anim.toggled.connect(self._on_settle_changed)
        self.spin_settle.valueChanged.connect(self._on_settle_changed)
        self.spin_thresh.valueChanged.connect(self._on_settle_changed)
        return grp

    def _collect_settle_params(self) -> dict:
        return {
            "suppress": self.chk_anim.isChecked(),
            "settle_ms": self.spin_settle.value(),
            "thresh": float(self.spin_thresh.value()),
        }

    def _on_settle_changed(self, *_args) -> None:
        if self.worker is not None:
            self.worker.post_settle(self._collect_settle_params())

    @Slot(dict)
    def on_settle_state(self, d: dict) -> None:
        if not hasattr(self, "lbl_settle_state"):
            return
        if not d.get("suppress"):
            self.lbl_settle_state.setText("已关闭（直接识别，不做稳定等待）")
        elif d.get("settling"):
            self.lbl_settle_state.setText(
                f"画面变化中（{float(d.get('ratio', 0)) * 100:.1f}%），等待稳定…")
        else:
            self.lbl_settle_state.setText(
                f"已启用 · 静置 {d.get('settle_ms', self.spin_settle.value())} ms "
                f"· 阈值 {d.get('thresh', self.spin_thresh.value()):.3f}")

    # ---------------- 自动接盘面板 ----------------
    def _current_window(self) -> tuple[int, str] | None:
        """取下拉框当前选中的 ``(hwnd, title)``；没选返回 None。

        ★ ``itemData`` 存的是**元组** ``(hwnd, title)``，不是裸 hwnd。
        直接 ``int(itemData(...))`` 会抛 TypeError，而 Qt 槽函数里的异常会被
        PySide 打印到 stderr 后吞掉 —— 程序用 pythonw 启动没有控制台，
        表现就是「点了按钮没反应」。这里统一收口，避免再踩。
        """
        data = self.combo.currentData()
        if not data:
            return None
        try:
            hwnd, title = data
            return int(hwnd), str(title)
        except Exception:
            return None

    # ---------------- 新局 / 我方执子 ----------------
    @safe_slot
    def _on_new_game(self) -> None:
        """开新局：停掉当前分析 + 准备自动判断我方执子，然后**由你点「分析」开始**。

        刻意不自动开始分析：新局是一个「准备」动作，什么时候开始由你决定。
        点「分析」后 Worker 会用全新的局面跟踪起步，并立刻自动判断我方执子。
        """
        self._newgame_armed = True
        if self.worker is not None:
            self.stop()          # on_done 里会提示「已开新局，点分析开始」
        else:
            self.statusBar().showMessage(
                "已开新局 —— 点「分析」开始（会自动判断我方执子）")

    def _on_my_side_changed(self, *_args) -> None:
        """手动改「我方执子」→ 立刻同步给正在跑的 Worker（走命令队列）。"""
        side = self.combo_my_side.currentData() or "w"
        if self.worker is not None:
            self.worker.post_my_side(side)

    @Slot(dict)
    def on_side_detected(self, d: dict) -> None:
        """Worker 自动判断出了我方执子 → 回填下拉框。"""
        side = d.get("side")
        if side not in ("w", "b"):
            return
        idx = self.combo_my_side.findData(side)
        if idx >= 0:
            self.combo_my_side.blockSignals(True)
            self.combo_my_side.setCurrentIndex(idx)
            self.combo_my_side.blockSignals(False)
        self.statusBar().showMessage(
            f"已自动判断我方执子：{self.combo_my_side.currentText()}"
            f"（{d.get('why', '')}）")

    # ---------------- 自动走棋面板 ----------------
    def _build_automove_panel(self) -> QGroupBox:
        """构建「自动走棋」面板（默认折叠）。

        该功能会**模拟鼠标点击替我方落子**，所以默认关闭、且默认处于
        「预览模式（只算不点）」，需要用户显式关闭预览才会真正点击。
        """
        grp = QGroupBox("自动走棋（模拟鼠标点击）")
        grp.setCheckable(True)
        grp.setChecked(False)                 # 默认折叠

        outer = QVBoxLayout(grp)
        outer.setContentsMargins(6, 6, 6, 6)

        # 运行指示放在 body 之外 —— 折叠时依然可见（安全相关，必须醒目）
        self.lbl_auto_state = QLabel("● 自动走棋运行中")
        self.lbl_auto_state.setAlignment(Qt.AlignCenter)
        self.lbl_auto_state.setStyleSheet(
            "background:#c0392b; color:#ffffff; font-weight:700; "
            "padding:3px 8px; border-radius:3px;")
        self.lbl_auto_state.hide()
        outer.addWidget(self.lbl_auto_state)

        body = QWidget()
        grid = QGridLayout(body)
        grid.setContentsMargins(0, 0, 0, 0)

        # 走棋模式：三态**互斥**（关闭 / 预览 / 真点）。
        # 需求要求「选其中一个另一个选不上」，所以用 QButtonGroup 做单选，
        # 而不是两个可以同时勾上的复选框。默认「关闭」，最安全。
        self.radio_off = QRadioButton("关闭自动走棋")
        self.radio_dry = QRadioButton("预览模式（只算不点）")
        self.radio_dry.setToolTip(
            "只把「将要点击的着法与坐标」显示出来，不真的动鼠标。\n"
            "首次使用请先用这个模式确认坐标无误。")
        self.radio_real = QRadioButton("启动走棋（真点落子）")
        self.radio_real.setChecked(True)     # 默认开启（用户要求）
        self.radio_real.setToolTip(
            "真正模拟鼠标点击替我方落子。\n"
            "对手仍在真实平台上下棋，本程序不动对方棋子。")
        self.auto_mode_group = QButtonGroup(self)
        for _r in (self.radio_off, self.radio_dry, self.radio_real):
            self.auto_mode_group.addButton(_r)
        self.auto_mode_group.setExclusive(True)

        self.spin_auto_think = QSpinBox()
        self.spin_auto_think.setRange(300, 10000)
        self.spin_auto_think.setSingleStep(100)
        self.spin_auto_think.setValue(1200)
        self.spin_auto_think.setSuffix(" ms")
        self.spin_auto_think.setToolTip(
            "自动走棋的思考时间。开启自动走棋时会强制用「限时搜索」\n"
            "（持续加深模式永远不返回确定着法，无法用于落子）。")

        self.combo_click_mode = QComboBox()
        self.combo_click_mode.addItem("两次点击（推荐）", "two_click")
        self.combo_click_mode.addItem("拖拽", "drag")
        self.combo_click_mode.setToolTip(
            "两次点击：点起点选子 → 点终点落子（多数象棋界面支持）。\n"
            "拖拽：按住起点拖到终点（部分界面只认这种）。")

        self.spin_auto_cooldown = QDoubleSpinBox()
        self.spin_auto_cooldown.setRange(0.0, 10.0)
        self.spin_auto_cooldown.setSingleStep(0.5)
        self.spin_auto_cooldown.setDecimals(1)
        self.spin_auto_cooldown.setValue(1.5)
        self.spin_auto_cooldown.setSuffix(" s")

        self.spin_auto_max = QSpinBox()
        self.spin_auto_max.setRange(1, 999)
        self.spin_auto_max.setValue(200)
        self.spin_auto_max.setToolTip("连续自动落子达到此数后自动停止（保险丝）。")

        self.chk_restore = QCheckBox("点击后恢复光标")
        self.chk_restore.setChecked(False)
        self.chk_restore.setToolTip(
            "落子后把鼠标移回原位。开启会与用户手动操作抢光标，默认关闭。")

        self.spin_steps = QSpinBox()
        self.spin_steps.setRange(1, 12)
        self.spin_steps.setValue(1)
        self.spin_steps.setToolTip(
            "光标移动的插值步数。1=直接跳到目标；\n"
            "若某些界面需要鼠标「移过去」才响应悬停，可调到 4~8。")

        self.btn_auto_stop = QPushButton("紧急停止自动走棋")
        self.btn_auto_stop.clicked.connect(self._on_auto_stop)

        self.lbl_auto_last = QLabel("—")
        self.lbl_auto_last.setWordWrap(True)
        self.lbl_auto_last.setStyleSheet("color:#98a2b0; font-size:11px;")

        grid.addWidget(QLabel("走棋模式"), 0, 0)
        grid.addWidget(self.radio_off, 0, 1)
        grid.addWidget(self.radio_dry, 1, 0, 1, 2)
        grid.addWidget(self.radio_real, 2, 0, 1, 2)
        grid.addWidget(QLabel("思考时间"), 3, 0)
        grid.addWidget(self.spin_auto_think, 3, 1)
        grid.addWidget(QLabel("落子方式"), 4, 0)
        grid.addWidget(self.combo_click_mode, 4, 1)
        grid.addWidget(QLabel("落子冷却"), 5, 0)
        grid.addWidget(self.spin_auto_cooldown, 5, 1)
        grid.addWidget(QLabel("最大连续走子"), 6, 0)
        grid.addWidget(self.spin_auto_max, 6, 1)
        grid.addWidget(QLabel("移动插值步数"), 7, 0)
        grid.addWidget(self.spin_steps, 7, 1)
        grid.addWidget(self.chk_restore, 8, 0, 1, 2)
        grid.addWidget(self.btn_auto_stop, 9, 0, 1, 2)
        grid.addWidget(self.lbl_auto_last, 10, 0, 1, 2)
        outer.addWidget(body)

        body.setVisible(False)
        grp.toggled.connect(body.setVisible)

        # 控件变化 → 下发 Worker（信号最后连，避免构造期触发）
        for _r in (self.radio_off, self.radio_dry, self.radio_real):
            _r.toggled.connect(self._on_auto_changed)
        for w in (self.spin_auto_think, self.spin_auto_max, self.spin_steps):
            w.valueChanged.connect(self._on_auto_changed)
        self.spin_auto_cooldown.valueChanged.connect(self._on_auto_changed)
        self.combo_click_mode.currentIndexChanged.connect(self._on_auto_changed)
        self.chk_restore.toggled.connect(self._on_auto_changed)
        return grp

    # ---- 走棋模式三态（互斥）----
    def _auto_mode(self) -> str:
        """当前走棋模式：``'off'`` / ``'dry'`` / ``'real'``。"""
        if self.radio_real.isChecked():
            return "real"
        if self.radio_dry.isChecked():
            return "dry"
        return "off"

    def _set_auto_mode(self, mode: str) -> None:
        """设置走棋模式（不回触发信号）。"""
        target = {"real": self.radio_real,
                  "dry": self.radio_dry}.get(mode, self.radio_off)
        if target.isChecked():
            return
        for r in (self.radio_off, self.radio_dry, self.radio_real):
            r.blockSignals(True)
        target.setChecked(True)
        for r in (self.radio_off, self.radio_dry, self.radio_real):
            r.blockSignals(False)

    def _collect_auto_params(self) -> dict:
        mode = self._auto_mode()
        return {
            "enabled": mode != "off",
            "think_ms": self.spin_auto_think.value(),
            # 只有「真点」才不是预览；关闭时也报预览（避免误点）
            "dry_run": mode != "real",
            "mode": mode,
            "click_mode": self.combo_click_mode.currentData() or "two_click",
            "cooldown": float(self.spin_auto_cooldown.value()),
            "max_moves": self.spin_auto_max.value(),
            "restore_cursor": self.chk_restore.isChecked(),
            "move_steps": self.spin_steps.value(),
        }

    def _on_auto_changed(self, *_args) -> None:
        """自动走棋参数变化 → 经命令队列下发（不能走 Qt 信号，见 post_params）。"""
        on = self._auto_mode() != "off"
        # 自动走棋需要确定 bestmove，而「持续加深」永不返回 → 置灰
        inf = getattr(self, "chk_infinite", None)
        if inf is not None:
            inf.setEnabled(not on)
        self._update_auto_indicator(on)
        self._schedule_auto_save()
        if self.worker is not None:
            self.worker.post_auto(self._collect_auto_params())

    def _on_auto_stop(self) -> None:
        """紧急停止：只关自动走棋，不终止识别。"""
        self._set_auto_mode("off")
        self._on_auto_changed()

    def _update_auto_indicator(self, on: bool) -> None:
        if not hasattr(self, "lbl_auto_state"):
            return
        if on:
            dry = self._auto_mode() != "real"
            self.lbl_auto_state.setText(
                "● 自动走棋运行中（预览）" if dry else "● 自动走棋运行中（真点）")
            self.lbl_auto_state.show()
        else:
            self.lbl_auto_state.hide()

    @Slot(bool)
    def on_auto_state(self, on: bool) -> None:
        """Worker 通知自动走棋开关变化（含保险丝/重试超限导致的自动关闭）。"""
        if not on:
            self._set_auto_mode("off")
        elif self._auto_mode() == "off":
            self._set_auto_mode("dry")          # 被外部打开时回到安全的预览态
        inf = getattr(self, "chk_infinite", None)
        if inf is not None:
            inf.setEnabled(not on)
        self._update_auto_indicator(on)

    @Slot(dict)
    def on_auto_preview(self, d: dict) -> None:
        """显示最近一次自动走棋的预览/落子信息。"""
        if not hasattr(self, "lbl_auto_last"):
            return
        tag = "预览" if d.get("dry") else "已落子"
        frm, to = d.get("from"), d.get("to")
        coord = ""
        if frm and to:
            coord = f"  起点({frm[0]:.0f},{frm[1]:.0f}) 终点({to[0]:.0f},{to[1]:.0f})"
        self.lbl_auto_last.setText(
            f"{tag}: {d.get('chinese', '')} ({d.get('best', '')}){coord}")

    # ---------------- 自动走棋配置持久化 ----------------
    def _auto_cfg_path(self) -> Path:
        return app_base() / "automove.json"

    def _load_auto_config(self) -> None:
        """读取 automove.json 回填面板。

        **刻意不持久化「启用」开关** —— 每次启动都从关闭状态开始，
        避免上次退出时开着、下次一启动就开始点鼠标。
        """
        try:
            p = self._auto_cfg_path()
            if not p.is_file():
                return
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return
        widgets = (self.radio_off, self.radio_dry, self.radio_real,
                   self.spin_auto_think, self.combo_click_mode,
                   self.spin_auto_cooldown, self.spin_auto_max,
                   self.chk_restore, self.spin_steps)
        for w in widgets:
            w.blockSignals(True)
        try:
            # ★ 启动一律回到安全态：即使上次存的是「真点」，本次也只恢复成
            #   「预览」，绝不因为读了个配置文件就开始点鼠标。
            # 默认「启动走棋（真点落子）」—— 按需求自动走棋默认开启。
            # 旧配置没有 mode 键时也落到真点。
            mode = str(data.get("mode", "real"))
            if mode not in ("off", "dry", "real"):
                mode = "real"
            self._set_auto_mode(mode)
            self.spin_auto_think.setValue(int(data.get("think_ms", 1200)))
            idx = self.combo_click_mode.findData(
                str(data.get("click_mode", "two_click")))
            if idx >= 0:
                self.combo_click_mode.setCurrentIndex(idx)
            self.spin_auto_cooldown.setValue(float(data.get("cooldown", 1.5)))
            self.spin_auto_max.setValue(int(data.get("max_moves", 200)))
            self.chk_restore.setChecked(bool(data.get("restore_cursor", False)))
            self.spin_steps.setValue(int(data.get("move_steps", 1)))
        except Exception:
            pass
        finally:
            for w in widgets:
                w.blockSignals(False)

    def _save_auto_config(self) -> None:
        try:
            p = self._auto_cfg_path()
            data = self._collect_auto_params()
            data.pop("enabled", None)          # 不持久化总开关（见 _load_auto_config）
            p.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                         encoding="utf-8")
        except Exception:
            pass

    def _schedule_auto_save(self) -> None:
        """去抖写盘：拖 SpinBox 时不要每次都落盘。"""
        if not hasattr(self, "_auto_save_timer"):
            self._auto_save_timer = QTimer(self)
            self._auto_save_timer.setSingleShot(True)
            self._auto_save_timer.timeout.connect(self._save_auto_config)
        self._auto_save_timer.start(600)

    def _on_infinite_toggled(self, on: bool) -> None:
        """「持续加深」勾选时禁用「思考」时间（该模式下无意义）。"""
        self.spin_mt.setEnabled(not on)
        self._on_param_changed()

    def _on_param_changed(self, *_args) -> None:
        # 只往 Worker 的命令队列里塞，由 Worker 线程自己消费（见 Worker.post_params）。
        # 不能用 Qt 信号：run() 是长驻阻塞循环，不会派发 queued 连接。
        if self.worker is not None:
            self.worker.post_params(self._collect_engine_params())

    def _on_clear_hash(self) -> None:
        if self.worker is not None:
            self.worker.post_clear_hash()

    @staticmethod
    def _adv_value(w, typ):
        """读取高级面板控件的当前值（归一成 int/bool/str）。"""
        if typ == "spin":
            return int(w.value())
        if typ == "check":
            return bool(w.isChecked())
        if typ == "combo":
            return w.currentText()
        return w.text()

    def _collect_advanced(self) -> dict:
        """只收集用户**实际改动过**的高级选项。

        以控件建好时的真实初值为基准（而不是引擎 option 行里的 default 字符串），
        避免 combo 的 default 不在 var 列表里时误报「已改动」而spurious下发。
        """
        out: dict = {}
        for name, (w, typ, initial) in self.adv_controls.items():
            try:
                v = self._adv_value(w, typ)
                if v != initial:
                    out[name] = v
            except Exception:
                continue
        return out

    def _collect_engine_params(self) -> dict:
        return {
            "threads": self.spin_threads.value(),
            "hash_mb": self.spin_hash.value(),
            "multipv": self.spin_multipv.value(),
            "move_overhead": self.spin_overhead.value(),
            "show_wdl": self.chk_wdl.isChecked(),
            "infinite": self.chk_infinite.isChecked(),
            "advanced": self._collect_advanced(),
        }

    # 已在「引擎参数」精选区提供控件的选项，高级区不再重复（避免两处打架）
    _CURATED_OPTS = ("Threads", "Hash", "MultiPV", "Move Overhead",
                     "UCI_ShowWDL", "Clear Hash")

    @Slot(list)
    def on_engine_options(self, options: list) -> None:
        """引擎握手后拿到可调项列表，动态构建「高级」面板。"""
        while self.adv_form.count():
            item = self.adv_form.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self.adv_controls.clear()

        for opt in options:
            name, typ = opt.get("name", ""), opt.get("type", "")
            default = opt.get("default", "")
            if not name or typ == "button" or name in self._CURATED_OPTS:
                continue          # button 型用独立的「清空置换表」按钮
            if typ == "spin":
                w = QSpinBox()
                lo, hi = opt.get("min"), opt.get("max")
                w.setRange(int(lo) if lo is not None else -1000000,
                           int(hi) if hi is not None else 1000000)
                try:
                    w.setValue(int(default))
                except Exception:
                    pass
                w.valueChanged.connect(self._on_param_changed)
            elif typ == "check":
                w = QCheckBox()
                w.setChecked(str(default).lower() == "true")
                w.toggled.connect(self._on_param_changed)
            elif typ == "combo":
                w = QComboBox()
                for v in (opt.get("var") or []):
                    w.addItem(str(v))
                idx = w.findText(str(default))
                if idx >= 0:
                    w.setCurrentIndex(idx)
                w.currentIndexChanged.connect(self._on_param_changed)
            else:
                w = QLineEdit(str(default))
                typ = "string"
                w.editingFinished.connect(self._on_param_changed)
            self.adv_form.addRow(name, w)
            # 记录控件真实初值作为「未改动」基准（见 _collect_advanced）
            self.adv_controls[name] = (w, typ, self._adv_value(w, typ))

        self.adv_grp.setVisible(bool(self.adv_controls))

    # ---------------- 结果渲染 ----------------
    def _fill_pv_table(self, diag: dict) -> None:
        """用真实 MultiPV 结果填充主变例表（中文 + ICCS 双列）。"""
        lines = diag.get("pv") or []
        self.table.setRowCount(len(lines))
        for i, ln in enumerate(lines):
            vals = [str(ln.get("multipv", i + 1)),
                    ln.get("score", "—"),
                    ln.get("winrate", "—"),
                    str(ln.get("depth") or "—"),
                    ln.get("chinese_seq", ""),
                    ln.get("pv_iccs", "")]
            for j, v in enumerate(vals):
                item = QTableWidgetItem(str(v))
                if i == 0:
                    item.setForeground(QColor(60, 230, 110))
                if j >= 4:
                    item.setToolTip(str(v))
                self.table.setItem(i, j, item)

    def _apply_diag(self, fen: str, diag: dict, with_status: bool = False) -> None:
        """把 diag 渲染到右栏标签与主变例表。"""
        if fen:
            self.lbl_fen.setText(fen)
        side_cn = "红方走" if diag.get("side") == "w" else "黑方走"
        best_txt = f"{diag.get('best') or '—'}   {diag.get('chinese', '')}"
        if diag.get("book_used"):
            best_txt += "   【开局库】"
        self.lbl_best.setText(best_txt)
        self.lbl_winrate.setText(diag.get("winrate", "—"))

        # 开局库命中情况（只有启用时才刷新这一行，避免覆盖「未启用」提示）
        if diag.get("book_enabled"):
            self._show_book_hit(diag.get("book"))

        sub = (f"{side_cn} · 分数 {diag.get('score', '—')} · "
               f"深度 {diag.get('depth') or '—'}")
        if diag.get("seldepth"):
            sub += f"/{diag['seldepth']}"
        sub += f" · 合法着法 {diag.get('moves', 0)}"
        if diag.get("nps"):
            sub += f" · {diag['nps'] / 1e6:.2f}M nps"
        if diag.get("engine_mode"):
            sub += f" · {diag['engine_mode']}"
        self.lbl_best_sub.setText(sub)

        if diag.get("note"):
            self.lbl_side_note.setText(diag["note"])
            self.lbl_side_note.show()
        else:
            self.lbl_side_note.hide()

        if with_status:
            self.statusBar().showMessage(
                f"识别 {diag.get('t_recog', 0):.0f}ms · "
                f"最低置信 {diag.get('conf', 0):.2f} · {diag.get('guard', '')}")
        self._fill_pv_table(diag)

    # ---------------- 窗口列表 ----------------
    # 自动选中棋局窗口的阈值：低于它视为"不像棋局窗口"，不自动选，交给用户手选。
    # 取值依据（各项权重见 _window_score）：
    #   天天象棋小程序（标题含"象棋" + 微信宿主 + CEF + 未最小化） = 170
    #   JJ象棋 PC 端（含"象棋" + CEF + 未最小化）                  = 130
    #   微信小程序宿主但标题不含"象棋"（普通小程序窗口）            =  70  ← 必须低于阈值
    #   微信主窗口 / 记事本 / 其它普通窗口                          ≤  50
    # 取 80 的效果：只有「标题里写了象棋」的窗口才会被自动选中。
    WINDOW_SCORE_MIN = 80

    @staticmethod
    def _window_score(w: dict) -> int:
        """给候选窗口打分，用于自动选中棋局窗口。

        为什么用打分而不是「标题里有 JJ象棋」：除了 JJ象棋 PC 端，还有
        天天象棋小程序（宿主进程 WeChatAppEx.exe / 窗口标题「天天象棋」）、
        各类网页版棋局，硬编码单一标题会全部漏掉。
        """
        t = w.get("title") or ""
        cls = w.get("class") or ""
        proc = (w.get("proc") or "").lower()
        s = 0
        if "象棋" in t:
            s += 100                     # 最强信号：标题里直接写了象棋
        if proc in ("wechatappex.exe", "weixin.exe", "wechat.exe"):
            s += 40                      # 微信 / 微信小程序宿主
        if cls.startswith("Chrome_WidgetWin"):
            s += 20                      # CEF / WebView 渲染窗口
        if not w.get("minimized"):
            s += 10
        return s

    def refresh_windows(self) -> None:
        self.combo.clear()
        # 用 list_windows_any：包含最小化的窗口，这样用户把对局窗口
        # 最小化后依然能在列表里选到它（只是需要先还原才能识别）。
        # 该函数已过滤系统壳 / 输入法 / 本程序自身的窗口。
        wins = list_windows_any(min_size=200)
        for w in wins:
            r = w["rect"]
            size = f"{r[2] - r[0]}x{r[3] - r[1]}"
            mark = "  [已最小化]" if w["minimized"] else ""
            proc = w.get("proc") or ""
            proc_part = f"  {proc}" if proc else ""
            label = f"{w['title']}  [{w['class']}]{proc_part}  {size}{mark}"
            self.combo.addItem(label, (w["hwnd"], w["title"]))
        if self.combo.count() == 0:
            self.combo.addItem("（未找到可用窗口）", None)
            return

        # 自动选中得分最高的候选（棋局窗口），而不是只看标题里有没有「JJ象棋」
        scored = [(self._window_score(w), i) for i, w in enumerate(wins)]
        best_score, best = max(scored, key=lambda x: x[0])
        if best_score < self.WINDOW_SCORE_MIN:
            # 一个都不像棋局窗口：停在第一项但明确提示，别让用户误以为选好了
            self.combo.setCurrentIndex(0)
            self.statusBar().showMessage(
                "未自动找到棋局窗口，请在下拉框里手动选择对局窗口"
                "（通常是标题含「象棋」的那个）")
            return

        self.combo.setCurrentIndex(best)
        w = wins[best]
        if w["minimized"]:
            self.statusBar().showMessage(
                f"已选中 {w['title']}，但它处于最小化状态 —— 请先还原窗口再点「开始」")
        else:
            self.statusBar().showMessage(
                f"已选中棋局窗口：{w['title']} · 点「开始」")

    # ---------------- 启停 ----------------
    def toggle(self) -> None:
        self.stop() if self.worker is not None else self.start()

    def start(self) -> None:
        cur = self._current_window()
        if not cur:
            QMessageBox.warning(self, "提示", "请先选择一个目标窗口")
            return
        hwnd, title = cur
        self.source.attach(hwnd, title)
        # 开始前先检查窗口状态，避免"最小化导致识别全错"这种难排查的情况
        win_ok, minimized, note = window_state(hwnd)
        if not win_ok:
            QMessageBox.warning(self, "提示", note or "目标窗口不可用")
            return
        if self.source.grab() is None:
            QMessageBox.warning(self, "提示", f"无法抓取窗口画面：{title}")
            return

        self.worker = Worker(self.source)
        self.worker.movetime = self.spin_mt.value()
        self.worker.confirm = self.spin_conf.value()
        self.worker.my_side = self.combo_my_side.currentData() or "w"
        # 轮次（原「先手」下拉已删除）：
        #   * 点过「新局」→ 按**规则**用「红方先手」（新局时红方还没动子）；
        #   * 直接点「分析」→ 默认按「轮到我方」。
        # 猜错也不怕，有两层兜底：
        #   ① BoardTracker 观察到第一次走子时会做「双方合法性自纠」；
        #   ② 自动走棋另有一道硬闸门，挡掉「盘面还是开局却认为轮到黑方」的情形。
        self.worker.first_side = ("w" if self._newgame_armed
                                  else (self.combo_my_side.currentData() or "w"))
        if self._newgame_armed:
            # 停止状态下点过「新局」→ 一开始分析就立刻自动判断执子
            self.worker.auto_side_pending = True
            self._newgame_armed = False
        # 引擎参数（可调）
        params = self._collect_engine_params()
        self.worker.threads = params["threads"]
        self.worker.hash_mb = params["hash_mb"]
        self.worker.multipv = params["multipv"]
        self.worker.move_overhead = params["move_overhead"]
        self.worker.show_wdl = params["show_wdl"]
        self.worker.infinite = params["infinite"]
        self.worker.engine_opts = params["advanced"]
        # 自动走棋初值（与 movetime/confirm 一样，启动前直接写字段）
        auto = self._collect_auto_params()
        self.worker.auto_move_enabled = auto["enabled"]
        self.worker.auto_think_ms = auto["think_ms"]
        self.worker.auto_dry_run = auto["dry_run"]
        self.worker.auto_click_mode = auto["click_mode"]
        self.worker.auto_cooldown = auto["cooldown"]
        self.worker.auto_max_moves = auto["max_moves"]
        self.worker.auto_restore_cursor = auto["restore_cursor"]
        self.worker.auto_move_steps = auto["move_steps"]
        self.thread = QThread()
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.frame_ready.connect(self.on_frame)
        self.worker.analysis_update.connect(self.on_analysis_update)
        self.worker.engine_options.connect(self.on_engine_options)
        self.worker.status.connect(self.statusBar().showMessage)
        self.worker.error.connect(lambda m: self.statusBar().showMessage(f"错误: {m}"))
        self.worker.done.connect(self.on_done)
        self.worker.auto_state_changed.connect(self.on_auto_state)
        self.worker.auto_preview.connect(self.on_auto_preview)
        self.worker.book_state.connect(self.on_book_state)
        self.worker.settle_state.connect(self.on_settle_state)
        self.worker.side_detected.connect(self.on_side_detected)
        self.thread.start()
        # 开局库 / 识别稳定：走命令队列下发，由 Worker 线程自己应用
        # （开局库必须在 Worker 线程里打开 sqlite 连接，见 apply_book_params）
        try:
            self.worker.post_book(self._collect_book_params())
            self.worker.post_settle(self._collect_settle_params())
        except Exception:
            pass
        self._update_auto_indicator(self._auto_mode() != "off")
        self.btn_run.setText("停止")
        self.combo.setEnabled(False)
        self.combo_my_side.setEnabled(False)

    def stop(self) -> None:
        if self.worker:
            # 先关自动走棋，避免停止过程中还去点鼠标
            try:
                self.worker.post_auto({"enabled": False})
            except Exception:
                pass
            self.worker.stop()
        self._update_auto_indicator(False)
        self.btn_run.setText("停止中…")

    # ---------------- 结果 ----------------
    @Slot(object, object, str, dict, str)
    def on_frame(self, bgr, warped, fen: str, diag: dict, key: str) -> None:
        # key 里的矩阵已是「标准朝向」（我方执黑时被翻转 180° 过）。
        flipped = bool(diag.get("flipped"))
        # 为了在真实画面上标注红圈，需要「画面朝向」的矩阵：翻转回来即可。
        rows = [key[i * 9:(i + 1) * 9] for i in range(10)]
        if flipped:
            rows = Worker.flip_rows(rows)
        alts = diag.get("alts") or []
        # 棋盘 + 走法箭头（最佳着法画绿色粗箭头，候选着法画蓝色细箭头）
        # 箭头坐标会由 BoardView 依据 flipped 做 180° 映射，以贴合画面朝向
        self.board.update_board(warped, rows, diag.get("arrow"), alts, flipped)
        self._apply_diag(fen, diag, with_status=True)

    @Slot(str, dict)
    def on_analysis_update(self, fen: str, diag: dict) -> None:
        """引擎持续加深时的流式刷新：只更新箭头与右栏，不重传棋盘图像。"""
        self.board.set_arrows(diag.get("arrow"), diag.get("alts") or [])
        self._apply_diag(fen, diag, with_status=False)

    @Slot()
    def on_done(self) -> None:
        if self.worker:
            self.worker.shutdown()
        if self.thread:
            self.thread.quit()
            self.thread.wait(3000)
        self.worker = None
        self.thread = None
        self.btn_run.setText("分析")
        self.combo.setEnabled(True)
        self.combo_my_side.setEnabled(True)
        self._update_auto_indicator(False)
        if self._newgame_armed:
            self.statusBar().showMessage(
                "已开新局 —— 点「分析」开始（会自动判断我方执子）")
        else:
            self.statusBar().showMessage("已停止")

    def closeEvent(self, ev) -> None:
        self._save_auto_config()
        self.stop()
        if self.thread:
            self.thread.quit()
            self.thread.wait(3000)
        # 线程已停，再确保引擎子进程被关掉（否则会留下孤儿进程）
        if self.worker:
            self.worker.shutdown()
        ev.accept()


def main() -> int:
    ap = argparse.ArgumentParser(description="XiangQiLens 象棋识别分析")
    ap.add_argument("--selftest", action="store_true", help="无界面自检")
    ap.add_argument("--image", default=None, help="自检用图片路径")
    ap.add_argument("--movetime", type=int, default=1200, help="自检思考时间 ms")
    args = ap.parse_args()

    if args.selftest:
        return selftest(args.image, args.movetime)

    # ★ 装上全局异常钩子：pythonw 没有控制台，否则槽函数里的异常会被
    #   PySide 静默吞掉，用户只看到「点了没反应」。
    sys.excepthook = lambda t, e, tb: report_exception(e, where="excepthook")

    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
