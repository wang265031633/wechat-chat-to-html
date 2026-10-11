# -*- coding: utf-8 -*-
"""把会话里的表情包还原成真正的图片 / GIF。

微信表情是私有格式，三层拼起来才能解：
  1. 密文 = AES-128-CBC(key, IV=key)，key 由 find_emoji_key.py 从进程内存里挖出来；
  2. 明文可能是 PNG / GIF / WEBP / JPG，也可能是私有的 wxgf 容器；
  3. wxgf = magic(4) | ver(2) | flags(1) | 宽(2,BE) | 高(2,BE) + Annex-B HEVC 码流，
     必须用 PyAV 以 `format="hevc"` 解复用（用 CodecContext.parse 会解出 0 帧）。

素材来源（按优先级）：
  * <wx_root>/cache/<YYYY-MM>/Emoticon/<2hex>/<md5>       —— 看过的表情
  * <wx_root>/business/emoticon/Persist/<2hex>/<md5>      —— 收藏过的表情
  * <wx_root>/business/emoticon/PersistStore/<2hex>/<md5(包id)> —— 整包密文，
    需要按 emoticon.db 的 kStoreEmoticonFilesTable(offset, size) 切片
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import io
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wxcommon as W                                                    # noqa: E402

import av                                                               # noqa: E402
import zstandard                                                        # noqa: E402
from Crypto.Cipher import AES                                           # noqa: E402
from PIL import Image                                                   # noqa: E402

HEXSET = set("0123456789abcdef")
MAX_FRAMES = 80


# ---------------------------------------------------------------- 解密

def cbc_decrypt(data, key):
    """AES-128-CBC，IV 就是 key；顺手剥掉 PKCS#7 填充。"""
    n = len(data) // 16 * 16
    pt = AES.new(key, AES.MODE_CBC, key).decrypt(data[:n])
    if pt:
        pad = pt[-1]
        if 1 <= pad <= 16 and pt[-pad:] == bytes([pad]) * pad:
            pt = pt[:-pad]
    return pt


# ---------------------------------------------------------------- wxgf

def _via_open(stream):
    try:
        with av.open(io.BytesIO(stream), format="hevc") as ct:
            return [f.to_image() for f in ct.decode(video=0)]
    except Exception:
        return []


def _via_parse(stream):
    try:
        c = av.CodecContext.create("hevc", "r")
        frames = []
        for pkt in c.parse(stream):
            frames += c.decode(pkt)
        frames += c.decode(None)
        return [f.to_image() for f in frames]
    except Exception:
        return []


def decode_wxgf(data):
    """返回 (PIL 帧列表, 尺寸是否与头部一致)。"""
    w = int.from_bytes(data[7:9], "big")
    h = int.from_bytes(data[9:11], "big")
    offs, last = [], -8
    i = data.find(b"\x00\x00\x01")
    while i >= 0 and len(offs) <= 24:
        if i - last > 4:
            offs.append(i)
            last = i
        i = data.find(b"\x00\x00\x01", i + 3)
    best = None
    for off in offs + [0]:
        for fn in (_via_open, _via_parse):
            frames = fn(data[off:])
            if not frames:
                continue
            good = w > 8 and h > 8 and abs(frames[0].size[0] - w) <= 4 \
                and abs(frames[0].size[1] - h) <= 4
            if best is None or (good and not best[1]) or (good == best[1]
                                                         and len(frames) > len(best[0])):
                best = (frames, good)
            if good:
                return frames, True
            break
    return (best[0] if best else []), bool(best and best[1])


