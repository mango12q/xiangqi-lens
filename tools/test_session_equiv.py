# -*- coding: utf-8 -*-
"""锁住「钉死模型输入维度」这件事：既不能丢性能，也不能改结果。

背景（第四轮评审 N1）：两个 ONNX 模型的输入声明带动态轴
（pose 是 ``['batch',3,256,256]``，cls 是 ``['batch',3,'height','width']``），
而管线喂入的尺寸其实是恒定的。带动态轴时 onnxruntime 的 DML provider 每帧走
慢路径 —— 实测 cls 124.45ms → 5.09ms（24.4x）、整体 ``infer`` 121.6ms → 8.8ms。
修法见 ``vendor/xq_research/xq_vision.py`` 的 ``make_session(fixed_dims=...)``。

这个测试守两件**会悄悄退化**的事：

1. **覆盖还在不在**：有人删掉 ``fixed_dims``、或换了模型（动态轴变了），
   性能会掉 13 倍而**没有任何报错** —— 第 1 组断言直接从会话声明的形状上抓。
2. **结果有没有变**：覆盖值必须与实际喂入尺寸严格相等。ORT 对尺寸错配会抛
   ``InvalidArgument``（第 4 组断言把「是硬失败而不是静默算错」也钉住），
   但覆盖本身仍可能改变 kernel 选择 → 输出有 1e-6 量级浮点噪声。
   第 2/3/5 组断言证明 **argmax / rows 不受影响**。

不需要识别模型的机器上会明确跳过（见 ``_common.skip``）。
"""
from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))          # 仓库根
sys.path.insert(0, str(HERE))                 # tools/（_common 在这里）

from _common import Checker, bootstrap, have_vision, skip    # noqa: E402

ROOT = bootstrap()

import xq_vision                                             # noqa: E402
from xq_vision import (BoardClassifier, Pose4Kpt,             # noqa: E402
                       XiangqiVision, make_session)

POSE_DIMS = {"batch": 1, "height": 256, "width": 256}
CLS_DIMS = {"batch": 1, "height": 315, "width": 280}
N_CLS, N_POSE = 12, 8


def _median_ms(fn, n: int = 5) -> float:
    fn()
    ts = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        ts.append((time.perf_counter() - t0) * 1000)
    return statistics.median(ts)


def _no_override_vision(pose_path, cls_path):
    """构造一个「不钉维度」的旧行为管线，用作等价性对照。

    要同时绕开两处：``make_session`` 的 fixed_dims，以及 ``assert_input_shape``
    （它按设计就是要在「没有覆盖」时报警，见 xq_vision.py）。
    只在这里临时打桩、``finally`` 恢复，不给生产代码留测试后门。
    """
    orig_make = xq_vision.make_session
    orig_assert = xq_vision.assert_input_shape

    def plain(path, prefer_cuda=True, fixed_dims=None):
        return orig_make(path, prefer_cuda, None)          # 丢弃 fixed_dims

    xq_vision.make_session = plain
    xq_vision.assert_input_shape = lambda *a, **k: None
    try:
        return XiangqiVision(pose_path, cls_path)
    finally:
        xq_vision.make_session = orig_make
        xq_vision.assert_input_shape = orig_assert


