# -*- coding: utf-8 -*-
"""开局库（OpeningBook）行为测试 —— 覆盖加载、查询、合法性过滤、失败路径。

不依赖真实棋局窗口；引擎也不需要（线路库用 cchess 回放即可，
xqb 用临时构造的库文件）。若仓库里已有 data/opening.xqb（由
tools/build_book.py 生成），会额外做一次真实库的读回校验。
"""
from __future__ import annotations

import sqlite3
import struct
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from app_backend import is_legal_move, is_strict_legal_move    # noqa: E402
from app_book import (START_FEN, OpeningBook, ObkBook, XqbBook,   # noqa: E402
                      board_of, obk_encode_move, obk_zhash,
                      xqb_encode_key, xqb_encode_move)

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [通过] {name}")
    else:
        FAIL += 1
        print(f"  [失败] {name}  {detail}")


def make_xqb(path: Path, rows) -> None:
    """按 xqb 规格造一个最小库：rows = [(fen, iccs, weight), ...]"""
    conn = sqlite3.connect(str(path))
    conn.execute('CREATE TABLE "book" ("id" INTEGER PRIMARY KEY AUTOINCREMENT '
                 'NOT NULL, "key" BLOB, "move" INTEGER, "score" INTEGER, '
                 '"win" INTEGER, "draw" INTEGER, "lost" INTEGER, '
                 '"valid" INTEGER, "memo" TEXT)')
    conn.execute('CREATE INDEX "idxkey" ON "book" ("key" ASC)')
    conn.execute('CREATE TABLE "information" ("name" TEXT, "value" TEXT)')
    conn.execute('INSERT INTO information VALUES (?,?)', ("version", "1"))
    conn.execute('INSERT INTO information VALUES (?,?)', ("type", "xiangqi"))
    for fen, mv, win, valid in rows:
        conn.execute(
            "INSERT INTO book (key, move, score, win, draw, lost, valid) "
            "VALUES (?, ?, 0, ?, 0, 0, ?)",
            (sqlite3.Binary(xqb_encode_key(fen)), xqb_encode_move(mv),
             win, valid))
    conn.commit()
    conn.close()


def make_obk(path: Path, rows, store_high_as_double: bool = False) -> None:
    """按 obk 规格造一个最小库。

    rows = ``[(fen, iccs, weight, valid)]``。``store_high_as_double=True`` 时
    模拟那个**生成工具的坑**：把键的 8 字节按 double 存进 INTEGER 列。
    """
    conn = sqlite3.connect(str(path))
    conn.execute('CREATE TABLE bhobk([id] INTEGER PRIMARY KEY AUTOINCREMENT, '
                 '[vkey] INTEGER, [vmove] INTEGER, [vscore] INTEGER, '
                 '[vwin] INTEGER, [vdraw] INTEGER, [vlost] INTEGER, '
                 '[vvalid] INTEGER, [vmemo] BLOB, [vindex] INTEGER)')
    conn.execute('CREATE INDEX idxkey on bhobk(vkey)')
    for fen, mv, win, valid in rows:
        key = obk_zhash(fen)
        if store_high_as_double and key >= (1 << 63):
            key = struct.unpack("<d", struct.pack("<Q", key))[0]
        elif key >= (1 << 63):
            key -= (1 << 64)               # 存成有符号整数
        conn.execute(
            "INSERT INTO bhobk (vkey, vmove, vscore, vwin, vdraw, vlost, "
            "vvalid) VALUES (?, ?, 0, ?, 0, 0, ?)",
            (key, obk_encode_move(mv), win, valid))
    conn.commit()
    conn.close()


