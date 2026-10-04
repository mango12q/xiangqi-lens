# -*- coding: utf-8 -*-
"""检查 JJ象棋 窗口状态：是否最小化、能否后台截图。

动机：用户最小化对局窗口后，按「可见窗口」枚举会找不到它；
但 PrintWindow 后台截图并不要求窗口可见，所以应当仍能工作。
"""
from __future__ import annotations

import ctypes
import sys
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from app_backend import ScreenSource, enable_dpi_awareness, create_vision

import win32gui
import win32con

print(f"DPI: {enable_dpi_awareness()}")
print()

print("=== 按标题查找（含最小化/隐藏窗口）===")
found = []


def cb(hwnd, _):
    title = win32gui.GetWindowText(hwnd)
    if not title:
        return True
    cls = win32gui.GetClassName(hwnd)
    vis = win32gui.IsWindowVisible(hwnd)
    icon = win32gui.IsIconic(hwnd)
    rect = win32gui.GetWindowRect(hwnd)
    w, h = rect[2] - rect[0], rect[3] - rect[1]
    if "象棋" in title or "JJ" in title:
        found.append((hwnd, title, cls, vis, icon, w, h, rect))
        print(f"  0x{hwnd:08X}  {title!r}")
        print(f"      类名={cls}  可见={vis}  最小化={icon}  尺寸={w}x{h}")
        # 还原状态的放置矩形（最小化时 GetWindowRect 不准）
        try:
            plc = win32gui.GetWindowPlacement(hwnd)
            print(f"      placement: showCmd={plc[1]}  "
                  f"normal={plc[4]}")
        except Exception as exc:
            print(f"      placement 读取失败: {exc}")
        return True
    return True


win32gui.EnumWindows(cb, None)

if not found:
    print("   未找到任何象棋相关窗口（连隐藏的也没有）")
    sys.exit(1)

print()
print("=== 对每个窗口尝试后台截图 ===")
for hwnd, title, cls, vis, icon, w, h, rect in found:
    print(f"  0x{hwnd:08X} {title!r} (可见={vis}, 最小化={icon})")
    src = ScreenSource()
    src.attach(hwnd, title)
    img = src.grab()
    if img is None:
        print("      抓帧: 失败")
        continue
    a = img.astype(np.float32)
    mean, std = float(a.mean()), float(a.std())
    nonblack = float((a.max(axis=2) > 12).mean())
    print(f"      抓帧: {img.shape[1]}x{img.shape[0]}  "
          f"均值={mean:.1f} 标准差={std:.1f} 非黑={nonblack:.3f}")
    usable = std > 3.0 and nonblack > 0.02
    print(f"      可用: {usable}")
    if usable:
        out = HERE / "shots" / "minimized_test.png"
        out.parent.mkdir(exist_ok=True)
        cv2.imencode(".png", img)[1].tofile(str(out))
        print(f"      已存 {out}")
        # 顺便跑一次识别
        try:
            vision = create_vision(cuda=True)
            res = vision.infer(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
            print(f"      识别: pose={res['t_pose_ms']:.0f}ms cls={res['t_cls_ms']:.0f}ms")
            print(f"      四角: {np.round(res['keypoints'],1).tolist()}")
            print(f"      minConf: {res['conf'].min():.2f}")
            for i, row in enumerate(res["rows"]):
                print(f"        {i}: {row}")
        except Exception as exc:
            print(f"      识别失败: {exc}")
