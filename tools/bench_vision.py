# -*- coding: utf-8 -*-
"""识别性能基准测试：测量 pose / cls 各阶段耗时分布，定位性能问题。

分别测：
1. 纯模型推理（同一张图重复 N 次），不受抓帧影响
2. 完整单帧（抓帧 + 推理）
并输出分位数，观察是否有偶发尖峰。
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from app_backend import ScreenSource, create_vision, enable_dpi_awareness
from xq_vision import BoardClassifier, Pose4Kpt


def stats(name: str, xs: list[float]) -> None:
    if not xs:
        print(f"    {name}: 无数据")
        return
    xs_sorted = sorted(xs)
    n = len(xs_sorted)
    print(f"    {name:12} 中位 {statistics.median(xs):7.1f}ms  "
          f"均值 {statistics.mean(xs):7.1f}ms  "
          f"p90 {xs_sorted[int(n * 0.9)]:7.1f}ms  "
          f"最小 {xs_sorted[0]:7.1f}ms  最大 {xs_sorted[-1]:7.1f}ms")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=20)
    ap.add_argument("--hwnd", default=None)
    args = ap.parse_args()

    print("=" * 74)
    print("识别性能基准")
    print("=" * 74)
    print(f"DPI: {enable_dpi_awareness()}")

    import onnxruntime as ort
    print(f"onnxruntime {ort.__version__}")
    print(f"可用 providers: {ort.get_available_providers()}")
    print()

    # ---- 准备一张图 ----
    src = ScreenSource()
    if args.hwnd:
        hwnd = int(args.hwnd, 0)
    else:
        cands = [w for w in ScreenSource.list_windows(120) if "JJ象棋" in w[1]]
        hwnd = cands[0][0] if cands else None
    if hwnd:
        src.attach(hwnd, "?")
        img = src.grab()
        print(f"抓帧来源: 窗口 0x{hwnd:08X}")
    else:
        img = cv2.imdecode(np.fromfile(str(HERE / "shots" / "bg_002A0A30.png"),
                                       dtype=np.uint8), cv2.IMREAD_COLOR)
        print("抓帧来源: 测试图")
    if img is None:
        print("[失败] 无可用图像")
        return 1
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    print(f"图像尺寸: {img.shape[1]}x{img.shape[0]}")
    print()

    print("[1] 加载模型并检查实际使用的 execution provider")
    vision = create_vision(cuda=True)
    for tag, sess in (("pose", vision.pose.sess), ("cls", vision.cls.sess)):
        used = sess.get_providers()
        print(f"    {tag}: {used}")
    print()

    print("[2] 纯推理基准（同一张图重复，不含抓帧）")
    pose_s, cls_s, total_s = [], [], []
    for i in range(args.runs):
        t0 = time.perf_counter()
        kps, scores = vision.pose.predict(rgb)
        t1 = time.perf_counter()
        warped, mat = __import__("xq_vision").extract_board(rgb, kps)
        rows, conf, idx = vision.cls.predict(warped)
        t2 = time.perf_counter()
        pose_s.append((t1 - t0) * 1000)
        cls_s.append((t2 - t1) * 1000)
        total_s.append((t2 - t0) * 1000)
        if i == 0:
            print(f"    首帧(含预热): {total_s[0]:.1f}ms")
    stats("pose", pose_s[1:])
    stats("cls", cls_s[1:])
    stats("合计", total_s[1:])
    print()

    print("[3] 完整单帧基准（抓帧 + 推理）")
    if hwnd:
        full_s = []
        for i in range(min(args.runs, 15)):
            t0 = time.perf_counter()
            im = src.grab()
            if im is not None:
                vision.infer(cv2.cvtColor(im, cv2.COLOR_BGR2RGB))
            full_s.append((time.perf_counter() - t0) * 1000)
        stats("完整单帧", full_s)
    else:
        print("    跳过（无窗口）")
    print()

    print("[4] 结论")
    med = statistics.median(total_s[1:]) if len(total_s) > 1 else total_s[0]
    if med < 300:
        print(f"    推理中位 {med:.0f}ms — 性能正常（GPU 加速生效）")
    elif med < 600:
        print(f"    推理中位 {med:.0f}ms — 偏慢，可能在用 CPU 推理")
    else:
        print(f"    推理中位 {med:.0f}ms — 明显偏慢，检查 provider 与本机负载")
    return 0


if __name__ == "__main__":
    sys.exit(main())
