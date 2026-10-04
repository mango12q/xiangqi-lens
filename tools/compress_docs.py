# -*- coding: utf-8 -*-
"""压缩 docs 下的展示图，减小仓库体积。

策略：
* 按目标宽度等比缩放
* 截图（UI）用 PNG（线条文字多，PNG 更清晰）
* 演示图（照片式棋盘）用 JPEG（渐变多，JPEG 压缩率高）
"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2

DOCS = Path(__file__).resolve().parent.parent / "docs"


def compress(src: Path, dst: Path, width: int, jpeg_quality: int = 88) -> tuple[int, int]:
    img = cv2.imdecode(np_fromfile(src), cv2.IMREAD_COLOR)
    if img is None:
        raise RuntimeError(f"读图失败: {src}")
    h, w = img.shape[:2]
    if w > width:
        nh = int(round(h * width / w))
        img = cv2.resize(img, (width, nh), interpolation=cv2.INTER_AREA)
    if dst.suffix.lower() in (".jpg", ".jpeg"):
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, jpeg_quality])
    else:
        ok, buf = cv2.imencode(".png", img, [cv2.IMWRITE_PNG_COMPRESSION, 9])
    if not ok:
        raise RuntimeError(f"编码失败: {dst}")
    buf.tofile(str(dst))
    return src.stat().st_size, dst.stat().st_size


def np_fromfile(p: Path):
    import numpy as np
    return np.fromfile(str(p), dtype=np.uint8)


def main() -> int:
    jobs = [
        ("screenshot.png", "screenshot.png", 560, 0),
        ("arrow-demo.png", "arrow-demo.jpg", 520, 88),
    ]
    for src_name, dst_name, width, quality in jobs:
        src = DOCS / src_name
        dst = DOCS / dst_name
        if not src.is_file():
            print(f"  跳过（不存在）: {src_name}")
            continue
        try:
            before, after = compress(src, dst, width, quality or 88)
            print(f"  {src_name} -> {dst_name}: {before/1024:.0f} KB -> {after/1024:.0f} KB "
                  f"({after/before*100:.0f}%)")
        except Exception as exc:
            print(f"  [失败] {src_name}: {exc}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
