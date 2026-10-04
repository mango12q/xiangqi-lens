"""中国象棋棋盘表示、FEN 转换与规则校验。

坐标约定（内部统一使用）
------------------------------------------------------------------
    row 0  = FEN 第 1 行 = 黑方底线（屏幕上方，若红方在下方）
    row 9  = FEN 第 10 行 = 红方底线（屏幕下方）
    col 0..8 = 从左到右，对应 FEN 的 a..i 列

ICCS / UCCI 走法坐标（引擎使用）
------------------------------------------------------------------
    格式 "h2e2"，file 为 a-i，rank 为 0-9，其中 rank 0 是红方底线。
    换算：file = chr(ord('a') + col)，rank = 9 - row

FEN 字符体系
------------------------------------------------------------------
    红方（大写）: K 帅  A 仕  B 相  N 马  R 车  C 炮  P 兵
    黑方（小写）: k 将  a 士  b 象  n 马  r 车  c 炮  p 卒
    空格用 '.' 表示（内部网格），FEN 字符串里用数字压缩
"""

from __future__ import annotations

from typing import Iterator

# --------------------------------------------------------------------------
# 常量
# --------------------------------------------------------------------------

ROWS = 10
COLS = 9
EMPTY = "."

# FEN 字符 -> 中文名
PIECE_NAMES: dict[str, str] = {
    "K": "帅", "A": "仕", "B": "相", "N": "马", "R": "车", "C": "炮", "P": "兵",
    "k": "将", "a": "士", "b": "象", "n": "马", "r": "车", "c": "炮", "p": "卒",
}

# 中文名 -> FEN 字符（红方优先）
NAME_TO_PIECE: dict[str, str] = {
    "帅": "K", "仕": "A", "相": "B", "马": "N", "车": "R", "炮": "C", "兵": "P",
    "将": "k", "士": "a", "象": "b", "馬": "N", "車": "R", "砲": "C",
    "卒": "p", "馬": "n", "象": "b",
}

RED_PIECES = frozenset("KABNRCP")
BLACK_PIECES = frozenset("kabnrcp")
ALL_PIECES = RED_PIECES | BLACK_PIECES

# 15 个非空类别，用于识别模型输出层（索引 0 保留给空格）
CLASS_ORDER: tuple[str, ...] = (".", "K", "A", "B", "N", "R", "C", "P",
                                "k", "a", "b", "n", "r", "c", "p")
CLASS_INDEX: dict[str, int] = {c: i for i, c in enumerate(CLASS_ORDER)}

# 初始局面（标准开局）
START_FEN = "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR w - - 0 1"


def initial_board() -> list[list[str]]:
    """返回标准开局网格。"""
    return [list(r) for r in [
        "rnbakabnr",
        ".........",
        ".c.....c.",
        "p.p.p.p.p",
        ".........",
        ".........",
        "P.P.P.P.P",
        ".C.....C.",
        ".........",
        "RNBAKABNR",
    ]]


# --------------------------------------------------------------------------
# 坐标换算
# --------------------------------------------------------------------------

def rc_to_iccs(row: int, col: int) -> str:
    """内部 (row, col) -> ICCS 坐标，如 (9, 7) -> 'h0'（红方右车）。"""
    return f"{chr(ord('a') + col)}{9 - row}"


def iccs_to_rc(square: str) -> tuple[int, int]:
    """ICCS 坐标 -> 内部 (row, col)。"""
    if len(square) != 2:
        raise ValueError(f"非法坐标: {square!r}")
    col = ord(square[0].lower()) - ord("a")
    rank = int(square[1])
    if not (0 <= col < COLS and 0 <= rank < ROWS):
        raise ValueError(f"坐标越界: {square!r}")
    return 9 - rank, col


def move_to_iccs(move: tuple[tuple[int, int], tuple[int, int]]) -> str:
    """((fr, fc), (tr, tc)) -> 'h2e2'"""
    (fr, fc), (tr, tc) = move
    return rc_to_iccs(fr, fc) + rc_to_iccs(tr, tc)


def iccs_to_move(iccs: str) -> tuple[tuple[int, int], tuple[int, int]]:
    """'h2e2' -> ((fr, fc), (tr, tc))"""
    if len(iccs) != 4:
        raise ValueError(f"非法走法: {iccs!r}")
    return iccs_to_rc(iccs[0:2]), iccs_to_rc(iccs[2:4])


# --------------------------------------------------------------------------
# 棋盘
# --------------------------------------------------------------------------

