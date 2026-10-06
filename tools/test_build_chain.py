# -*- coding: utf-8 -*-
"""打包链路回归测试 —— 不真的打包（几分钟），只验证最易出事故的几处。

覆盖三轮评审里关于「发出去的包和仓库对不上」的四个缺陷：

1. ``--paths`` 指向**未跟踪**的 ``xq_research/``，而分发用 ``vendor/`` ⇒
   同一次发布里塞进两份不同版本的同一个模块（且无任何报错）。
2. ``asset_src()`` 对**代码**资产静默回退到未跟踪目录 ⇒ 构建照样通过，
   但 exe 跑的是仓库里没有的代码。
3. 冒烟测试只看退出码 ⇒ 一个**写不了盘**的 exe 也算通过。
4. 图标指向**另一个仓库**（``refs/chessboard/...``）⇒ 换台机器静默丢图标。

另外验证 zip 只有一条实现（不再有 ``shutil.make_archive`` 旁路）。
"""
from __future__ import annotations

import importlib.util
import struct
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "tools"))

from _common import Checker  # noqa: E402

# tools/build_exe.py 不是包，按文件路径加载
_spec = importlib.util.spec_from_file_location(
    "build_exe", str(HERE / "tools" / "build_exe.py"))
build_exe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build_exe)          # type: ignore[union-attr]

SRC = (HERE / "tools" / "build_exe.py").read_text(encoding="utf-8")


def main() -> int:
    c = Checker("打包链路测试（不真的打包）")

    print()
    print("[1] 代码资产只认 vendor/ 的入库副本")
    for rel in build_exe.CODE_ASSETS:
        try:
            p = build_exe.asset_src(rel)
            c.check(f"{rel} 取自 vendor/", build_exe.VENDOR in p.parents,
                    f"实得 {p}")
        except Exception as exc:                     # noqa: BLE001
            c.check(f"{rel} 取自 vendor/", False, f"抛异常 {exc}")

    print()
    print("[2] 入库副本缺失时必须**硬失败**，绝不能回退到未跟踪目录")
    real_vendor = build_exe.VENDOR
    with tempfile.TemporaryDirectory() as td:
        build_exe.VENDOR = Path(td)                  # 空的 vendor/
        try:
            for rel in build_exe.CODE_ASSETS:
                try:
                    got = build_exe.asset_src(rel)
                    c.check(f"{rel} 缺失时抛 MissingAsset", False,
                            f"却返回了 {got}（静默回退！）")
                except build_exe.MissingAsset:
                    c.check(f"{rel} 缺失时抛 MissingAsset", True)
                except Exception as exc:             # noqa: BLE001
                    c.check(f"{rel} 缺失时抛 MissingAsset", False,
                            f"抛了别的异常 {type(exc).__name__}: {exc}")
        finally:
            build_exe.VENDOR = real_vendor

    print()
    print("[3] 资源（模型/引擎）仍从 xq_research/ 取")
    res = build_exe.asset_src("pikafish/pikafish.nnue")
    c.check("资源取自 RESEARCH", build_exe.RESEARCH in res.parents, f"实得 {res}")

    print()
    print("[4] PyInstaller 的 --paths 必须指向 VENDOR，不能是 RESEARCH")
    args = build_exe.pyinstaller_args()
    paths = [args[i + 1] for i, a in enumerate(args) if a == "--paths"]
    c.check("命令行里有 --paths", bool(paths), f"args={args[:8]}…")
    c.check("--paths 指向 VENDOR",
            paths and paths[0] == str(build_exe.VENDOR), f"实得 {paths}")
    c.check("--paths 不含 RESEARCH",
            all(str(build_exe.RESEARCH) != p for p in paths), f"实得 {paths}")

    print()
    print("[5] 图标必须已入库且落在仓库内（原来指向另一个仓库 refs/chessboard）")
    c.check("ICON 在仓库内 assets/ 下",
            build_exe.ICON.parent == HERE / "assets", f"实得 {build_exe.ICON}")
    c.check("assets/icon.ico 确实入库了", build_exe.ICON.is_file(),
            f"缺失 {build_exe.ICON}（重新生成: python tools/make_icon.py）")
    icon_args = [args[i + 1] for i, a in enumerate(args) if a == "--icon"]
    c.check("构建命令里带上了 --icon",
            icon_args and icon_args[0] == str(build_exe.ICON),
            f"实得 {icon_args}")
    outside = []
    for i, a in enumerate(args):
        if a in ("--paths", "--icon"):
            v = Path(args[i + 1])
            if v != HERE and HERE not in v.parents:
                outside.append((a, str(v)))
    c.check("--paths / --icon 都落在仓库内", not outside,
            f"仓库外: {outside}（原缺陷就是指向了另一个仓库 refs/chessboard）")

    print()
    print("[5b] 图标缺失时必须硬失败（不能静默无图标）")
    c.check("check_env 里校验图标存在",
            "图标缺失" in SRC and "make_icon.py" in SRC,
            "缺失时应有明确报错并指向生成脚本")

    print()
    print("[5c] assets/icon.ico 是合法的多尺寸 ICO（不依赖 Pillow 解析头部）")
    data = build_exe.ICON.read_bytes()
    c.check("文件够大（含多个尺寸）", len(data) > 5000, f"{len(data)} 字节")
    if len(data) >= 6:
        reserved, itype, count = struct.unpack("<HHH", data[:6])
        c.check("ICO 头合法（reserved=0, type=1）",
                reserved == 0 and itype == 1, f"reserved={reserved} type={itype}")
        c.check("含 5 个以上尺寸", count >= 5, f"实得 {count}")
        sizes = set()
        for i in range(count):
            off = 6 + i * 16
            if off + 2 > len(data):
                break
            w, h = data[off], data[off + 1]
            sizes.add((w or 256, h or 256))
        c.check("含 16/32/48/256 四个常用尺寸",
                {(16, 16), (32, 32), (48, 48), (256, 256)} <= sizes,
                f"实得 {sorted(sizes)}")
    else:
        c.check("ICO 头合法", False, "文件过短")

    print()
    print("[6] zip 只有一条实现（统一走 make_release_zip.py）")
    c.check("build_exe 不再直接调用 shutil.make_archive",
            "make_archive(" not in SRC, "仍存在旁路实现")
    c.check("make_zip 委托给 make_release_zip.py",
            "make_release_zip.py" in SRC)

    print()
    print("[7] 冒烟测试不能只看退出码")
    c.check("smoke_test 检查日志里有没有 [通过]",
            "has_pass" in SRC and '"[通过]" in text' in SRC,
            "只看 rc 的话，写不了盘的 exe 也算通过")
    c.check("缺日志时直接判失败",
            "未生成 selftest.log" in SRC and "return False" in SRC)

    print()
    print("[8] 陈旧 spec 会被清掉")
    c.check("build() 里删除 XiangQiLens.spec", "SPEC.unlink" in SRC)
    c.check("SPEC 指向仓库内的 spec 文件",
            build_exe.SPEC.parent == HERE, f"实得 {build_exe.SPEC}")

    return c.done()


if __name__ == "__main__":
    sys.exit(main())
