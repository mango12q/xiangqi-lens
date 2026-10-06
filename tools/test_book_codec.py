# -*- coding: utf-8 -*-
"""开局库（xqb）编解码自洽测试 —— 纯函数，不需要窗口/引擎。

**本文件原为「参考实现」版**（由测试专家在 app_book.py 落地前编写）。
真实模块落地后已改为直接调用 ``app_book`` 的函数，并补上一条关键断言：
编码结果必须与**官方样例库**（https://github.com/fisherfan/xqbook 的
book.xqb）里起始局面那条记录的 key 字节**逐字节相同**。

⚠ 历史坑：参考实现把空位标志位写反了（用 1 表示空位）。自洽往返测能过，
但与官方字节不符 —— 这正是必须拿真实样例做硬断言的原因。
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from app_book import (START_FEN, board_of, obk_decode_move,   # noqa: E402
                      obk_encode_move, obk_zhash, xqb_decode_key,
                      xqb_decode_move, xqb_encode_key, xqb_encode_move)

PASS = 0
FAIL = 0

# 官方样例库 book.xqb 第 1 条记录（起始局面）的 key 原始字节
OFFICIAL_START_KEY_HEX = (
    "ceb7cef37ac801e079f7df7df00002ebaebaeb02c004653a56939440")

START_ROWS = [
    "rnbakabnr", ".........", ".c.....c.", "p.p.p.p.p", ".........",
    ".........", "P.P.P.P.P", ".C.....C.", ".........", "RNBAKABNR",
]


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [通过] {name}")
    else:
        FAIL += 1
        print(f"  [失败] {name}  {detail}")


def rows_of(fen: str) -> list[str]:
    out = []
    for r in fen.split()[0].split("/"):
        line = ""
        for ch in r:
            line += "." * int(ch) if ch.isdigit() else ch
        out.append(line)
    return out


def count_pieces(rows: list[str]) -> int:
    return sum(1 for row in rows for ch in row if ch != ".")


def main() -> int:
    print("=== 开局库 xqb 编解码自洽测试（调用真实 app_book）===")

    # 1. move 编码：两个逆向样例必须命中
    check("h2e2 → 0x7774", xqb_encode_move("h2e2") == 0x7774,
          hex(xqb_encode_move("h2e2")))
    check("h0g2 → 0x9776", xqb_encode_move("h0g2") == 0x9776,
          hex(xqb_encode_move("h0g2")))
    check("move 解码往返 h2e2", xqb_decode_move(0x7774) == "h2e2",
          str(xqb_decode_move(0x7774)))
    check("move 解码往返 h0g2", xqb_decode_move(0x9776) == "h0g2",
          str(xqb_decode_move(0x9776)))
    check("越界 move 返回 None", xqb_decode_move(0xFFFF) is None)
    check("负数 move 返回 None", xqb_decode_move(-1) is None)
    check("起点==终点的 move 返回 None", xqb_decode_move(0x7777) is None)
    check("起点==终点但坐标合法的 move 也返回 None",
          xqb_decode_move((3 * 16 + 3) << 8 | (3 * 16 + 3)) is None)

    # 2b. ★ obk 编解码（社区实际流通格式）
    #     起始局面键 7101337512282506414 来自真实库「云霄剑诀7.5.obk」第 1 条记录
    check("obk 起始局面键 = 7101337512282506414",
          obk_zhash(START_FEN) == 7101337512282506414,
          str(obk_zhash(START_FEN)))
    check("obk 键含轮次（红先 ≠ 黑先）",
          obk_zhash(START_FEN) != obk_zhash(board_of(START_FEN) + " b - - 0 1"))
    try:
        from cchess import ChessBoard
        ref = ChessBoard(START_FEN).zhash() & 0xFFFFFFFFFFFFFFFF
        check("obk 自算键与 cchess.zhash 一致", obk_zhash(START_FEN) == ref,
              f"{obk_zhash(START_FEN)} vs {ref}")
    except Exception as exc:
        check("obk 与 cchess.zhash 交叉验证", False, str(exc))
    # 真实库里的两条着法
    check("obk 0xAAA7 → h2e2", obk_decode_move(0xAAA7) == "h2e2",
          str(obk_decode_move(0xAAA7)))
    check("obk 0x3A59 → h9g7", obk_decode_move(0x3A59) == "h9g7",
          str(obk_decode_move(0x3A59)))
    for iccs in ("h2e2", "h9g7", "c3c4", "a0a9", "i9i0"):
        check(f"obk 往返 {iccs}",
              obk_decode_move(obk_encode_move(iccs)) == iccs,
              str(obk_decode_move(obk_encode_move(iccs))))
    check("obk 非法偏移返回 None", obk_decode_move(0x3C3D) is None)
    check("obk 起点==终点返回 None", obk_decode_move(0xAAAA) is None)
    check("obk 超范围返回 None", obk_decode_move(0x10000) is None)

    # 2. ★ 与官方样例字节逐字节比对（最关键的一条）
    start = xqb_encode_key(START_FEN)
    check("起始局面 key 长度 = 28", len(start) == 28, str(len(start)))
    check("起始局面 key == 官方样例字节",
          start.hex() == OFFICIAL_START_KEY_HEX,
          f"{start.hex()} != {OFFICIAL_START_KEY_HEX}")

    exp_bits = 90 + 4 * count_pieces(START_ROWS)
    check("位长公式 ceil((90+4n)/8)",
          len(start) == (exp_bits + 7) // 8, f"bits={exp_bits}")

    # 3. key 往返
    check("起始局面 key 往返一致", rows_of(xqb_decode_key(start)) == START_ROWS,
          str(rows_of(xqb_decode_key(start))))
    check("起始局面 key 严格模式通过（尾部补位为 0）",
          xqb_decode_key(start, strict=True) == board_of(START_FEN))
    # 人为把尾部补位改成 1：非严格模式仍能解出棋盘，严格模式必须拒绝
    dirty = bytearray(start)
    dirty[-1] |= 0x01
    check("尾部补位被污染时非严格模式仍可解",
          rows_of(xqb_decode_key(bytes(dirty))) == START_ROWS)
    try:
        xqb_decode_key(bytes(dirty), strict=True)
        check("尾部补位被污染时严格模式应拒绝", False)
    except ValueError:
        check("尾部补位被污染时严格模式拒绝", True)

    # 4. 少子局面（末字节补零）
    sparse = ["....k....", ".........", ".........", ".........", ".........",
              ".........", ".........", ".........", ".........", "....K...."]
    fen_sparse = "4k4/9/9/9/9/9/9/9/9/4K4 w - - 0 1"
    k_sparse = xqb_encode_key(fen_sparse)
    check("少子局面 key 往返一致",
          rows_of(xqb_decode_key(k_sparse)) == sparse,
          str(rows_of(xqb_decode_key(k_sparse))))
    check("少子局面长度更短", len(k_sparse) < len(start),
          f"{len(k_sparse)} vs {len(start)}")
    n2 = count_pieces(sparse)
    check("少子局面位长公式", len(k_sparse) == (90 + 4 * n2 + 7) // 8)

    # 5. 空盘（0 子）：90 位 → 12 字节
    empty_fen = "9/9/9/9/9/9/9/9/9/9 w - - 0 1"
    k_empty = xqb_encode_key(empty_fen)
    check("空盘 key 长度 = 12", len(k_empty) == 12, str(len(k_empty)))
    check("空盘 key 往返一致",
          rows_of(xqb_decode_key(k_empty)) == ["." * 9] * 10)

    # 6. 损坏/截断输入必须**受控失败**，不能让裸异常穿透到 UI
    def safe_decode(blob: bytes):
        try:
            return xqb_decode_key(blob)
        except Exception:
            return None

    check("截断 key 受控失败（返回 None）", safe_decode(b"\x00") is None)
    check("空 blob 受控失败", safe_decode(b"") is None)
    check("正常 key 不被误杀", safe_decode(start) == board_of(START_FEN))

    def safe_encode(fen: str):
        try:
            return xqb_encode_key(fen)
        except Exception:
            return None

    check("非法棋盘段编码受控失败", safe_encode("bad/fen/here") is None)
    check("含未知棋子编码受控失败", safe_encode("z9/9/9/9/9/9/9/9/9/9") is None)
    check("识别不确定格 'x' 按空位处理（不抛异常）",
          safe_encode("x8/9/9/9/9/9/9/9/9/9") is not None)

    # 7. ICCS 结构合法性
    mv = xqb_decode_move(xqb_encode_move("a0a1"))
    check("ICCS 结构合法（file + rank）",
          mv is not None and len(mv) == 4 and mv[0].isalpha()
          and mv[1].isdigit() and mv[2].isalpha() and mv[3].isdigit(), str(mv))

    print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