class Board:
    """10x9 中国象棋棋盘。

    grid[row][col] 为 '.' 或 FEN 字符。所有对外接口只操作这个网格。
    """

    __slots__ = ("grid",)

    def __init__(self, grid: list[list[str]] | None = None):
        if grid is None:
            self.grid = initial_board()
        else:
            self.grid = [list(r) for r in grid]
        self._validate_shape()

    def _validate_shape(self) -> None:
        if len(self.grid) != ROWS or any(len(r) != COLS for r in self.grid):
            raise ValueError(f"棋盘尺寸必须是 {ROWS}x{COLS}")

    # ---------------- 基础访问 ----------------

    def get(self, row: int, col: int) -> str:
        return self.grid[row][col]

    def set(self, row: int, col: int, piece: str) -> None:
        self.grid[row][col] = piece

    def copy(self) -> "Board":
        return Board(self.grid)

    def iter_pieces(self) -> Iterator[tuple[int, int, str]]:
        for r in range(ROWS):
            for c in range(COLS):
                p = self.grid[r][c]
                if p != EMPTY:
                    yield r, c, p

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for _, _, p in self.iter_pieces():
            out[p] = out.get(p, 0) + 1
        return out

    def side_to_move_from_fen(self) -> str:
        return "w"

    # ---------------- FEN ----------------

    def to_fen(self, side_to_move: str = "w", halfmove: int = 0,
               fullmove: int = 1) -> str:
        """生成标准象棋 FEN（含 side/halfmove/fullmove 字段）。"""
        rows: list[str] = []
        for r in range(ROWS):
            line = ""
            empty = 0
            for c in range(COLS):
                p = self.grid[r][c]
                if p == EMPTY:
                    empty += 1
                else:
                    if empty:
                        line += str(empty)
                        empty = 0
                    line += p
            if empty:
                line += str(empty)
            rows.append(line)
        return f"{'/'.join(rows)} {side_to_move} - - {halfmove} {fullmove}"

    @classmethod
    def from_fen(cls, fen: str) -> "Board":
        """解析 FEN，忽略 side/halfmove/fullmove 之外的状态字段。"""
        parts = fen.strip().split()
        if not parts:
            raise ValueError("空 FEN")
        board_part = parts[0]
        rows = board_part.split("/")
        if len(rows) != ROWS:
            raise ValueError(f"FEN 行数必须为 {ROWS}，实际 {len(rows)}")

        grid: list[list[str]] = []
        for r, row in enumerate(rows):
            line: list[str] = []
            for ch in row:
                if ch.isdigit():
                    line.extend([EMPTY] * int(ch))
                else:
                    if ch not in ALL_PIECES:
                        raise ValueError(f"FEN 第 {r + 1} 行含非法字符 {ch!r}")
                    line.append(ch)
            if len(line) != COLS:
                raise ValueError(f"FEN 第 {r + 1} 行展开后为 {len(line)} 列，应为 {COLS}")
            grid.append(line)
        return cls(grid)

    # ---------------- 校验（用于过滤识别错误） ----------------

    def check_sanity(self, strict: bool = False) -> list[str]:
        """返回局面异常描述列表；空列表表示通过基本校验。

        这些规则用于过滤视觉识别的误判帧 —— 一个识别错误的局面
        几乎必然违反其中某一条。
        """
        problems: list[str] = []
        counts = self.counts()

        def n(p: str) -> int:
            return counts.get(p, 0)

        # 双方各恰好一个将/帅
        if n("K") != 1:
            problems.append(f"红方帅数量为 {n('K')}，应为 1")
        if n("k") != 1:
            problems.append(f"黑方将数量为 {n('k')}，应为 1")

        # 各兵种上限
        limits = {"A": 2, "B": 2, "N": 2, "R": 2, "C": 2, "P": 5}
        for red_piece, cap in limits.items():
            black_piece = red_piece.lower()
            if n(red_piece) > cap:
                problems.append(f"红方 {PIECE_NAMES[red_piece]} 数量 {n(red_piece)} 超过上限 {cap}")
            if n(black_piece) > cap:
                problems.append(f"黑方 {PIECE_NAMES[black_piece]} 数量 {n(black_piece)} 超过上限 {cap}")

        # 棋子总数不超过 32
        total = sum(n(p) for p in ALL_PIECES)
        if total > 32:
            problems.append(f"棋子总数 {total} 超过 32")

        # 将帅不能照面（同一列且中间无子）
        if n("K") == 1 and n("k") == 1:
            kr = kc = br = bc = None
            for r in range(ROWS):
                for c in range(COLS):
                    p = self.grid[r][c]
                    if p == "K":
                        kr, kc = r, c
                    elif p == "k":
                        br, bc = r, c
            if kc == bc:
                between = [self.grid[r][kc] for r in range(min(kr, br) + 1, max(kr, br))]
                if all(x == EMPTY for x in between):
                    problems.append("将帅照面（同一列且中间无子）")

        # 将/帅必须在九宫内
        for r in range(ROWS):
            for c in range(COLS):
                p = self.grid[r][c]
                if p == "K" and not (7 <= r <= 9 and 3 <= c <= 5):
                    problems.append(f"红方帅在九宫外 ({r},{c})")
                if p == "k" and not (0 <= r <= 2 and 3 <= c <= 5):
                    problems.append(f"黑方将在九宫外 ({r},{c})")

        if strict:
            # 进一步：兵/卒不能出现在本方底线附近（简单合法性）
            for r in range(ROWS):
                for c in range(COLS):
                    if self.grid[r][c] == "P" and r <= 2:
                        problems.append(f"红兵出现在第 {r} 行（已过河过深，可疑）")
                    if self.grid[r][c] == "p" and r >= 7:
                        problems.append(f"黑卒出现在第 {r} 行（已过河过深，可疑）")
        return problems

    def is_sane(self) -> bool:
        return not self.check_sanity()

    # ---------------- 走子 ----------------

    def apply_move(self, move: tuple[tuple[int, int], tuple[int, int]]) -> str:
        """执行走法，返回被吃棋子（'.' 表示空格）。"""
        (fr, fc), (tr, tc) = move
        piece = self.grid[fr][fc]
        if piece == EMPTY:
            raise ValueError(f"起点无子: {rc_to_iccs(fr, fc)}")
        captured = self.grid[tr][tc]
        self.grid[tr][tc] = piece
        self.grid[fr][fc] = EMPTY
        return captured

    def apply_iccs(self, iccs: str) -> str:
        return self.apply_move(iccs_to_move(iccs))

    # ---------------- 展示 ----------------

    def to_ascii(self) -> str:
        """终端可视化，辨识度高于 FEN 字符串。"""
        lines = ["   " + " ".join(chr(ord('a') + c) for c in range(COLS))]
        for r in range(ROWS):
            cells = []
            for c in range(COLS):
                p = self.grid[r][c]
                cells.append(PIECE_NAMES.get(p, "·"))
            lines.append(f"{9 - r}  " + " ".join(cells))
        return "\n".join(lines)

    def to_unicode(self) -> str:
        """使用中文棋子名，红黑用符号区分，便于快速扫视。"""
        out = []
        for r in range(ROWS):
            row = []
            for c in range(COLS):
                p = self.grid[r][c]
                if p == EMPTY:
                    row.append("＋")
                elif p.isupper():
                    row.append(PIECE_NAMES[p])
                else:
                    row.append(PIECE_NAMES[p])
            out.append("".join(row))
        return "\n".join(out)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Board) and self.grid == other.grid

    def __repr__(self) -> str:
        return f"Board({self.to_fen()!r})"


