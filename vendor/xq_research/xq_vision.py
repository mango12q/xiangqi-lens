# -*- coding: utf-8 -*-
"""
中国象棋「棋盘定位 + 90 交叉点分类」最小可运行推理管线（纯本地 ONNX，无 torch 依赖）。

模型来源：
  pose       : https://huggingface.co/spaces/yolo12138/Chinese_Chess_Recognition/tree/main/onnx/pose
  classifier : https://huggingface.co/spaces/yolo12138/Chinese_Chess_Recognition/tree/main/onnx/layout_recognition
许可：HF Space 声明 MIT；上游 https://github.com/TheOne1006/chinese-chess-recognition 为 Apache-2.0。

用法：
  python xq_vision.py <image.png> [--providers cuda|cpu]
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path

import cv2
import numpy as np
import onnxruntime as ort

# ---------------- 类别表（与模型训练一致，顺序不可改） ----------------
CLASSES = [
    "point", "other",
    "red_king", "red_advisor", "red_bishop", "red_knight", "red_rook", "red_cannon", "red_pawn",
    "black_king", "black_advisor", "black_bishop", "black_knight", "black_rook", "black_cannon", "black_pawn",
]
SHORT = {
    "point": ".", "other": "x",
    "red_king": "K", "red_advisor": "A", "red_bishop": "B", "red_knight": "N", "red_rook": "R",
    "red_cannon": "C", "red_pawn": "P",
    "black_king": "k", "black_advisor": "a", "black_bishop": "b", "black_knight": "n", "black_rook": "r",
    "black_cannon": "c", "black_pawn": "p",
}
MEAN = np.array([123.675, 116.28, 103.53], dtype=np.float32)
STD = np.array([58.395, 57.12, 57.375], dtype=np.float32)
BONE_NAMES = ["A0", "A8", "J0", "J8"]  # 黑方两角 / 红方两角


def grid_to_iccs(row: int, col: int) -> str:
    """矩阵下标 → ICCS 坐标（第 0 行 = 黑方底线 → 行 '9'）"""
    return f"{chr(ord('a') + col)}{9 - row}"


def iccs_to_grid(sq: str) -> tuple[int, int]:
    """ICCS 坐标 → 矩阵下标"""
    return 9 - int(sq[1]), ord(sq[0]) - ord('a')
DST_SIZE = (450, 500)   # 透视后棋盘尺寸 (w, h)，500/450 ≈ 10 行 9 列的交叉点间距
PADDING = 50


def make_session(path: str, prefer_cuda: bool = True) -> ort.InferenceSession:
    avail = ort.get_available_providers()
    providers = []
    if prefer_cuda and "CUDAExecutionProvider" in avail:
        providers.append("CUDAExecutionProvider")
    if "DmlExecutionProvider" in avail:
        providers.append("DmlExecutionProvider")
    providers.append("CPUExecutionProvider")
    return ort.InferenceSession(path, providers=providers)


# ---------------- pose: 4 个角点 ----------------
def _rotate_point(pt, angle_rad):
    sn, cs = np.sin(angle_rad), np.cos(angle_rad)
    return np.array([[cs, -sn], [sn, cs]]) @ pt


def _get_3rd_point(a, b):
    d = a - b
    return b + np.r_[-d[1], d[0]]


def _warp_matrix(center, scale, out_w, out_h, inv=False):
    src_dir = _rotate_point(np.array([scale[0] * -0.5, 0.0]), 0.0)
    dst_dir = np.array([out_w * -0.5, 0.0])
    src = np.zeros((3, 2), dtype=np.float32)
    src[0] = center
    src[1] = center + src_dir
    src[2] = _get_3rd_point(src[0], src[1])
    dst = np.zeros((3, 2), dtype=np.float32)
    dst[0] = [out_w * 0.5, out_h * 0.5]
    dst[1] = np.array([out_w * 0.5, out_h * 0.5]) + dst_dir
    dst[2] = _get_3rd_point(dst[0], dst[1])
    if inv:
        return cv2.getAffineTransform(np.float32(dst), np.float32(src))
    return cv2.getAffineTransform(np.float32(src), np.float32(dst))


def _fix_scale(scale, w, h):
    aspect = w / h
    if scale[0] > scale[1] * aspect:
        return [scale[0], scale[0] / aspect]
    return [scale[1] * aspect, scale[1]]


class Pose4Kpt:
    """输入整图(RGB) → 输出 4 个棋盘外角点（A0/A8/J0/J8）在原图中的像素坐标。"""

    def __init__(self, model_path, size=(256, 256), padding=1.25, prefer_cuda=True):
        self.sess = make_session(model_path, prefer_cuda)
        self.in_name = self.sess.get_inputs()[0].name
        self.size = size
        self.padding = padding

    def predict(self, img_rgb: np.ndarray):
        h, w = img_rgb.shape[:2]
        center = np.array([w / 2.0, h / 2.0], dtype=np.float32)
        scale = np.array([w, h], dtype=np.float32) * self.padding
        scale = np.array(_fix_scale(list(scale), *self.size), dtype=np.float32)
        mat = _warp_matrix(center, scale, *self.size)
        warp = cv2.warpAffine(img_rgb, mat, self.size, flags=cv2.INTER_LINEAR)
        x = ((warp.astype(np.float32) - MEAN) / STD).transpose(2, 0, 1)[None]
        simcc_x, simcc_y = self.sess.run(None, {self.in_name: x})
        xi = np.argmax(simcc_x[0], axis=1)
        yi = np.argmax(simcc_y[0], axis=1)
        kps = np.stack([xi / (self.size[0] * 2), yi / (self.size[1] * 2)], axis=1)  # 归一化
        scores = np.max(simcc_x[0], axis=1) * np.max(simcc_y[0], axis=1)
        kps[:, 0] *= self.size[0]
        kps[:, 1] *= self.size[1]
        inv = _warp_matrix(center, scale, *self.size, inv=True)
        kps = np.hstack([kps, np.ones((len(kps), 1))]) @ inv.T
        return kps, scores


# ---------------- 透视对齐 ----------------
def extract_board(img_rgb: np.ndarray, kps: np.ndarray):
    src = np.float32(kps)  # A0 A8 J0 J8
    dst = np.float32([
        [PADDING, PADDING],
        [DST_SIZE[0] - PADDING, PADDING],
        [PADDING, DST_SIZE[1] - PADDING],
        [DST_SIZE[0] - PADDING, DST_SIZE[1] - PADDING],
    ])
    mat = cv2.getPerspectiveTransform(src, dst)
    warped = cv2.warpPerspective(img_rgb, mat, DST_SIZE)
    return warped, mat


# ---------------- 90 点分类 ----------------
class BoardClassifier:
    def __init__(self, model_path, input_size=(280, 315), prefer_cuda=True):
        self.sess = make_session(model_path, prefer_cuda)
        self.in_name = self.sess.get_inputs()[0].name
        self.input_size = input_size  # (w, h)

    def predict(self, board_rgb: np.ndarray):
        img = cv2.resize(board_rgb, self.input_size)
        x = ((img.astype(np.float32) - MEAN) / STD).transpose(2, 0, 1)[None]
        (out,) = self.sess.run(None, {self.in_name: x})
        assert out.shape[1:] == (90, 16), out.shape
        idx = out[0].argmax(-1)
        conf = out[0][np.arange(90), idx]
        shorts = [SHORT[CLASSES[i]] for i in idx]
        rows = ["".join(shorts[i * 9:(i + 1) * 9]) for i in range(10)]
        return rows, conf.reshape(10, 9), idx.reshape(10, 9)


class XiangqiVision:
    def __init__(self, pose_path, cls_path, prefer_cuda=True):
        self.pose = Pose4Kpt(pose_path, prefer_cuda=prefer_cuda)
        self.cls = BoardClassifier(cls_path, prefer_cuda=prefer_cuda)

    def infer(self, img_rgb: np.ndarray):
        t0 = time.perf_counter()
        kps, kps_scores = self.pose.predict(img_rgb)
        t_pose = time.perf_counter() - t0
        t1 = time.perf_counter()
        warped, mat = extract_board(img_rgb, kps)
        rows, conf, idx = self.cls.predict(warped)
        t_cls = time.perf_counter() - t1
        return {
            "keypoints": kps, "keypoint_scores": kps_scores,
            "warped": warped, "warp_mat": mat,
            "rows": rows, "conf": conf, "idx": idx,
            "t_pose_ms": t_pose * 1e3, "t_cls_ms": t_cls * 1e3,
        }

    # 把棋盘交叉点 (row, col) 映射回原图像素坐标：直接对标准网格点做逆透视
    @staticmethod
    def grid_to_source(mat: np.ndarray, row: int, col: int):
        inv = np.linalg.inv(mat)
        x = PADDING + col * (DST_SIZE[0] - 2 * PADDING) / 8.0
        y = PADDING + row * (DST_SIZE[1] - 2 * PADDING) / 9.0
        p = inv @ np.array([x, y, 1.0])
        return float(p[0] / p[2]), float(p[1] / p[2])

    @staticmethod
    def to_fen(rows, side_to_move="w"):
        """90 格短标签 → 象棋 FEN（第 0 行 = 黑方底线）。"""
        board = []
        for r in rows:
            s, empty = "", 0
            for ch in r:
                if ch in ".x":          # 空位 / 识别失败都按空处理
                    empty += 1
                else:
                    if empty:
                        s += str(empty)
                        empty = 0
                    s += ch
            if empty:
                s += str(empty)
            board.append(s)
        return "/".join(board) + f" {side_to_move} - - 0 1"


def _default_research() -> Path:
    """尽力定位含 ``hf_model`` 的 ``xq_research`` 目录（不依赖 app_backend）。

    本模块是**独立可运行**的（``python xq_vision.py 图片.png``），所以不能
    import app_backend 来拿路径。原来这里硬编码了开发机的
    ``D:\\opencode\\xq_research\\...``，换台机器直接失效。
    """
    here = Path(__file__).resolve().parent
    for base in (here.parent, here.parent.parent, Path.cwd(),
                 Path.cwd().parent):
        cand = base / "xq_research"
        if (cand / "hf_model").is_dir():
            return cand
    return here.parent.parent / "xq_research"       # 兜底：让报错指出缺什么


def main():
    research = _default_research()
    ap = argparse.ArgumentParser()
    ap.add_argument("images", nargs="+")
    ap.add_argument("--pose", default=str(
        research / "hf_model" / "onnx" / "pose" / "4_v6-0301.onnx"))
    ap.add_argument("--cls", default=str(
        research / "hf_model" / "onnx" / "layout_recognition"
        / "nano_v3-0319.onnx"))
    ap.add_argument("--cpu", action="store_true", help="强制 CPU")
    ap.add_argument("--save", default=str(research / "shots" / "vision"))
    args = ap.parse_args()

    import os
    os.makedirs(args.save, exist_ok=True)
    v = XiangqiVision(args.pose, args.cls, prefer_cuda=not args.cpu)

    for p in args.images:
        bgr = cv2.imread(p)
        if bgr is None:
            print("读图失败:", p)
            continue
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        res = v.infer(rgb)
        print("=" * 78)
        print("图片:", p, "尺寸:", rgb.shape)
        print("角点(A0,A8,J0,J8):", np.round(res["keypoints"], 1).tolist(), "分数:", np.round(res["keypoint_scores"], 3).tolist())
        print(f"耗时: pose={res['t_pose_ms']:.1f}ms cls={res['t_cls_ms']:.1f}ms")
        print("棋盘(第0行=黑方底线):")
        for i, r in enumerate(res["rows"]):
            print(f"  {i}: {r}   minConf={res['conf'][i].min():.2f}")
        print("FEN:", v.to_fen(res["rows"]))
        base = os.path.splitext(os.path.basename(p))[0]
        cv2.imwrite(os.path.join(args.save, f"{base}_warped.png"), cv2.cvtColor(res["warped"], cv2.COLOR_RGB2BGR))
        vis = bgr.copy()
        pts = res["keypoints"].astype(int)
        for (x, y) in pts:
            cv2.circle(vis, (int(x), int(y)), 6, (0, 0, 255), -1)
        cv2.polylines(vis, [pts.reshape(-1, 1, 2)], True, (0, 255, 0), 2)
        cv2.imwrite(os.path.join(args.save, f"{base}_kpt.png"), vis)


if __name__ == "__main__":
    main()