def plaintext_to_file(md5, data, src, dst_dir):
    head = data[:4]
    if head == b"\x89PNG":
        open(os.path.join(dst_dir, md5 + ".png"), "wb").write(data)
        return {"file": "表情/" + md5 + ".png", "frames": 1, "src": src}
    if head[:3] == b"GIF":
        open(os.path.join(dst_dir, md5 + ".gif"), "wb").write(data)
        try:
            with Image.open(os.path.join(dst_dir, md5 + ".gif")) as im:
                nf = getattr(im, "n_frames", 1)
        except Exception:
            nf = None
        return {"file": "表情/" + md5 + ".gif", "frames": nf, "src": src}
    if head == b"RIFF":
        open(os.path.join(dst_dir, md5 + ".webp"), "wb").write(data)
        return {"file": "表情/" + md5 + ".webp", "frames": 1, "src": src}
    if head[:3] == b"\xff\xd8\xff":
        open(os.path.join(dst_dir, md5 + ".jpg"), "wb").write(data)
        return {"file": "表情/" + md5 + ".jpg", "frames": 1, "src": src}

    frames, _ = decode_wxgf(data)
    if not frames:
        # 极少数解不动的（HEVC 解码器报错）保留原始容器，便于人工复现
        open(os.path.join(dst_dir, md5 + ".wxgf"), "wb").write(data)
        return {"file": "表情/" + md5 + ".wxgf", "frames": 0, "raw": True, "src": src}
    if len(frames) == 1:
        frames[0].save(os.path.join(dst_dir, md5 + ".png"))
        return {"file": "表情/" + md5 + ".png", "frames": 1, "src": src}
    fr = [im.convert("P", palette=Image.ADAPTIVE, colors=128) for im in frames[:MAX_FRAMES]]
    fr[0].save(os.path.join(dst_dir, md5 + ".gif"), save_all=True, append_images=fr[1:],
               duration=80, loop=0, disposal=2, optimize=True)
    return {"file": "表情/" + md5 + ".gif", "frames": len(frames), "src": src}


# ---------------------------------------------------------------- 素材索引

def index_hashed_dir(root):
    """目录结构 <root>/<2hex>/<md5> → {md5: 路径}。"""
    idx = {}
    if not os.path.isdir(root):
        return idx
    for sub in os.listdir(root):
        d = os.path.join(root, sub)
        if not os.path.isdir(d) or len(sub) != 2:
            continue
        for fn in os.listdir(d):
            if len(fn) == 32 and all(ch in HEXSET for ch in fn):
                idx.setdefault(fn, os.path.join(d, fn))
    return idx


def collect_sources(env, workdir, log=print):
    wx_root = env["wx_root"]
    cache = {}
    for f in glob.glob(os.path.join(wx_root, "cache", "*", "Emoticon", "*", "*")):
        if os.path.isfile(f):
            cache.setdefault(os.path.basename(f), f)
    persist = index_hashed_dir(os.path.join(wx_root, "business", "emoticon", "Persist"))
    store = index_hashed_dir(os.path.join(wx_root, "business", "emoticon", "PersistStore"))
    log("素材索引：cache=%d persist=%d storepack=%d" % (len(cache), len(persist), len(store)))
    return cache, persist, store


def slice_store_packages(env, workdir, store_idx, key, log=print):
    """整包密文按 emoticon.db 记录的 offset/size 切片，并用 md5 校验。"""
    if not store_idx:
        return {}
    con = W.open_ro(W.emoticon_db(env, workdir))
    rows = con.execute("SELECT package_id_, md5_, emoticon_size_, emoticon_offset_ "
                       "FROM kStoreEmoticonFilesTable").fetchall()
    by_pack = {}
    for pid, md5_, size, off in rows:
        by_pack.setdefault(hashlib.md5(pid.encode("utf-8")).hexdigest(), []).append((md5_, size, off))
    sliced, verified, failed = {}, 0, 0
    for h, packpath in store_idx.items():
        entries = by_pack.get(h)
        if not entries:
            continue
        pt = cbc_decrypt(open(packpath, "rb").read(), key)
        for md5_, size, off in entries:
            blob = pt[off:off + size]
            if hashlib.md5(blob).hexdigest() == md5_:
                sliced[md5_] = blob
                verified += 1
            else:
                failed += 1
    log("商店包切片：校验通过 %d，失败 %d" % (verified, failed))
    return sliced


