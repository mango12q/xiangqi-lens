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
                         list_windows_any, move_arrow, move_screen_points,
                         move_to_chinese, parse_engine_options, pv_to_chinese,
                         selftest, short_winrate, validate_fen, wdl_to_red,
                         window_state)

from app_input import MouseClicker, ensure_foreground, is_point_on_window

from PySide6.QtCore import QObject, QRect, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox,
                               QDoubleSpinBox, QFormLayout, QGridLayout,
                               QGroupBox, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QMainWindow, QMessageBox, QPushButton,
                               QScrollArea, QSpinBox, QSplitter, QStatusBar,
                               QTableWidget, QTableWidgetItem, QVBoxLayout,
                               QWidget)


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

    def __init__(self, source: ScreenSource) -> None:
        super().__init__()
        self.source = source
        self.threads = 8
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
        best = self._last_bestmove
        fen = self._last_bestmove_fen
        if not best or best in ("(none)", "0000"):
            return
        if self._searching:
            return                                  # 搜索未结束，bestmove 未定
        if not fen or not self._cur_fen_base:
            return
        if fen != fen_with_side(self._cur_fen_base, self._cur_side):
            return                                  # bestmove 属于旧局面
        if self._cur_side != self.my_side:
            return                                  # ★ 只走我方
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

    # ---- 主线程 → Worker 的命令入口（线程安全，只往队列里塞）----
    def post_params(self, params: dict) -> None:
        """提交引擎参数变更（主线程调用）。"""
        self._cmds.put(("params", params))

    def post_clear_hash(self) -> None:
        """请求清空置换表（主线程调用）。"""
        self._cmds.put(("clear", None))

    def post_auto(self, params: dict) -> None:
        """提交自动走棋参数变更（主线程调用）。"""
        self._cmds.put(("auto", params))

    def _process_commands(self) -> None:
        """在 Worker 线程内消费命令队列（循环顶部调用）。

        会**合并**积压的命令：连续改多个参数只应用最后一次，避免拖拽
        SpinBox 时反复「停搜索→下发→重启」把引擎抖崩。
        """
        last_params = None
        last_auto = None
        do_clear = False
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
            elif kind == "clear":
                do_clear = True
        if not drained:
            return
        if do_clear:
            try:
                self.clear_hash_requested()
            except Exception as exc:
                self.error.emit(f"清空置换表失败: {exc}")
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

        self.combo_side = QComboBox()
        self.combo_side.addItem("红方先行", "w")
        self.combo_side.addItem("黑方先行", "b")
        self.combo_side.setToolTip(
            "开局轮次（该谁先走）。识别看不出轮到谁走，只能先猜；\n"
            "若猜错，引擎着法的合法性会在第一次分析后自动纠正。")

        # 我方执子方 —— 与「先手」是两个独立维度：
        # 「先手」决定该谁走，「我方执子」只决定左栏显示朝向。
        # 识别模型的拉正流程按内容摆正棋盘（黑方底线恒在 row 0），
        # 所以这个选项不再影响局面识别，只决定「左栏显示图朝哪边」
        # （执黑时把显示图转 180°，让我方在下）。
        self.combo_my_side = QComboBox()
        self.combo_my_side.addItem("我方执红", "w")
        self.combo_my_side.addItem("我方执黑", "b")
        self.combo_my_side.setToolTip(
            "你在这局里执哪一方。\n"
            "识别模型会自动把棋盘摆正（谁在下都能正确识别），\n"
            "此项只影响：左栏盘面朝向我方。\n"
            "「当前建议」栏的胜率按红/黑方绝对视角给出，与本项无关。\n"
            "选错不会导致局面颠倒，只会让左栏朝向反着。")

        self.btn_run = QPushButton("开始")
        self.btn_run.setMinimumWidth(96)
        self.btn_run.clicked.connect(self.toggle)

        top = QHBoxLayout()
        top.addWidget(QLabel("目标窗口"))
        top.addWidget(self.combo, 1)
        top.addWidget(btn_refresh)
        top.addSpacing(10)
        top.addWidget(QLabel("我方执子"))
        top.addWidget(self.combo_my_side)
        top.addSpacing(6)
        top.addWidget(QLabel("先手"))
        top.addWidget(self.combo_side)
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
        self._update_auto_indicator(self.chk_auto.isChecked())

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
        self.spin_threads.setValue(8)
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

        self.chk_auto = QCheckBox("启用自动走棋（只走我方）")
        self.chk_auto.setToolTip(
            "开启后：识别到轮到我方且局面稳定时，取引擎最佳着法，\n"
            "用鼠标模拟点击「起点 → 终点」替我落子。\n"
            "对手仍在真实平台上下棋，本程序不动对方棋子。")

        self.chk_dry = QCheckBox("预览模式（只算不点）")
        self.chk_dry.setChecked(True)
        self.chk_dry.setToolTip(
            "勾选时只把「将要点击的着法与坐标」显示出来，不真的动鼠标。\n"
            "首次使用请先保持勾选，确认坐标无误后再取消。")

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

        grid.addWidget(self.chk_auto, 0, 0, 1, 2)
        grid.addWidget(self.chk_dry, 1, 0, 1, 2)
        grid.addWidget(QLabel("思考时间"), 2, 0)
        grid.addWidget(self.spin_auto_think, 2, 1)
        grid.addWidget(QLabel("落子方式"), 3, 0)
        grid.addWidget(self.combo_click_mode, 3, 1)
        grid.addWidget(QLabel("落子冷却"), 4, 0)
        grid.addWidget(self.spin_auto_cooldown, 4, 1)
        grid.addWidget(QLabel("最大连续走子"), 5, 0)
        grid.addWidget(self.spin_auto_max, 5, 1)
        grid.addWidget(QLabel("移动插值步数"), 6, 0)
        grid.addWidget(self.spin_steps, 6, 1)
        grid.addWidget(self.chk_restore, 7, 0, 1, 2)
        grid.addWidget(self.btn_auto_stop, 8, 0, 1, 2)
        grid.addWidget(self.lbl_auto_last, 9, 0, 1, 2)
        outer.addWidget(body)

        body.setVisible(False)
        grp.toggled.connect(body.setVisible)

        # 控件变化 → 下发 Worker（信号最后连，避免构造期触发）
        self.chk_auto.toggled.connect(self._on_auto_changed)
        self.chk_dry.toggled.connect(self._on_auto_changed)
        for w in (self.spin_auto_think, self.spin_auto_max, self.spin_steps):
            w.valueChanged.connect(self._on_auto_changed)
        self.spin_auto_cooldown.valueChanged.connect(self._on_auto_changed)
        self.combo_click_mode.currentIndexChanged.connect(self._on_auto_changed)
        self.chk_restore.toggled.connect(self._on_auto_changed)
        return grp

    def _collect_auto_params(self) -> dict:
        return {
            "enabled": self.chk_auto.isChecked(),
            "think_ms": self.spin_auto_think.value(),
            "dry_run": self.chk_dry.isChecked(),
            "click_mode": self.combo_click_mode.currentData() or "two_click",
            "cooldown": float(self.spin_auto_cooldown.value()),
            "max_moves": self.spin_auto_max.value(),
            "restore_cursor": self.chk_restore.isChecked(),
            "move_steps": self.spin_steps.value(),
        }

    def _on_auto_changed(self, *_args) -> None:
        """自动走棋参数变化 → 经命令队列下发（不能走 Qt 信号，见 post_params）。"""
        on = self.chk_auto.isChecked()
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
        self.chk_auto.setChecked(False)

    def _update_auto_indicator(self, on: bool) -> None:
        if not hasattr(self, "lbl_auto_state"):
            return
        if on:
            dry = self.chk_dry.isChecked()
            self.lbl_auto_state.setText(
                "● 自动走棋运行中（预览）" if dry else "● 自动走棋运行中（真点）")
            self.lbl_auto_state.show()
        else:
            self.lbl_auto_state.hide()

    @Slot(bool)
    def on_auto_state(self, on: bool) -> None:
        """Worker 通知自动走棋开关变化（含保险丝/重试超限导致的自动关闭）。"""
        self.chk_auto.blockSignals(True)
        self.chk_auto.setChecked(bool(on))
        self.chk_auto.blockSignals(False)
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
        widgets = (self.chk_auto, self.chk_dry, self.spin_auto_think,
                   self.combo_click_mode, self.spin_auto_cooldown,
                   self.spin_auto_max, self.chk_restore, self.spin_steps)
        for w in widgets:
            w.blockSignals(True)
        try:
            self.chk_dry.setChecked(bool(data.get("dry_run", True)))
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
        self.lbl_best.setText(
            f"{diag.get('best') or '—'}   {diag.get('chinese', '')}")
        self.lbl_winrate.setText(diag.get("winrate", "—"))

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
        data = self.combo.currentData()
        if not data:
            QMessageBox.warning(self, "提示", "请先选择一个目标窗口")
            return
        hwnd, title = data
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
        self.worker.first_side = self.combo_side.currentData() or "w"
        self.worker.my_side = self.combo_my_side.currentData() or "w"
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
        self.thread.start()
        self._update_auto_indicator(self.chk_auto.isChecked())
        self.btn_run.setText("停止")
        self.combo.setEnabled(False)
        self.combo_my_side.setEnabled(False)
        self.combo_side.setEnabled(False)

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
        self.btn_run.setText("开始")
        self.combo.setEnabled(True)
        self.combo_my_side.setEnabled(True)
        self.combo_side.setEnabled(True)
        self._update_auto_indicator(False)
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

    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
