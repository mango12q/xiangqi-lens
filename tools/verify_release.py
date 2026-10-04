# -*- coding: utf-8 -*-
"""验证 GitHub Release 及其资产。默认验证最新的一个。

用法::

    python tools/verify_release.py            # 最新 release
    python tools/verify_release.py v0.2.0     # 指定 tag
"""
from __future__ import annotations

import sys

import requests

REPO = "mango12q/xiangqi-lens"
API = f"https://api.github.com/repos/{REPO}"
HEAD = {"User-Agent": "XiangQiLens-verify", "Accept": "application/vnd.github+json"}


def human(n: int) -> str:
    return f"{n / 1024 / 1024:.1f} MB"


def main() -> int:
    tag = sys.argv[1] if len(sys.argv) > 1 else None

    print("=" * 72)
    print(f"验证 Release {'最新' if tag is None else tag}")
    print("=" * 72)

    if tag is None:
        r = requests.get(f"{API}/releases/latest", headers=HEAD, timeout=30)
    else:
        r = requests.get(f"{API}/releases/tags/{tag}", headers=HEAD, timeout=30)
    if r.status_code != 200:
        print(f"[失败] HTTP {r.status_code}: {r.text[:200]}")
        return 1
    d = r.json()

    print()
    print("[1] Release 信息")
    print(f"    标题     : {d['name']}")
    print(f"    标签     : {d['tag_name']}")
    print(f"    状态     : {'草稿' if d['draft'] else '已发布'}"
          f"{' / 预发布' if d['prerelease'] else ''}")
    print(f"    发布时间 : {d['published_at']}")
    print(f"    页面     : {d['html_url']}")
    body = d.get("body") or ""
    print(f"    说明     : {len(body)} 字符")
    # 检查 release notes 里是否残留错误版本号
    import re
    vers = set(re.findall(r"v\d+\.\d+\.\d+", body))
    print(f"    说明中提到的版本: {sorted(vers)}")

    print()
    print("[2] 资产清单")
    assets = d.get("assets") or []
    if not assets:
        print("    [失败] 没有资产")
        return 1
    ok = True
    for a in assets:
        print(f"    名称     : {a['name']}")
        print(f"    大小     : {human(a['size'])}  ({a['size']} 字节)")
        print(f"    下载次数 : {a['download_count']}")

        h = requests.head(a["browser_download_url"], timeout=30,
                          allow_redirects=True, headers=HEAD)
        print(f"    HEAD     : HTTP {h.status_code}")
        if h.status_code != 200:
            ok = False
            continue
        g = requests.get(a["browser_download_url"],
                         headers={**HEAD, "Range": "bytes=0-3"},
                         timeout=60, allow_redirects=True)
        magic = g.content[:4]
        is_zip = magic.startswith(b"PK")
        print(f"    Range    : HTTP {g.status_code}  文件头={magic!r}  "
              f"{'是 zip' if is_zip else '[失败] 不是 zip'}")
        if not is_zip:
            ok = False

    print()
    print("[3] 全部 Release 一览")
    rl = requests.get(f"{API}/releases", headers=HEAD, timeout=30)
    if rl.status_code == 200:
        for rel in rl.json():
            mark = "预发布" if rel["prerelease"] else ("草稿" if rel["draft"] else "已发布")
            n = len(rel.get("assets") or [])
            print(f"    {rel['tag_name']:10}  {mark:6}  {n} 个资产  {rel['published_at']}")

    print()
    print("=" * 72)
    if ok:
        print(f"[通过] Release 可正常下载：{d['html_url']}")
        return 0
    print("[失败] 资产校验未通过")
    return 1


if __name__ == "__main__":
    sys.exit(main())
