# -*- coding: utf-8 -*-
"""界面冒烟测试（离屏）—— 不弹窗口、不启动识别/引擎。

覆盖三轮评审里改动过的界面部分：
* 「自动走棋」面板新增的「鼠标注入」下拉，以及它到 Worker / MouseClicker 的贯通；
* MultiPV 默认值改为 1（原先 3，会把同一份思考时间摊给多条线）；
* 参数收集 → 配置文件往返（backend 键必须能存下来、读回来）。

⚠ 会把 automove.json 指向临时文件，**不动用户的真实配置**。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tools"))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")   # 离屏，不弹窗口

from _common import Checker  # noqa: E402


def main() -> int:
    c = Checker("界面冒烟测试（离屏）")

    from PySide6.QtWidgets import QApplication
    import app_vision
    from app_vision import MainWindow

    tmp = tempfile.TemporaryDirectory()
    cfg = Path(tmp.name) / "automove.json"
    # 隔离配置：绝不动用户真实的 automove.json
    MainWindow._auto_cfg_path = lambda self: cfg

    app = QApplication.instance() or QApplication([])

    print()
    print("[1] 主窗口能离屏构造（覆盖面板/网格/信号连接）")
    try:
        win = MainWindow()
        c.check("MainWindow() 构造成功", True)
    except Exception as exc:                             # noqa: BLE001
        import traceback
        traceback.print_exc()
        c.check("MainWindow() 构造成功", False, f"{type(exc).__name__}: {exc}")
        return c.done()

    print()
    print("[2] MultiPV 默认必须是 1")
    c.check("引擎面板 spin_multipv 默认 1",
            win.spin_multipv.value() == 1, f"实得 {win.spin_multipv.value()}")
    mpv = _worker_default_multipv()
    c.check("Worker 默认 multipv 1", mpv == 1, f"实得 {mpv}")

    print()
    print("[3] 「鼠标注入」下拉存在且可选项正确")
    c.check("存在 combo_backend", hasattr(win, "combo_backend"))
    items = [win.combo_backend.itemData(i)
             for i in range(win.combo_backend.count())]
    c.check("两个后端都可选",
            set(items) == {"mouse_event", "sendinput"}, f"实得 {items}")
    c.check("默认 mouse_event",
            win.combo_backend.currentData() == "mouse_event",
            f"实得 {win.combo_backend.currentData()}")

    print()
    print("[4] 参数收集把 backend 带上了")
    params = win._collect_auto_params()
    c.check("params 里有 backend 键", "backend" in params, f"实得 {sorted(params)}")
    c.check("backend 值正确", params.get("backend") == "mouse_event")

    print()
    print("[5] backend 能存进配置并读回")
    win.combo_backend.setCurrentIndex(
        win.combo_backend.findData("sendinput"))
    win._save_auto_config()
    saved = json.loads(cfg.read_text(encoding="utf-8"))
    c.check("配置里写入了 sendinput",
            saved.get("backend") == "sendinput", f"实得 {saved.get('backend')}")
    c.check("配置里没有 enabled（派生字段不持久化）", "enabled" not in saved)

    win.combo_backend.setCurrentIndex(
        win.combo_backend.findData("mouse_event"))
    win._load_auto_config()
    c.check("读回后下拉恢复为 sendinput",
            win.combo_backend.currentData() == "sendinput",
            f"实得 {win.combo_backend.currentData()}")

    print()
    print("[6] backend 能下发到 Worker（经参数应用）")
    from app_backend import ScreenSource
    w = app_vision.Worker(ScreenSource())
    w.apply_auto_params({"enabled": True, "backend": "sendinput",
                         "click_mode": "drag"})
    c.check("Worker.auto_backend 已更新",
            w.auto_backend == "sendinput", f"实得 {w.auto_backend}")
    c.check("MouseClicker 真的拿到了该 backend",
            w._clicker is not None and w._clicker.backend == "sendinput",
            f"实得 {getattr(w._clicker, 'backend', None)}")
    c.check("click_mode 也一并生效",
            w._clicker is not None and w._clicker.mode == "drag")

    win.deleteLater()
    app.processEvents()
    tmp.cleanup()
    return c.done()


def _worker_default_multipv() -> int:
    """单独实例化一个 Worker 读它的默认 multipv（不启动线程）。"""
    import app_vision
    from app_backend import ScreenSource
    w = app_vision.Worker(ScreenSource())
    return int(w.multipv)


if __name__ == "__main__":
    sys.exit(main())
