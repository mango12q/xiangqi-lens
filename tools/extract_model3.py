"""精确提取 xqlink.exe 内嵌 ONNX 模型（定长版）。

解析序列（实测）：
    offset 0x607B50:  08 07                    ir_version = 7
                      12 07 "pytorch"          producer_name
                      1a 07 "pytorch"          producer_version
                      22 04 "ai.onnx"          domain
                      2a 0f "..."              doc_string/model_version 等
                      42 <varint>              graph  (字段 8, length-delimited)

读到字段 8 即停止 —— 其后是 Rust 的静态数据段，不是模型内容。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def read_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = 0
    shift = 0
    while True:
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
        if shift > 70:
            raise ValueError("varint 过长")


def scan_model(data: bytes, start: int, max_fields: int = 40) -> dict:
    """从 start 顺序扫描 ModelProto 顶层字段，遇到 graph(8) 即返回。"""
    pos = start
    info: dict = {"start": start}
    for _ in range(max_fields):
        key, pos = read_varint(data, pos)
        field_no, wire = key >> 3, key & 0x07

        if wire == 0:
            val, pos = read_varint(data, pos)
            info[f"field{field_no}_varint"] = val
        elif wire == 2:
            ln, pos = read_varint(data, pos)
            payload = data[pos:pos + ln]
            if field_no == 3:
                info["producer_name"] = payload.decode("utf-8", "replace")
            elif field_no == 4:
                info["producer_version"] = payload.decode("utf-8", "replace")
            elif field_no == 6:
                info["domain"] = payload.decode("utf-8", "replace")
            elif field_no == 7:
                info["doc_string"] = payload.decode("utf-8", "replace")[:80]
            elif field_no == 8:
                info["graph_offset"] = pos
                info["graph_len"] = ln
                info["end"] = pos + ln
                return info
            elif field_no == 2:
                info["opset_raw_len"] = ln
            pos += ln
        elif wire == 5:
            pos += 4
        elif wire == 1:
            pos += 8
        else:
            raise ValueError(f"未知 wire type {wire} (字段 {field_no})")
    return info


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", default=r"C:\Program Files\xqlink\xqlink.exe")
    ap.add_argument("--outdir", default="analysis/extracted")
    args = ap.parse_args()

    exe = Path(args.exe)
    data = exe.read_bytes()
    print(f"文件: {exe}  {len(data) / 1024 / 1024:.2f} MB")

    # 用精确的 pytorch 生产者签名定位模型起点
    sig = b"\x08\x07\x12\x07pytorch"
    offsets = []
    i = 0
    while True:
        i = data.find(sig, i)
        if i < 0:
            break
        offsets.append(i)
        i += 1
    print(f"找到 {len(offsets)} 个 pytorch 模型签名: {[hex(o) for o in offsets]}")

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    ok = 0

    for n, start in enumerate(offsets):
        print()
        print(f"===== 模型 {n + 1} @ 0x{start:X} =====")
        try:
            info = scan_model(data, start)
        except Exception as exc:
            print(f"  解析失败: {exc}")
            continue
        for k in ("producer_name", "producer_version", "domain", "doc_string",
                  "field1_varint"):
            if k in info:
                print(f"  {k} = {info[k]}")
        if "end" not in info:
            print("  未找到 graph 字段")
            continue
        print(f"  graph: 偏移 {info['graph_offset']} 长度 "
              f"{info['graph_len'] / 1024 / 1024:.2f} MB")
        print(f"  模型区间: [{start}, {info['end']})  "
              f"总长 {(info['end'] - start) / 1024 / 1024:.2f} MB")

        blob = data[start:info["end"]]
        out = outdir / f"xqlink_model{n + 1}.onnx"
        out.write_bytes(blob)
        print(f"  已写出: {out}")

        try:
            import onnxruntime as ort
            sess = ort.InferenceSession(str(out), providers=["CPUExecutionProvider"])
            print("  onnxruntime 验证: 成功")
            for x in sess.get_inputs():
                print(f"    输入 {x.name}  {x.shape}  {x.type}")
            for x in sess.get_outputs():
                print(f"    输出 {x.name}  {x.shape}")
            ok += 1
        except Exception as exc:
            print(f"  加载失败: {type(exc).__name__}: {str(exc)[:300]}")

    print()
    print(f"成功提取并验证 {ok} 个模型")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
