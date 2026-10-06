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

# 上面这两项是**代码**，仓库里有入库副本（vendor/）。打包一律优先用入库副本，
# 否则会出现「发出去的 exe 跑的是 xq_research/ 里的未跟踪文件、跟仓库对不上」。
CODE_ASSETS = ("xq_vision.py", "engine_client.py")


def asset_src(rel: str) -> Path:
    """返回某个待分发文件的来源路径（代码优先取 vendor/，资源只能取 xq_research/）。"""
    if rel in CODE_ASSETS:
        p = VENDOR / rel
        if p.is_file():
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
        p = asset_src(rel)
        if not p.is_file():
            missing.append(rel)
    if missing:
        print("    [失败] 缺少资源文件:")
        for m in missing:
            print(f"           {asset_src(m)}")
        print("           模型与引擎的获取方式见 README「依赖与准备」")
        return False
    total = sum(asset_src(r).stat().st_size for r in ASSETS)
    print(f"    资源齐备，共 {total / 1024 / 1024:.1f} MB")
    return True


def build() -> bool:
    print()
    print("[2] 运行 PyInstaller")

    # 清掉上次的产物，避免残留旧文件混进新包
    for d in (BUILD, DIST):
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)

    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--windowed",                  # GUI 程序，不弹控制台
        "--name", APP_NAME,
        str(ROOT / "app_vision.py"),
        # app_vision 会 import 这两个（不在包内，属于外部资源）
        "--paths", str(RESEARCH),
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
    # 有图标就带上
    icon = ROOT.parent / "refs" / "chessboard" / "server" / "icons" / "icon.ico"
    if icon.is_file():
        args += ["--icon", str(icon)]
        print(f"    使用图标: {icon.name}")

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
    else:
        print("    [警告] 未生成 selftest.log")

    if rc == 0:
        print("    [通过] exe 能正常加载资源、棋规与引擎")
        return True

    print("    [失败] 自检未通过 —— 常见原因：")
    print("           · xq_research 资源目录不在 exe 同级")
    print("           · 模型或引擎文件缺失")
    print("           · PyInstaller 漏了某个隐藏依赖")
    return False


def make_zip() -> Path | None:
    print()
    print("[5] 压缩为 zip")
    src = DIST / APP_NAME
    out = DIST / f"{APP_NAME}"
    arch = shutil.make_archive(str(out), "zip", root_dir=str(DIST), base_dir=APP_NAME)
    p = Path(arch)
    print(f"    {p}  ({p.stat().st_size / 1024 / 1024:.1f} MB)")
    return p


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
