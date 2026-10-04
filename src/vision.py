"""棋盘识别层：从截图中还原出 10x9 的棋子矩阵。

算法总览
------------------------------------------------------------------
1. **棋盘定位**：由 ``BoardGeometry.region`` 给出 9x10 个交叉点的外框，
   交叉点按等分计算（见 ``capture.BoardGeometry``）。
2. **有无棋子 + 红黑判定**：在每个交叉点取一个圆形 patch，比较
   "字区（中心半径 r_in 内）" 与 "边缘环（r_in..r_out）" 的亮度。
   棋子是浅色木盘 + 深色字，字区明显更暗；棋盘底色则整体平坦。

   * 空点：字区与边缘环亮度接近（contrast 小），且没有深色笔画
   * 有子：字区存在深色笔画（min_val 低）

   红黑判定用 **dark_redness**：取字区中最暗的 25% 像素，算
   ``红通道 - (绿+蓝)/2`` 的均值。红字约 +90~+110，黑字约 0~+5，
   棋盘底色约 0。实测分离度为 89~108 vs 1~5，几乎没有重叠。

3. **兵种判定**：把字区的深色笔画二值化、裁剪到字形外框、归一化到
   固定尺寸，然后与模板库做归一化互相关（NCC）。模板库首次由
   ``build_templates`` 从一张真实截图自动生成，之后可复用。

为什么不用 YOLO
------------------------------------------------------------------
棋盘是**固定网格**，落子点只有 90 个且位置已知，目标检测要解决的
"棋子在哪里"这个问题在本场景里已经有解析解。剩下只有分类问题，
用几何 + 模板匹配就足够，而且：
* 无需训练数据、无需 GPU、启动即用
* 对皮肤/配色变化的适应通过重新采集模板完成，比重新训练快得多
* 可解释：每个判断都能回溯到具体特征值，便于排查误判
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from .capture import BoardGeometry, Region
from .xiangqi import COLS, EMPTY, ROWS, Board, PIECE_NAMES

# --------------------------------------------------------------------------
# 参数
# --------------------------------------------------------------------------


@dataclass
class VisionConfig:
    """识别参数。默认值来自 JJ 象棋小程序截图的实测校准。

    占用判据的设计依据（实测数据）
    ----------------------------------------------------------------
    黑字棋子：字区明显比边缘环暗 → contrast 为正且较大（实测 +8~+51），
              且 min_val 很低（实测 32~36）。
    红字棋子：红字亮度被红色通道拉高，用 max 通道衡量时字区反而比
              边缘环**亮** → contrast 为负（实测 -6~-16），min_val 也
              偏高（实测 116~140）。
              因此红方**不能**用明暗判据，必须靠 dark_redness 识别。
    空  点：contrast 接近 0（实测 -2~+8），dark_redness 接近 0。
    """

    # 采样半径，以交叉点间距为单位
    r_in_ratio: float = 0.27     # 字区半径
    r_out_ratio: float = 0.45    # 采样外半径（略小于半个格距）

    # ---- 红方棋子判据：dark_redness（最暗 25% 像素的红通道优势）----
    # 实测红子 88~108，黑子与空点 -3.3~+4.7，取 45 作为分界，余量充足
    red_threshold: float = 45.0

    # ---- 黑方棋子判据：暗笔画 ----
    black_redness_max: float = 15.0   # 红度低于此值才可能是黑子
    black_dark_val_max: float = 70.0  # 字区最暗 10% 像素亮度上限（实测 32~36）
    black_contrast_min: float = 10.0  # 或字区比边缘环暗这么多（实测 +8~+51）

    # 字形归一化尺寸
    glyph_size: int = 32

    # 模板匹配阈值：低于此分数视为不可信，需要用户确认
    match_score_min: float = 0.45


# --------------------------------------------------------------------------
# 棋子模板
# --------------------------------------------------------------------------


@dataclass
class PieceTemplate:
    """一个兵种的模板：字形图 + 统计信息。"""

    piece: str                    # FEN 字符，如 'N' / 'n'
    glyph: np.ndarray             # float32, (glyph_size, glyph_size), 已归一化
    samples: int = 1
    dark_redness: float = 0.0

    def to_dict(self) -> dict:
        return {
            "piece": self.piece,
            "glyph": self.glyph.round(4).flatten().tolist(),
            "shape": list(self.glyph.shape),
            "samples": self.samples,
            "dark_redness": round(self.dark_redness, 2),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PieceTemplate":
        shape = tuple(d.get("shape", [32, 32]))
        return cls(piece=d["piece"],
                   glyph=np.asarray(d["glyph"], dtype=np.float32).reshape(shape),
                   samples=int(d.get("samples", 1)),
                   dark_redness=float(d.get("dark_redness", 0.0)))


class TemplateLibrary:
    """不同棋盘皮肤对应一套模板。用 profile 名区分。"""

    def __init__(self, profile: str = "default") -> None:
        self.profile = profile
        self.templates: dict[str, list[PieceTemplate]] = {}

    def add(self, tpl: PieceTemplate) -> None:
        self.templates.setdefault(tpl.piece, []).append(tpl)

    def get(self, piece: str) -> list[PieceTemplate]:
        return self.templates.get(piece, [])

    @property
    def pieces(self) -> list[str]:
        return sorted(self.templates)

    def all_templates(self) -> list[PieceTemplate]:
        out: list[PieceTemplate] = []
        for v in self.templates.values():
            out.extend(v)
        return out

    # ---------------- 持久化 ----------------

    @staticmethod
    def default_dir() -> Path:
        return Path(__file__).resolve().parent.parent / "data" / "templates"

    def save(self, path: Path | None = None) -> Path:
        p = path or (self.default_dir() / f"{self.profile}.json")
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "profile": self.profile,
            "templates": [t.to_dict() for t in self.all_templates()],
        }
        p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return p

    @classmethod
    def load(cls, path: Path) -> "TemplateLibrary":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        lib = cls(profile=data.get("profile", Path(path).stem))
        for d in data.get("templates", []):
            lib.add(PieceTemplate.from_dict(d))
        return lib

    def merge(self, other: "TemplateLibrary") -> None:
        for t in other.all_templates():
            self.add(t)


# --------------------------------------------------------------------------
# 网格精化
# --------------------------------------------------------------------------


def _occupied_mask(img_bgr: np.ndarray,
                   config: "VisionConfig") -> np.ndarray:
    """生成"疑似棋子"掩码：红度高的像素 + 深色笔画像素。

    红棋子靠色度就能提取（木色底盘的红度接近 0），黑棋子靠暗色提取。
    两者合并后基本只留下棋子区域，棋盘底色与网格线都被滤掉。
    """
    b = img_bgr[:, :, 0].astype(np.float32)
    g = img_bgr[:, :, 1].astype(np.float32)
    r = img_bgr[:, :, 2].astype(np.float32)
    redness = r - (g + b) / 2.0
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    red_part = (redness > 40).astype(np.uint8)
    dark_part = (gray < config.black_dark_val_max).astype(np.uint8)
    mask = cv2.bitwise_or(red_part, dark_part)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE,
                            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9)))
    return mask


def align_grid(img_bgr: np.ndarray, region: Region,
               config: "VisionConfig | None" = None,
               rows: int = ROWS, cols: int = COLS,
               search: int = 26) -> tuple[Region, float]:
    """用图像证据精化交叉点网格，返回 (精化后的 Region, 对齐质量 0..1)。

    动机
    ----------------------------------------------------------------
    仅靠颜色阈值求棋盘外接矩形，边界会被棋子、装饰、光照影响，得到的
    间距可能有 2~3px 误差；误差在第 8 列会累积到 16px 以上，足以让
    采样框滑出棋子中心，造成整行误判。

    方法
    ----------------------------------------------------------------
    1. 提取"疑似棋子"掩码（红子靠色度、黑子靠暗度）。
    2. 对掩码按行/列做投影。若网格对齐，棋子中心应恰好落在投影的
       局部峰值上。
    3. 穷举原点偏移 (ox, oy) 与间距 (dx, dy)，用**掩码在网格点上的
       采样值之和**作为评分 —— 对齐时每个交叉点都落在棋子中心，
       得分最高。
    4. 返回得分最高的网格。搜索范围默认 ±26px 原点、间距 ±4%。

    这样网格是由"棋子实际在哪"决定的，而不是由棋盘外框决定的。
    """
    cfg = config or VisionConfig()
    h, w = img_bgr.shape[:2]
    mask = _occupied_mask(img_bgr, cfg).astype(np.float32)
    mask = cv2.GaussianBlur(mask, (5, 5), 0)

    base_dx = region.w / (cols - 1)
    base_dy = region.h / (rows - 1)

    def score(ox: float, oy: float, dx: float, dy: float) -> float:
        xs = np.rint(ox + np.arange(cols) * dx).astype(np.int32)
        ys = np.rint(oy + np.arange(rows) * dy).astype(np.int32)
        if xs[0] < 0 or ys[0] < 0 or xs[-1] >= w or ys[-1] >= h:
            return -1.0
        return float(mask[np.ix_(ys, xs)].mean())

    best = (region.x, region.y, base_dx, base_dy)
    best_score = score(*best)

    # 两轮由粗到细
    for round_scale in (3.0, 1.0):
        ox0, oy0, dx0, dy0 = best
        ox_range = np.arange(ox0 - search / round_scale, ox0 + search / round_scale + 0.1,
                             max(1.0, 2.0 / round_scale))
        oy_range = np.arange(oy0 - search / round_scale, oy0 + search / round_scale + 0.1,
                             max(1.0, 2.0 / round_scale))
        dx_range = np.linspace(dx0 * 0.96, dx0 * 1.04, 9 if round_scale > 1 else 5)
        dy_range = np.linspace(dy0 * 0.96, dy0 * 1.04, 9 if round_scale > 1 else 5)
        for ox in ox_range:
            for oy in oy_range:
                for dx in dx_range:
                    for dy in dy_range:
                        s = score(ox, oy, dx, dy)
                        if s > best_score:
                            best_score = s
                            best = (float(ox), float(oy), float(dx), float(dy))

    ox, oy, dx, dy = best
    refined = Region(int(round(ox)), int(round(oy)),
                     int(round(dx * (cols - 1))), int(round(dy * (rows - 1))))
    # 对齐质量：理想情况下每个交叉点都命中棋子中心
    quality = float(np.clip(best_score / 255.0 * 2.2, 0.0, 1.0))
    return refined, quality


# --------------------------------------------------------------------------
# 单点采样结果
# --------------------------------------------------------------------------


@dataclass
class SampleResult:
    """一个交叉点的采样与判定结果。"""

    row: int
    col: int
    x: int
    y: int
    occupied: bool = False
    is_red: bool = False
    piece: str = EMPTY
    confidence: float = 0.0
    # 诊断特征
    contrast: float = 0.0
    dark_redness: float = 0.0
    min_val: float = 255.0
    glyph: np.ndarray | None = None
    runner_up: str = ""
    runner_up_score: float = 0.0

    def to_dict(self) -> dict:
        return {
            "row": self.row, "col": self.col, "x": self.x, "y": self.y,
            "occupied": self.occupied, "is_red": self.is_red,
            "piece": self.piece, "confidence": round(self.confidence, 3),
            "contrast": round(self.contrast, 1),
            "dark_redness": round(self.dark_redness, 1),
            "min_val": round(self.min_val, 1),
        }


# --------------------------------------------------------------------------
# 识别器
# --------------------------------------------------------------------------


class BoardReader:
    """把截图读成棋盘矩阵。"""

    def __init__(self, geometry: BoardGeometry,
                 library: TemplateLibrary | None = None,
                 config: VisionConfig | None = None) -> None:
        self.geometry = geometry
        self.library = library or TemplateLibrary()
        self.config = config or VisionConfig()

    # ---------------- 采样 ----------------

    def _sample(self, img_bgr: np.ndarray, row: int, col: int) -> SampleResult:
        cfg = self.config
        sx, sy = self.geometry.cell_size(ROWS, COLS)
        cx, cy = self.geometry.point_of(row, col, ROWS, COLS)
        r_out = int(round(min(sx, sy) * cfg.r_out_ratio))
        r_in = int(round(min(sx, sy) * cfg.r_in_ratio))
        res = SampleResult(row=row, col=col, x=cx, y=cy)

        h, w = img_bgr.shape[:2]
        y0, y1 = max(0, cy - r_out), min(h, cy + r_out + 1)
        x0, x1 = max(0, cx - r_out), min(w, cx + r_out + 1)
        patch = img_bgr[y0:y1, x0:x1]
        if patch.size == 0:
            return res

        ph, pw = patch.shape[:2]
        yy, xx = np.mgrid[0:ph, 0:pw]
        dist2 = (yy - (cy - y0)) ** 2 + (xx - (cx - x0)) ** 2
        core_mask = dist2 <= r_in ** 2
        ring_mask = (dist2 > r_in ** 2) & (dist2 <= r_out ** 2)
        if core_mask.sum() < 10 or ring_mask.sum() < 10:
            return res

        core = patch[core_mask].astype(np.float32)
        ring = patch[ring_mask].astype(np.float32)
        val = core.max(axis=1)
        ring_val = float(ring.max(axis=1).mean())

        res.contrast = ring_val - float(val.mean())
        p10 = float(np.percentile(val, 10))
        res.min_val = p10

        # 最暗 25% 像素的红度
        order = np.argsort(val)
        k = max(1, int(len(val) * 0.25))
        darkest = core[order[:k]]
        db, dg, dr = darkest[:, 0], darkest[:, 1], darkest[:, 2]
        res.dark_redness = float((dr - (dg + db) / 2).mean())

        # ---- 占用与红黑判定 ----
        # 红方：红度显著（红字即使偏亮也能靠颜色认出）
        # 黑方：红度低，且存在深色笔画（暗 或 字区明显比边缘暗）
        is_red = res.dark_redness >= cfg.red_threshold
        is_black = (res.dark_redness < cfg.black_redness_max
                    and (res.min_val <= cfg.black_dark_val_max
                         or res.contrast >= cfg.black_contrast_min))
        res.occupied = bool(is_red or is_black)
        if not res.occupied:
            res.piece = EMPTY
            res.confidence = 1.0
            return res

        res.is_red = bool(is_red)

        # 提取字形
        glyph = self._extract_glyph(patch, core_mask, res.is_red)
        res.glyph = glyph
        if glyph is not None and self.library.all_templates():
            piece, score, second, second_score = self._match(glyph, res.is_red)
            res.piece, res.confidence = piece, score
            res.runner_up, res.runner_up_score = second, second_score
        else:
            # 没有模板时只能给出颜色判断
            res.piece = "K" if res.is_red else "k"
            res.confidence = 0.0
        return res

    def _extract_glyph(self, patch: np.ndarray, core_mask: np.ndarray,
                       is_red: bool) -> np.ndarray | None:
        """从棋子 patch 中提取归一化字形。

        返回 (glyph_size, glyph_size) 的 float32 图，值域 0..1，
        1 表示笔画。返回 None 表示提取失败。

        红黑两方用**不同通道**分割：
        * 黑字：亮度低，用灰度 + Otsu 反相即可。
        * 红字：亮度反而高于木色底盘（红通道拉高），必须用色度
          特征 ``红通道 - (绿+蓝)/2`` 来分离笔画，用亮度会失败。
        """
        size = self.config.glyph_size
        b = patch[:, :, 0].astype(np.float32)
        g = patch[:, :, 1].astype(np.float32)
        r = patch[:, :, 2].astype(np.float32)

        if is_red:
            feature = r - (g + b) / 2.0
        else:
            feature = -cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY).astype(np.float32)

        # 只在字区内取特征，区外置为背景值，避免圆形边框干扰 Otsu
        inside = core_mask
        if inside.sum() < 10:
            return None
        bg = float(np.median(feature[inside]))
        work = np.full(feature.shape, bg, np.float32)
        work[inside] = feature[inside]

        work_u8 = cv2.normalize(work, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)
        work_u8 = cv2.GaussianBlur(work_u8, (3, 3), 0)
        _, binary = cv2.threshold(work_u8, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        binary = cv2.bitwise_and(binary, (inside.astype(np.uint8) * 255))

        # 笔画占比不合理时（几乎全白/全黑）说明分割失败
        ratio = float(binary.mean()) / 255.0
        if ratio < 0.02 or ratio > 0.75:
            return None

        ys, xs = np.nonzero(binary)
        if len(ys) < 6:
            return None
        y0, y1 = int(ys.min()), int(ys.max()) + 1
        x0, x1 = int(xs.min()), int(xs.max()) + 1
        glyph = binary[y0:y1, x0:x1]

        # 补成正方形，保持长宽比
        gh, gw = glyph.shape
        side = max(gh, gw)
        canvas = np.zeros((side, side), np.uint8)
        canvas[(side - gh) // 2:(side - gh) // 2 + gh,
               (side - gw) // 2:(side - gw) // 2 + gw] = glyph

        norm = cv2.resize(canvas, (size, size), interpolation=cv2.INTER_AREA)
        return norm.astype(np.float32) / 255.0

    def _match(self, glyph: np.ndarray, is_red: bool) -> tuple[str, float, str, float]:
        """与模板库比对，返回 (最佳兵种, 分数, 次佳, 次佳分数)。"""
        want_upper = is_red
        best_piece, best_score = "", -2.0
        second_piece, second_score = "", -2.0
        for piece in self.library.pieces:
            if piece == EMPTY:
                continue
            # 红黑颜色不一致的模板直接跳过（颜色已经判过，不必再比）
            if piece.isupper() != want_upper:
                continue
            for tpl in self.library.get(piece):
                score = self._ncc(glyph, tpl.glyph)
                if score > best_score:
                    second_piece, second_score = best_piece, best_score
                    best_piece, best_score = piece, score
                elif score > second_score:
                    second_piece, second_score = piece, score
        if best_score < -1.0:
            return "", 0.0, "", 0.0
        return best_piece, float(best_score), second_piece, float(second_score)

    @staticmethod
    def _ncc(a: np.ndarray, b: np.ndarray) -> float:
        """零均值归一化互相关，值域 -1..1。"""
        av = a.reshape(-1).astype(np.float32)
        bv = b.reshape(-1).astype(np.float32)
        av = av - av.mean()
        bv = bv - bv.mean()
        na = float(np.linalg.norm(av))
        nb = float(np.linalg.norm(bv))
        if na < 1e-6 or nb < 1e-6:
            return 0.0
        return float(np.dot(av, bv) / (na * nb))

    # ---------------- 整盘读取 ----------------

    def read(self, img_bgr: np.ndarray, collect_glyphs: bool = False) -> tuple[Board, list[SampleResult]]:
        """识别整张棋盘，返回 (Board, 每个交叉点的采样结果)。"""
        results: list[SampleResult] = []
        grid = [[EMPTY] * COLS for _ in range(ROWS)]
        for r in range(ROWS):
            for c in range(COLS):
                res = self._sample(img_bgr, r, c)
                if not collect_glyphs:
                    res.glyph = None
                results.append(res)
                grid[r][c] = res.piece
        return Board(grid), results

    def read_board(self, img_bgr: np.ndarray) -> Board:
        board, _ = self.read(img_bgr)
        return board

    def read_with_flip(self, img_bgr: np.ndarray) -> Board:
        """按 geometry.flipped 把画面方向还原成"红方在下"的标准朝向。"""
        board, _ = self.read(img_bgr)
        if not self.geometry.flipped:
            return board
        grid = [list(reversed(row)) for row in reversed(board.grid)]
        return Board(grid)


# --------------------------------------------------------------------------
# 模板构建
# --------------------------------------------------------------------------


def build_templates(img_bgr: np.ndarray, geometry: BoardGeometry,
                    labels: dict[tuple[int, int], str],
                    name: str = "default",
                    config: VisionConfig | None = None) -> TemplateLibrary:
    """从一张带标注的截图里采集模板。

    ``labels`` 是 ``{(row, col): FEN字符}``，只需标注若干棋子，同兵种
    多标几个能提高鲁棒性（会在匹配时都参与比较）。
    """
    reader = BoardReader(geometry, TemplateLibrary(), config)
    lib = TemplateLibrary(name)
    for (row, col), piece in labels.items():
        res = reader._sample(img_bgr, row, col)
        if res.glyph is None:
            continue
        lib.add(PieceTemplate(piece=piece, glyph=res.glyph,
                              samples=1, dark_redness=res.dark_redness))
    return lib


# --------------------------------------------------------------------------
# 帧序列防抖
# --------------------------------------------------------------------------


class FrameStabilizer:
    """连续多帧一致才确认局面，用于抑制瞬时误识别。

    引擎和界面的输入都取自这里确认过的局面，避免鼠标掠过、动画
    过渡帧造成棋子位置抖动。
    """

    def __init__(self, confirm_frames: int = 2, max_gap: int = 1) -> None:
        self.confirm_frames = max(1, confirm_frames)
        self.max_gap = max_gap
        self._candidate: Board | None = None
        self._count = 0
        self._stable: Board | None = None
        self.rejected = 0

    @property
    def stable(self) -> Board | None:
        """当前已确认的局面。"""
        return self._stable

    @property
    def pending_count(self) -> int:
        return self._count

    def update(self, board: Board, sanity: bool = True) -> Board | None:
        """喂入一帧识别结果。

        返回**本帧新确认**的局面（即刚刚稳定下来的那一帧），否则 None。
        """
        if sanity and not board.is_sane():
            self._candidate = None
            self._count = 0
            self.rejected += 1
            return None

        if self._candidate is not None and board == self._candidate:
            self._count += 1
        else:
            self._candidate = board.copy()
            self._count = 1

        if self._count >= self.confirm_frames:
            if self._stable is None or self._stable != board:
                self._stable = board.copy()
                return self._stable
            self._stable = board.copy()
        return None

    def reset(self) -> None:
        self._candidate = None
        self._count = 0
        self._stable = None

    def force(self, board: Board) -> None:
        """直接设定当前局面（用于用户手动修正）。"""
        self._candidate = board.copy()
        self._count = self.confirm_frames
        self._stable = board.copy()
