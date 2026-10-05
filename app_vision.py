# -*- coding: utf-8 -*-
"""XiangQiLens GUI —— 界面与调度层（识别/引擎逻辑见 app_backend.py）。

不含自动点击：只给走法建议，落子由用户自行操作。
"""
from __future__ import annotations

import argparse
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

from app_backend import (HERE as BACKEND_DIR, ScreenSource, __version__,
                         check_board_geometry, check_position, create_engine,
                         create_vision, enable_dpi_awareness, fen_with_side,
                         format_score, format_winrate, list_windows_any,
                         move_arrow, move_to_chinese, parse_alternatives,
                         parse_wdl, selftest, validate_fen, window_state)

from PySide6.QtCore import QObject, QRect, QThread, Qt, Signal, Slot
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen
from PySide6.QtWidgets import (QApplication, QComboBox, QGroupBox,
                               QHBoxLayout, QLabel, QMainWindow, QMessageBox,
                               QPushButton, QSpinBox, QSplitter, QStatusBar,
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

    def __init__(self, source: ScreenSource) -> None:
        super().__init__()
        self.source = source
        self.threads = 8
        self.hash_mb = 256
        self.movetime = 1200
        self.confirm = 3
        # first_side：首帧轮次（红先/黑先），只影响"该谁走"
        self.first_side = "w"
        # my_side：我方执子方（红/黑）。**只影响两处呈现**：胜率视角
        # （format_winrate）与左栏显示图的朝向。矩阵/FEN 永远是标准朝向，
        # 因为识别模型的拉正流程已按内容把黑方底线摆到 row 0（见 run() 里的注释）。
        # 这是与 first_side 完全独立的一维。
        self.my_side = "w"
        self._run = False
        self._vision = None
        self._engine = None
        self._last_key = ""
        self._same = 0
        self._published = ""
        self._tracker = None
        self._last_fen = ""
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
            self._engine = create_engine(self.threads, self.hash_mb)
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
            self.status.emit(move_note or "引擎分析中…")
            try:
                r = self._engine.analyse(fen, movetime_ms=self.movetime)
            except Exception as exc:
                # 引擎可能已经崩溃退出（非法局面 / 资源问题），重建它，
                # 否则后续每一帧都会在同一个死进程上重复失败。
                self.error.emit(f"引擎异常，正在重启: {exc}")
                self._log(f"引擎异常: {exc}")
                try:
                    self._engine.quit()
                except Exception:
                    pass
                self._engine = None
                try:
                    self._engine = create_engine(self.threads, self.hash_mb)
                    self.status.emit("引擎已重启")
                except Exception as exc2:
                    self.error.emit(f"引擎重启失败: {exc2}")
                time.sleep(0.5)
                continue

            best = r.get("bestmove") or ""
            # 用引擎着法的合法性反推轮次（能自动纠正开局猜错的情况）
            if self._tracker.correct_by_engine_move(best):
                side = self._tracker.side_to_move
                fen = fen_with_side(fen_base, side)
                move_note = f"轮次已校正为{'红' if side == 'w' else '黑'}方走"
                try:
                    r = self._engine.analyse(fen, movetime_ms=self.movetime)
                    best = r.get("bestmove") or ""
                except Exception as exc:
                    self.error.emit(f"引擎重算失败: {exc}")
                    continue

            alts = parse_alternatives(r.get("info", []), fen)
            # 为每个候选着法算好箭头坐标（拉正图坐标 + 原图坐标）
            for a in alts:
                a["arrow"] = move_arrow(res["warp_mat"], a["iccs"])

            # 从引擎 info 行里取 wdl（胜/和/负），转成我方视角的胜率描述
            wdl = None
            for line in reversed(r.get("info", [])):
                wdl = parse_wdl(line)
                if wdl is not None:
                    break
            winrate = format_winrate(wdl, r.get("score_cp"), self.my_side)

            diag = {
                "score": format_score(r.get("score_cp")),
                "depth": r.get("depth"),
                "best": best,
                "chinese": move_to_chinese(fen, best) if best else "",
                "t_recog": t_recog,
                "conf": float(res["conf"].min()),
                "moves": n_moves,
                "side": side,
                "note": move_note,
                # 仅表示「左栏显示图是否转 180° 让画面朝向与我方一致」，
                # 与矩阵/FEN 无关（矩阵永远保持标准朝向）
                "flipped": self.my_side == "b",
                "winrate": winrate,
                "wdl": wdl,
                "arrow": move_arrow(res["warp_mat"], best),
                "alts": alts,
                "guard": self._tracker.stats,
            }
            warped = cv2.cvtColor(res["warped"], cv2.COLOR_RGB2BGR)
            if self.my_side == "b":
                # 只转「拿来显示」的那张图：我方执黑时让盘面在左栏里也是
                # 我方在下，看着顺手。矩阵不动，FEN 仍然正确。
                warped = cv2.rotate(warped, cv2.ROTATE_180)
            self.frame_ready.emit(bgr, warped, fen, diag, key)
            side_cn = "红" if side == "w" else "黑"
            self.status.emit(f"{side_cn}方走 · 深度 {diag['depth']} · "
                             f"分数 {diag['score']} · 识别 {t_recog:.0f}ms · "
                             f"{self._tracker.stats}")

        self.done.emit()

    def stop(self) -> None:
        self._run = False

    def shutdown(self) -> None:
        self._run = False
        if self._engine is not None:
            try:
                self._engine.quit()
            except Exception:
                pass
            self._engine = None
        if self._log_fh is not None:
            try:
                self._log("已停止")
                self._log_fh.close()
            except Exception:
                pass
            self._log_fh = None


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

    def update_board(self, warped: np.ndarray, rows: list[str],
                     arrow: dict | None = None, alts: list[dict] | None = None,
                     flipped: bool = False) -> None:
        self._warped = warped
        self._rows = rows
        self._arrow = arrow
        self._alts = alts or []
        self._flipped = flipped
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

            rgb = cv2.cvtColor(self._warped, cv2.COLOR_BGR2RGB)
            qimg = QImage(rgb.data, w, h, 3 * w,
                          QImage.Format_RGB888).copy()
            # PySide6 的 drawImage 没有 (x, y, w, h, img) 重载，必须传 QRect
            p.drawImage(QRect(ox, oy, dw, dh), qimg)

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

        info = QGroupBox("当前建议")
        iv = QVBoxLayout(info)
        iv.addWidget(self.lbl_best)
        iv.addWidget(self.lbl_winrate)
        iv.addWidget(self.lbl_best_sub)
        iv.addWidget(self.lbl_side_note)
        iv.addSpacing(8)
        iv.addWidget(QLabel("局面 FEN"))
        iv.addWidget(self.lbl_fen)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["着法", "中文", "分数", "深度"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.verticalHeader().setVisible(False)

        alt = QGroupBox("候选着法")
        av = QVBoxLayout(alt)
        av.addWidget(self.table)

        right = QVBoxLayout()
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

        self.combo_side = QComboBox()
        self.combo_side.addItem("红方先行", "w")
        self.combo_side.addItem("黑方先行", "b")
        self.combo_side.setToolTip(
            "开局轮次（该谁先走）。识别看不出轮到谁走，只能先猜；\n"
            "若猜错，引擎着法的合法性会在第一次分析后自动纠正。")

        # 我方执子方 —— 与「先手」是两个独立维度：
        # 「先手」决定该谁走，「我方执子」只决定胜率视角与左栏显示朝向。
        # 识别模型的拉正流程按内容摆正棋盘（黑方底线恒在 row 0），
        # 所以这个选项不再影响局面识别，只决定「胜率按谁的视角」与
        # 「左栏显示图朝哪边」（执黑时把显示图转 180°，让我方在下）。
        self.combo_my_side = QComboBox()
        self.combo_my_side.addItem("我方执红", "w")
        self.combo_my_side.addItem("我方执黑", "b")
        self.combo_my_side.setToolTip(
            "你在这局里执哪一方。\n"
            "识别模型会自动把棋盘摆正（谁在下都能正确识别），\n"
            "此项只影响：胜率按我方视角换算、左栏盘面朝向我方。\n"
            "选错不会导致局面颠倒，只会让胜率视角与左栏朝向反着。")

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

    # ---------------- 窗口列表 ----------------
    def refresh_windows(self) -> None:
        self.combo.clear()
        # 用 list_windows_any：包含最小化的窗口，这样用户把对局窗口
        # 最小化后依然能在列表里选到它（只是需要先还原才能识别）。
        wins = list_windows_any(min_size=200)
        for w in wins:
            r = w["rect"]
            size = f"{r[2] - r[0]}x{r[3] - r[1]}"
            mark = "  [已最小化]" if w["minimized"] else ""
            label = f"{w['title']}  [{w['class']}]  {size}{mark}"
            self.combo.addItem(label, (w["hwnd"], w["title"]))
        if self.combo.count() == 0:
            self.combo.addItem("（未找到可用窗口）", None)
            return

        # 优先选真正在对局的棋局窗口：标题带「JJ象棋 / 象棋」，且已还原
        best = -1
        for i, w in enumerate(wins):
            if "JJ象棋" in w["title"] and not w["minimized"]:
                best = i
                break
        if best < 0:
            for i, w in enumerate(wins):
                if "JJ象棋" in w["title"]:
                    best = i
                    break
        if best < 0:
            for i, w in enumerate(wins):
                if "象棋" in w["title"] and not w["minimized"]:
                    best = i
                    break

        if best >= 0:
            self.combo.setCurrentIndex(best)
            w = wins[best]
            if w["minimized"]:
                self.statusBar().showMessage(
                    f"已选中 {w['title']}，但它处于最小化状态 —— 请先还原窗口再点「开始」")
            else:
                self.statusBar().showMessage(
                    f"已选中棋局窗口：{w['title']} · 点「开始」")
        else:
            self.statusBar().showMessage(
                "未自动找到棋局窗口，请在下拉框里手动选择 JJ象棋 窗口")

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
        self.thread = QThread()
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.frame_ready.connect(self.on_frame)
        self.worker.status.connect(self.statusBar().showMessage)
        self.worker.error.connect(lambda m: self.statusBar().showMessage(f"错误: {m}"))
        self.worker.done.connect(self.on_done)
        self.thread.start()
        self.btn_run.setText("停止")
        self.combo.setEnabled(False)
        self.combo_my_side.setEnabled(False)
        self.combo_side.setEnabled(False)

    def stop(self) -> None:
        if self.worker:
            self.worker.stop()
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
        self.lbl_fen.setText(fen)

        side_cn = "红方走" if diag.get("side") == "w" else "黑方走"
        self.lbl_best.setText(f"{diag.get('best', '—')}   {diag.get('chinese', '')}")
        self.lbl_winrate.setText(diag.get("winrate", "—"))
        # 技术指标挪到副行与状态栏，避免干扰主要信息
        self.lbl_best_sub.setText(
            f"{side_cn} · 分数 {diag.get('score')} · 深度 {diag.get('depth')} · "
            f"合法着法 {diag.get('moves', 0)}")
        self.statusBar().showMessage(
            f"识别 {diag.get('t_recog', 0):.0f}ms · "
            f"最低置信 {diag.get('conf', 0):.2f} · {diag.get('guard', '')}")
        if diag.get("note"):
            self.lbl_side_note.setText(diag["note"])
            self.lbl_side_note.show()
        else:
            self.lbl_side_note.hide()

        self.table.setRowCount(len(alts))
        for i, a in enumerate(alts):
            for j, v in enumerate([a["iccs"], a["chinese"], a["score"], a["depth"]]):
                item = QTableWidgetItem(str(v))
                if i == 0:
                    item.setForeground(QColor(60, 230, 110))
                self.table.setItem(i, j, item)

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
        self.statusBar().showMessage("已停止")

    def closeEvent(self, ev) -> None:
        self.stop()
        if self.thread:
            self.thread.quit()
            self.thread.wait(3000)
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
