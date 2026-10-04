"""生成全盘对照图：每个交叉点标注坐标 + 识别结果 + 置信度。

用于人工快速核对识别是否准确 —— 一眼扫过即可发现错位或误判。
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

from src.capture import BoardGeometry, Region              # noqa: E402
from src.vision import BoardReader, TemplateLibrary, VisionConfig  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", default="shots/bg_002A0A30.png")
    ap.add_argument("--geometry", default="analysis/jj_board.json")
    ap.add_argument("--templates", default="data/templates/jj_shitou.json")
    ap.add_argument("--out", default="analysis/full_annotated.png")
    ap.add_argument("--scale", type=float, default=1.6)
    args = ap.parse_args()

    img = cv2.imdecode(np.fromfile(args.image, dtype=np.uint8), cv2.IMREAD_COLOR)
    data = json.loads(Path(args.geometry).read_text(encoding="utf-8"))
    geo = BoardGeometry(Region.from_dict(data["region"]), data.get("flipped", False))
    lib = TemplateLibrary.load(Path(args.templates))
    reader = BoardReader(geo, lib, VisionConfig())
    board, results = reader.read(img, collect_glyphs=True)

    # 在棋盘区域上下留白，用于写坐标
    pad_top, pad_bottom, pad_side = 34, 18, 8
    x, y, w, h = geo.region.x, geo.region.y, geo.region.w, geo.region.h
    canvas = img[max(0, y - pad_top):y + h + pad_bottom,
                 max(0, x - pad_side):x + w + pad_side].copy()
    canvas = cv2.resize(canvas, None, fx=args.scale, fy=args.scale,
                        interpolation=cv2.INTER_CUBIC)
    ox = int((x - max(0, x - pad_side)) * args.scale)
    oy = int((y - max(0, y - pad_top)) * args.scale)
    sx = args.scale * w / 8
    sy = args.scale * h / 9

    for res in results:
        px = int(ox + res.col * sx)
        py = int(oy + res.row * sy)
        if res.occupied:
            color = (0, 0, 255) if res.is_red else (255, 0, 0)
            label = res.piece
            if res.confidence < 0.6:
                color = (0, 165, 255)      # 低置信度标橙
            cv2.putText(canvas, label, (px - 8, py + 2),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.85, (0, 0, 0), 4, cv2.LINE_AA)
            cv2.putText(canvas, label, (px - 8, py + 2),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.85, color, 2, cv2.LINE_AA)
        tag = f"{chr(97 + res.col)}{9 - res.row}"
        cv2.putText(canvas, tag, (px - 12, py - 18),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.34, (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(canvas, tag, (px - 12, py - 18),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.34, (0, 220, 0), 1, cv2.LINE_AA)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imencode(".png", canvas)[1].tofile(str(out))
    print(f"全盘标注图: {out}  {canvas.shape}")

    print()
    print("=== 识别出的棋子一览（按位置排序）===")
    occ = [r for r in results if r.occupied]
    occ.sort(key=lambda r: (r.row, r.col))
    for r in occ:
        flag = "" if r.confidence >= 0.6 else "  <- 低置信度"
        print(f"  {chr(97 + r.col)}{9 - r.row}  ({r.row:2d},{r.col})  {r.piece}  "
              f"{'红' if r.is_red else '黑'}  conf={r.confidence:.3f}{flag}")
    print()
    print(f"总计 {len(occ)} 个棋子（标准开局为 32 个）")
    extra = len(occ) - 32
    if extra > 0:
        print(f"  多出 {extra} 个，可能是误检")
    elif extra < 0:
        print(f"  少了 {-extra} 个，可能是漏检")
    return 0


if __name__ == "__main__":
    sys.exit(main())
