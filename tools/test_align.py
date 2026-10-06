"""验证网格精化对识别准确率的影响。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from _common import bootstrap, skip                        # noqa: E402

bootstrap()

from src.capture import BoardGeometry, Region                    # noqa: E402
from src.vision import (BoardReader, TemplateLibrary, VisionConfig,  # noqa: E402
                        align_grid)

# 本脚本验证的是 src/ 那条早期自研路线，依赖三份**不入库**的本地产物：
#   shots/bg_002A0A30.png（截图）、analysis/jj_board.json（定位结果）、
#   data/templates/jj_shitou.json（模板库，可由 build_templates.py 重建）
_NEEDED = ("shots/bg_002A0A30.png", "analysis/jj_board.json",
           "data/templates/jj_shitou.json")


def main() -> int:
    missing = [p for p in _NEEDED if not Path(p).is_file()]
    if missing:
        return skip("缺少本地产物: " + ", ".join(missing),
                    "先截一张图跑 tools/analyze_board.py 与 "
                    "tools/build_templates.py 生成，然后重跑")
    img = cv2.imdecode(np.fromfile("shots/bg_002A0A30.png", dtype=np.uint8), cv2.IMREAD_COLOR)
    data = json.loads(Path("analysis/jj_board.json").read_text(encoding="utf-8"))
    raw = Region.from_dict(data["region"])
    print(f"原始自动定位: x={raw.x} y={raw.y} w={raw.w} h={raw.h}  "
          f"间距 {raw.w/8:.2f} x {raw.h/9:.2f}")
    print(f"  实测真值:   x=57  y=320  间距 71.2 x 70.7  (由棋子中心反推)")
    print()

    refined, quality = align_grid(img, raw, VisionConfig())
    print(f"精化后网格: x={refined.x} y={refined.y} w={refined.w} h={refined.h}  "
          f"间距 {refined.w/8:.2f} x {refined.h/9:.2f}")
    print(f"对齐质量: {quality:.3f}")
    print()

    lib = TemplateLibrary.load(Path("data/templates/jj_shitou.json"))
    cfg = VisionConfig()

    for name, region in (("精化前", raw), ("精化后", refined)):
        geo = BoardGeometry(region, False)
        reader = BoardReader(geo, lib, cfg)
        board, results = reader.read(img)
        occ = [r for r in results if r.occupied]
        confs = [r.confidence for r in occ]
        low = [r for r in occ if r.confidence < 0.6]
        print(f"=== {name} ===")
        print(board.to_ascii())
        print(f"  识别棋子数: {len(occ)}  (标准开局 32)")
        print(f"  置信度: min={min(confs):.3f} 中位={np.median(confs):.3f} "
              f"均值={np.mean(confs):.3f}")
        print(f"  低置信度(<0.6): {len(low)} 个")
        for r in low:
            print(f"    ({r.row},{r.col}) {r.piece} conf={r.confidence:.3f} "
                  f"次佳={r.runner_up} {r.runner_up_score:.3f}")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
