"""用棋盘线（网格线）检测交叉点位置 —— 比用外接矩形精确。

原理
------------------------------------------------------------------
象棋棋盘有 9 条竖线、10 条横线，**等间距**且横贯整盘，是最可靠的
几何基准。把这些线的位置测出来，交叉点就是它们的笛卡尔积。

做法：
1. 在粗定位区域内取灰度图，做 Scharr 梯度。
2. 沿 Y 轴累加 |dI/dx| 得到"竖线响应"信号；沿 X 轴累加 |dI/dy|
   得到"横线响应"信号。棋盘线会在信号里形成等间距峰。
3. 对这种周期性信号做自相关，主峰位置即线间距 —— 这比取最大
   外接矩形稳健得多，不受棋子装饰影响。
4. 以间距为约束，用相位搜索（对每个候选相位求峰值的和）定出
   线序列的起点。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def line_signals(gray: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """返回 (竖线响应, 横线响应)，长度分别为图像宽、高。"""
    gx = cv2.Scharr(gray, cv2.CV_32F, 1, 0)
    gy = cv2.Scharr(gray, cv2.CV_32F, 0, 1)
    col_sig = np.abs(gx).sum(axis=0)     # 每个 x 位置的垂直边缘强度
    row_sig = np.abs(gy).sum(axis=1)     # 每个 y 位置的水平边缘强度
    return col_sig, row_sig


def dominant_spacing(sig: np.ndarray, lo: float, hi: float) -> tuple[float, float]:
    """用自相关找主周期，返回 (间距, 置信度)。"""
    s = sig.astype(np.float64)
    s = s - s.mean()
    if s.std() < 1e-6:
        return 0.0, 0.0
    ac = np.correlate(s, s, mode="full")[len(s) - 1:]
    ac = ac / max(ac[0], 1e-9)
    lo_i, hi_i = max(2, int(lo)), min(len(ac) - 1, int(hi))
    if hi_i <= lo_i:
        return 0.0, 0.0
    seg = ac[lo_i:hi_i]
    # 取局部极大中最高的那个
    peaks = [i for i in range(1, len(seg) - 1) if seg[i] >= seg[i - 1] and seg[i] >= seg[i + 1]]
    if not peaks:
        return 0.0, 0.0
    best_i = max(peaks, key=lambda i: seg[i])
    # 抛物线插值提高亚像素精度
    i = best_i
    if 0 < i < len(seg) - 1:
        y0, y1, y2 = seg[i - 1], seg[i], seg[i + 1]
        denom = (y0 - 2 * y1 + y2)
        delta = 0.5 * (y0 - y2) / denom if abs(denom) > 1e-9 else 0.0
    else:
        delta = 0.0
    return lo_i + best_i + delta, float(seg[best_i])


def phase_search(sig: np.ndarray, spacing: float, count: int,
                 lo: int, hi: int) -> tuple[float, float]:
    """在 [lo,hi) 里找最佳起始相位，返回 (起始位置, 平均响应)。"""
    s = sig.astype(np.float64)
    if spacing <= 1 or count < 2:
        return float(lo), 0.0
    span = spacing * (count - 1)
    best_phase, best_score = float(lo), -1.0
    lo_i, hi_i = int(lo), int(hi)
    for phase in np.arange(lo_i, hi_i, 0.5):
        if phase + span > len(s):
            break
        xs = np.rint(phase + np.arange(count) * spacing).astype(np.int64)
        xs = xs[(xs >= 0) & (xs < len(s))]
        if len(xs) < count - 2:
            continue
        # 用 ±2 邻域取局部最大，容忍亚像素误差
        vals = []
        for x in xs:
            a, b = max(0, x - 2), min(len(s), x + 3)
            vals.append(s[a:b].max())
        score = float(np.mean(vals))
        if score > best_score:
            best_score, best_phase = score, float(phase)
    return best_phase, best_score


def main() -> int:
    img_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("shots/bg_002A0A30.png")
    img = cv2.imdecode(np.fromfile(str(img_path), dtype=np.uint8), cv2.IMREAD_COLOR)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    data = json.loads(Path("analysis/jj_board.json").read_text(encoding="utf-8"))
    r = data["region"]
    # 略微向外扩，保证棋盘线都落在里面
    x0, y0 = max(0, r["x"] - 20), max(0, r["y"] - 20)
    x1, y1 = min(img.shape[1], r["x"] + r["w"] + 20), min(img.shape[0], r["y"] + r["h"] + 20)
    print(f"粗定位区域: x {x0}..{x1}  y {y0}..{y1}  ({x1-x0}x{y1-y0})")

    crop = gray[y0:y1, x0:x1]
    col_sig, row_sig = line_signals(crop)

    xs_spacing, xs_conf = dominant_spacing(col_sig, 45, 110)
    ys_spacing, ys_conf = dominant_spacing(row_sig, 45, 110)
    print()
    print("=== 自相关求线间距 ===")
    print(f"  竖线间距: {xs_spacing:.2f}px  置信度 {xs_conf:.3f}")
    print(f"  横线间距: {ys_spacing:.2f}px  置信度 {ys_conf:.3f}")
    print(f"  实测真值: 71.2 / 70.7")

    # 用间距与 9/10 条线的约束找相位
    print()
    print("=== 相位搜索（9 条竖线 / 10 条横线）===")
    best = None
    for sp_x in np.linspace(xs_spacing * 0.97, xs_spacing * 1.03, 13):
        for sp_y in np.linspace(ys_spacing * 0.97, ys_spacing * 1.03, 13):
            px, sx = phase_search(col_sig, sp_x, 9, 0, int(sp_x) + 1)
            py, sy = phase_search(row_sig, sp_y, 10, 0, int(sp_y) + 1)
            if px + sp_x * 8 > len(col_sig) or py + sp_y * 9 > len(row_sig):
                continue
            score = sx + sy
            if best is None or score > best[0]:
                best = (score, sp_x, sp_y, px, py)

    if best is None:
        print("  未找到有效相位")
        return 1
    score, sp_x, sp_y, px, py = best
    gx0, gy0 = x0 + px, y0 + py
    print(f"  竖线: 起点 x={gx0:.1f}  间距 {sp_x:.2f}  ->  末线 x={gx0 + sp_x*8:.1f}")
    print(f"  横线: 起点 y={gy0:.1f}  间距 {sp_y:.2f}  ->  末线 y={gy0 + sp_y*9:.1f}")
    print(f"  综合响应 {score:.1f}")

    print()
    print("=== 与实测对比 ===")
    print(f"  x: 检测 {gx0:.1f} vs 实际 57    偏差 {gx0-57:+.1f}px")
    print(f"  y: 检测 {gy0:.1f} vs 实际 320   偏差 {gy0-320:+.1f}px")
    print(f"  dx: 检测 {sp_x:.2f} vs 实际 71.2  偏差 {sp_x-71.2:+.2f}px  "
          f"(第 8 列累积 {(sp_x-71.2)*8:+.1f}px)")
    print(f"  dy: 检测 {sp_y:.2f} vs 实际 70.7  偏差 {sp_y-70.7:+.2f}px  "
          f"(第 9 行累积 {(sp_y-70.7)*9:+.1f}px)")

    # 可视化验算
    print()
    print("=== 检测出的交叉点 vs 实测棋子中心 ===")
    truth = {"(0,0)": (57, 320), "(0,3)": (350, 320), "(0,4)": (421, 320),
             "(0,5)": (492, 320), "(0,6)": (564, 320), "(9,4)": (342, 956)}
    for tag, (tx, ty) in truth.items():
        rr, cc = int(tag[3]), int(tag[1])
        dx_ = gx0 + cc * sp_x
        dy_ = gy0 + rr * sp_y
        print(f"  {tag}  检测({dx_:6.1f},{dy_:6.1f})  实测({tx},{ty})  "
              f"偏差({dx_-tx:+5.1f},{dy_-ty:+5.1f})")

    out = {"x": int(round(gx0)), "y": int(round(gy0)),
           "w": int(round(sp_x * 8)), "h": int(round(sp_y * 9)),
           "spacing": [round(sp_x, 3), round(sp_y, 3)]}
    Path("analysis/grid_by_lines.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print(f"已保存: analysis/grid_by_lines.json -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
