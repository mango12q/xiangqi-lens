"""诊断棋盘候选掩码的连通块，用于校准比例与尺寸阈值。"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "shots/bg_002A0A30.png")
    img = cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
    h, w = img.shape[:2]
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    hh, ss, vv = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    print(f"{path.name} {w}x{h}")

    masks = {
        "石板色 H30-50,S<60,V100-200": ((hh >= 30) & (hh <= 50) & (ss < 60) & (vv > 100) & (vv < 200)),
        "宽松 S<70,V90-210": ((ss < 70) & (vv > 90) & (vv < 210)),
        "宽泛 S<90,V80-230": ((ss < 90) & (vv > 80) & (vv < 230)),
    }

    for name, m in masks.items():
        mask = (m.astype(np.uint8)) * 255
        print()
        print(f"=== {name} ===  覆盖 {m.mean()*100:.1f}%")

        for k_frac in (0.015, 0.03, 0.05):
            k = max(3, int(min(w, h) * k_frac) | 1)
            ker = cv2.getStructuringElement(cv2.MORPH_RECT, (k, k))
            mm = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, ker)
            mm = cv2.morphologyEx(mm, cv2.MORPH_OPEN, ker)
            cnts, _ = cv2.findContours(mm, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            blocks = []
            for c in cnts:
                x, y, bw, bh = cv2.boundingRect(c)
                if bw * bh < w * h * 0.03:
                    continue
                blocks.append((bw * bh, x, y, bw, bh))
            blocks.sort(reverse=True)
            print(f"  kern={k:3d} ({k_frac:.3f})  大块数={len(blocks)}")
            for area, x, y, bw, bh in blocks[:4]:
                print(f"      bbox=({x:4d},{y:4d},{bw:4d}x{bh:4d})  面积占比={area/(w*h)*100:5.1f}%  "
                      f"宽高比 h/w={bh/bw:.3f}  w/W={bw/w:.3f} h/H={bh/h:.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
