# -*- coding: utf-8 -*-
"""验证 GitHub 仓库的最终状态：仓库信息、文件、README 渲染、图片可访问性。"""
from __future__ import annotations

import json
import sys

import requests

REPO = "mango12q/xiangqi-lens"
API = f"https://api.github.com/repos/{REPO}"
RAW = f"https://raw.githubusercontent.com/{REPO}/main"
HEAD = {"User-Agent": "XiangQiLens-verify", "Accept": "application/vnd.github+json"}


def main() -> int:
    print("=" * 70)
    print(f"验证仓库 {REPO}")
    print("=" * 70)

    print()
    print("[1] 仓库信息")
    r = requests.get(API, headers=HEAD, timeout=20)
    if r.status_code != 200:
        print(f"    [失败] HTTP {r.status_code}")
        return 1
    d = r.json()
    print(f"    名称     : {d['full_name']}")
    print(f"    可见性   : {d['visibility']}")
    print(f"    体积     : {d['size']} KB")
    print(f"    许可     : {(d.get('license') or {}).get('spdx_id')}")
    print(f"    默认分支 : {d['default_branch']}")
    print(f"    星标/复刻: {d['stargazers_count']} / {d['forks_count']}")
    print(f"    话题     : {', '.join(d.get('topics') or [])}")
    print(f"    描述     : {d.get('description')}")
    print(f"    创建时间 : {d['created_at']}")

    print()
    print("[2] 文件清单（远程实际内容）")
    r2 = requests.get(f"{API}/git/trees/main?recursive=1", headers=HEAD, timeout=20)
    if r2.status_code == 200:
        files = [t for t in r2.json()["tree"] if t["type"] == "blob"]
        print(f"    文件数: {len(files)}   合计: {sum(f['size'] for f in files)/1024:.1f} KB")
        core = [f for f in files if "/" not in f["path"]]
        print(f"    根目录文件: {', '.join(sorted(f['path'] for f in core))}")

    print()
    print("[3] README 渲染检查")
    r3 = requests.get(f"{API}/readme", headers=HEAD, timeout=20)
    if r3.status_code != 200:
        print(f"    [失败] HTTP {r3.status_code}")
        return 1
    print(f"    README.md 存在，{r3.json()['size']} bytes")

    md = requests.get(f"{RAW}/README.md", timeout=20).text
    print(f"    拉取正文: {len(md)} 字符")
    # 检查引用的图片是否都能访问
    import re
    imgs = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", md)
    print(f"    引用图片 {len(imgs)} 张:")
    ok = 0
    for src in imgs:
        if src.startswith("http"):
            url = src
        else:
            url = f"{RAW}/{src.lstrip('./')}"
        resp = requests.get(url, timeout=20, stream=True)
        size = int(resp.headers.get("Content-Length") or 0)
        status = "OK " if resp.status_code == 200 else "失败"
        if resp.status_code == 200:
            ok += 1
        print(f"      [{status}] {src}  {size/1024:.0f} KB")
    if ok != len(imgs):
        print("    [警告] 有图片无法访问")

    print()
    print("[4] 关键文件可访问性")
    for path in ("LICENSE", ".gitignore", ".gitattributes", "app_vision.py",
                 "app_backend.py", "tools/setup_engine.py"):
        resp = requests.get(f"{RAW}/{path}", timeout=20, stream=True)
        size = int(resp.headers.get("Content-Length") or 0)
        print(f"    [{resp.status_code}] {path:28} {size/1024:.1f} KB")

    print()
    print("=" * 70)
    print(f"[通过] 仓库可访问：https://github.com/{REPO}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
