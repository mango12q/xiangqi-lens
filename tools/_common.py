# -*- coding: utf-8 -*-
"""tools/ 下各脚本的公共脚手架。

存在理由（三轮评审反复提到，这里一次收敛）：

* **44 个脚本各自抄了一遍** ``sys.path.insert`` 路径样板；
* **22 处**各自写了一遍窗口查找；
* 一批测试脚本在「没有对局窗口 / 没有模型与引擎」的环境下**硬失败** ——
  这会让「加 CI」直接变成 6 个假红。而**假红比没有 CI 更糟**：它会训练
  维护者忽略 CI 失败，CI 从此失去意义。

因此约定：**环境不具备 ≠ 代码回归**。这类情况一律走 :func:`skip`
（打印 ``[跳过]`` 并 **return 0**），由 ``tools/run_tests.py`` 汇总成
``PASS / SKIP / FAIL`` 三类。

用法::

    from _common import bootstrap, resolve_hwnd, skip, Checker

    bootstrap()                                   # 取代 sys.path.insert 样板
    hwnd = resolve_hwnd(args.hwnd, "JJ象棋")
    if not hwnd:
        return skip("未找到 JJ象棋 窗口", "先打开对局界面，或 --hwnd 指定句柄")
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent      # 仓库根目录

_BOOTSTRAPPED = False


def bootstrap() -> Path:
    """把仓库根目录与 vendor/ 插进 ``sys.path``（可重复调用，只生效一次）。

    取代各脚本里那段复制了 44 遍的样板。
    """
    global _BOOTSTRAPPED
    root = str(HERE)
    vendor = str(HERE / "vendor" / "xq_research")
    if not _BOOTSTRAPPED:
        for p in (vendor, root):
            if p not in sys.path:
                sys.path.insert(0, p)
        _BOOTSTRAPPED = True
    return HERE


# ---------------------------------------------------------------------------
# 跳过（环境不具备，不是回归）
# ---------------------------------------------------------------------------
def skip(reason: str, hint: str = "") -> int:
    """打印标准「跳过」块并返回 **0**（供 ``return skip(...)`` 使用）。

    返回 0 是关键：环境不具备不是代码回归，返回 1 会让 CI 变红。
    """
    print(f"  [跳过] {reason}")
    if hint:
        print(f"         {hint}")
    print("         （不算失败：环境不具备，不是代码回归）")
    return 0


def resolve_hwnd(hwnd_arg=None, keyword: str = "JJ象棋", min_size: int = 120):
    """解析目标窗口句柄；找不到返回 ``None``。

    :param hwnd_arg: ``--hwnd`` 传进来的值（字符串或 int），优先使用。
    :param keyword:  按标题关键字在候选窗口里找（默认 JJ象棋）。
    """
    bootstrap()
    if hwnd_arg:
        try:
            return int(str(hwnd_arg), 0)
        except Exception:
            return None
    try:
        from app_backend import ScreenSource
    except Exception:
        return None
    try:
        cands = [w for w in ScreenSource.list_windows(min_size) if keyword in w[1]]
    except Exception:
        return None
    return cands[0][0] if cands else None


def find_any_window(min_size: int = 120):
    """找一个可用的顶层窗口（测试用，不关心是不是棋局）。找不到返回 None。"""
    bootstrap()
    try:
        from app_backend import ScreenSource
        ws = ScreenSource.list_windows(min_size)
        return ws[0][0] if ws else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 资源可用性（模型 / 引擎不在仓库里，CI 上必然没有）
# ---------------------------------------------------------------------------
def have_vision() -> bool:
    """识别模型是否齐备（``xq_research/hf_model/...`` 两个 onnx）。"""
    bootstrap()
    try:
        from app_backend import CLS_ONNX, POSE_ONNX
        return POSE_ONNX.is_file() and CLS_ONNX.is_file()
    except Exception:
        return False


def have_engine() -> bool:
    """Pikafish 可执行文件与 nnue 权重是否齐备。"""
    bootstrap()
    try:
        from app_backend import ENGINE_EXE
        return ENGINE_EXE.is_file()
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 统一的断言与汇总
# ---------------------------------------------------------------------------
class Checker:
    """统一 ``[通过] / [失败]`` 输出与退出码（取代各脚本自带的 check + 全局计数）。"""

    def __init__(self, title: str = "") -> None:
        self.title = title
        self.passed = 0
        self.failed = 0
        if title:
            print(title)

    def check(self, name: str, cond: bool, detail: str = "") -> bool:
        if cond:
            self.passed += 1
            print(f"  [通过] {name}")
        else:
            self.failed += 1
            print(f"  [失败] {name}" + (f"  {detail}" if detail else ""))
        return bool(cond)

    def done(self) -> int:
        """打印汇总并返回退出码（0 = 全通过）。"""
        print()
        print(f"通过 {self.passed} / 失败 {self.failed}")
        return 0 if self.failed == 0 else 1
