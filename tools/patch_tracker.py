# -*- coding: utf-8 -*-
"""把 app_backend.py 里的 BoardTracker 类整体替换为带轮次管理的新版本。

用脚本替换而不是手工编辑，避免大段文本失配。
"""
from __future__ import annotations

from pathlib import Path

# 相对本脚本定位仓库，不再硬编码本机绝对路径
TARGET = Path(__file__).resolve().parent.parent / "app_backend.py"

NEW_CLASS = '''
class BoardTracker:
    """过滤单帧误识别，同时维护轮次。

    动机
    ----------------------------------------------------------------
    识别模型偶尔会把一个棋子读错（例如把红傌读成红炮）。这种错误一旦
    被采信，整局分析都会跑偏。但它有个可利用的性质：

        **正确的局面演化，必然能由上一局面走一步合法着法得到。**

    因此对每个新识别出的局面做三重校验，满足其一才采信：

    1. 能由已确认局面**一步合法着法**解释 —— 正常走子，立即采信
    2. 与上一帧识别结果一致（连续两帧）—— 稳定的新局面（例如刚接手
       中盘、或用户跳过了中间几步）
    3. 连续 ``force_after`` 帧都无法解释 —— 兜底采信，避免永久卡死，
       同时给出警告

    不满足任一条的局面只作为"候选"暂存、不送引擎 —— 单帧噪声就挡在这里。

    轮次
    ----------------------------------------------------------------
    校验着法合法性必须结合轮次：同一个 ICCS 着法，红方轮次下可能合法、
    黑方轮次下必然非法。因此内部维护 ``side_to_move``：

    * 首个局面取构造参数 ``first_side``（默认红先）
    * 每采信一步走子后翻转
    * 引擎给出着法后可用 :meth:`correct_by_engine_move` 反推校正 ——
      引擎只从当前行棋方视角给着法，若其着法仅对另一方合法，说明轮次设错
    """

    def __init__(self, force_after: int = 6, sanity_check: bool = True,
                 first_side: str = "w") -> None:
        self.force_after = max(2, force_after)
        self.sanity_check = sanity_check
        self.side_to_move = first_side if first_side in ("w", "b") else "w"
        self.stable_fen: str | None = None      # 已采信局面（仅棋盘段）
        self.pending_fen: str | None = None     # 待确认候选
        self.pending_count = 0
        self.dropped = 0                        # 被挡掉的帧数
        self.forced = 0                         # 兜底采信次数
        self.corrections = 0                    # 轮次被校正次数
        self.moves: list[str] = []              # 已采信的着法序列

    # ---------------- 轮次 ----------------

    def toggle_side(self) -> str:
        self.side_to_move = "b" if self.side_to_move == "w" else "w"
        return self.side_to_move

    @property
    def side_cn(self) -> str:
        return "红" if self.side_to_move == "w" else "黑"

    def full_fen(self) -> str | None:
        """已采信局面 + 当前轮次 的完整 FEN。"""
        if self.stable_fen is None:
            return None
        return fen_with_side(self.stable_fen, self.side_to_move)

    def _explain(self, prev: str, cur: str) -> str | None:
        """cur 能否由 prev 走一步合法着法得到？返回该着法或 None。

        先用纯格子差分拿候选着法，再用 cchess 校验合法性 —— 校验时必须
        带上**当前轮次**，否则黑方着法会被判非法。
        """
        mv = diff_single_move(prev, cur)
        if mv is None:
            return None
        return mv if is_legal_move(fen_with_side(prev, self.side_to_move), mv) else None

    # ---------------- 主入口 ----------------

    def update(self, fen: str) -> tuple[bool, str, str, str]:
        """喂入一帧识别结果（只取棋盘段）。

        返回 ``(是否采信, 原因, 着法, 轮次)``。
        原因：``初始局面 / 走子 / 稳定新局面 / 强制采信 / unchanged / drop``
        """
        board_only = fen.split()[0]

        if self.sanity_check:
            try:
                from cchess import ChessBoard
                ChessBoard(board_only)
            except Exception as exc:
                self.dropped += 1
                return False, "drop", f"局面非法: {exc}", self.side_to_move

        if self.stable_fen is None:
            self.stable_fen = board_only
            self.pending_fen = None
            self.pending_count = 0
            return True, "初始局面", "", self.side_to_move

        if board_only == self.stable_fen:
            self.pending_fen = None
            self.pending_count = 0
            return False, "unchanged", "", self.side_to_move

        mv = self._explain(self.stable_fen, board_only)
        if mv:
            self.stable_fen = board_only
            self.pending_fen = None
            self.pending_count = 0
            self.moves.append(mv)
            self.toggle_side()
            return True, "走子", mv, self.side_to_move

        if self.pending_fen is not None and board_only == self.pending_fen:
            self.pending_count += 1
            if self.pending_count >= 2:
                self.stable_fen = board_only
                self.pending_fen = None
                self.pending_count = 0
                return True, "稳定新局面", "", self.side_to_move
        else:
            self.pending_fen = board_only
            self.pending_count = 1

        if self.pending_count >= self.force_after:
            self.stable_fen = board_only
            self.pending_fen = None
            self.pending_count = 0
            self.forced += 1
            return True, "强制采信", "", self.side_to_move

        self.dropped += 1
        return False, "drop", "", self.side_to_move

    def correct_by_engine_move(self, bestmove: str) -> bool:
        """用引擎着法的合法性反推轮次。True 表示轮次被修正过。"""
        if not bestmove or bestmove in ("(none)", "0000") or self.stable_fen is None:
            return False
        try:
            if is_legal_move(fen_with_side(self.stable_fen, self.side_to_move), bestmove):
                return False
            other = "b" if self.side_to_move == "w" else "w"
            if is_legal_move(fen_with_side(self.stable_fen, other), bestmove):
                self.side_to_move = other
                self.corrections += 1
                return True
        except Exception:
            pass
        return False

    def reset(self) -> None:
        self.stable_fen = None
        self.pending_fen = None
        self.pending_count = 0
        self.dropped = 0
        self.forced = 0
        self.corrections = 0
        self.moves.clear()

    @property
    def stats(self) -> str:
        s = f"已挡 {self.dropped} 帧"
        if self.forced:
            s += f" · 强制采信 {self.forced} 次"
        if self.corrections:
            s += f" · 轮次校正 {self.corrections} 次"
        return s
'''


