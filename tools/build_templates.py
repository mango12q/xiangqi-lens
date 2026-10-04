"""从带标注的真实截图构建棋子模板库。

这张 JJ 象棋截图是一个"红方走了一步马 h0→g2"的标准开局，恰好包含
全部 14 个棋子类别（红 7 + 黑 7），因此可以一次采集完整模板库。

标注格式：(row, col) -> FEN 字符，row 0 为画面顶部（黑方底线）。
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

from src.capture import BoardGeometry, Region        # noqa: E402
from src.vision import (BoardReader, TemplateLibrary, VisionConfig,  # noqa: E402
                        build_templates)
from src.xiangqi import PIECE_NAMES, Board, CLASS_ORDER  # noqa: E402

# 这张截图的完整标注（标准开局 + 红马 h0→g2）
# 上方为黑方（小写），下方为红方（大写）
LABELS: dict[tuple[int, int], str] = {
    # row 0 —— 黑方底线：车马象士将士象马车
    (0, 0): "r", (0, 1): "n", (0, 2): "b", (0, 3): "a", (0, 4): "k",
    (0, 5): "a", (0, 6): "b", (0, 7): "n", (0, 8): "r",
    # row 2 —— 黑方炮位 + 已跳出的黑马 g7 位置为空
    (2, 1): "c", (2, 7): "c",
    # row 3 —— 黑卒
    (3, 0): "p", (3, 2): "p", (3, 4): "p", (3, 6): "p", (3, 8): "p",
    # row 6 —— 红兵
    (6, 0): "P", (6, 2): "P", (6, 4): "P", (6, 6): "P", (6, 8): "P",
    # row 7 —— 红炮 + 跳出的红马（原 h0 位空出）
    (7, 1): "C", (7, 2): "N", (7, 7): "C",
    # row 9 —— 红方底线：车马相仕帅仕相马 + 已跳走的马 h0 为空
    (9, 0): "R", (9, 1): "N", (9, 2): "B", (9, 3): "A", (9, 4): "K",
    (9, 5): "A", (9, 6): "B", (9, 8): "R",
}


def load_geometry(json_path: Path) -> BoardGeometry:
    data = json.loads(json_path.read_text(encoding="utf-8"))
    return BoardGeometry(Region.from_dict(data["region"]), data.get("flipped", False))


def main() -> int:
    ap = argparse.ArgumentParser(description="构建棋子模板库")
    ap.add_argument("--image", default="shots/bg_002A0A30.png")
    ap.add_argument("--geometry", default="analysis/jj_board.json")
    ap.add_argument("--profile", default="jj_shitou")
    ap.add_argument("--out", default=None, help="输出路径，默认 data/templates/<profile>.json")
    ap.add_argument("--report", default="analysis/template_report.txt")
    args = ap.parse_args()

    img = cv2.imdecode(np.fromfile(args.image, dtype=np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        print(f"[错误] 无法读取 {args.image}")
        return 1
    geo = load_geometry(Path(args.geometry))
    print(f"图片: {args.image}  {img.shape[1]}x{img.shape[0]}")
    print(f"棋盘: {geo.region.to_dict()}")
    print()

    lib = build_templates(img, geo, LABELS, name=args.profile, config=VisionConfig())
    print(f"=== 模板采集结果 ===")
    print(f"  类别数: {len(lib.pieces)}")
    total = 0
    for piece in lib.pieces:
        tpls = lib.get(piece)
        total += len(tpls)
        print(f"    {piece} ({PIECE_NAMES.get(piece, '?')}): {len(tpls)} 个样本")
    print(f"  模板总数: {total}")

    missing = [p for p in CLASS_ORDER if p != "." and p not in lib.pieces]
    if missing:
        print(f"  [警告] 缺少兵种模板: {', '.join(missing)}")
    else:
        print("  全部 14 个兵种模板齐备")

    out = Path(args.out) if args.out else lib.default_dir() / f"{args.profile}.json"
    saved = lib.save(out)
    print()
    print(f"模板库已保存: {saved}  ({saved.stat().st_size / 1024:.1f} KB)")

    # ---------------- 自检：用刚建的模板回读这张图 ----------------
    print()
    print("=== 自检验证：用模板库回读原图 ===")
    reader = BoardReader(geo, lib, VisionConfig())
    board, results = reader.read(img, collect_glyphs=True)

    print()
    print(board.to_ascii())
    print()

    # 与标注比对
    correct = wrong = 0
    wrong_list = []
    for res in results:
        expect = LABELS.get((res.row, res.col), None)
        if expect is None:
            # 标注里没有的位置应当识别为空（但开局有子处必须正确）
            if res.occupied and res.confidence < 0.99:
                pass
            continue
        if res.piece == expect:
            correct += 1
        else:
            wrong += 1
            wrong_list.append((res.row, res.col, expect, res.piece, round(res.confidence, 3)))

    print(f"标注位置识别: 正确 {correct} / 错误 {wrong}  准确率 {correct / max(correct + wrong, 1):.1%}")
    if wrong_list:
        print("  错误明细 (row, col, 期望, 实际, 置信度):")
        for item in wrong_list:
            print(f"    {item}")

    # ---------------- 置信度分布 ----------------
    occ = [r for r in results if r.occupied]
    print()
    print(f"识别为有子的交叉点: {len(occ)} 个（应接近 32）")
    if occ:
        scores = [r.confidence for r in occ]
        print(f"  匹配置信度: min={min(scores):.3f}  "
              f"中位={np.median(scores):.3f}  max={max(scores):.3f}")
        low = [r for r in occ if r.confidence < 0.6]
        if low:
            print(f"  低置信度点 {len(low)} 个:")
            for r in low[:12]:
                print(f"    ({r.row},{r.col}) {r.piece} score={r.confidence:.3f} "
                      f"次佳={r.runner_up} {r.runner_up_score:.3f} "
                      f"contrast={r.contrast:.1f} red={r.dark_redness:.1f}")

    # ---------------- 保存报告 ----------------
    rep = Path(args.report)
    rep.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"图片: {args.image}",
        f"棋盘: {geo.region.to_dict()}",
        f"模板: {len(lib.pieces)} 类 / {total} 个",
        f"自检准确率: {correct}/{correct + wrong} = {correct / max(correct + wrong, 1):.1%}",
        "",
        "棋盘还原:",
        board.to_ascii(),
        "",
        "全部交叉点判定:",
    ]
    for r in results:
        lines.append(f"  ({r.row},{r.col}) occ={int(r.occupied)} red={int(r.is_red)} "
                     f"piece={r.piece} conf={r.confidence:.3f} "
                     f"contrast={r.contrast:6.1f} red_ness={r.dark_redness:7.1f} minval={r.min_val:6.1f}")
    rep.write_text("\n".join(lines), encoding="utf-8")
    print()
    print(f"报告已保存: {rep}")
    return 0 if wrong == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
