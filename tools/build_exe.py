# -*- coding: utf-8 -*-
"""把 XiangQiLens 打包成 Windows exe（PyInstaller onedir）。

策略
------------------------------------------------------------------
* **Python 运行时与依赖打进 exe**，用户无需安装 Python
* **模型与引擎作为外部资源放同级目录**（约 95MB）：
  体积大、且用户可能想单独替换模型练新皮肤，不该塞进 exe。
  运行时由 ``app_backend.app_base()`` 相对 exe 目录查找。
* 用 ``--onedir`` 而不是 ``--onefile``：onefile 每次启动都要把
  上百 MB 解包到临时目录，启动慢且更容易被安全软件拦。

用法::

    python tools/build_exe.py            # 打包
    python tools/build_exe.py --zip      # 打包并压缩成 zip
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # XiangQiLink/
RESEARCH = ROOT.parent / "xq_research"                 # 资源目录（模型/引擎，不入库）
VENDOR = ROOT / "vendor" / "xq_research"               # 代码目录（入库，见 .gitignore 注释）
DIST = ROOT / "dist"
BUILD = ROOT / "build"
APP_NAME = "XiangQiLens"

# 需要随 exe 一起分发的资源（相对 xq_research 的路径）
ASSETS = [
    "hf_model/onnx/pose/4_v6-0301.onnx",
    "hf_model/onnx/layout_recognition/nano_v3-0319.onnx",
    "pikafish/Pikafish-Windows-x86-64-universal.exe",
    "pikafish/pikafish.nnue",
    # 被 app_backend 直接 import 的模块
    "xq_vision.py",
    "engine_client.py",
]

# 上面这两项是**代码**，仓库里有入库副本（vendor/）。打包一律只用入库副本，
# 否则会出现「发出去的 exe 跑的是 xq_research/ 里的未跟踪文件、跟仓库对不上」。
CODE_ASSETS = ("xq_vision.py", "engine_client.py")

# 图标：入库在 assets/ 下。**故意不再指向别的仓库**（原来是
# ROOT.parent/refs/chessboard/...，换台机器就静默丢图标，而且那与本项目无关）。
ICON = ROOT / "assets" / "icon.ico"

# 打包用的 spec 由 PyInstaller 生成、被 .gitignore 排除。它写着**绝对路径**，
# 一旦留在工作区，别人直接跑 `pyinstaller XiangQiLens.spec` 就会走错目录。
SPEC = ROOT / f"{APP_NAME}.spec"


class MissingAsset(RuntimeError):
    """待分发的文件缺失。代码资产缺失必须硬失败，不能静默回退。"""


def asset_src(rel: str) -> Path:
    """返回某个待分发文件的来源路径。

    * **代码**（``CODE_ASSETS``）：只认 ``vendor/`` 的入库副本，缺失就硬失败。
      文件头注释早就写明「打包一律优先用入库副本」，但原来的兜底
      （``return RESEARCH / rel``）恰恰允许了静默回退到**未跟踪**的那份，
      而且 ``check_env()`` 校验的就是这个兜底路径 —— 所以构建照样通过，
      发出去的 exe 跑的是仓库里没有的代码。
    * **资源**（模型/引擎）：只能取 ``xq_research/``（体积大、各有许可证，不入库）。
    """
    if rel in CODE_ASSETS:
        p = VENDOR / rel
        if not p.is_file():
            raise MissingAsset(
                f"入库的代码副本缺失: {p}\n"
                f"        {rel} 必须存在于 vendor/xq_research/ 才能打包 ——\n"
                f"        绝不回退到未跟踪的 {RESEARCH / rel}，否则发出去的 exe\n"
                f"        跑的是仓库里没有的那份代码（而且不会有任何报错）。")
        return p
    return RESEARCH / rel


def run(cmd: list[str], **kw) -> int:
    print(f"  $ {' '.join(cmd)}")
    return subprocess.call(cmd, **kw)


def check_env() -> bool:
    print("[1] 检查打包环境")
    try:
        import PyInstaller  # noqa: F401
        import PyInstaller.__main__  # noqa: F401
        from PyInstaller import __version__ as ver
        print(f"    PyInstaller {ver}")
    except ImportError:
        print("    [失败] 未安装 PyInstaller")
        print("           请运行: python -m pip install pyinstaller")
        return False

    missing = []
    for rel in ASSETS:
        try:
            p = asset_src(rel)
        except MissingAsset as exc:
            print(f"    [失败] {exc}")
            return False
        if not p.is_file():
            missing.append(rel)
    if missing:
        print("    [失败] 缺少资源文件:")
        for m in missing:
            print(f"           {asset_src(m)}")
        print("           模型与引擎的获取方式见 README「依赖资源：模型与引擎」")
        return False
    # 图标已入库（assets/icon.ico，由 tools/make_icon.py 生成），因此缺失
    # 就是**真问题**（被误删/误加进 .gitignore），直接硬失败而不是静默无图标。
    if not ICON.is_file():
        print(f"    [失败] 图标缺失: {ICON}")
        print("           它应当已入库。重新生成：python tools/make_icon.py")
        return False

    total = sum(asset_src(r).stat().st_size for r in ASSETS)
    print(f"    资源齐备，共 {total / 1024 / 1024:.1f} MB")

    # 代码资产的两份副本必须一致，否则 exe 内（VENDOR）与同级 xq_research/
    # 分发的（也是 VENDOR）虽然同源，但本机 xq_research/ 可能已经漂移 ——
    # 源码运行时 vendor/ 优先，所以只提示，不算失败。
    for rel in CODE_ASSETS:
        local = RESEARCH / rel
        if local.is_file() and local.read_bytes() != (VENDOR / rel).read_bytes():
            print(f"    [提示] {local} 与入库副本不一致（打包只用入库副本）")
    return True


def pyinstaller_args() -> list[str]:
    """组装 PyInstaller 命令行（抽出来是为了能脱离真实构建做单元测试）。

    见 ``tools/test_build_chain.py``：这里最容易出的事故是 ``--paths`` 指向
    **未跟踪**的 ``xq_research/``，而同一次发布分发出去的却是 ``vendor/`` 的
    副本 —— 于是 exe 里和同级目录里躺着两份不同版本的同一个模块。
    """
    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--windowed",                  # GUI 程序，不弹控制台
        "--name", APP_NAME,
        str(ROOT / "app_vision.py"),
        # ★ 用 VENDOR 而不是 RESEARCH：让「打包进 exe 的代码」与
        #   「stage_assets() 分发出去的代码」来自同一次 asset_src() 的产物。
        #   原来这里指 RESEARCH（未跟踪），而分发用 VENDOR，同一次发布里会塞进
        #   两份不同版本的同一个模块，且不会有任何报错。
        "--paths", str(VENDOR),
        # 明确声明隐藏依赖，避免被漏掉
        "--hidden-import", "xq_vision",
        "--hidden-import", "engine_client",
        "--hidden-import", "cchess",
        "--hidden-import", "win32gui",
        "--hidden-import", "win32ui",
        "--hidden-import", "win32con",
        "--hidden-import", "mss",
        # 带上 onnxruntime 的原生库（含 DirectML.dll，GPU 推理要用）
        "--collect-all", "onnxruntime",
    ]

    # 排除本项目完全不用的模块，避免被依赖链无意拖进来。
    # 保守策略：只排除明确无关的（加密、测试框架、网络），
    # 不碰 Qt 组件 —— 删 Qt 模块容易在运行时才暴露导入错误。
    for mod in ("cryptography", "lxml", "bcrypt",
                "pytest", "IPython", "jupyter", "tornado", "zmq"):
        args += ["--exclude-module", mod]
    # 图标：仓库自有的 assets/icon.ico（由 tools/make_icon.py 生成）。
    # 原来这里指向**另一个仓库**（../refs/chessboard/server/icons/icon.ico）——
    # 换台机器就静默丢图标，而且那个文件与本项目无依赖、许可也不明。
    args += ["--icon", str(ICON)]
    return args


def build() -> bool:
    print()
    print("[2] 运行 PyInstaller")

    # 清掉上次的产物，避免残留旧文件混进新包
    for d in (BUILD, DIST):
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)

    args = pyinstaller_args()
    print(f"    使用图标: {ICON}")

    # 清掉上一轮生成的 spec：它写着**绝对路径**，留在工作区会让人直接
    # `pyinstaller XiangQiLens.spec` 时走错目录（见文件头 SPEC 注释）
    try:
        SPEC.unlink(missing_ok=True)
    except Exception:
        pass

    t0 = time.time()
    rc = run(args, cwd=str(ROOT))
    if rc != 0:
        print(f"    [失败] PyInstaller 退出码 {rc}")
        return False
    print(f"    完成，耗时 {time.time() - t0:.0f}s")
    return True


def stage_assets() -> bool:
    print()
    print("[3] 放置外部资源")
    out = DIST / APP_NAME
    if not out.is_dir():
        print(f"    [失败] 找不到输出目录 {out}")
        return False

    tgt = out / "xq_research"
    for rel in ASSETS:
        src = asset_src(rel)
        dst = tgt / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        print(f"    {rel}  ({src.stat().st_size / 1024 / 1024:.1f} MB)  <- {src.parent}")

    # 顺带放一份使用说明与许可，方便直接分发
    for extra in ("README.md", "LICENSE"):
        p = ROOT / extra
        if p.is_file():
            shutil.copy2(p, out / extra)

    total = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    print(f"    发行目录: {out}")
    print(f"    总体积  : {total / 1024 / 1024:.1f} MB")
    return True


def smoke_test() -> bool:
    """跑一次打包后 exe 的自检，确认能加载模型与引擎。

    打包时用了 ``--windowed``（无控制台），拿不到 stdout，因此自检会把
    结果写到 exe 同级的 ``selftest.log``，这里读它判断。
    """
    print()
    print("[4] 冒烟测试（运行 exe 的自检模式）")
    exe = DIST / APP_NAME / f"{APP_NAME}.exe"
    if not exe.is_file():
        print(f"    [失败] 找不到 {exe}")
        return False

    log = exe.parent / "selftest.log"
    log.unlink(missing_ok=True)

    print(f"    {exe.name} --selftest")
    try:
        rc = subprocess.call([str(exe), "--selftest"],
                             cwd=str(exe.parent), timeout=240)
    except subprocess.TimeoutExpired:
        print("    [失败] 超时（>240s）")
        return False
    print(f"    退出码: {rc}")

    if log.is_file():
        text = log.read_text(encoding="utf-8", errors="replace")
        print("    ---- 自检日志 ----")
        for line in text.splitlines():
            print(f"    {line}")
        print("    ------------------")
        log.unlink(missing_ok=True)
        has_pass = "[通过]" in text
    else:
        print("    [失败] 未生成 selftest.log")
        print("           exe 连日志都写不出来（或自检根本没跑到写日志那一步）——")
        print("           这种 exe 不能算通过。")
        return False

    # ★ 判定不能只看 rc：_write_selftest_log 吞掉所有写入异常，所以
    #   一个「写不了盘的 exe」也会 rc=0。日志里必须有 [通过] 才算真通过。
    if rc == 0 and not has_pass:
        print("    [失败] 退出码 0，但自检日志里没有 [通过] 行 —— 视为未通过")
        return False

    if rc == 0:
        print("    [通过] exe 能正常加载资源、棋规与引擎")
        return True

    print("    [失败] 自检未通过 —— 常见原因：")
    print("           · xq_research 资源目录不在 exe 同级")
    print("           · 模型或引擎文件缺失")
    print("           · PyInstaller 漏了某个隐藏依赖")
    return False


def make_zip() -> Path | None:
    """压缩成可发布的 zip。

    ★ 只走 ``tools/make_release_zip.py`` 这一条实现：它按扩展名分级压缩
      （已压过的 .onnx/.nnue 用 STORED，文本用 level 9），产物名带版本号，
      符合发布惯例。原来这里还有一条 ``shutil.make_archive`` 的旁路，
      产出无版本号的 ``dist/XiangQiLens.zip`` 且用默认压缩 —— 慢、体积大、
      名字还不对，发布时容易发错包。
    """
    print()
    print("[5] 压缩为可发布的 zip（委托 tools/make_release_zip.py）")
    rc = run([sys.executable, str(ROOT / "tools" / "make_release_zip.py")],
             cwd=str(ROOT))
    if rc != 0:
        print(f"    [失败] make_release_zip 退出码 {rc}")
        return None
    cands = sorted(DIST.glob(f"{APP_NAME}-v*.zip"))
    return cands[-1] if cands else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", action="store_true", help="打包后压缩成 zip")
    ap.add_argument("--skip-test", action="store_true", help="跳过冒烟测试")
    args = ap.parse_args()

    print("=" * 72)
    print(f"打包 {APP_NAME} 为 exe")
    print("=" * 72)

    if not check_env():
        return 1
    if not build():
        return 1
    if not stage_assets():
        return 1
    if not args.skip_test and not smoke_test():
        return 1
    if args.zip:
        make_zip()

    print()
    print("=" * 72)
    print(f"[完成] 发行目录: {DIST / APP_NAME}")
    print(f"       可执行文件: {DIST / APP_NAME / (APP_NAME + '.exe')}")
    print("       整个目录一起分发（exe 需要同级的 xq_research 资源）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
