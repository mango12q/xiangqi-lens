"""用提取出的 xqlink 模型对真实截图做推理，验证识别能力。

完全复刻 xqlink 的推理流程（见 server/src/yolo.rs）：
  1. 图像 resize 到 640x640（Triangle 插值 / OpenCV INTER_TRIANGLE）
  2. 归一化到 0..1，转 CHW
  3. 输出 [1, 25200, 20]，取 [4] 为 objectness、[5:20] 为 15 类分数
  4. conf = objectness * max_class_prob，阈值 0.7
  5. NMS，IoU 阈值 0.5
  6. 每类数量上限过滤 LIMIT

额外做一步 xqlink 没做的优化：先在原图上裁出棋盘区域再缩放，
避免整窗缩放导致棋子变小、长宽比失真。
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

# 与 xqlink 的 yolo.rs 完全一致
LABELS = ['n', 'b', 'a', 'k', 'r', 'c', 'p', 'R', 'N', 'A', 'K', 'B', 'C', 'P', '0']
LABEL_NAMES = {
    'n': '黑马', 'b': '黑象', 'a': '黑士', 'k': '黑将', 'r': '黑车', 'c': '黑炮', 'p': '黑卒',
    'R': '红车', 'N': '红马', 'A': '红仕', 'K': '红帅', 'B': '红相', 'C': '红炮', 'P': '红兵',
    '0': '标记',
}
LIMIT = [2, 2, 2, 1, 2, 2, 5, 2, 2, 2, 1, 2, 2, 5, 1]
CONF_THRESHOLD = 0.7
IOU_THRESHOLD = 0.5
MODEL_SIZE = 640
# Rust image crate 的 FilterType::Triangle == OpenCV 的 INTER_LINEAR
INTERP = cv2.INTER_LINEAR


def letterbox(img: np.ndarray, size: int = MODEL_SIZE,
              stretch: bool = False) -> tuple[np.ndarray, float, tuple[int, int]]:
    """缩放图像到 size x size。stretch=True 时不保持长宽比（复刻 xqlink 行为）。"""
    h, w = img.shape[:2]
    if stretch:
        return cv2.resize(img, (size, size), interpolation=INTERP), 1.0, (0, 0)
    scale = min(size / w, size / h)
    nw, nh = int(round(w * scale)), int(round(h * scale))
    resized = cv2.resize(img, (nw, nh), interpolation=INTERP)
    canvas = np.full((size, size, 3), 114, np.uint8)
    px, py = (size - nw) // 2, (size - nh) // 2
    canvas[py:py + nh, px:px + nw] = resized
    return canvas, scale, (px, py)


def preprocess(img: np.ndarray, stretch: bool) -> tuple[np.ndarray, float, tuple[int, int]]:
    lb, scale, pad = letterbox(img, MODEL_SIZE, stretch)
    rgb = cv2.cvtColor(lb, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    chw = np.transpose(rgb, (2, 0, 1))[None]     # 1,3,H,W
    return np.ascontiguousarray(chw), scale, pad


def iou_matrix(boxes: np.ndarray) -> np.ndarray:
    x1 = np.maximum(boxes[:, None, 0], boxes[None, :, 0])
    y1 = np.maximum(boxes[:, None, 1], boxes[None, :, 1])
    x2 = np.minimum(boxes[:, None, 2], boxes[None, :, 2])
    y2 = np.minimum(boxes[:, None, 3], boxes[None, :, 3])
    inter = np.clip(x2 - x1, 0, None) * np.clip(y2 - y1, 0, None)
    a1 = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
    union = a1[:, None] + a1[None, :] - inter
    return inter / np.maximum(union, 1e-9)


def nms_per_class(dets: list[dict]) -> list[dict]:
    """带每类数量上限的 NMS，严格复刻 xqlink 的 nms()。

    原版 Rust 逻辑：
      1. 按 confidence 升序排序，从尾部 pop（每次取最高分）
      2. 若该类已保留数量 +1 超过 LIMIT，直接丢弃
      3. 保留该检测，并把与它 IoU >= 阈值的候选全部移除
      4. 循环直到候选清空

    注意：IoU 抑制是**跨类**的（原版 retain 未判 class）—— 同一位置
    只可能有一个棋子，跨类抑制是正确行为。这里保持一致。
    """
    work = sorted(dets, key=lambda d: d["conf"])   # 升序，pop() 取最高分
    keep: list[dict] = []
    sizes = [0] * len(LABELS)

    while work:
        cur = work.pop()
        ci = cur["class_id"]
        if sizes[ci] + 1 > LIMIT[ci]:
            continue
        keep.append(cur)
        sizes[ci] += 1
        work = [o for o in work if _iou(cur["box"], o["box"]) < IOU_THRESHOLD]
    return keep


def _iou(a: np.ndarray, b: np.ndarray) -> float:
    iw = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    ih = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = iw * ih
    aa = max(0.0, a[2] - a[0]) * max(0.0, a[3] - a[1])
    ba = max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])
    union = aa + ba - inter
    return float(inter / union) if union > 1e-9 else 0.0


def detect(session, img: np.ndarray, stretch: bool = False,
           conf_thr: float = CONF_THRESHOLD) -> list[dict]:
    inp, scale, pad = preprocess(img, stretch)
    out = session.run(None, {"images": inp})[0]      # 1, 25200, 20
    pred = out[0]
    n = pred.shape[0]
    print(f"  模型输出: {out.shape}  (候选框 {n} 个)")

    cls_scores = pred[:, 5:20]
    obj = pred[:, 4]
    class_ids = cls_scores.argmax(axis=1)
    max_cls = cls_scores[np.arange(n), class_ids]
    conf = obj * max_cls
    mask = conf >= conf_thr
    print(f"  置信度 >= {conf_thr}: {int(mask.sum())} 个候选")

    dets = []
    for i in np.nonzero(mask)[0]:
        cx, cy, w, h = pred[i, 0], pred[i, 1], pred[i, 2], pred[i, 3]
        box = np.array([cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2])
        dets.append({"box": box, "conf": float(conf[i]),
                     "class_id": int(class_ids[i]), "label": LABELS[class_ids[i]]})

    final = nms_per_class(dets)
    print(f"  NMS + 数量上限过滤后: {len(final)} 个棋子")

    # 把框坐标反映射回原图（letterbox 时需还原）
    for d in final:
        b = d["box"].copy()
        if not stretch:
            px, py = pad
            b[0] = (b[0] - px) / scale
            b[2] = (b[2] - px) / scale
            b[1] = (b[1] - py) / scale
            b[3] = (b[3] - py) / scale
        d["box"] = b
    return final


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="analysis/extracted/xqlink_model1.onnx")
    ap.add_argument("--image", default="shots/bg_002A0A30.png")
    ap.add_argument("--crop", default=None, help="棋盘区域 x,y,w,h；不填则用整图")
    ap.add_argument("--stretch", action="store_true",
                    help="复刻 xqlink 的直接拉伸（不保持长宽比）")
    ap.add_argument("--conf", type=float, default=CONF_THRESHOLD)
    ap.add_argument("--out", default="analysis/detection_result.png")
    args = ap.parse_args()

    # 本脚本验证的是**从另一个项目提取出来的** YOLO 模型（xqlink），
    # 模型与截图都在 .gitignore 的 analysis/ 与 shots/ 下，CI 上必然没有。
    # 环境不具备不是回归 —— 明确跳过而不是报失败。
    if not Path(args.model).is_file():
        from _common import skip
        return skip(f"模型文件不在本机: {args.model}",
                    "先用 tools/extract_model.py 提取，或用 --model 指定路径")
    if not Path(args.image).is_file():
        from _common import skip
        return skip(f"测试截图不在本机: {args.image}",
                    "shots/ 已被 .gitignore 排除；用 --image 指定一张截图")

    import onnxruntime as ort
    sess = ort.InferenceSession(args.model, providers=["CPUExecutionProvider"])

    full = cv2.imdecode(np.fromfile(args.image, dtype=np.uint8), cv2.IMREAD_COLOR)
    print(f"图像: {args.image}  {full.shape[1]}x{full.shape[0]}")

    if args.crop:
        cx, cy, cw, ch = (int(v) for v in args.crop.split(","))
        img = full[cy:cy + ch, cx:cx + cw]
        print(f"裁剪棋盘区域: x={cx} y={cy} {cw}x{ch}")
    else:
        cx = cy = 0
        img = full

    print()
    print(f"=== 推理 (stretch={args.stretch}) ===")
    dets = detect(sess, img, args.stretch, args.conf)

    print()
    print("=== 检测结果 ===")
    by_label: dict[str, int] = {}
    for d in sorted(dets, key=lambda x: (x["class_id"], x["box"][1])):
        b = d["box"]
        cxm, cym = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        name = LABEL_NAMES.get(d["label"], d["label"])
        print(f"  {d['label']} ({name:4})  conf={d['conf']:.3f}  "
              f"中心=({cxm:6.1f},{cym:6.1f})  尺寸={b[2]-b[0]:5.1f}x{b[3]-b[1]:5.1f}")
        by_label[d["label"]] = by_label.get(d["label"], 0) + 1

    print()
    print("=== 按类别统计 ===")
    total = 0
    for lb in LABELS:
        if lb in by_label:
            print(f"  {lb} ({LABEL_NAMES.get(lb, lb)}): {by_label[lb]}  "
                  f"(上限 {LIMIT[LABELS.index(lb)]})")
            total += by_label[lb]
    print(f"  合计 {total} 个")

    red = sum(v for k, v in by_label.items() if k.isupper() and k != '0')
    black = sum(v for k, v in by_label.items() if k.islower())
    print(f"  红方 {red} 个 / 黑方 {black} 个  (标准开局各 16 个)")

    # 可视化
    vis = img.copy()
    for d in dets:
        b = d["box"]
        p1 = (int(b[0]), int(b[1]))
        p2 = (int(b[2]), int(b[3]))
        color = (0, 0, 255) if d["label"].isupper() else (255, 0, 0)
        cv2.rectangle(vis, p1, p2, color, 2)
        cv2.putText(vis, d["label"], (p1[0], p1[1] - 3),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 4, cv2.LINE_AA)
        cv2.putText(vis, d["label"], (p1[0], p1[1] - 3),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2, cv2.LINE_AA)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imencode(".png", vis)[1].tofile(str(out))
    print()
    print(f"可视化结果: {out}")

    Path("analysis/detection_result.json").write_text(
        json.dumps([{**{k: v for k, v in d.items() if k != 'box'},
                     "box": [round(float(x), 2) for x in d["box"]]} for d in dets],
                   ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
