# -*- coding: utf-8 -*-
"""临时探针：验证 BoardTracker 在各输入下的真实分支（用完即删）。

目的：确认屏幕上「局面变化无法解释，已挡下（已挡 N 帧）」到底由哪条路径产生，
以及 docstring 承诺的「连续 force_after 帧兜底采信」是否真的可达。
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from app_backend import BoardTracker, check_position  # noqa: E402

INIT = "rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR"
ILLEGAL = "9/9/9/9/9/9/9/9/9/4k3K"          # 黑将出现在红方底线 (9,4)：正是日志里的报错
LEGAL_B = "1nbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/1NBAKABNR"   # a0 空 + b0 换成 R
LEGAL_C = "1nbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR"   # a0 空 + c0 换成 R

for name, fen in (("ILLEGAL", ILLEGAL), ("LEGAL_B", LEGAL_B), ("LEGAL_C", LEGAL_C)):
    ok, why = check_position(fen)
    print(f"check_position({name}) = {ok} {why}")


def fresh():
    return BoardTracker(force_after=6, first_side="w")


print("\n=== 场景1：持续性非法局面（日志里的 黑将在九宫外 (9,4)） ===")
t = fresh()
for i in range(1, 12):
    ok, reason, extra, side = t.update(ILLEGAL)
    if i in (1, 2, 3, 11):
        print(f"  帧{i:>2}: accept={ok} reason={reason!r} extra={extra!r} dropped={t.dropped} forced={t.forced}")

print("\n=== 场景2：持续性「合法但无法解释」局面 ===")
t = fresh()
t.update(INIT)
for i in range(1, 5):
    ok, reason, extra, side = t.update(LEGAL_B)
    print(f"  帧{i}: accept={ok} reason={reason!r} dropped={t.dropped} forced={t.forced} pending={t.pending_count}")

print("\n=== 场景3：抖动型「合法但无法解释」（B/C 交替） ===")
t = fresh()
t.update(INIT)
for i in range(1, 25):
    ok, reason, extra, side = t.update(LEGAL_B if i % 2 else LEGAL_C)
    if i in (1, 2, 3, 24):
        print(f"  帧{i:>2}: accept={ok} reason={reason!r} dropped={t.dropped} forced={t.forced} pending={t.pending_count}")
print(f"  结论：24 帧后 dropped={t.dropped} forced={t.forced}（force_after={t.force_after} 从未触发）")
