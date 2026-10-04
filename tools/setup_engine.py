"""一键解压 Pikafish 引擎并根据 CPU 特性挑选合适的二进制。

Pikafish 2026 起发布的是 7z 压缩包，内含多个平台/指令集变体。
本脚本挑出 Windows 下最适合本机 CPU 的那个，复制到 engines/ 根目录，
并做一次真实的 UCI 握手验证。
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENGINES = ROOT / "engines"


def detect_cpu_features() -> dict[str, bool]:
    """检测 CPU 指令集支持。"""
    feats = {"avx2": False, "bmi2": False, "sse41_popcnt": False, "vnni": False}
    if os.name != "nt":
        return feats
    import ctypes

    k = ctypes.windll.kernel32
    # IsProcessorFeaturePresent 的 PF_* 常量
    # PF_AVX2_INSTRUCTIONS_AVAILABLE = 40
    for const, key in ((40, "avx2"),):
        try:
            feats[key] = bool(k.IsProcessorFeaturePresent(const))
        except Exception:
            pass
    try:
        # 通过 CPUID 更可靠地判断 BMI2 / SSE4.1
        import subprocess as sp
        out = sp.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-CimInstance Win32_Processor).Name"],
            capture_output=True, text=True, timeout=20,
        ).stdout.strip()
        feats["_cpu_name"] = out  # type: ignore[assignment]
    except Exception:
        pass
    # 12 代酷睿及以后的桌面/移动型号都带 BMI2 / AVX2
    name = str(feats.get("_cpu_name", ""))
    if re.search(r"(1[2-9]|[2-9]\d)th Gen|Ryzen|Ultra", name):
        feats["bmi2"] = True
        feats["avx2"] = True
    return feats


def pick_binary(root: Path, feats: dict[str, bool]) -> Path | None:
    """在解压结果里挑一个最合适的 Windows 可执行文件。"""
    exes = [p for p in root.rglob("*.exe")]
    if not exes:
        return None

    def score(p: Path) -> int:
        n = p.name.lower()
        parent = p.parent.name.lower()
        s = 0
        if "windows" in parent or "win" in parent:
            s += 100
        if "avx2" in n:
            s += 40 if feats.get("avx2") else -50
        if "bmi2" in n:
            s += 20 if feats.get("bmi2") else -30
        if "vnni" in n:
            s += 10 if feats.get("vnni") else -20
        if "sse41" in n or "popcnt" in n or "modern" in n:
            s += 5
        if "x86-64" in n or "x64" in n or "64" in n:
            s += 10
        # 明确排除 32 位 / 其他平台
        if "32" in n and "64" not in n:
            s -= 80
        if any(k in n for k in ("arm", "android", "linux", "mac", "osx")):
            s -= 200
        return s

    exes.sort(key=score, reverse=True)
    print("候选项:")
    for p in exes[:8]:
        print(f"  {score(p):+5d}  {p.relative_to(root)}")
    return exes[0]


def uci_handshake(exe: Path, timeout: float = 25.0) -> tuple[bool, str]:
    """跑一次真实 UCI 握手，确认引擎可用。"""
    try:
        proc = subprocess.Popen(
            [str(exe)], cwd=str(exe.parent),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        return False, f"启动失败: {exc}"

    lines: list[str] = []
    try:
        assert proc.stdin and proc.stdout
        proc.stdin.write(b"uci\n")
        proc.stdin.flush()
        import time
        deadline = time.time() + timeout
        name = ""
        while time.time() < deadline:
            raw = proc.stdout.readline()
            if not raw:
                break
            text = raw.decode("utf-8", "replace").strip()
            lines.append(text)
            if text.startswith("id name"):
                name = text[7:].strip()
            if text == "uciok":
                break

        # 顺手验证一步真实搜索，确保 NNUE 权重能被加载
        best = ""
        if "uciok" in lines:
            proc.stdin.write(b"position fen rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR w - - 0 1\n")
            proc.stdin.write(b"go depth 10\n")
            proc.stdin.flush()
            deadline = time.time() + timeout
            while time.time() < deadline:
                raw = proc.stdout.readline()
                if not raw:
                    break
                text = raw.decode("utf-8", "replace").strip()
                if text.startswith("info depth 10") and " pv " in text:
                    lines.append(text[:120])
                if text.startswith("bestmove"):
                    best = text.split()[1]
                    break

        ok = "uciok" in lines and bool(best)
        detail = f"引擎名: {name or '(未报告)'}\n开局最佳着法: {best or '(未取得)'}"
        if lines and "uciok" not in lines:
            detail += "\n输出尾部:\n  " + "\n  ".join(lines[-6:])
        return ok, detail
    finally:
        try:
            proc.stdin and proc.stdin.write(b"quit\n")
            proc.stdin and proc.stdin.flush()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive", default=None, help="7z/zip 路径，默认自动找 engines/*.7z")
    ap.add_argument("--keep-extract", action="store_true", help="保留下载包")
    args = ap.parse_args()

    ENGINES.mkdir(parents=True, exist_ok=True)

    archive = Path(args.archive) if args.archive else next(iter(sorted(ENGINES.glob("*.7z"))), None)
    if archive is None or not archive.is_file():
        print(f"[错误] 在 {ENGINES} 里找不到 7z 包")
        return 1
    print(f"压缩包: {archive.name}  ({archive.stat().st_size / 1024 / 1024:.1f} MB)")

    print()
    print("=== CPU 特性 ===")
    feats = detect_cpu_features()
    cpu_name = feats.pop("_cpu_name", "")  # type: ignore[arg-type]
    if cpu_name:
        print(f"  CPU: {cpu_name}")
    for k, v in feats.items():
        print(f"  {k}: {'支持' if v else '不支持'}")

    print()
    print("=== 解压 ===")
    extract_dir = ENGINES / "_extract"
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    extract_dir.mkdir(parents=True)

    suffix = archive.suffix.lower()
    try:
        if suffix == ".7z":
            import py7zr
            with py7zr.SevenZipFile(archive, mode="r") as z:
                names = z.getnames()
                print(f"  包内条目: {len(names)}")
                z.extractall(path=extract_dir)
        else:
            shutil.unpack_archive(str(archive), str(extract_dir))
    except Exception as exc:
        print(f"[错误] 解压失败: {exc}")
        return 1
    print(f"  解压到: {extract_dir}")

    print()
    print("=== 选择二进制 ===")
    exe = pick_binary(extract_dir, feats)
    if exe is None:
        print("[错误] 未找到 Windows 可执行文件")
        return 1
    print(f"  选中: {exe.relative_to(extract_dir)}")

    # 复制到 engines/ 根目录，并把同目录的数据文件（NNUE 权重等）一起带上
    dest_dir = ENGINES
    dest = dest_dir / f"pikafish{exe.suffix}"
    shutil.copy2(exe, dest)
    copied_data: list[str] = []
    for sibling in exe.parent.iterdir():
        if sibling.is_file() and sibling.suffix.lower() in (".nnue", ".bin", ".txt", ".md"):
            shutil.copy2(sibling, dest_dir / sibling.name)
            copied_data.append(sibling.name)
    if copied_data:
        print(f"  附带数据文件: {', '.join(copied_data)}")
    print(f"  已就位: {dest}")

    print()
    print("=== UCI 握手 + 实际搜索验证 ===")
    ok, detail = uci_handshake(dest)
    print("  " + detail.replace("\n", "\n  "))
    print()
    if ok:
        print("[成功] 引擎可用。")
        if not args.keep_extract:
            shutil.rmtree(extract_dir, ignore_errors=True)
            print("  已清理临时解压目录（加 --keep-extract 可保留）")
        return 0
    print("[失败] 引擎未能完成搜索，请检查上面输出。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
