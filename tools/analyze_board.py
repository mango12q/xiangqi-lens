"""棋盘几何自动定位与网格分析。

用途
------------------------------------------------------------------
从一张对局截图里找出棋盘的**交叉点网格**，即 9 列 x 10 行共 90 个
落子点的图像坐标。这是整套识别算法的地基：有了网格，棋子分类只需
要在每个交叉点附近取一小块做判断，而不必依赖整幅图的目标检测。

定位策略
------------------------------------------------------------------
1. 棋盘是一块**低饱和度的浅色石板/木色区域**，在 HSV 空间里饱和度
   低、亮度中等偏高。用它做阈值分割。
2. 对mask 做形态学闭运算连成整块，取最大轮廓的外接矩形。
3. 用该矩形的宽高比做校验（象棋棋盘 9x10 交叉点的比例约 8:9，
   加上边框后接近 0.82~0.92）。
4. 交叉点位置 = 外接矩形内按 (cols-1)/(rows-1) 等分。

用法::

    python tools/analyze_board.py shots/bg_002A0A30.png
    python tools/analyze_board.py shots/bg_002A0A30.png --debug out.png
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.capture import Region  # noqa: E402


# --------------------------------------------------------------------------
# 候选棋盘分割
# --------------------------------------------------------------------------


def board_mask_candidates(img_bgr: np.ndarray) -> list[tuple[str, np.ndarray]]:
    """生成若干种棋盘候选掩码，返回 (名称, mask) 列表。

    参数来自对 JJ 象棋小程序截图的像素实测：
    棋盘石板底色典型值 HSV≈(37, 34, 150)，即低饱和度、中等亮度。
    """
    hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
    h, s, v = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    out: list[tuple[str, np.ndarray]] = []

    # 石板色棋盘（JJ 象棋 / 多数小程序皮肤）：低饱和、中亮度
    out.append(("slate_low_sat",
                ((h >= 30) & (h <= 50) & (s < 60) & (v > 100) & (v < 200)).astype(np.uint8) * 255))
    # 更宽松的灰调，容忍其他配色
    out.append(("gray_loose",
                ((s < 70) & (v > 90) & (v < 210)).astype(np.uint8) * 255))
    # 木色棋盘（天天象棋 / QQ 象棋等暖色皮肤）
    out.append(("warm_wood",
                ((h >= 5) & (h <= 35) & (s >= 30) & (s < 160) & (v >= 90)).astype(np.uint8) * 255))
    return out


def largest_rect_of(mask: np.ndarray, img_shape: tuple[int, int],
                    ratio_range: tuple[float, float] = (0.85, 1.35),
                    min_frac: float = 0.15) -> tuple[Region, float] | None:
    """在 mask 中找最像棋盘的最大矩形，返回 (Region, 宽高比)。

    象棋棋盘 9x10 交叉点的标准比是 10/9≈1.111（纵向略长）；加上棋盘
    外框、不同皮肤留白后实测在 1.0~1.25 之间，故默认范围放宽到
    (0.85, 1.35)。"""
    img_h, img_w = img_shape
    k = max(3, int(min(img_w, img_h) * 0.03) | 1)
    ker = cv2.getStructuringElement(cv2.MORPH_RECT, (k, k))
    m = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, ker)
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, ker)
    contours, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    best = None
    best_score = 0.0
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        if w < img_w * min_frac or h < img_h * min_frac:
            continue
        ratio = h / max(w, 1)
        if not (ratio_range[0] <= ratio <= ratio_range[1]):
            continue
        # 面积大 + 比例接近 1.111 的优先
        score = w * h * (1.0 - min(abs(ratio - 1.111) / 1.111, 1.0))
        if score > best_score:
            best_score = score
            best = (Region(x, y, w, h), ratio)
    return best


def refine_by_grid_lines(img_bgr: np.ndarray, region: Region) -> Region | None:
    """用棋盘内部的**直线**修正外接矩形，得到真正的交叉点外框。

    思路：在候选区域里做边缘检测，分别沿 X/Y 方向累加边缘强度，
    找等间距的峰簇；首末峰即棋盘的首末棋路。
    """
    x, y, w, h = region.x, region.y, region.w, region.h
    crop = img_bgr[max(y, 0):y + h, max(x, 0):x + w]
    if crop.size == 0:
        return None
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    edges = cv2.Canny(gray, 40, 120)

    col_sum = edges.sum(axis=0).astype(np.float64)
    row_sum = edges.sum(axis=1).astype(np.float64)

    def peaks(sig: np.ndarray, expect: int, tol_frac: float = 0.35) -> list[int]:
        """在信号里找期望数量的等间距峰。"""
        if sig.size == 0:
            return []
        n = sig.size
        step = n / (expect - 1) if expect > 1 else n
        found: list[int] = []
        for i in range(expect):
            center = int(i * step)
            lo = max(0, int(center - step * tol_frac))
            hi = min(n, int(center + step * tol_frac) + 1)
            if hi <= lo:
                continue
            seg = sig[lo:hi]
            found.append(lo + int(np.argmax(seg)))
        return found

    cols = peaks(col_sum, 9)
    rows = peaks(row_sum, 10)
    if len(cols) != 9 or len(rows) != 10:
        return None
    # 峰簇应当单调递增且间距大体均匀
    if any(b <= a for a, b in zip(cols, cols[1:])) or any(b <= a for a, b in zip(rows, rows[1:])):
        return None

    l = x + cols[0]
    r = x + cols[-1]
    t = y + rows[0]
    b = y + rows[-1]
    if r - l < w * 0.6 or b - t < h * 0.6:
        return None
    return Region(l, t, r - l, b - t)


def locate_board(img_bgr: np.ndarray, verbose: bool = True) -> tuple[Region, dict]:
    """综合定位棋盘，返回 (region, 诊断信息)。"""
    diag: dict = {"candidates": []}
    img_h, img_w = img_bgr.shape[:2]
    best: tuple[Region, float, str] | None = None

    for name, mask in board_mask_candidates(img_bgr):
        got = largest_rect_of(mask, (img_h, img_w))
        if got is None:
            if verbose:
                print(f"  [{name}] 无候选")
            continue
        region, ratio = got
        diag["candidates"].append({"method": name, "region": region.to_dict(),
                                   "ratio": round(ratio, 3)})
        if verbose:
            print(f"  [{name}] {region.x},{region.y} {region.w}x{region.h}  比例={ratio:.3f}")
        if best is None or region.w * region.h > best[0].w * best[0].h:
            best = (region, ratio, name)

    if best is None:
        raise RuntimeError("未能定位棋盘，请手动标定")

    region, ratio, method = best
    diag["chosen"] = {"method": method, "region": region.to_dict(), "ratio": round(ratio, 3)}

    refined = refine_by_grid_lines(img_bgr, region)
    if refined is not None:
        diag["refined"] = refined.to_dict()
        if verbose:
            print(f"  直线精修 -> {refined.x},{refined.y} {refined.w}x{refined.h}")
        return refined, diag
    if verbose:
        print("  直线精修未生效，使用外接矩形")
    return region, diag


# --------------------------------------------------------------------------
# 网格可视化与取样
# --------------------------------------------------------------------------


def grid_points(region: Region, rows: int = 10, cols: int = 9) -> list[list[tuple[int, int]]]:
    pts = []
    for r in range(rows):
        line = []
        for c in range(cols):
            px = region.x + round(c * region.w / (cols - 1))
            py = region.y + round(r * region.h / (rows - 1))
            line.append((px, py))
        pts.append(line)
    return pts


def draw_grid(img_bgr: np.ndarray, region: Region, rows: int = 10, cols: int = 9) -> np.ndarray:
    out = img_bgr.copy()
    cv2.rectangle(out, (region.x, region.y),
                  (region.x + region.w, region.y + region.h), (0, 0, 255), 2)
    pts = grid_points(region, rows, cols)
    step_x = region.w / (cols - 1)
    step_y = region.h / (rows - 1)
    rad = max(3, int(min(step_x, step_y) * 0.22))
    for r, line in enumerate(pts):
        for c, (px, py) in enumerate(line):
            cv2.circle(out, (px, py), rad, (255, 0, 0), -1)
            cv2.circle(out, (px, py), rad, (255, 255, 255), 1)
            cv2.putText(out, f"{chr(97 + c)}{9 - r}", (px + rad + 1, py - rad),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.32, (0, 0, 0), 2, cv2.LINE_AA)
            cv2.putText(out, f"{chr(97 + c)}{9 - r}", (px + rad + 1, py - rad),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.32, (0, 255, 255), 1, cv2.LINE_AA)
    return out


def sample_patch(img_bgr: np.ndarray, cx: int, cy: int, radius: int) -> np.ndarray:
    """取以 (cx,cy) 为中心、边长 2*radius 的正方形 patch（越界补边）。"""
    h, w = img_bgr.shape[:2]
    x0, y0 = cx - radius, cy - radius
    x1, y1 = cx + radius, cy + radius
    px0, py0 = max(0, -x0), max(0, -y0)
    x0c, y0c = max(0, x0), max(0, y0)
    x1c, y1c = min(w, x1), min(h, y1)
    patch = img_bgr[y0c:y1c, x0c:x1c]
    if patch.size == 0:
        return np.zeros((2 * radius, 2 * radius, 3), np.uint8)
    if px0 or py0 or (x1c - x0c) != 2 * radius or (y1c - y0c) != 2 * radius:
        patch = cv2.copyMakeBorder(patch, py0, 2 * radius - patch.shape[0] - py0,
                                   px0, 2 * radius - patch.shape[1] - px0,
                                   cv2.BORDER_REPLICATE)
    return patch


def analyze_point_colors(img_bgr: np.ndarray, pts: list[list[tuple[int, int]]],
                         step: tuple[float, float]) -> list[list[dict]]:
    """在每个交叉点处统计"棋子特征"，用于区分空点 / 红子 / 黑子。

    判据（针对圆形棋子 + 书法字）：
    * 空点：中心区域是棋盘底色，梯度低、无强边缘
    * 有子：存在圆形轮廓，中心有高对比度的字形笔画
    * 红子：中心区域的红色通道显著高于蓝绿通道
    * 黑子：中心区域三通道都低（黑字）
    """
    rad = max(4, int(min(step) * 0.28))
    rows_out: list[list[dict]] = []
    for line in pts:
        row_out = []
        for (cx, cy) in line:
            patch = sample_patch(img_bgr, cx, cy, rad)
            b, g, r = (patch[:, :, i].astype(np.float32) for i in range(3))
            # 红色强势程度：红通道减掉蓝绿均值
            redness = float((r - (g + b) / 2).mean())
            # 暗像素占比（黑字、或深色棋子）
            v = patch.max(axis=2)
            dark_ratio = float((v < 90).mean())
            bright_ratio = float((v > 170).mean())
            gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
            edge = float(cv2.Laplacian(gray, cv2.CV_32F).var())
            row_out.append({
                "redness": round(redness, 2),
                "dark_ratio": round(dark_ratio, 3),
                "bright_ratio": round(bright_ratio, 3),
                "edge_var": round(edge, 1),
                "mean_bgr": [round(float(x), 1) for x in patch.reshape(-1, 3).mean(axis=0)],
            })
        rows_out.append(row_out)
    return rows_out


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main() -> int:
    ap = argparse.ArgumentParser(description="棋盘几何定位与网格分析")
    ap.add_argument("image", help="截图路径")
    ap.add_argument("--debug", default=None, help="输出叠加网格的调试图")
    ap.add_argument("--json", default=None, help="输出诊断 JSON")
    ap.add_argument("--outdir", default="analysis", help="调试图输出目录")
    args = ap.parse_args()

    src = Path(args.image)
    if not src.is_file():
        print(f"[错误] 文件不存在: {src}")
        return 1
    img = cv2.imdecode(np.fromfile(str(src), dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        print(f"[错误] 无法解码图片: {src}")
        return 1
    h, w = img.shape[:2]
    print(f"图片: {src.name}  {w}x{h}")

    print()
    print("=== 棋盘候选定位 ===")
    region, diag = locate_board(img)

    step = (region.w / 8, region.h / 9)
    print()
    print("=== 棋盘几何 ===")
    print(f"  交叉点外框: x={region.x} y={region.y} w={region.w} h={region.h}")
    print(f"  格子间距:   横 {step[0]:.1f}px  纵 {step[1]:.1f}px  比值 {step[1]/step[0]:.3f}")
    print(f"  棋盘宽高比: {region.h / region.w:.3f}")
    print(f"  图像占比:   {region.w / w:.1%} x {region.h / h:.1%}")

    pts = grid_points(region)
    stats = analyze_point_colors(img, pts, step)

    print()
    print("=== 网格点特征矩阵（redness / dark_ratio）===")
    print("      " + "".join(f"{chr(97 + c):>13}" for c in range(9)))
    for r, line in enumerate(stats):
        cells = []
        for st in line:
            cells.append(f"{st['redness']:>6.0f}/{st['dark_ratio']:.2f}")
        print(f"  {9 - r}   " + " ".join(f"{c:>12}" for c in cells))

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    debug_path = Path(args.debug) if args.debug else outdir / f"grid_{src.stem}.png"
    dbg = draw_grid(img, region)
    cv2.imencode(".png", dbg)[1].tofile(str(debug_path))
    print()
    print(f"调试图已保存: {debug_path}")

    if args.json:
        payload = {"image": str(src), "size": [w, h], "diagnostics": diag,
                   "region": region.to_dict(),
                   "step": [round(step[0], 2), round(step[1], 2)],
                   "grid_stats": stats}
        Path(args.json).write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        print(f"诊断 JSON: {args.json}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
