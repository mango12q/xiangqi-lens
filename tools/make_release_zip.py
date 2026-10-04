# -*- coding: utf-8 -*-
"""把 dist/XiangQiLens 打包成用于 GitHub Release 的 zip。

用 zipfile 而不是 shutil.make_archive：可以对不同体积的文件选择
不同的压缩级别，且能显示进度（437MB 压起来要几分钟）。
"""
from __future__ import annotations

import re
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "dist" / "XiangQiLens"
ARCNAME = "XiangQiLens"


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
    return f"{n / 1024 / 1024:.1f} MB"


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
    print()

    OUT.unlink(missing_ok=True)
    # 已经压过的格式（模型、权重）用低压缩级别，省时间；
    # 文本与二进制程序用高压缩级别。
    LOW = {".onnx", ".nnue", ".7z", ".zip", ".png", ".jpg", ".pyd", ".dll"}
    HIGH = {".py", ".txt", ".md", ".json", ".xml", ".html"}

    t0 = time.time()
    done = 0
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED) as z:
        for i, p in enumerate(sorted(files), 1):
            ext = p.suffix.lower()
            if ext in LOW:
                z.write(p, f"{ARCNAME}/{p.relative_to(SRC)}",
                        compress_type=zipfile.ZIP_STORED)
            else:
                lvl = 9 if ext in HIGH else 6
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
    print()
    print(f"完成: {OUT}")
    print(f"      压缩包 {human(zs)}   原始 {human(total)}   "
          f"压缩率 {zs / max(total, 1) * 100:.0f}%   耗时 {time.time() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