def main() -> int:
    print("=== 开局库行为测试 ===")
    tmp = Path(tempfile.mkdtemp(prefix="xqlbook_"))

    # ---- 1. xqb：正常读取 + 权重排序 ----
    print("\n① xqb 读取")
    p1 = tmp / "a.xqb"
    make_xqb(p1, [
        (START_FEN, "h2e2", 10, 1),      # 权重 20
        (START_FEN, "b2e2", 3, 1),       # 权重 6
        (START_FEN, "h0g2", 1, 0),       # valid=0 → 必须被跳过
    ])
    bk = OpeningBook(p1)
    check("类型识别为 xqb", bk.kind == "xqb", bk.kind)
    hits = bk.probe(START_FEN)
    moves = sorted(h["move"] for h in hits)
    check("命中 2 个着法（valid=0 被跳过）", len(hits) == 2, str(hits))
    check("着法集合正确", moves == ["b2e2", "h2e2"], str(moves))
    picked = bk.pick(START_FEN, "best")
    check("best 取权重最高 h2e2", picked and picked["move"] == "h2e2",
          str(picked))
    check("info 表可读", bk.info.get("type") == "xiangqi", str(bk.info))
    bk.close()

    # ---- 2. 库中着法在当前局面非法 → 被 legal_check 过滤 ----
    print("\n② 合法性过滤（轮次歧义）")
    bk = OpeningBook(p1)
    # 只允许 h0g2 —— 库里的两个着法都会被过滤掉，应返回 None
    only = bk.pick(START_FEN, "best",
                   legal_check=lambda f, m: m == "h0g2")
    check("非法着法被过滤 → None", only is None, str(only))
    some = bk.pick(START_FEN, "best", legal_check=lambda f, m: m == "b2e2")
    check("只放行 b2e2 时返回 b2e2", some and some["move"] == "b2e2",
          str(some))
    # 真实棋规校验：h2e2 在起始局面合法，a0a9 非法
    check("真实棋规：h2e2 合法", is_legal_move(START_FEN, "h2e2"))
    check("真实棋规：a0a9 非法", not is_legal_move(START_FEN, "a0a9"))
    bk.close()

    # ---- 3. 缓存不能跨轮次串味（同一布局、不同轮次）----
    print("\n③ 轮次隔离（严格棋规过滤）")
    bk = OpeningBook(p1)
    fen_w = board_of(START_FEN) + " w - - 0 1"
    fen_b = board_of(START_FEN) + " b - - 0 1"
    rw = bk.pick(fen_w, "best", legal_check=is_strict_legal_move)
    rb = bk.pick(fen_b, "best", legal_check=is_strict_legal_move)
    check("红轮命中 h2e2", rw is not None and rw["move"] == "h2e2", str(rw))
    check("黑轮无命中（红方着法对黑方非法）", rb is None, str(rb))
    rw2 = bk.pick(fen_w, "best", legal_check=is_strict_legal_move)
    check("重复查询结果一致（无脏缓存）", rw2 == rw, f"{rw2} vs {rw}")
    bk.close()

    # ---- 4. 失败路径 ----
    print("\n④ 失败路径")
    try:
        OpeningBook(tmp / "nope.xqb")
        check("不存在的文件应抛异常", False)
    except Exception as exc:
        check("不存在的文件抛异常", isinstance(exc, FileNotFoundError), str(exc))

    bad = tmp / "bad.xqb"
    bad.write_bytes(b"this is not sqlite")
    try:
        XqbBook(bad)
        check("非 sqlite 文件应抛异常", False)
    except Exception as exc:
        check("非 sqlite 文件受控失败", True, type(exc).__name__)

    empty_db = tmp / "empty.xqb"
    conn = sqlite3.connect(str(empty_db))
    conn.execute("CREATE TABLE other (x INT)")
    conn.commit()
    conn.close()
    try:
        XqbBook(empty_db)
        check("缺 book 表应抛异常", False)
    except Exception as exc:
        check("缺 book 表受控失败", isinstance(exc, ValueError), str(exc))

    try:
        OpeningBook(tmp / "x.doc")
        check("未知扩展名应抛异常", False)
    except Exception as exc:
        check("未知扩展名受控失败", isinstance(exc, ValueError), str(exc))

    # ---- 5. 线路库（txt / json）----
    print("\n⑤ 线路库")
    t = tmp / "lines.txt"
    t.write_text(
        "# 中炮对屏风马\n"
        "h2e2 h9g7 h0g2 i9h9\n"
        "*3 h2e2 h9g7 b0c2\n"
        "h2e2 坏着法 h0g2\n",            # 第 3 条会在坏着法处截断
        encoding="utf-8")
    lb = OpeningBook(t)
    check("类型识别为 line", lb.kind == "line", lb.kind)
    check("起始局面命中 h2e2", [h["move"] for h in lb.probe(START_FEN)]
          == ["h2e2"], str(lb.probe(START_FEN)))
    after = ("rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C2C4/9/RNBAKABNR"
             " b - - 0 1")
    nxt = sorted(h["move"] for h in lb.probe(after))
    check("中炮后命中 h9g7", nxt == ["h9g7"], str(nxt))
    check("坏着法只截断该条线路，不影响其它（合法前缀仍入库）",
          "3 条线路" in lb.describe() and "1 处无法解析" in lb.describe(),
          lb.describe())

    j = tmp / "lines.json"
    j.write_text('{"lines": [["h2e2", "h9g7"], '
                 '{"moves": ["b2e2"], "weight": 5}]}', encoding="utf-8")
    jb = OpeningBook(j)
    hj = jb.probe(START_FEN)
    check("json 库命中 2 个着法", len(hj) == 2, str(hj))
    check("json 权重生效",
          jb.pick(START_FEN, "best")["move"] == "b2e2",
          str(jb.pick(START_FEN, "best")))

    # ---- 6. obk（社区实际流通格式）----
    print("\n⑥ obk 读取")
    # 起始局面（红先）的键高位为 0 → 走索引；「炮二平五后(黑先)」的键高位为 1
    # → 会被生成工具存成 double，必须走 CAST 兜底。两种都要覆盖。
    AFTER_CANNON = ("rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C2C4/9/"
                    "RNBAKABNR b - - 0 1")
    check("起始局面键高位为 0（走索引路径）",
          obk_zhash(START_FEN) < (1 << 63), hex(obk_zhash(START_FEN)))
    check("炮二平五后键高位为 1（走 CAST 兜底）",
          obk_zhash(AFTER_CANNON) >= (1 << 63), hex(obk_zhash(AFTER_CANNON)))

    for tag, as_double in (("整数存储", False), ("double 存储（生成工具坑）", True)):
        p = tmp / ("obk_" + ("dbl" if as_double else "int") + ".obk")
        make_obk(p, [
            (START_FEN, "h2e2", 10, 1),
            (START_FEN, "b2e2", 3, 1),
            (START_FEN, "h0g2", 99, 0),          # valid=0 必须跳过
            (AFTER_CANNON, "h9g7", 7, 1),
        ], store_high_as_double=as_double)
        bk = OpeningBook(p)
        check(f"[{tag}] 类型识别为 obk", bk.kind == "obk", bk.kind)
        hits = bk.probe(START_FEN)
        check(f"[{tag}] 起始局面命中 2 着（valid=0 跳过）",
              sorted(h["move"] for h in hits) == ["b2e2", "h2e2"], str(hits))
        check(f"[{tag}] best 取权重最高 h2e2",
              bk.pick(START_FEN, "best", is_strict_legal_move)["move"] == "h2e2")
        hb = bk.probe(AFTER_CANNON)
        check(f"[{tag}] 黑方局面命中 h9g7（含高位键）",
              [h["move"] for h in hb] == ["h9g7"], str(hb))
        check(f"[{tag}] 描述串含记录数",
              "4 条记录" in bk.describe(), bk.describe())
        bk.close()

    # obk 的键含轮次 → 同一布局红/黑两轮次应查到不同结果
    p = tmp / "obk_int.obk"
    bk = OpeningBook(p)
    same_board_black = board_of(START_FEN) + " b - - 0 1"
    check("obk 键含轮次：黑轮查同一布局无命中",
          bk.probe(same_board_black) == [], str(bk.probe(same_board_black)))
    check("obk 用严格合法性过滤后仍能取到着法",
          bk.pick(START_FEN, "best", is_strict_legal_move) is not None)
    bk.close()

    # ---- 7. 真实库（若存在）----
    print("\n⑦ 真实库读回（若存在）")
    real_obk = Path.home() / "Downloads" / "云霄剑诀7.5.obk"
    if real_obk.is_file():
        bk = OpeningBook(real_obk)
        hits = bk.probe(START_FEN)
        check("真实 obk 起始局面有命中", len(hits) > 0, str(len(hits)))
        check("真实 obk 命中着法全部严格合法",
              all(is_strict_legal_move(START_FEN, h["move"]) for h in hits),
              str([h["move"] for h in hits]))
        bk.close()
    else:
        print(f"  [跳过] 未找到 {real_obk}")

    real = HERE / "data" / "opening.xqb"
    if real.is_file():
        rb2 = OpeningBook(real)
        rh = rb2.probe(START_FEN)
        check("data/opening.xqb 起始局面有命中", len(rh) > 0, str(rh))
        check("命中着法在起始局面合法",
              all(is_legal_move(START_FEN, h["move"]) for h in rh), str(rh))
        rb2.close()
    else:
        print("  [跳过] 尚未生成 data/opening.xqb"
              "（可运行 python tools/build_book.py 生成）")

    print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
