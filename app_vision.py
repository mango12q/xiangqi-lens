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

# 切到脚本所在目录：模型与引擎用的是相对路径，从桌面快捷方式启动时
# 当前目录可能是任意位置，不切换会找不到资源。
# 放在 import 之前，保证后续所有相对路径解析都基于脚本目录。
import os as _os
try:
    _os.chdir(HERE)
except OSError:
    pass

sys.path.insert(0, str(HERE))

from app_backend import (HERE as BACKEND_DIR, ScreenSource,
                         check_board_geometry, check_position, create_engine,
                         create_vision, enable_dpi_awareness, fen_with_side,
                         format_score, move_arrow, move_to_chinese,
                         parse_alternatives, selftest, validate_fen)

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
        self.first_side = "w"          # 首帧轮次：w=红先 / b=黑先
        self._run = False
        self._vision = None
        self._engine = None
        self._last_key = ""
        self._same = 0
        self._published = ""
        self._tracker = None
        self._last_fen = ""

    @Slot()
    def run(self) -> None:
        self._run = True
        try:
            self.status.emit("加载识别模型…")
            self._vision = create_vision(cuda=True)
            self.status.emit("启动引擎…")
            self._engine = create_engine(self.threads, self.hash_mb)
        except Exception as exc:
            self.error.emit(f"初始化失败: {exc}")
            self.done.emit()
            return

        from app_backend import BoardTracker
        # BoardTracker 同时负责「演化校验」和「轮次管理」
        self._tracker = BoardTracker(force_after=6, first_side=self.first_side)

        self.status.emit("就绪，开始识别…")
        while self._run:
            t0 = time.perf_counter()
            bgr = self.source.grab()
            if bgr is None:
                self.status.emit("抓帧失败（窗口已关闭？）")
                time.sleep(0.5)
                continue

            try:
                res = self._vision.infer(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
            except Exception as exc:
                self.status.emit(f"识别异常: {exc}")
                time.sleep(0.3)
                continue

            rows = res["rows"]
            key = "".join(rows)

            # pose 几何校验：画面里没有棋盘时，模型会输出退化的四边形
            # （例如被 JJ象棋 主界面的 3D 棋盘摆件误导），这时直接跳过，
            # 否则会拿一个扭曲的图去分类，产出完全错误的局面。
            geo_ok, geo_why = check_board_geometry(
                res["keypoints"], img_shape=bgr.shape)
            if not geo_ok:
                self.status.emit(f"未定位到棋盘：{geo_why}（请确认对局界面已打开）")
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

            fen_base = self._vision.to_fen(rows, side_to_move="w")

            # 演化校验 + 轮次推进（BoardTracker 内部处理）
            accept, reason, move, side = self._tracker.update(fen_base)
            if not accept:
                if reason == "drop":
                    self.status.emit(
                        f"局面变化无法解释，已挡下（{self._tracker.stats}）"
                        f" · minConf={res['conf'].min():.2f}")
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
                time.sleep(0.25)
                continue
            ok, err, n_moves = validate_fen(fen)
            if not ok:
                self.status.emit(f"棋规校验未通过：{err[:60]}")
                time.sleep(0.25)
                continue

            self._published = key
            t_recog = (time.perf_counter() - t0) * 1000
            self.status.emit(move_note or "引擎分析中…")
            try:
                r = self._engine.analyse(fen, movetime_ms=self.movetime)
            except Exception as exc:
                # 引擎可能已经崩溃退出（非法局面 / 资源问题），重建它，
                # 否则后续每一帧都会在同一个死进程上重复失败。
                self.error.emit(f"引擎异常，正在重启: {exc}")
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
                "arrow": move_arrow(res["warp_mat"], best),
                "alts": alts,
                "guard": self._tracker.stats,
            }
            warped = cv2.cvtColor(res["warped"], cv2.COLOR_RGB2BGR)
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

    def update_board(self, warped: np.ndarray, rows: list[str],
                     arrow: dict | None = None, alts: list[dict] | None = None) -> None:
        self._warped = warped
        self._rows = rows
        self._arrow = arrow
        self._alts = alts or []
        self.update()

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
                self._draw_arrow(p, f[0], f[1], t[0], t[1], scale, ox, oy,
                                 QColor(90, 170, 255, 110), max(2, base_w - 2), head)

            # 最佳着法：先描深色边，再画亮绿主体，保证在任何底色上都看得清
            ar = self._arrow
            if ar and ar.get("from") and ar.get("to"):
                f, t = ar["from"], ar["to"]
                self._draw_arrow(p, f[0], f[1], t[0], t[1], scale, ox, oy,
                                 QColor(0, 0, 0, 170), base_w + 4, head * 1.15)
                self._draw_arrow(p, f[0], f[1], t[0], t[1], scale, ox, oy,
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
        self.setWindowTitle("XiangQiLens · 象棋识别分析")
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
        self.lbl_best_sub = QLabel("—")
        self.lbl_best_sub.setStyleSheet("color:#98a2b0; font-size:12px;")
        # 轮次提示（差分推进 / 引擎校正时显示）
        self.lbl_side_note = QLabel("")
        self.lbl_side_note.setStyleSheet("color:#e8b93c; font-size:12px;")
        self.lbl_side_note.hide()

        info = QGroupBox("当前建议")
        iv = QVBoxLayout(info)
        iv.addWidget(self.lbl_best)
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
            "开局轮次。识别看不出轮到谁走，只能先猜；\n"
            "若猜错，引擎着法的合法性会在第一次分析后自动纠正。")

        self.btn_run = QPushButton("开始")
        self.btn_run.setMinimumWidth(96)
        self.btn_run.clicked.connect(self.toggle)

        top = QHBoxLayout()
        top.addWidget(QLabel("目标窗口"))
        top.addWidget(self.combo, 1)
        top.addWidget(btn_refresh)
        top.addSpacing(14)
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
        for hwnd, title, cls, rect in ScreenSource.list_windows():
            w, h = rect[2] - rect[0], rect[3] - rect[1]
            self.combo.addItem(f"{title}  [{cls}]  {w}x{h}", (hwnd, title))
        if self.combo.count() == 0:
            self.combo.addItem("（未找到可用窗口）", None)
            return

        # 优先选真正的棋局窗口：标题里带「JJ象棋 / 象棋」的独立小程序窗口，
        # 而不是整个「微信」窗口 —— 微信窗口里棋盘占比太小，
        # 棋盘角点定位容易失败。
        best = -1
        for i in range(self.combo.count()):
            data = self.combo.itemData(i)
            if not data:
                continue
            title = data[1]
            if "JJ象棋" in title:
                best = i
                break
            if "象棋" in title and best < 0:
                best = i
        if best >= 0:
            self.combo.setCurrentIndex(best)
            self.statusBar().showMessage(
                f"已选中棋局窗口：{self.combo.currentText().split('  [')[0]} · 点「开始」")
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
        if self.source.grab() is None:
            QMessageBox.warning(self, "提示", f"无法抓取窗口画面：{title}")
            return

        self.worker = Worker(self.source)
        self.worker.movetime = self.spin_mt.value()
        self.worker.confirm = self.spin_conf.value()
        self.worker.first_side = self.combo_side.currentData() or "w"
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

    def stop(self) -> None:
        if self.worker:
            self.worker.stop()
        self.btn_run.setText("停止中…")

    # ---------------- 结果 ----------------
    @Slot(object, object, str, dict, str)
    def on_frame(self, bgr, warped, fen: str, diag: dict, key: str) -> None:
        rows = [key[i * 9:(i + 1) * 9] for i in range(10)]
        alts = diag.get("alts") or []
        # 棋盘 + 走法箭头（最佳着法画绿色粗箭头，候选着法画蓝色细箭头）
        self.board.update_board(warped, rows, diag.get("arrow"), alts)
        self.lbl_fen.setText(fen)

        side_cn = "红方走" if diag.get("side") == "w" else "黑方走"
        self.lbl_best.setText(f"{diag.get('best', '—')}   {diag.get('chinese', '')}")
        self.lbl_best_sub.setText(
            f"{side_cn} · 分数 {diag.get('score')} · 深度 {diag.get('depth')} · "
            f"识别 {diag.get('t_recog', 0):.0f}ms · "
            f"最低置信 {diag.get('conf', 0):.2f} · 合法着法 {diag.get('moves', 0)}")
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
