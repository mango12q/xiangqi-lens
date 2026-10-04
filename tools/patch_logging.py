# -*- coding: utf-8 -*-
"""给 app_vision.py 的识别循环插入日志点。

单独写脚本而不是手工编辑：这些插入点分散在循环里，用锚点替换更可靠。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

TARGET = Path(__file__).resolve().parent.parent / "app_vision.py"

# (锚点原文, 替换为) —— 每处只在首次出现时替换
EDITS: list[tuple[str, str]] = [
    # 窗口状态异常
    ("""                if not win_ok:
                    if note != _last_state_note:
                        self.status.emit(note)
                        _last_state_note = note""",
     """                if not win_ok:
                    if note != _last_state_note:
                        self.status.emit(note)
                        self._log(f"窗口状态异常: {note}")
                        _last_state_note = note"""),

    # 抓帧失败
    ("""            if bgr is None:
                self.status.emit("抓帧失败（窗口已关闭？）")""",
     """            if bgr is None:
                self.status.emit("抓帧失败（窗口已关闭？）")
                self._log("抓帧失败")"""),

    # 识别异常
    ("""            except Exception as exc:
                self.status.emit(f"识别异常: {exc}")
                time.sleep(0.3)
                continue""",
     """            except Exception as exc:
                self.status.emit(f"识别异常: {exc}")
                self._log(f"识别异常: {exc}")
                time.sleep(0.3)
                continue"""),

    # 几何校验失败
    ("""            if not geo_ok:
                self.status.emit(f"未定位到棋盘：{geo_why}（请确认对局界面已打开）")""",
     """            if not geo_ok:
                self.status.emit(f"未定位到棋盘：{geo_why}（请确认对局界面已打开）")
                self._log(f"几何校验失败: {geo_why}")"""),

    # 演化校验挡下
    ("""                if reason == "drop":
                    self.status.emit(
                        f"局面变化无法解释，已挡下（{self._tracker.stats}）"
                        f" · minConf={res['conf'].min():.2f}")""",
     """                if reason == "drop":
                    self.status.emit(
                        f"局面变化无法解释，已挡下（{self._tracker.stats}）"
                        f" · minConf={res['conf'].min():.2f}")
                    self._log(f"演化校验挡下: {move}  minConf={res['conf'].min():.2f}")"""),

    # 局面校验失败
    ("""            if not pos_ok:
                self.status.emit(f"局面不合法已跳过：{pos_why}")""",
     """            if not pos_ok:
                self.status.emit(f"局面不合法已跳过：{pos_why}")
                self._log(f"局面校验失败: {pos_why}  FEN={fen}")"""),

    # 采纳并分析
    ("""            self._published = key
            t_recog = (time.perf_counter() - t0) * 1000
            self.status.emit(move_note or "引擎分析中…")""",
     """            self._published = key
            t_recog = (time.perf_counter() - t0) * 1000
            self._log(f"采纳局面({reason}) {fen}  识别 {t_recog:.0f}ms")
            self.status.emit(move_note or "引擎分析中…")"""),

    # 引擎异常
    ("""                self.error.emit(f"引擎异常，正在重启: {exc}")""",
     """                self.error.emit(f"引擎异常，正在重启: {exc}")
                self._log(f"引擎异常: {exc}")"""),
]


def main() -> int:
    src = TARGET.read_text(encoding="utf-8")
    applied = 0
    for old, new in EDITS:
        if new in src:
            print(f"  跳过（已存在）: {old.strip().splitlines()[0][:50]}")
            continue
        if old not in src:
            print(f"  [警告] 未找到锚点: {old.strip().splitlines()[0][:50]}")
            continue
        src = src.replace(old, new, 1)
        applied += 1
    print(f"应用了 {applied} 处编辑")

    try:
        ast.parse(src)
    except SyntaxError as exc:
        print(f"[失败] 语法错误: {exc}")
        return 1

    TARGET.write_text(src, encoding="utf-8")
    print("已写入，语法检查通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
