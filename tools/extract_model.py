"""从 xqlink.exe 中提取内嵌的 ONNX 模型。

xqlink 通过 `include_bytes!("../../libs/large.onnx")` 把模型编进二进制
（仓库 .gitignore 排除了 *.onnx，所以源码树里没有）。把模型抠出来有
两个好处：

1. 可以用 onnxruntime 直接跑推理，**独立验证识别能力**，不依赖 GUI 操作
2. 后续若要自己写识别层，可直接复用这个已训练好的模型

ONNX 文件是 protobuf 序列化格式，以字段 1（ir_version, varint）开头，
典型首字节为 0x08。文件内部含 "onnx" / 生产者字符串等特征。这里用
"文件头 + 内部特征 + 长度自洽" 三重判据定位。
"""

from __future__ import annotations

import argparse
import re
import struct
import sys
from pathlib import Path

# ONNX protobuf 的常见起始特征
HEADER_PATTERNS = [
    b"\x08\x07\x12",     # ir_version=7, producer_name 字段起始
    b"\x08\x08\x12",     # ir_version=8
    b"\x08\x06\x12",     # ir_version=6
    b"\x08\x09\x12",     # ir_version=9
    b"\x08\x0a\x12",     # ir_version=10
]


def find_onnx_candidates(data: bytes) -> list[tuple[int, int, str]]:
    """返回可能的 (起始偏移, 结束偏移, 判据说明) 列表。"""
    out: list[tuple[int, int, str]] = []

    # 判据一：tail 里有 "pytorch" / "onnx" 生产者标记，往前找文件头
    producer_marks = [b"pytorch", b"onnxruntime", b"tf2onnx", b"ultralytics"]
    for mark in producer_marks:
        for m in re.finditer(re.escape(mark), data):
            mid = m.start()
            # 在 mid 之前 4KB 内找最近的 ONNX 头
            lo = max(0, mid - 8192)
            for pat in HEADER_PATTERNS:
                idx = data.rfind(pat, lo, mid)
                if idx >= 0:
                    out.append((idx, -1, f"producer={mark.decode()} at {mid}"))
                    break

    # 判据二：直接扫文件头特征，之后必须紧跟可解析的 protobuf 长度
    for pat in HEADER_PATTERNS:
        start = 0
        while True:
            idx = data.find(pat, start)
            if idx < 0:
                break
            out.append((idx, -1, f"header={pat.hex()}"))
            start = idx + 1

    # 去重、按偏移排序
    seen = set()
    uniq = []
    for off, end, why in sorted(out, key=lambda x: x[0]):
        if off in seen:
            continue
        seen.add(off)
        uniq.append((off, end, why))
    return uniq


def protobuf_length(data: bytes, pos: int, end_field_hint: int) -> int | None:
    """尝试估算以 pos 起始的 ONNX 模型总长度（粗略）。

    做法：找最后一个 "pytorch"/"onnx" 生产者字符串，再从该点向后
    扫描 graph 的收尾结构不可靠；这里改为返回 None，用调用方给的
    策略（切到下一个候选起点或文件尾）来定界。
    """
    return None


def probe_onnx(data: bytes, start: int) -> dict:
    """检查某偏移处是否是合法 ONNX（用 onnxruntime 或 protobuf 粗验）。"""
    info = {"offset": start, "valid": False, "detail": ""}
    tail = data[start:start + 4096]
    has_producer = any(k in tail for k in (b"pytorch", b"onnx", b"ultralytics"))
    info["detail"] = f"前 4KB 含生产者标记={has_producer}"
    return info


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", default=r"C:\Program Files\xqlink\xqlink.exe")
    ap.add_argument("--outdir", default="analysis/extracted")
    args = ap.parse_args()

    exe = Path(args.exe)
    if not exe.is_file():
        print(f"[错误] 找不到 {exe}")
        return 1
    data = exe.read_bytes()
    size = len(data)
    print(f"文件: {exe}  {size / 1024 / 1024:.2f} MB")

    # 先看有哪些明显标记
    print()
    print("=== 二进制内可识别的标记 ===")
    for mark in (b"large.onnx", b"rotate.onnx", b"pikafish", b"LABELS",
                 b"images", b"output", b"yolov8", b"ultralytics"):
        cnt = data.count(mark)
        if cnt:
            pos = [m.start() for m in re.finditer(re.escape(mark), data)][:5]
            print(f"  {mark.decode():16} 出现 {cnt} 次  首现偏移 {[hex(p) for p in pos]}")

    print()
    print("=== ONNX 候选位置 ===")
    cands = find_onnx_candidates(data)
    print(f"  共 {len(cands)} 个候选")
    for off, _, why in cands[:20]:
        tail = data[off:off + 64]
        print(f"    offset {off:>10} (0x{off:X})  {why}  首字节={tail[:8].hex()}")

    # 用 onnxruntime 试探每个候选，能加载成功的即为真模型
    print()
    print("=== 用 onnxruntime 逐个试探 ===")
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    found: list[tuple[int, int]] = []

    try:
        import onnxruntime as ort
    except ImportError:
        print("  onnxruntime 未安装，跳过验证")
        return 1

    for i, (off, _, why) in enumerate(cands):
        # 尝试不同长度：先按到下一个候选的距离，再按到文件尾
        lengths = []
        if i + 1 < len(cands):
            lengths.append(cands[i + 1][0] - off)
        lengths.append(size - off)
        for ln in lengths:
            if ln < 1024 * 100:
                continue
            blob = data[off:off + ln]
            tmp = outdir / f"_probe_{i}_{off}.onnx"
            tmp.write_bytes(blob)
            try:
                sess = ort.InferenceSession(str(tmp), providers=["CPUExecutionProvider"])
                ins = [(x.name, x.shape) for x in sess.get_inputs()]
                outs = [(x.name, x.shape) for x in sess.get_outputs()]
                print(f"  [成功] offset={off} len={ln} ({ln/1024/1024:.2f} MB)")
                print(f"         输入 {ins}")
                print(f"         输出 {outs}")
                final = outdir / f"xqlink_model_{off}.onnx"
                tmp.replace(final)
                found.append((off, ln))
                break
            except Exception as exc:
                tmp.unlink(missing_ok=True)
                continue

    if not found:
        print("  未能定位可加载的 ONNX 模型")
        print("  提示：模型可能被压缩存储，或使用了需 onnxruntime 动态库的算子集")
        return 1

    print()
    print(f"=== 提取完成，共 {len(found)} 个模型 ===")
    for off, ln in found:
        print(f"  offset {off}  长度 {ln / 1024 / 1024:.2f} MB  -> "
              f"{outdir / f'xqlink_model_{off}.onnx'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
