# -*- coding: utf-8 -*-
"""验证 GitHub Release 及其资产。"""
from __future__ import annotations

import json
import sys

import requests

REPO = "mango12q/xiangqi-lens"
TAG = "v0.1.0"
API = f"https://api.github.com/repos/{REPO}/releases/tags/{TAG}"
HEAD = {"User-Agent": "XiangQiLens-verify", "Accept": "application/vnd.github+json"}


def human(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MB"


def main() -> int:
    print("=" * 72)
    print(f"验证 Release {TAG}")
    print("=" * 72)

    r = requests.get(API, headers=HEAD, timeout=30)
    if r.status_code != 200:
        print(f"[失败] HTTP {r.status_code}")
        return 1
    d = r.json()

    print()
    print("[1] Release 信息")
    print(f"    标题     : {d['name']}")
    print(f"    标签     : {d['tag_name']}")
    print(f"    发布状态 : {'草稿' if d['draft'] else '已发布'}"
          f"{' / 预发布' if d['prerelease'] else ''}")
    print(f"    发布时间 : {d['published_at']}")
    print(f"    页面     : {d['html_url']}")
    print(f"    说明长度 : {len(d.get('body') or '')} 字符")

    print()
    print("[2] 资产清单")
    assets = d.get("assets") or []
    if not assets:
        print("    [失败] 没有资产")
        return 1
    ok = True
    for a in assets:
        print(f"    名称   : {a['name']}")
        print(f"    大小   : {human(a['size'])}  ({a['size']} 字节)")
        print(f"    下载数 : {a['download_count']}")
        print(f"    链接   : {a['browser_download_url']}")

        # 用 HEAD 检查可下载性
        h = requests.head(a["browser_download_url"], timeout=30,
                          allow_redirects=True, headers=HEAD)
        print(f"    HEAD   : HTTP {h.status_code}  "
              f"Content-Length={h.headers.get('Content-Length')}")
        if h.status_code != 200:
            ok = False
        else:
            # 只下载前 1KB 验证 zip 魔数（不拉整个 400MB）
            g = requests.get(a["browser_download_url"], timeout=60,
                             headers={**HEAD, "Range": "bytes=0-3"},
                             allow_redirects=True)
            magic = g.content[:4]
            is_zip = magic.startswith(b"PK")
            print(f"    Range  : HTTP {g.status_code}  文件头={magic!r}  "
                  f"{'是 zip' if is_zip else '[失败] 不是 zip'}")
            if not is_zip:
                ok = False

    print()
    print("[3] 同 tag 的源码归档")
    for key in ("zipball_url", "tarball_url"):
        if d.get(key):
            print(f"    {key}: {d[key]}")

    print()
    print("=" * 72)
    if ok:
        print(f"[通过] Release 可正常下载：{d['html_url']}")
        return 0
    print("[失败] 资产校验未通过")
    return 1


if __name__ == "__main__":
    sys.exit(main())
