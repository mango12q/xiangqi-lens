# -*- coding: utf-8 -*-
"""自动走棋状态机干跑：接真实窗口跑完整识别→引擎链路，默认**不真点**。

逐回合打印「轮到我方 → 将点击哪一步 / 哪两个屏幕坐标」，用于验证闸门
（只走我方、同局面只点一次、冷却、保险丝、重试上限）是否符合预期。

    python tools/test_automove_e2e.py                 # 干跑 60 秒
    python tools/test_automove_e2e.py --duration 120 --think 1500
    python tools/test_automove_e2e.py --real --yes    # 真点（危险，需二次确认）
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from _common import bootstrap, have_engine, have_vision, resolve_hwnd, skip  # noqa: E402

bootstrap()

from app_backend import ScreenSource, enable_dpi_awareness  # noqa: E402
from app_vision import Worker  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hwnd", default=None)
    ap.add_argument("--duration", type=float, default=60.0, help="运行秒数")
    ap.add_argument("--think", type=int, default=1200, help="自动走棋思考时间 ms")
    ap.add_argument("--my-side", default="w", choices=["w", "b"])
    ap.add_argument("--first-side", default="w", choices=["w", "b"])
    ap.add_argument("--cooldown", type=float, default=1.5)
    ap.add_argument("--max-moves", type=int, default=200)
    ap.add_argument("--real", action="store_true", help="真正点击（默认只预览）")
    ap.add_argument("--yes", action="store_true", help="--real 时的确认开关")
    args = ap.parse_args()

    if args.real and not args.yes:
        print("--real 会真的模拟鼠标点击并抢占鼠标。加 --yes 确认。")
        return 1

    print(f"DPI: {enable_dpi_awareness()}")
    if not have_vision():
        return skip("识别模型不在本机（xq_research/hf_model/... 两个 onnx 缺失）",
                    "按 README「依赖资源：模型与引擎」下载后重跑")
    if not have_engine():
        return skip("Pikafish 引擎不在本机（xq_research/pikafish/ 缺失）",
                    "运行 python tools/setup_engine.py 后重跑")
    src = ScreenSource()
    if args.hwnd:
        hwnd = int(args.hwnd, 0)
        title = "?"
    else:
        hwnd = resolve_hwnd(None, "JJ象棋")
        if not hwnd:
            return skip("未找到 JJ象棋 窗口（需要真实对局窗口才能验证）",
                        "先打开对局界面，或用 --hwnd 指定句柄后重跑")
        title = next((t for h, t, c, r in ScreenSource.list_windows(120)
                      if h == hwnd), "?")
    # ★ 原来这里只在 else 分支 attach，导致 --hwnd 时 src.hwnd 仍是 None、
    #   抓帧永远返回 None，--hwnd 实际不可用。现在两个分支都 attach。
    src.attach(hwnd, title)
    print(f"目标窗口 hwnd=0x{hwnd:X}")

    worker = Worker(src)
    worker.auto_move_enabled = True
    worker.auto_dry_run = not args.real
    worker.auto_think_ms = args.think
    worker.auto_cooldown = args.cooldown
    worker.auto_max_moves = args.max_moves
    worker.my_side = args.my_side
    worker.first_side = args.first_side
    worker.movetime = args.think
    worker.multipv = 1

    worker.status.connect(lambda m: print(f"[状态] {m}", flush=True))
    worker.error.connect(lambda m: print(f"[错误] {m}", flush=True))
    worker.auto_state_changed.connect(
        lambda on: print(f"[自动走棋] {'开启' if on else '关闭'}", flush=True))

    def on_preview(d: dict) -> None:
        tag = "预览" if d.get("dry") else "已点击"
        print(f"[{tag}] {d.get('chinese')} ({d.get('best')})  "
              f"{d.get('from')} → {d.get('to')}", flush=True)

    worker.auto_preview.connect(on_preview)
    worker.analysis_update.connect(
        lambda fen, diag: print(f"[分析] {fen}  best={diag.get('best')} "
                                f"depth={diag.get('depth')}", flush=True)
        if diag.get("best") else None)

    print(f"运行 {args.duration:.0f}s，模式={'真点' if args.real else '预览(不真点)'}…")
    th = threading.Thread(target=worker.run, daemon=True)
    th.start()
    try:
        time.sleep(args.duration)
    except KeyboardInterrupt:
        print("\n收到 Ctrl-C")
    worker.stop()
    th.join(timeout=6)
    try:
        worker.shutdown()
    except Exception:
        pass
    print("已结束。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
