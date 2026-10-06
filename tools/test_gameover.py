# -*- coding: utf-8 -*-
"""终局判定与自动接盘测试。

分两层：
1. 纯函数层：``game_over_decision``（多信号投票规则）；
2. 真实棋规层：用 cchess 的 ``enumerate_legal_moves`` 验证「将死局面确实
   枚举不出合法着法、正常局面能枚举出」，这是终局判定最可信的那一路信号。

不需要窗口，也不需要引擎。
"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from app_backend import (enumerate_legal_moves, has_no_legal_moves,
                         is_legal_move, is_strict_legal_move)   # noqa: E402
from app_vision import game_over_decision                     # noqa: E402

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


START_FEN = "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR w - - 0 1"

# 构造的双车杀：黑将在 e9 被红车 a9 与 i9 封锁横线、红车 e8 将军；
# 黑将吃 e8 的车会与 e0 的红帅照面（非法）→ 无任何合法着法。
MATE_FEN = "R3k3R/4R4/9/9/9/9/9/9/9/4K4 b - - 0 1"

# 困毙（无子可动但未被将军）：黑只剩一个被困住的将，且未受攻击。
# 这里用一个近似构造，若 cchess 认为仍有着法则跳过该断言。
STALE_FEN = "9/9/9/9/9/9/9/9/4a4/3kK4 b - - 0 1"


def main() -> int:
    print("=== 终局判定与自动接盘测试 ===")

    # ---- 1. 纯函数判定规则 ----
    print("\n① game_over_decision 判定规则")
    check("正常局面 → 未结束", game_over_decision(False, False) == "",
          game_over_decision(False, False))
    check("无合法着法 → 立即结束", game_over_decision(True, False) != "")
    check("无合法着法优先于其它信号",
          "无合法着法" in game_over_decision(True, True, True))
    check("停滞但未开兜底 → 不结束（防对手长考误判）",
          game_over_decision(False, True, False) == "")
    check("停滞 + 开启兜底 → 结束",
          game_over_decision(False, True, True) != "")
    check("无停滞时兜底不生效",
          game_over_decision(False, False, True) == "")

    # ★ 关键回归：「引擎报绝杀分」曾被当作独立终局信号，那是错的 ——
    #   绝杀分只说明某一方**能够取胜**，我方即将杀棋时同样报绝杀，
    #   拿它判定会把正常对局误判成终局。这里把签名钉死，防止回退。
    import inspect
    params = list(inspect.signature(game_over_decision).parameters)
    check("绝杀分已从判定依据中移除（防「即将杀棋」被误判成终局）",
          "mate_seen" not in params, str(params))

    # ---- 2. 真实棋规：将死局面确实没有真合法着法 ----
    print("\n② 真实棋规（cchess）")
    n_start = len(enumerate_legal_moves(START_FEN))
    check("起始局面伪合法着法 = 44", n_start == 44, f"实得 {n_start}")
    check("起始局面 no_moves() = False",
          has_no_legal_moves(START_FEN) is False)

    check("双车杀局面 has_no_legal_moves = True",
          has_no_legal_moves(MATE_FEN) is True)
    check("该局面被 game_over_decision 判为结束",
          game_over_decision(has_no_legal_moves(MATE_FEN), False) != "")

    # ★ 回归：cchess 的 is_valid_iccs_move 是**伪合法**（不查自将/照面），
    #   所以终局判定绝不能用 enumerate_legal_moves。这里把差异钉死。
    print("\n③ 伪合法 vs 真合法（关键回归）")
    pseudo = [m for m in ("e9d9", "e9f9", "e9e8")
              if is_legal_move(MATE_FEN, m)]
    check("杀局里 3 个送将着法都通过**伪合法**校验（证明该 API 不可用于终局）",
          len(pseudo) == 3, str(pseudo))
    strict = [m for m in ("e9d9", "e9f9", "e9e8")
              if is_strict_legal_move(MATE_FEN, m)]
    check("同样的着法全部被**严格**校验拒绝", strict == [], str(strict))

    face = "4k4/9/9/9/9/9/9/9/9/4K4 w - - 0 1"
    check("照面局面 e0e1 伪合法通过", is_legal_move(face, "e0e1"))
    check("照面局面 e0e1 严格拒绝（会让将帅照面）",
          not is_strict_legal_move(face, "e0e1"))
    check("照面局面 e0d0 严格通过", is_strict_legal_move(face, "e0d0"))

    n_stale = len(enumerate_legal_moves(STALE_FEN))
    print(f"  [信息] 困毙构造局面伪合法着法数 = {n_stale}")

    # ---- 3. 自动接盘参数与模板匹配 ----
    print("\n④ 自动接盘参数与模板匹配")
    import tempfile
    import cv2
    import numpy as np
    from app_vision import Worker
    from app_backend import ScreenSource
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])

    w = Worker(ScreenSource())
    check("自动接盘默认关闭", w.auto_restart is False)
    check("默认匹配阈值 0.80", abs(w.restart_match - 0.80) < 1e-6,
          str(w.restart_match))
    check("未设模板时 _tpl_restart 为 None", w._tpl_restart is None)

    w.apply_restart_params({"enabled": True, "wait": 15, "match": 0.9,
                            "stall_fallback": True})
    check("参数下发生效",
          w.auto_restart and w.restart_wait == 15.0
          and abs(w.restart_match - 0.9) < 1e-6 and w.restart_stall_fallback,
          f"{w.auto_restart} {w.restart_wait} {w.restart_match} "
          f"{w.restart_stall_fallback}")
    w.apply_restart_params({"enabled": False})
    check("关闭后 game_over 复位", w._game_over is False)
    w.apply_restart_params({"enabled": True, "wait": 0.5, "match": 5.0})
    check("wait 下限夹到 3s", w.restart_wait >= 3.0, str(w.restart_wait))
    check("match 上限夹到 0.99", w.restart_match <= 0.99, str(w.restart_match))

    # 模板匹配：在合成帧里找一个小图块
    frame = np.full((400, 600, 3), 30, np.uint8)
    tpl = np.full((30, 90, 3), 200, np.uint8)
    tpl[10:20, 30:60] = 90                 # 加点结构，避免同色导致匹配退化
    frame[150:180, 200:290] = tpl          # 左上角 (200,150) → 中心 (245,165)
    w.restart_match = 0.80
    hit = w._match_template(frame, tpl)
    check("模板匹配命中", hit is not None, str(hit))
    if hit:
        check("命中中心坐标 ≈ (245,165)",
              abs(hit[0] - 245) < 2 and abs(hit[1] - 165) < 2,
              f"({hit[0]:.1f},{hit[1]:.1f})")
        check("相似度接近 1", hit[2] > 0.99, f"{hit[2]:.3f}")
    w.restart_match = 1.5
    check("阈值荒谬地高时不命中", w._match_template(frame, tpl) is None)
    w.restart_match = 0.80
    check("模板比帧还大时返回 None", w._match_template(tpl, frame) is None)
    check("frame 为 None 时返回 None", w._match_template(None, tpl) is None)
    check("tpl 为 None 时返回 None", w._match_template(frame, None) is None)

    # 模板读写：★ 必须走 imencode/imdecode，cv2.imread 不支持非 ASCII 路径
    d = Path(tempfile.mkdtemp(prefix="xqltpl_"))
    p = d / "中文目录_再来一局.png"
    ok, buf = cv2.imencode(".png", tpl)
    check("imencode 成功", bool(ok))
    buf.tofile(str(p))
    loaded = Worker._read_tpl(str(p))
    check("非 ASCII 路径下模板可读回",
          loaded is not None and loaded.shape == tpl.shape,
          str(None if loaded is None else loaded.shape))
    check("不存在的模板路径返回 None", Worker._read_tpl(str(d / "nope.png")) is None)
    check("空路径返回 None", Worker._read_tpl("") is None)

    print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
