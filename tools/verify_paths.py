# -*- coding: utf-8 -*-
"""验证资源路径解析（源码模式 + 模拟打包模式）。"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))


def main() -> int:
    from app_backend import CLS_ONNX, ENGINE_EXE, POSE_ONNX, RESEARCH, app_base

    print("=" * 70)
    print("资源路径解析验证")
    print("=" * 70)
    print(f"  sys.frozen   : {getattr(sys, 'frozen', False)}")
    print(f"  app_base()   : {app_base()}")
    print(f"  RESEARCH     : {RESEARCH}")
    print()

    ok = True
    for name, p in (("识别模型 pose", POSE_ONNX),
                    ("识别模型 cls", CLS_ONNX),
                    ("引擎 exe", ENGINE_EXE)):
        exists = p.is_file()
        size = f"{p.stat().st_size / 1024 / 1024:.1f} MB" if exists else "-"
        flag = "OK " if exists else "缺失"
        print(f"  [{flag}] {name:14} {size:>10}  {p}")
        if not exists:
            ok = False

    # 引擎同目录还必须有 nnue 权重，否则引擎会启动失败
    nnue = ENGINE_EXE.parent / "pikafish.nnue"
    exists = nnue.is_file()
    print(f"  [{'OK ' if exists else '缺失'}] {'引擎权重 nnue':14} "
          f"{(f'{nnue.stat().st_size/1024/1024:.1f} MB' if exists else '-'):>10}  {nnue}")
    if not exists:
        ok = False

    print()
    print(f"RESEARCH 下 hf_model 目录存在: {(RESEARCH / 'hf_model').is_dir()}")
    print()
    if ok:
        print("[通过] 所有资源就位")
        return 0
    print("[失败] 有资源缺失")
    return 1


if __name__ == "__main__":
    sys.exit(main())
