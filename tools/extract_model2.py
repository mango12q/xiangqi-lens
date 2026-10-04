"""精确解析 ONNX 模型边界并从 xqlink.exe 中提取。

ONNX 的 ModelProto（onnx.proto3）字段编号：
    1  ir_version        (varint)
    2  opset_import      (repeated OperatorSetIdProto, length-delimited)
    3  producer_name     (string)
    4  producer_version  (string)
    5  domain            (string)
    6  model_version     (varint)
    7  doc_string        (string)
    8  graph             (GraphProto, length-delimited)  <- 主体，占绝大部分
    14 metadata_props    (repeated StringStringEntryProto)

因此只要顺序读字段头，读到字段 8 时取其 length 就能算出模型总长度。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

WIRE_VARINT = 0
WIRE_64BIT = 1
WIRE_LEN = 2
WIRE_32BIT = 5


def read_varint(data: bytes, pos: int) -> tuple[int, int]:
    """读 varint，返回 (值, 新位置)。"""
    result = 0
    shift = 0
    while True:
        if pos >= len(data):
            raise ValueError("varint 越界")
        b = data[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
        if shift > 70:
            raise ValueError("varint 过长")


def parse_model_proto(data: bytes, start: int) -> dict:
    """解析从 start 开始的 ModelProto，返回字段信息与总长度。"""
    pos = start
    info: dict = {"start": start, "fields": {}, "graph_end": None}

    while pos < len(data):
        key, pos = read_varint(data, pos)
        field_no = key >> 3
        wire = key & 0x07

        if wire == WIRE_VARINT:
            val, pos = read_varint(data, pos)
            info["fields"].setdefault(field_no, val)
            if field_no == 1:
                info["ir_version"] = val
        elif wire == WIRE_LEN:
            ln, pos = read_varint(data, pos)
            payload = data[pos:pos + ln]
            if field_no == 3:
                info["producer_name"] = payload.decode("utf-8", "replace")
            elif field_no == 4:
                info["producer_version"] = payload.decode("utf-8", "replace")
            elif field_no == 2:
                # OperatorSetIdProto: 字段1=domain, 字段2=version
                try:
                    vpos = 0
                    while vpos < len(payload):
                        k2, vpos = read_varint(payload, vpos)
                        f2, w2 = k2 >> 3, k2 & 7
                        if w2 == WIRE_VARINT:
                            v2, vpos = read_varint(payload, vpos)
                            if f2 == 2:
                                info["opset"] = v2
                        elif w2 == WIRE_LEN:
                            l2, vpos = read_varint(payload, vpos)
                            vpos += l2
                        else:
                            break
                except Exception:
                    pass
            elif field_no == 8:
                # 找到 graph，模型到这里基本结束（后面只有 metadata_props 等小字段）
                info["graph_offset"] = pos
                info["graph_len"] = ln
                info["graph_end"] = pos + ln
            pos += ln
        elif wire == WIRE_64BIT:
            pos += 8
        elif wire == WIRE_32BIT:
            pos += 4
        else:
            raise ValueError(f"未知 wire type {wire} (字段 {field_no})")

        # 字段 8 之后还有可能有 metadata（14），继续往下读到文件尾会读到垃圾，
        # 这里在拿到 graph 后允许再读少量字段，一旦解不出合法字段头就停。
        if info.get("graph_end") is not None and pos > info["graph_end"] + 1024:
            break

    return info


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", default=r"C:\Program Files\xqlink\xqlink.exe")
    ap.add_argument("--offset", type=lambda s: int(s, 0), default=0x607B50)
    ap.add_argument("--outdir", default="analysis/extracted")
    args = ap.parse_args()

    exe = Path(args.exe)
    data = exe.read_bytes()
    print(f"文件: {exe}  {len(data) / 1024 / 1024:.2f} MB")
    print(f"模型起点: 0x{args.offset:X}")

    # 从起点往前找真正的 `08 xx 12` 头（偏移可能落在 producer 字符串上）
    start = args.offset
    for back in range(0, 64):
        cand = args.offset - back
        if cand < 0:
            break
        if data[cand] == 0x08 and data[cand + 2] == 0x12:
            start = cand
            break
    print(f"校正后起点: 0x{start:X}  首字节: {data[start:start + 12].hex()}")

    info = parse_model_proto(data, start)
    print()
    print("=== ModelProto 解析结果 ===")
    for k in ("ir_version", "producer_name", "producer_version", "opset"):
        if k in info:
            print(f"  {k}: {info[k]}")
    if "graph_end" in info:
        print(f"  graph: 偏移 {info['graph_offset']}  长度 {info['graph_len']} "
              f"({info['graph_len'] / 1024 / 1024:.2f} MB)")
        print(f"  graph 结束于: {info['graph_end']} (0x{info['graph_end']:X})")

    end = info.get("graph_end")
    if end is None:
        print("[失败] 未解析出 graph 字段")
        return 1

    # 模型 = [start, graph_end)，之后的字节属于 Rust 的其他数据
    blob = data[start:end]
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    out = outdir / "xqlink_large.onnx"
    out.write_bytes(blob)
    print()
    print(f"已提取: {out}  {len(blob) / 1024 / 1024:.2f} MB")

    # 验证
    print()
    print("=== onnxruntime 加载验证 ===")
    try:
        import onnxruntime as ort
        sess = ort.InferenceSession(str(out), providers=["CPUExecutionProvider"])
        print(f"  输入: {[(i.name, i.shape, i.type) for i in sess.get_inputs()]}")
        print(f"  输出: {[(o.name, o.shape) for o in sess.get_outputs()]}")
        print("  加载成功")
    except Exception as exc:
        print(f"  加载失败: {type(exc).__name__}: {str(exc)[:400]}")
        return 2

    # 继续往后找第二个模型（rotate 版）
    print()
    print("=== 搜索同文件内是否有第二个模型 ===")
    nxt = data.find(b"\x08\x07\x12\x07pytorch", end)
    print(f"  下一个 'pytorch' 生产者标记位置: "
          f"{'0x%X' % nxt if nxt > 0 else '无'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