def main() -> int:
    src = TARGET.read_text(encoding="utf-8")

    marker = "class BoardTracker:"
    start = src.find(marker)
    if start < 0:
        print("[失败] 找不到 class BoardTracker")
        return 1

    # 类结束位置：下一个顶层定义（class/def 或注释分隔块）
    rest = src[start + len(marker):]
    end_rel = None
    for cand in ("\n# ---------------------------------------------------------------------------\n# 自检",
                 "\ndef selftest("):
        p = rest.find(cand)
        if p >= 0 and (end_rel is None or p < end_rel):
            end_rel = p
    if end_rel is None:
        print("[失败] 找不到 BoardTracker 的结束位置")
        return 1
    end = start + len(marker) + end_rel

    old = src[start:end]
    print(f"替换区间: 字符 {start}..{end}  (长度 {len(old)})")
    print(f"原类首行: {old.splitlines()[0]}")
    print(f"原类末行: {old.rstrip().splitlines()[-1]}")

    new_src = src[:start] + NEW_CLASS.strip() + "\n\n" + src[end:].lstrip("\n")
    TARGET.write_text(new_src, encoding="utf-8")
    print(f"已写入，新文件 {len(new_src)} 字符")

    import ast
    try:
        ast.parse(new_src)
        print("语法检查: OK")
    except SyntaxError as exc:
        print(f"[失败] 语法错误: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
