"""用棋子位置的自相关性精确定位网格 —— 最终方案。

思路
------------------------------------------------------------------
棋盘线的信号会被棋子遮挡、被装饰干扰（实测横线间距误差 1.8px，
第 9 行累积到 16px）。但**棋子本身的分布就是周期性的**：

* 每一行的棋子水平间距 = 格距
* 每一列的棋子垂直间距 = 格距

因此对"疑似棋子"掩码做自相关，主峰就是格距；再用相位搜索定原点。
实测证明这个方法比棋盘线稳健，因为信号源（棋子）本身就是最终的
采样目标 —— 采样点落在棋子中心时响应最大。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.capture import Region                       # noqa: E402
from src.vision import VisionConfig, _occupied_mask  # noqa: E402


def autocorr_spacing(sig: np.ndarray, lo: int, hi: int) -> tuple[float, float]:
    s = sig.astype(np.float64)
    s = s - s.mean()
    if s.std() < 1e-9:
        return 0.0, 0.0
    ac = np.correlate(s, s, mode="full")[len(s) - 1:]
    ac = ac / max(ac[0], 1e-12)
    lo_i, hi_i = max(2, lo), min(len(ac) - 1, hi)
    if hi_i <= lo_i:
        return 0.0, 0.0
    seg = ac[lo_i:hi_i]
    best_i = int(np.argmax(seg))
    i = best_i
    if 0 < i < len(seg) - 1:
        y0, y1, y2 = seg[i - 1], seg[i], seg[i + 1]
        den = y0 - 2 * y1 + y2
        delta = 0.5 * (y0 - y2) / den if abs(den) > 1e-12 else 0.0
    else:
        delta = 0.0
    return lo_i + best_i + delta, float(seg[best_i])


def refine(mask: np.ndarray, count: int, spacing: float,
           origin_lo: float, origin_hi: float,
           spacing_pct: float = 0.02) -> tuple[float, float, float]:
    """搜索 (origin, spacing) 使 count 个采样点上的掩码响应之和最大。"""
    col = mask.sum(axis=0).astype(np.float64)
    row = mask.sum(axis=1).astype(np.float64)
    n = len(col) if count > 1 else 0

    def score(sig: np.ndarray, origin: float, sp: float) -> float:
        xs = np.rint(origin + np.arange(count) * sp).astype(np.int64)
        if xs[0] < 0 or xs[-1] >= len(sig):
            return -1.0
        vals = []
        for x in xs:
            a, b = max(0, x - 3), min(len(sig), x + 4)
            vals.append(sig[a:b].max())
        return float(np.mean(vals)) if vals else -1.0

    best = (origin_lo, spacing, -1.0)
    for sp in np.linspace(spacing * (1 - spacing_pct), spacing * (1 + spacing_pct), 41):
        for org in np.arange(origin_lo, origin_hi, 0.25):
            s = score(col if count else row, org, sp)
            if s > best[2]:
                best = (float(org), float(sp), s)
    return best


def main() -> int:
    img_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("shots/bg_002A0A30.png")
    img = cv2.imdecode(np.fromfile(str(img_path), dtype=np.uint8), cv2.IMREAD_COLOR)
    cfg = VisionConfig()

    mask = _occupied_mask(img, cfg)
    print(f"棋子掩码覆盖率: {mask.mean():.4f}")
    col_proj = mask.sum(axis=0).astype(np.float64)
    row_proj = mask.sum(axis=1).astype(np.float64)
    print(f"列投影: min={col_proj.min():.0f} max={col_proj.max():.0f} 峰数={int((col_proj > col_proj.max()*0.3).sum())}")
    print(f"行投影: min={row_proj.min():.0f} max={row_proj.max():.0f}")
    print()

    print("=== 棋子掩码自相关求格距 ===")
    dx_ac, dx_conf = autocorr_spacing(col_proj, 50, 100)
    dy_ac, dy_conf = autocorr_spacing(row_proj, 50, 100)
    print(f"  水平格距: {dx_ac:.2f}px (置信度 {dx_conf:.3f})")
    print(f"  垂直格距: {dy_ac:.2f}px (置信度 {dy_conf:.3f})")
    print()

    print("=== 联合搜索 原点 + 格距 ===")
    ox, dx, sx = refine(mask, 9, dx_ac, 20, 90)
    oy, dy, sy = refine(mask, 10, dy_ac, 290, 370)
    print(f"  竖线: 原点 x={ox:.2f}  格距 {dx:.3f}  响应 {sx:.1f}  -> 末列 x={ox+dx*8:.1f}")
    print(f"  横线: 原点 y={oy:.2f}  格距 {dy:.3f}  响应 {sy:.1f}  -> 末行 y={oy+dy*9:.1f}")
    print()

    # 用每个交叉点的局部最大位置反算实际棋子中心，评估对齐误差
    print("=== 对齐误差评估（每个交叉点的掩码质心偏移）===")
    offsets = []
    for r in range(10):
        for c in range(9):
            px, py = int(round(ox + c * dx)), int(round(oy + r * dy))
            R = int(min(dx, dy) * 0.42)
            y0, y1 = max(0, py - R), min(mask.shape[0], py + R)
            x0, x1 = max(0, px - R), min(mask.shape[1], px + R)
            patch = mask[y0:y1, x0:x1]
            if patch.sum() < 40:
                continue
            ys, xs = np.nonzero(patch)
            cx, cy = x0 + xs.mean(), y0 + ys.mean()
            offsets.append((r, c, cx - px, cy - py))
    if offsets:
        ddx = np.array([o[2] for o in offsets])
        ddy = np.array([o[3] for o in offsets])
        print(f"  有信号的交叉点: {len(offsets)}")
        print(f"  X 方向偏移: 均值 {ddx.mean():+.2f}px  标准差 {ddx.std():.2f}  最大 {np.abs(ddx).max():.2f}")
        print(f"  Y 方向偏移: 均值 {ddy.mean():+.2f}px  标准差 {ddy.std():.2f}  最大 {np.abs(ddy).max():.2f}")
        worst = sorted(offsets, key=lambda o: -max(abs(o[2]), abs(o[3])))[:5]
        print("  偏移最大的点:")
        for r, c, ddx_, ddy_ in worst:
            print(f"    ({r},{c}) 偏移 ({ddx_:+.1f},{ddy_:+.1f})")

    out = {"x": int(round(ox)), "y": int(round(oy)),
           "w": int(round(dx * 8)), "h": int(round(dy * 9)),
           "spacing": [round(dx, 3), round(dy, 3)]}
    Path("analysis/grid_final.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print(f"已保存: analysis/grid_final.json -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
