# -*- coding: utf-8 -*-
"""把 dist/XiangQiLens 打包成用于 GitHub Release 的 zip。

用 zipfile 而不是 shutil.make_archive：可以对不同体积的文件选择
不同的压缩级别，且能显示进度。

压缩策略：**抽样实测**，不按扩展名猜
------------------------------------
v0.5.3 及以前按扩展名把 ``.dll/.pyd/.onnx/.nnue`` 一律当成「已压缩格式」
用 ``ZIP_STORED`` 原样存。实测这四类在同一份产物里的真实压缩率是：

    ==========  ======  ======  =======
    扩展名       总大小   压缩后   结论
    ==========  ======  ======  =======
    .dll        175 MB     51%   该压
    .pyd        143 MB     33%   该压
    .onnx        40 MB     79%   该压
    .nnue        48 MB    100%   压不动
    ==========  ======  ======  =======

即前三类白扔了约 190MB，只有 ``.nnue``（稠密权重）确实压不动。

但「一律 deflate」同样不对：``.nnue`` / ``.png`` 这类压完反而更大，
而 **zipfile 不会自动回退**（实测对随机数据 deflate 会多出 65 字节，
``compress_type`` 仍是 8）。

所以改成逐个文件先试压：取最多 1.5MB（首 / 中 / 尾各 512KB）用 zlib
level 6 压一次，压不动（≥ :data:`WORTH_RATIO`）就 ``ZIP_STORED``，
压得动才真正 deflate。抽样成本约几秒，两头都不会错。
"""
from __future__ import annotations

import re
import sys
import time
import zipfile
import zlib
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "dist" / "XiangQiLens"
ARCNAME = "XiangQiLens"

#: 抽样块大小：首 / 中 / 尾各取这么多字节（文件小时取全文件）。
PROBE_CHUNK = 512 * 1024
#: 抽样压缩比 ≥ 此值即认为「压不动」，原样存。
WORTH_RATIO = 0.98
#: 文本类用 level 9，其余二进制用 level 6（再高只换来 1~2%，耗时翻倍）。
TEXT_EXT = {".py", ".txt", ".md", ".json", ".xml", ".html",
            ".yml", ".yaml", ".css", ".js", ".ini", ".cfg", ".toml", ".csv"}


def read_version() -> str:
    """从 app_backend.py 读版本号，避免打包脚本里手写导致不一致。"""
    txt = (ROOT / "app_backend.py").read_text(encoding="utf-8")
    m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', txt, re.M)
    if not m:
        print("[警告] app_backend.py 里找不到 __version__，回退为 0.0.0")
        return "0.0.0"
    return m.group(1)


OUT = ROOT / "dist" / f"XiangQiLens-v{read_version()}.zip"


def human(n: float) -> str:
    """1024 进制（MiB）—— 资源管理器与 GitHub 都用这个口径。"""
    return f"{n / 1024 / 1024:.1f} MiB"


def probe_ratio(p: Path) -> float:
    """抽样估算压缩比；1.0 = 完全压不动（越大越不值得压）。

    取首 / 中 / 尾三块而不是只取开头：只取开头会被 PE 头、ONNX protobuf
    头这类「开头很规整」的段落骗到。
    """
    size = p.stat().st_size
    if size <= 0:
        return 1.0
    with p.open("rb") as fh:
        if size <= PROBE_CHUNK * 3:
            sample = fh.read()
        else:
            sample = fh.read(PROBE_CHUNK)
            fh.seek(size // 2)
            sample += fh.read(PROBE_CHUNK)
            fh.seek(-PROBE_CHUNK, 2)
            sample += fh.read(PROBE_CHUNK)
    # zlib.compress 会加 2 字节头 + 4 字节 adler32；小文件上这 6 字节不可忽略，
    # 但对「要不要压」的判断没有影响（比值只会略微偏大 → 偏向 STORED）。
    return len(zlib.compress(sample, 6)) / len(sample)


def plan(p: Path) -> tuple[int, int | None]:
    """决定单个文件的压缩方式，返回 ``(compress_type, compresslevel)``。"""
    if probe_ratio(p) >= WORTH_RATIO:
        return zipfile.ZIP_STORED, None
    lvl = 9 if p.suffix.lower() in TEXT_EXT else 6
    return zipfile.ZIP_DEFLATED, lvl


def summarize(out: Path) -> None:
    """按扩展名统计实际压缩效果（只读中央目录，很便宜）。"""
    stat: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])   # n / 原始 / 压后
    with zipfile.ZipFile(out) as z:
        for i in z.infolist():
            row = stat[Path(i.filename).suffix.lower() or "(无扩展名)"]
            row[0] += 1
            row[1] += i.file_size
            row[2] += i.compress_size

    print()
    print(f"{'扩展名':<12}{'文件':>6}{'原始':>11}{'压后':>11}{'省下':>11}")
    print("-" * 53)
    for ext, (n, raw, comp) in sorted(stat.items(), key=lambda kv: kv[1][1] - kv[1][2],
                                      reverse=True):
        if raw < 256 * 1024:                    # 零头不逐条列
            continue
        print(f"{ext:<12}{n:>6}{human(raw):>11}{human(comp):>11}"
              f"{human(raw - comp):>11}")
    print("-" * 53)


def main() -> int:
    if not SRC.is_dir():
        print(f"[失败] 找不到 {SRC}")
        return 1

    files = [p for p in SRC.rglob("*") if p.is_file()]
    total = sum(p.stat().st_size for p in files)
    print(f"版本号: v{read_version()}")
    print(f"源目录: {SRC}")
    print(f"文件数: {len(files)}   总大小: {human(total)}")
    print(f"输出  : {OUT}")
    print(f"策略  : 逐文件抽样试压（{PROBE_CHUNK // 1024}KB×3），"
          f"压不动（≥{WORTH_RATIO:.0%}）原样存")
    print()

    OUT.unlink(missing_ok=True)

    t0 = time.time()
    done = 0
    stored = 0
    stored_bytes = 0
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        for i, p in enumerate(sorted(files), 1):
            ctype, lvl = plan(p)
            if ctype == zipfile.ZIP_STORED:
                stored += 1
                stored_bytes += p.stat().st_size
                z.write(p, f"{ARCNAME}/{p.relative_to(SRC)}",
                        compress_type=zipfile.ZIP_STORED)
            else:
                z.write(p, f"{ARCNAME}/{p.relative_to(SRC)}",
                        compress_type=zipfile.ZIP_DEFLATED, compresslevel=lvl)
            done += p.stat().st_size
            if i % 200 == 0 or i == len(files):
                pct = done / max(total, 1) * 100
                print(f"  [{pct:5.1f}%] {i}/{len(files)}  "
                      f"{human(done)}  {time.time() - t0:.0f}s", flush=True)

    if not OUT.is_file():
        print("[失败] 未生成 zip")
        return 1

    zs = OUT.stat().st_size
    summarize(OUT)
    print(f"原样存（抽样判定压不动）: {stored} 个文件 / {human(stored_bytes)}")
    print()
    print(f"完成: {OUT}")
    print(f"      压缩包 {human(zs)}（{zs / 1e6:.1f} MB 十进制）   "
          f"原始 {human(total)}  压缩率 {zs / max(total, 1) * 100:.0f}%  "
          f"耗时 {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
