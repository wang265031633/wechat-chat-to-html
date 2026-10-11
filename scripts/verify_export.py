# -*- coding: utf-8 -*-
"""导出结果自检：4 个文件是否齐、HTML 里的媒体引用是否都能落到磁盘。

用法：python verify_export.py <存放文件夹> [对方名]
     对方名省略时按目录里现成的文件名推断。
"""
from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wxcommon as W                                                    # noqa: E402


def guess_peer(out_dir):
    for f in os.listdir(out_dir):
        m = re.match(r"^(.+)_聊天记录\.html$", f)
        if m:
            return m.group(1)
    return None


def check(out_dir, peer=None):
    out_dir = os.path.abspath(out_dir)
    if not os.path.isdir(out_dir):
        print("× 目录不存在：%s" % out_dir)
        return 1
    peer = peer or guess_peer(out_dir)
    if not peer:
        print("× 目录里没有 <对方>_聊天记录.html，没法判断这是不是一次完整导出")
        return 1

    rc = 0
    print("存放位置 : %s" % out_dir)
    print("对方     : %s" % peer)
    print("--- 交付清单（4 个文件 + 1 个文件夹） ---")
    for n in W.deliverables(peer):
        p = os.path.join(out_dir, n)
        if os.path.isdir(p):
            cnt = sum(len(f) for _, _, f in os.walk(p))
            sub = "，".join("%s %d" % (d, len(os.listdir(os.path.join(p, d))))
                            for d in sorted(os.listdir(p))
                            if os.path.isdir(os.path.join(p, d)))
            print("  ✓ %-30s 文件夹，%d 个文件（%s）" % (n + os.sep, cnt, sub))
        elif os.path.exists(p):
            print("  ✓ %-30s %8.0f KB" % (n, os.path.getsize(p) / 1024))
        else:
            print("  × %-30s 缺失" % n)
            rc = 1

    html_path = os.path.join(out_dir, W.file_names(peer)[0])
    if not os.path.exists(html_path):
        return rc
    html = open(html_path, encoding="utf-8").read()
    refs = set(re.findall(r'(?:src|href)="([^"]+)"', html))
    media = sorted(r for r in refs if not r.startswith(("http", "#", "data:")))
    missing = [r for r in media
               if not os.path.exists(os.path.join(out_dir, r.replace("/", os.sep)))]
    print("--- 内容 ---")
    print("  媒体引用 %d 个，缺失 %d 个" % (len(media), len(missing)))
    for m in missing[:10]:
        print("     × %s" % m)
    print("  表情 <img> %d 个 · 语音 <audio> %d 个" % (html.count("<img"), html.count("<audio")))
    if missing:
        rc = 1

    mdir = os.path.join(out_dir, W.media_dirname(peer))
    for name, kind in (("sticker_manifest.json", "表情"), ("voice_manifest.json", "语音")):
        p = os.path.join(mdir, name)
        if not os.path.exists(p):
            print("  %s清单：无（该类未提取）" % kind)
            continue
        data = json.load(open(p, encoding="utf-8"))
        items = [v for v in (data.values() if isinstance(data, dict) else data)
                 if isinstance(v, dict)]
        ok = [v for v in items if v.get("file") and not v.get("raw")
              and os.path.exists(os.path.join(mdir, v["file"].replace("/", os.sep)))]
        raw = sum(1 for v in items if v.get("raw"))
        miss = sum(1 for v in items if v.get("missing"))
        print("  %s清单：%d 项（可用 %d，未解码 %d，素材缺失 %d）"
              % (kind, len(items), len(ok), raw, miss))
    return rc


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(check(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None))