def main() -> int:
    if not have_vision():
        return skip("识别模型不在本机（xq_research/hf_model/... 两个 onnx）",
                    "模型不入库；本机跑需要先按 README「依赖资源」一节下载")

    from app_backend import CLS_ONNX, POSE_ONNX

    ck = Checker("会话输入维度：等价性与性能回归")
    rng = np.random.default_rng(20261008)

    print("  构建生产会话（应带 fixed_dims）与对照会话（不带）…")
    prod = XiangqiVision(str(POSE_ONNX), str(CLS_ONNX))
    base = _no_override_vision(str(POSE_ONNX), str(CLS_ONNX))

    # ---- 1) 覆盖还在不在（丢了就静默慢 13 倍）----
    got_pose = list(prod.pose.sess.get_inputs()[0].shape)
    got_cls = list(prod.cls.sess.get_inputs()[0].shape)
    ck.check("pose 输入形状已钉死为 (1,3,256,256)", got_pose == [1, 3, 256, 256],
             f"实际 {got_pose} —— fixed_dims 丢了或模型换了")
    ck.check("cls 输入形状已钉死为 (1,3,315,280)", got_cls == [1, 3, 315, 280],
             f"实际 {got_cls} —— fixed_dims 丢了或模型换了")
    ck.check("对照会话确实是动态维度（否则本测试无意义）",
             any(isinstance(d, str) for d in base.cls.sess.get_inputs()[0].shape),
             f"对照 cls 形状 {base.cls.sess.get_inputs()[0].shape}")

    # ---- 2) cls 输出等价 ----
    worst, bad = 0.0, 0
    for i in range(N_CLS):
        kind = i % 4
        if kind == 0:
            x = rng.random((1, 3, 315, 280), dtype=np.float32)
        elif kind == 1:
            x = np.full((1, 3, 315, 280), 0.02, np.float32)      # 近全黑
        elif kind == 2:
            x = (rng.random((1, 3, 315, 280)) > 0.5).astype(np.float32)   # 高对比
        else:
            x = np.clip(rng.random((1, 3, 315, 280), dtype=np.float32) * 0.3 + 0.5,
                        0, 1)
        oa = base.cls.sess.run(None, {base.cls.in_name: x})[0]
        ob = prod.cls.sess.run(None, {prod.cls.in_name: x})[0]
        worst = max(worst, float(np.abs(oa - ob).max()))
        if not (oa.argmax(-1) == ob.argmax(-1)).all():
            bad += 1
    ck.check(f"cls：{N_CLS} 个输入的 argmax 全一致", bad == 0,
             f"{bad} 个输入分类结果变了（最大绝对差 {worst:.2e}）")
    ck.check("cls：数值差在浮点噪声量级（<1e-4）", worst < 1e-4,
             f"最大绝对差 {worst:.3e} —— 超出浮点噪声，需人工确认")

    # ---- 3) pose 输出等价 ----
    worst_p, bad_p = 0.0, 0
    for _ in range(N_POSE):
        xp = rng.random((1, 3, 256, 256), dtype=np.float32)
        ra = base.pose.sess.run(None, {base.pose.in_name: xp})
        rb = prod.pose.sess.run(None, {prod.pose.in_name: xp})
        for u, w in zip(ra, rb):
            worst_p = max(worst_p, float(np.abs(u - w).max()))
            if not (u.argmax(-1) == w.argmax(-1)).all():
                bad_p += 1
    ck.check(f"pose：{N_POSE} 个输入的 argmax 全一致", bad_p == 0,
             f"{bad_p} 处不一致（最大绝对差 {worst_p:.2e}）")

    # ---- 4) 尺寸错配必须是硬失败，不能静默算错 ----
    loud = False
    try:
        prod.cls.sess.run(None, {prod.cls.in_name:
                                 np.zeros((1, 3, 320, 288), np.float32)})
    except Exception:
        loud = True
    ck.check("喂入不匹配尺寸时抛异常（不是静默算错）", loud,
             "ORT 未报错 —— 尺寸错配会静默产出错误分类")

    # ---- 5) 真实截图全链路（shots/ 不入库，没有就跳过这一组）----
    try:
        import glob
        import cv2
        files = sorted(glob.glob(str(ROOT / "shots" / "*.png")))
    except Exception:
        files = []
    if not files:
        # 注意用「[信息]」而不是「[跳过]」：run_tests.py 见到 [跳过] 会把**整个
        # 脚本**归类为 SKIP，而这里只是可选的补充一组，前面的断言已经跑过了。
        print("  [信息] shots/ 下没有截图，跳过「真实图全链路」一组")
    else:
        n = same = 0
        conf_delta = 0.0
        for f in files:
            img = cv2.imread(f)
            if img is None:
                continue
            rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            try:
                r0, r1 = base.infer(rgb), prod.infer(rgb)
            except Exception:
                continue
            n += 1
            same += int(r0["rows"] == r1["rows"])
            conf_delta = max(conf_delta, float(np.abs(r0["conf"] - r1["conf"]).max()))
        if n:
            ck.check(f"真实截图全链路：{n} 张图的 rows 全部相同", same == n,
                     f"{n - same} 张识别结果变了（置信度最大差 {conf_delta:.2e}）")
        else:
            print("  [信息] 截图都读不出棋盘，跳过「真实图全链路」一组")

    # ---- 6) 性能（只打印，不断言：CI 机器噪声大，断言会假红）----
    x = rng.random((1, 3, 315, 280), dtype=np.float32)
    t_base = _median_ms(lambda: base.cls.sess.run(None, {base.cls.in_name: x}))
    t_prod = _median_ms(lambda: prod.cls.sess.run(None, {prod.cls.in_name: x}))
    ratio = t_base / max(t_prod, 1e-9)
    print(f"  [信息] cls 单次：动态维度 {t_base:.1f}ms → 钉死后 {t_prod:.1f}ms"
          f"（{ratio:.1f}x）")
    print("         （不设阈值断言：CI 上机器噪声大，写死会变成假红）")

    return ck.done()


if __name__ == "__main__":
    raise SystemExit(main())
