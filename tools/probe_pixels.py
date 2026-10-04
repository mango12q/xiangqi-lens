"""对截图做像素级探查，用于校准棋盘分割参数。

输出：
* 若干水平/垂直扫描线上的颜色变化，定位棋盘边界
* HSV 分布统计
* 棋盘底色、棋子底色、红字、黑字的参考色值
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "shots/bg_002A0A30.png")
    img = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
    h, w = img.shape[:2]
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    print(f"{path.name} {w}x{h}")
    print()

    print("=== 垂直扫描线（取图像中线 x=w/2）颜色分段 ===")
    x = w // 2
    col = img[:, x]
    prev = None
    seg_start = 0
    for y in range(h):
        b, g, r = (int(v) for v in col[y])
        key = (b // 40, g // 40, r // 40)
        if prev is None:
            prev, seg_start = key, y
        elif key != prev:
            if y - seg_start >= 8:
                mid = (seg_start + y) // 2
                bm, gm, rm = (int(v) for v in col[mid])
                print(f"  y {seg_start:4d}-{y:4d} ({y-seg_start:4d}px)  BGR=({bm:3d},{gm:3d},{rm:3d})")
            prev, seg_start = key, y
    print(f"  y {seg_start:4d}-{h:4d} ({h-seg_start:4d}px)")

    print()
    print("=== 水平扫描线（取 y = 棋盘中部）===")
    ys = [int(h * f) for f in (0.25, 0.4, 0.5, 0.6)]
    for y in ys:
        row = img[y]
        prev = None
        seg_start = 0
        segs = []
        for xx in range(w):
            b, g, r = (int(v) for v in row[xx])
            key = (b // 40, g // 40, r // 40)
            if prev is None:
                prev, seg_start = key, xx
            elif key != prev:
                if xx - seg_start >= 10:
                    mid = (seg_start + xx) // 2
                    bm, gm, rm = (int(v) for v in row[mid])
                    segs.append(f"{seg_start}-{xx}({bm},{gm},{rm})")
                prev, seg_start = key, xx
        print(f"  y={y:4d}: " + "  ".join(segs[:12]))

    print()
    print("=== HSV 直方图（粗分箱）===")
    hh, ss, vv = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    for lo in range(0, 180, 20):
        m = (hh >= lo) & (hh < lo + 20)
        if m.sum() == 0:
            continue
        print(f"  H {lo:3d}-{lo+20:3d}: {m.mean()*100:5.1f}%  平均S={ss[m].mean():6.1f} 平均V={vv[m].mean():6.1f}")
    print()
    s_bins = [(0, 40), (40, 80), (80, 120), (120, 180), (180, 256)]
    for lo, hi in s_bins:
        m = (ss >= lo) & (ss < hi)
        print(f"  S {lo:3d}-{hi:3d}: {m.mean()*100:5.1f}%")
    v_bins = [(0, 60), (60, 100), (100, 150), (150, 200), (200, 256)]
    for lo, hi in v_bins:
        m = (vv >= lo) & (vv < hi)
        print(f"  V {lo:3d}-{hi:3d}: {m.mean()*100:5.1f}%")

    print()
    print("=== 参考采样点 ===")
    points = {
        "棋盘底色(河界空白)": (w // 2, int(h * 0.50)),
        "棋盘底色(左上空白)": (int(w * 0.42), int(h * 0.30)),
        "红兵棋子底": (int(w * 0.09), int(h * 0.615)),
        "黑卒棋子底": (int(w * 0.09), int(h * 0.44)),
    }
    for name, (px, py) in points.items():
        patch = img[max(0, py - 4):py + 4, max(0, px - 4):px + 4]
        b, g, r = (float(v) for v in patch.reshape(-1, 3).mean(axis=0))
        hh2, ss2, vv2 = (float(v) for v in hsv[max(0, py - 4):py + 4, max(0, px - 4):px + 4].reshape(-1, 3).mean(axis=0))
        print(f"  {name:22} @({px},{py})  BGR=({b:5.1f},{g:5.1f},{r:5.1f})  HSV=({hh2:5.1f},{ss2:5.1f},{vv2:5.1f})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
