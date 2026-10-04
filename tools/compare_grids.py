"""并排可视化多个候选网格，用视觉定案哪个是对齐的。

每个候选把采样点画成圆环，圆环恰好套住棋子即为正确。
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CANDS: list[tuple[str, float, float, float, float]] = [
    # (标签, 原点x, 原点y, 格距x, 格距y)
    ("A 自动外接矩形", 49, 334, 73.25, 70.67),
    ("B 竖线检测+实测y", 55.5, 341.5, 71.38, 70.7),
    ("C 竖线检测全用", 55.5, 341.5, 71.38, 68.89),
    ("D 微调候选", 57, 341, 71.2, 70.5),
]


def main() -> int:
    img_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("shots/bg_002A0A30.png")
    img = cv2.imdecode(np.fromfile(str(img_path), dtype=np.uint8), cv2.IMREAD_COLOR)
    h, w = img.shape[:2]

    tiles = []
    for label, ox, oy, dx, dy in CANDS:
        canvas = img.copy()
        for r in range(10):
            for c in range(9):
                px = int(round(ox + c * dx))
                py = int(round(oy + r * dy))
                cv2.circle(canvas, (px, py), 34, (0, 0, 255), 2)
                cv2.drawMarker(canvas, (px, py), (0, 255, 255),
                               cv2.MARKER_CROSS, 11, 2)
        cv2.rectangle(canvas, (0, 0), (w - 1, 30), (30, 30, 30), -1)
        cv2.putText(canvas, f"{label}  ox={ox} oy={oy} dx={dx} dy={dy}",
                    (6, 21), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 255), 1, cv2.LINE_AA)
        tiles.append(canvas)

    # 2x2 拼接
    rows = [np.hstack(tiles[i:i + 2]) for i in range(0, 4, 2)]
    sheet = np.vstack(rows)
    out = Path("analysis/grid_candidates.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    # 缩放到合理尺寸
    scale = min(1.0, 1800 / sheet.shape[1])
    if scale < 1.0:
        sheet = cv2.resize(sheet, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    cv2.imencode(".png", sheet)[1].tofile(str(out))
    print(f"候选对比图: {out}  {sheet.shape}")

    # 顺便输出每个候选在"左上、中上、右下"三个角的对齐偏差
    print()
    print("=== 采样点与棋子中心偏差（用掩码质心估计）===")
    from src.vision import VisionConfig, _occupied_mask
    mask = _occupied_mask(img, VisionConfig())
    for label, ox, oy, dx, dy in CANDS:
        devs = []
        for r in range(10):
            for c in range(9):
                px = int(round(ox + c * dx))
                py = int(round(oy + r * dy))
                R = 30
                y0, y1 = max(0, py - R), min(h, py + R)
                x0, x1 = max(0, px - R), min(w, px + R)
                patch = mask[y0:y1, x0:x1]
                if patch.sum() < 200:
                    continue
                ys, xs = np.nonzero(patch)
                devs.append((x0 + xs.mean() - px, y0 + ys.mean() - py))
        if not devs:
            print(f"  {label}: 无数据")
            continue
        arr = np.array(devs)
        mag = np.hypot(arr[:, 0], arr[:, 1])
        print(f"  {label:18} n={len(devs):2d}  "
              f"偏差均值 {mag.mean():5.2f}px  中位 {np.median(mag):5.2f}  "
              f"90分位 {np.percentile(mag, 90):5.2f}  最大 {mag.max():5.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
