"""校准棋子颜色判据。

目标：找到一组稳定特征，把 90 个交叉点分成 {空, 红子, 黑子}。

关键认识
------------------------------------------------------------------
棋子是带立体高光的木纹圆盘，直接对整块 patch 求均值会被高光和
木纹污染。真正有判别力的是**文字笔画的颜色**（红方红字、黑方黑字），
因此应当只在棋子中心的"字区"里统计，并且用**分位数**而不是均值，
避免浅色底纹把结果拉平。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def load_points(json_path: Path) -> tuple[np.ndarray, dict]:
    data = json.loads(json_path.read_text(encoding="utf-8"))
    region = data["region"]
    pts = []
    for r in range(10):
        for c in range(9):
            x = region["x"] + round(c * region["w"] / 8)
            y = region["y"] + round(r * region["h"] / 9)
            pts.append((r, c, x, y))
    return np.array([(x, y) for _, _, x, y in pts]), data


def analyze(img: np.ndarray, x: int, y: int, r_in: int, r_out: int) -> dict:
    h, w = img.shape[:2]
    y0, y1 = max(0, y - r_out), min(h, y + r_out)
    x0, x1 = max(0, x - r_out), min(w, x + r_out)
    patch = img[y0:y1, x0:x1]
    ph, pw = patch.shape[:2]
    yy, xx = np.mgrid[0:ph, 0:pw]
    cy, cx = y - y0, x - x0
    dist2 = (yy - cy) ** 2 + (xx - cx) ** 2

    core = patch[dist2 <= r_in ** 2]            # 字区
    ring = patch[(dist2 > r_in ** 2) & (dist2 <= r_out ** 2)]   # 边缘环
    if core.size == 0 or ring.size == 0:
        return {}

    core_f = core.astype(np.float32)
    ring_f = ring.astype(np.float32)
    b, g, r = core_f[:, 0], core_f[:, 1], core_f[:, 2]

    # 亮度（用 max 通道近似，抗色偏）
    val = core_f.max(axis=1)
    # 字色：取最暗的 25% 像素
    k = max(1, int(len(core_f) * 0.25))
    order = np.argsort(val)
    darkest = core_f[order[:k]]
    db, dg, dr = darkest[:, 0], darkest[:, 1], darkest[:, 2]

    # 红字判据：暗像素中红通道明显高于蓝绿
    dark_redness = float((dr - (dg + db) / 2).mean())
    # 红像素占比：红通道领先蓝绿 40 以上
    red_mask = (r - (g + b) / 2) > 40
    red_ratio = float(red_mask.mean())
    # 暗像素占比
    val_ring = ring_f.max(axis=1)
    ring_val = float(val_ring.mean())
    core_val = float(val.mean())

    return {
        "core_val": round(core_val, 1),
        "ring_val": round(ring_val, 1),
        "contrast": round(ring_val - core_val, 1),
        "dark_redness": round(dark_redness, 1),
        "red_ratio": round(red_ratio, 3),
        "darkest_bgr": [int(v) for v in darkest.mean(axis=0)],
        "min_val": int(val.min()),
        "p10_val": int(np.percentile(val, 10)),
    }


def main() -> int:
    json_path = Path(sys.argv[1] if len(sys.argv) > 1 else "analysis/jj_board.json")
    img_path = Path(sys.argv[2] if len(sys.argv) > 2 else "shots/bg_002A0A30.png")
    img = cv2.imdecode(np.fromfile(str(img_path), dtype=np.uint8), cv2.IMREAD_COLOR)
    pts, data = load_points(json_path)
    region = data["region"]
    step = min(region["w"] / 8, region["h"] / 9)
    r_out = int(step * 0.46)
    r_in = int(step * 0.28)
    print(f"网格: {region}  间距≈{step:.1f}px  采样 r_in={r_in} r_out={r_out}")
    print()

    feats = {}
    for i, (x, y) in enumerate(pts):
        r, c = divmod(i, 9)
        feats[(r, c)] = analyze(img, int(x), int(y), r_in, r_out)

    print("=== 逐点特征（row 从上到下 0..9，col a..i）===")
    hdr = (f"{'':4}" + "".join(f"{chr(97+c):>22}" for c in range(9)))
    print(hdr)
    for r in range(10):
        cells = []
        for c in range(9):
            f = feats[(r, c)]
            if not f:
                cells.append(f"{'--':>22}")
                continue
            cells.append(f"{f['dark_redness']:>7.0f}/{f['red_ratio']:.2f}/{f['min_val']:>3}")
        print(f" {9-r:>2} " + "".join(cells))
    print("      格式: dark_redness / red_ratio / min_val")

    print()
    print("=== 候选判据分离度 ===")
    # 用接触表人工确认的棋子分布做参考
    empty_like = []
    red_like = []
    black_like = []
    for r in range(10):
        for c in range(9):
            f = feats[(r, c)]
            if not f:
                continue
            # 初步自动分组（仅用于观察分离度）
            if f["red_ratio"] > 0.10:
                red_like.append(f)
            elif f["contrast"] > 25:
                black_like.append(f)
            else:
                empty_like.append(f)

    for name, group in (("疑似红子", red_like), ("疑似黑子", black_like), ("疑似空点", empty_like)):
        if not group:
            print(f"  {name}: 无")
            continue
        rr = [g["red_ratio"] for g in group]
        dr = [g["dark_redness"] for g in group]
        ctr = [g["contrast"] for g in group]
        print(f"  {name:8} n={len(group):2d}  "
              f"red_ratio {min(rr):.2f}~{max(rr):.2f}  "
              f"dark_redness {min(dr):6.1f}~{max(dr):6.1f}  "
              f"contrast {min(ctr):6.1f}~{max(ctr):6.1f}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