def wanted_stickers(tables):
    """从会话消息里取 (md5 -> 出现次数 / productid)；跨库并集。"""
    dctx = zstandard.ZstdDecompressor()
    want = {}
    for t in tables:
        con = W.open_ro(t["path"])
        rows = con.execute(
            'SELECT message_content, WCDB_CT_message_content FROM "%s" '
            'WHERE (local_type & 0xFFFFFFFF)=47' % t["table"]).fetchall()
        for mc, ctf in rows:
            if ctf == 4 and isinstance(mc, (bytes, bytearray)):
                try:
                    s = dctx.decompress(bytes(mc)).decode("utf-8", "replace")
                except Exception:
                    continue
            else:
                s = mc if isinstance(mc, str) else bytes(mc or b"").decode("utf-8", "replace")
            m = re.search(r'md5="([0-9a-f]{32})"', s)
            if not m:
                continue
            pid = re.search(r'productid="([^"]*)"', s)
            ent = want.setdefault(m.group(1),
                                  {"count": 0, "productid": pid.group(1) if pid else ""})
            ent["count"] += 1
    return want


def extract(env, workdir, media_root, tables, key, log=print):
    dst_dir = os.path.join(media_root, "表情")
    os.makedirs(dst_dir, exist_ok=True)

    cache, persist, store = collect_sources(env, workdir, log)
    sliced = slice_store_packages(env, workdir, store, key, log)
    want = wanted_stickers(tables)

    manifest, stat = {}, {"cache": 0, "persist": 0, "store": 0, "missing": 0}
    for md5 in sorted(want):
        try:
            if md5 in cache:
                ent = plaintext_to_file(md5, cbc_decrypt(open(cache[md5], "rb").read(), key),
                                        "cache", dst_dir)
                stat["cache"] += 1
            elif md5 in persist:
                ent = plaintext_to_file(md5, cbc_decrypt(open(persist[md5], "rb").read(), key),
                                        "persist", dst_dir)
                stat["persist"] += 1
            elif md5 in sliced:
                ent = plaintext_to_file(md5, sliced[md5], "store", dst_dir)
                stat["store"] += 1
            else:
                stat["missing"] += 1
                manifest[md5] = {"missing": True, "count": want[md5]["count"],
                                 "productid": want[md5]["productid"]}
                continue
        except Exception as e:
            stat["missing"] += 1
            manifest[md5] = {"missing": True, "error": repr(e)}
            continue
        ent.update(count=want[md5]["count"], productid=want[md5]["productid"])
        manifest[md5] = ent

    W.write_json(os.path.join(media_root, "sticker_manifest.json"), manifest)
    usable = sum(1 for v in manifest.values() if not v.get("raw") and not v.get("missing"))
    log("表情：会话内去重 %d 个 → 还原 %d 个，缺失 %d 个（%s）"
        % (len(want), usable, stat["missing"], json.dumps(stat, ensure_ascii=False)))
    return manifest


def main():
    ap = argparse.ArgumentParser(description="提取会话表情包为图片/GIF")
    ap.add_argument("--name", required=True)
    ap.add_argument("--peer-username")
    ap.add_argument("--out", required=True, help="用户指定的存放文件夹")
    ap.add_argument("--workdir")
    ap.add_argument("--db-dir")
    ap.add_argument("--pid", type=int, help="找密钥时指定 Weixin.exe 的 pid")
    a = ap.parse_args()

    import find_emoji_key as FEK

    env = W.discover(a.db_dir)
    workdir = a.workdir or W.default_workdir(env)
    W.decrypt_all(env, workdir)
    peer = ({"label": a.name, "username": a.peer_username} if a.peer_username
            else W.pick_peer(env, workdir, a.name))
    tables = W.find_msg_tables(env, workdir, peer["username"])
    key = FEK.get_key(env, pid=a.pid)
    W.ensure_dirs(a.out)
    extract(env, workdir, os.path.join(a.out, W.media_dirname(peer["label"])),
            tables, key)


if __name__ == "__main__":
    try:
        main()
    except W.WxError as e:
        print("错误：%s" % e, file=sys.stderr)
        sys.exit(2)
