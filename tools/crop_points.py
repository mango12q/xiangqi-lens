"""裁剪指定交叉点的图像，用于人工核对识别结果。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.capture import BoardGeometry, Region  # noqa: E402


def main() -> int:
    img_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("shots/bg_002A0A30.png")
    geo_path = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("analysis/jj_board.json")
    out_path = Path(sys.argv[3]) if len(sys.argv) > 3 else Path("analysis/points_check.png")

    # 需要核对的交叉点（row, col）
    points = [(0, 1), (0, 2), (0, 7), (9, 1), (1, 3), (2, 3), (2, 4), (2, 6), (3, 5)]
    notes = {
        (0, 1): "expect n", (0, 2): "expect b", (0, 7): "expect n", (9, 1): "expect N",
        (1, 3): "expect . (误检)", (2, 3): "expect . (误检)", (2, 4): "expect . (误检)",
        (2, 6): "expect . (误检)", (3, 5): "expect . (误检)",
    }

    img = cv2.imdecode(np.fromfile(str(img_path), dtype=np.uint8), cv2.IMREAD_COLOR)
    data = json.loads(geo_path.read_text(encoding="utf-8"))
    geo = BoardGeometry(Region.from_dict(data["region"]), data.get("flipped", False))
    step = min(geo.cell_size())
    R = int(step * 0.62)

    cell = 2 * R + 24
    cols = 5
    rows = (len(points) + cols - 1) // cols
    sheet = np.full((rows * cell, cols * cell, 3), 240, np.uint8)

    for i, (r, c) in enumerate(points):
        x, y = geo.point_of(r, c)
        patch = img[max(0, y - R):y + R, max(0, x - R):x + R]
        rr, cc = divmod(i, cols)
        oy, ox = rr * cell + 12, cc * cell + 12
        h, w = patch.shape[:2]
        sheet[oy:oy + h, ox:ox + w] = patch
        # 圆心十字
        cv2.drawMarker(sheet, (ox + w // 2, oy + h // 2), (0, 0, 255),
                       cv2.MARKER_CROSS, 14, 1)
        cv2.putText(sheet, f"({r},{c})", (ox, oy - 2),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.42, (0, 0, 0), 1, cv2.LINE_AA)
        cv2.putText(sheet, notes[(r, c)], (ox, oy + h + 14),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.38, (0, 0, 200), 1, cv2.LINE_AA)
        cv2.rectangle(sheet, (ox - 1, oy - 1), (ox + w, oy + h), (150, 150, 150), 1)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imencode(".png", sheet)[1].tofile(str(out_path))
    print(f"已保存: {out_path}  {sheet.shape}")
    for r, c in points:
        x, y = geo.point_of(r, c)
        print(f"  ({r},{c}) -> 图像坐标 ({x},{y})  {notes[(r, c)]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
