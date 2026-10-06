# -*- coding: utf-8 -*-
"""窗口候选过滤规则测试（纯函数，无需真实窗口）。

覆盖 ``app_backend.is_candidate_window`` 的三类判据 —— 类名 / 标题 / 进程，
以及它对本次故障根因窗口（``MS_WebcheckMonitor``）的剔除。

用法::

    python tools/test_window_filter.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import app_backend as B

PASS = FAIL = 0


def check(name: str, got, want) -> None:
    global PASS, FAIL
    if got == want:
        PASS += 1
        print(f"  [OK]   {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}: got={got!r} want={want!r}")


def main() -> int:
    # hwnd=0 → _window_pid 得 0、_proc_name 拿不到进程名，只走 class/title 分支
    print("--- 类名判据（系统壳 / 通用控件 / 辅助窗口）---")
    check("MS_WebcheckMonitor 被剔除（本次根因窗口）",
          B.is_candidate_window(0, "MS_WebcheckMonitor", "MS_WebcheckMonitor")[0], False)
    check("Progman 被剔除",
          B.is_candidate_window(0, "Program Manager", "Progman")[0], False)
    check("WorkerW 被剔除",
          B.is_candidate_window(0, "", "WorkerW")[0], False)
    check("ApplicationFrameWindow 被剔除",
          B.is_candidate_window(0, "设置", "ApplicationFrameWindow")[0], False)
    check("CEF-OSC-WIDGET 被剔除（NVIDIA Overlay）",
          B.is_candidate_window(0, "NVIDIA GeForce Overlay", "CEF-OSC-WIDGET")[0], False)
    check("Static 控件类被剔除",
          B.is_candidate_window(0, "TCLS_CORE_WND_05BE9B58", "Static")[0], False)
    check("TWINCONTROL 被剔除",
          B.is_candidate_window(0, "EOG_SUMMARIZE_WINDOW", "TWINCONTROL")[0], False)
    check("类名子串 atl: 被剔除",
          B.is_candidate_window(0, "wegame_env", "ATL:00007FF7D56C9450")[0], False)
    check("类名子串 trayicon 被剔除",
          B.is_candidate_window(0, "WxTrayIconMessageWindow",
                                "Qt51514WxTrayIconMessageWindowClass")[0], False)
    check("类名子串 notification 被剔除",
          B.is_candidate_window(0, "位置通知", "LOCATIONNOTIFICATION")[0], False)
    check("类名子串 uuremoteclass 被剔除",
          B.is_candidate_window(0, "HiddenWindow", "UURemoteClass_F85F8A7B")[0], False)

    print("--- 标题判据（本程序 / 分析工具自身）---")
    check("本程序窗口被剔除",
          B.is_candidate_window(0, "XiangQiLens v0.4.1 · 象棋识别分析",
                                "Qt6112QWindowIcon")[0], False)
    check("WorkBuddy 窗口被剔除",
          B.is_candidate_window(0, "WorkBuddy AI", "Chrome_WidgetWin_1")[0], False)

    print("--- 真实棋局窗口必须保留（防误杀）---")
    check("天天象棋保留",
          B.is_candidate_window(0, "天天象棋", "Chrome_WidgetWin_0")[0], True)
    check("JJ象棋保留",
          B.is_candidate_window(0, "JJ象棋", "Chrome_WidgetWin_0")[0], True)
    check("中国象棋保留",
          B.is_candidate_window(0, "中国象棋", "Chrome_WidgetWin_1")[0], True)
    check("微信主窗口保留",
          B.is_candidate_window(0, "微信", "Qt51514QWindowIcon")[0], True)
    check("WeGame 保留",
          B.is_candidate_window(0, "WeGame", "CefTopWindow")[0], True)
    check("普通记事本保留（交打分逻辑决定不选它）",
          B.is_candidate_window(0, "未命名 - 记事本", "Notepad")[0], True)

    print("--- 进程判据（monkeypatch _proc_name）---")
    orig_proc = B._proc_name
    try:
        for proc, want in (("explorer.exe", False), ("TextInputHost.exe", False),
                           ("nvcontainer.exe", False), ("rail.exe", False),
                           ("WeChatAppEx.exe", True), ("JJ.exe", True)):
            B._proc_name = lambda pid, _p=proc: _p
            check(f"proc={proc}",
                  B.is_candidate_window(0, "某窗口", "SomeCustomClass")[0], want)
    finally:
        B._proc_name = orig_proc

    print("--- 自身进程判据（monkeypatch _window_pid）---")
    orig_pid = B._window_pid
    try:
        B._window_pid = lambda h: os.getpid()
        check("自身进程的窗口被剔除",
              B.is_candidate_window(999, "随便什么标题", "SomeCustomClass")[0], False)
    finally:
        B._window_pid = orig_pid

    print("--- _proc_name 真实可用（ctypes 实现，不依赖 psutil）---")
    own = B._proc_name(os.getpid())
    check("能取到自身进程名", bool(own), True)
    check("进程名形如 *.exe", own.lower().endswith(".exe"), True)
    check("无效 pid 返回空串", B._proc_name(0), "")
    check("同一 pid 命中缓存", B._proc_name(os.getpid()), own)

    print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