# --------------------------------------------------------------------------
# 局面差分
# --------------------------------------------------------------------------

def diff_squares(a: Board, b: Board) -> list[tuple[int, int]]:
    """返回两局面之间内容不同的格子列表。"""
    out = []
    for r in range(ROWS):
        for c in range(COLS):
            if a.grid[r][c] != b.grid[r][c]:
                out.append((r, c))
    return out


def infer_move(prev: Board, curr: Board) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """从相邻两帧局面推断走法。

    正常一步棋只会造成 2 处变化（起点清空、终点落子）；
    吃子时起点变化，终点由对方子变我方子，仍是 2 处变化。
    返回 None 表示变化不是一步棋（识别噪声或多次落子）。
    """
    changed = diff_squares(prev, curr)
    if len(changed) != 2:
        return None

    (r1, c1), (r2, c2) = changed
    # 找出哪一格是"变空"，那一格是起点
    if curr.grid[r1][c1] == EMPTY and prev.grid[r1][c1] != EMPTY:
        return (r1, c1), (r2, c2)
    if curr.grid[r2][c2] == EMPTY and prev.grid[r2][c2] != EMPTY:
        return (r2, c2), (r1, c1)
    return None


def describe_move(prev: Board, move: tuple[tuple[int, int], tuple[int, int]]) -> str:
    """生成可读的走法描述，例如 '炮(C) h2→e2 吃 卒'。"""
    (fr, fc), (tr, tc) = move
    piece = prev.grid[fr][fc]
    target = prev.grid[tr][tc]
    name = PIECE_NAMES.get(piece, "?")
    side = "红" if piece.isupper() else "黑"
    txt = f"{side}{name} {rc_to_iccs(fr, fc)}→{rc_to_iccs(tr, tc)}"
    if target != EMPTY:
        txt += f" 吃{PIECE_NAMES.get(target, '?')}"
    return txt
