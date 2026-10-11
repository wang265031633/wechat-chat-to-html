# -*- coding: utf-8 -*-
"""在 Weixin.exe 内存里找出表情缓存的 AES 密钥。

已知算法（微信 4.1.x Windows）：
    key = md5(f"{seed}{wxid}EMOTICON").digest()[:16]
    文件 = AES-128-CBC(key, IV=key)
    oracle：P0 = AES-ECB-Dec(C0, key) XOR key  应当命中图片魔数
其中 seed 是一串 6~14 位十进制数字，随机生成后常驻进程内存，不落盘。

用法：
    python find_emoji_key.py                 # 自动扫描并缓存
    python find_emoji_key.py --pid 1234      # 指定进程
    python find_emoji_key.py --seed 1234567890   # 已知 seed 时直接推导
    python find_emoji_key.py --show          # 只看当前缓存（含个人密钥，勿外传）
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import glob
import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wxcommon as W                                                    # noqa: E402

from Crypto.Cipher import AES                                           # noqa: E402

CACHE = os.path.expanduser("~/.wechat-cli/emoji_key.json")

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
MEM_COMMIT = 0x1000
PAGE_NOACCESS = 0x01
PAGE_GUARD = 0x100
PROC_NAMES = ("Weixin.exe", "WeChat.exe")

# 换 seed 派生公式时的备选组合（命中率从高到低）
FORMULAS = {
    "seed+wxid+EMOTICON": lambda s, w: "%s%sEMOTICON" % (s, w),
    "seed+wxid+Emoticon": lambda s, w: "%s%sEmoticon" % (s, w),
    "seed+wxid": lambda s, w: "%s%s" % (s, w),
    "wxid+seed+EMOTICON": lambda s, w: "%s%sEMOTICON" % (w, s),
    "seed": lambda s, w: "%s" % s,
}


# ---------------------------------------------------------------- 内存读取

class MEMORY_BASIC_INFORMATION64(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_ulonglong),
        ("AllocationBase", ctypes.c_ulonglong),
        ("AllocationProtect", wt.DWORD),
        ("__alignment1", wt.DWORD),
        ("RegionSize", ctypes.c_ulonglong),
        ("State", wt.DWORD),
        ("Protect", wt.DWORD),
        ("Type", wt.DWORD),
        ("__alignment2", wt.DWORD),
    ]


def find_pids(names=PROC_NAMES):
    import subprocess
    pids = []
    for name in names:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq %s" % name,
                              "/FO", "CSV", "/NH"],
                             capture_output=True, text=True,
                             encoding="utf-8", errors="replace").stdout
        for line in out.splitlines():
            parts = [p.strip('"') for p in line.split('","')]
            if len(parts) >= 2 and parts[0].lower() == name.lower() and parts[1].isdigit():
                pids.append(int(parts[1]))
    return pids


def iter_regions(k32, handle):
    addr = 0
    mbi = MEMORY_BASIC_INFORMATION64()
    size = ctypes.sizeof(mbi)
    while addr < 0x7FFFFFFFFFFF:
        if not k32.VirtualQueryEx(wt.HANDLE(handle), ctypes.c_void_p(addr),
                                  ctypes.byref(mbi), size):
            break
        base, rsize = mbi.BaseAddress, mbi.RegionSize
        if not rsize:
            break
        if mbi.State == MEM_COMMIT and mbi.Protect != PAGE_NOACCESS and not (mbi.Protect & PAGE_GUARD):
            yield base, rsize
        addr = base + rsize


def read_mem(k32, handle, base, size, chunk=1 << 22):
    buf = ctypes.create_string_buffer(chunk)
    read = ctypes.c_size_t(0)
    off = 0
    while off < size:
        n = min(chunk, size - off)
        ok = k32.ReadProcessMemory(wt.HANDLE(handle), ctypes.c_void_p(base + off),
                                   buf, n, ctypes.byref(read))
        if ok and read.value > 0:
            yield buf.raw[:read.value]
        off += n


def collect_seeds(k32, handle, log):
    cand = set()
    total = regions = 0
    for base, rsize in iter_regions(k32, handle):
        regions += 1
        try:
            for blk in read_mem(k32, handle, base, rsize):
                total += len(blk)
                for m in re.finditer(rb"\d{6,14}", blk):
                    cand.add(m.group().decode())
        except Exception:
            continue
    log("扫描 %d 个内存区、约 %.0f MB，得到 %d 个 seed 候选" % (regions, total / 1048576, len(cand)))
    return cand


# ---------------------------------------------------------------- oracle

def magic_ok(pt):
    return (pt[:8] == b"\x89PNG\r\n\x1a\n"
            or pt[:6] in (b"GIF87a", b"GIF89a")
            or pt[:3] == b"\xff\xd8\xff"
            or pt[:4] in (b"wxgf", b"wxam")
            or (pt[:4] == b"RIFF" and pt[8:12] == b"WEBP"))


def load_oracles(wx_root, log, limit=8):
    """拿表情缓存文件的前 16 字节当验证样本。"""
    files = sorted(glob.glob(os.path.join(wx_root, "cache", "*", "Emoticon", "*", "*")))
    files = [f for f in files if os.path.isfile(f) and os.path.getsize(f) % 16 == 0]
    if not files:
        raise W.WxError(
            "在 %s\\cache\\*\\Emoticon 下没有可用样本。请先在微信里打开含有表情包的聊天，"
            "让表情落盘后再试。" % wx_root)
    oracles = [(os.path.basename(f), open(f, "rb").read()[:16]) for f in files[:limit]]
    log("oracle 样本 %d 个（取自 Emoticon 缓存）" % len(oracles))
    return oracles


def oracle_hits(kb, oracles):
    n = 0
    for _, c0 in oracles:
        pt = bytes(a ^ b for a, b in zip(AES.new(kb, AES.MODE_ECB).decrypt(c0), kb))
        if magic_ok(pt):
            n += 1
    return n


def derive(seed, wxid, formula="seed+wxid+EMOTICON"):
    material = FORMULAS[formula](seed, wxid)
    return hashlib.md5(material.encode("utf-8")).digest()[:16]


# ---------------------------------------------------------------- 缓存

def cache_load():
    if os.path.exists(CACHE):
        try:
            return json.load(open(CACHE, encoding="utf-8"))
        except Exception:
            return {}
    return {}


def cache_save(wxid, key_hex, seed=None, formula=None, hits=None):
    data = cache_load()
    data[wxid] = {"key": key_hex, "seed": seed, "formula": formula, "oracle_hits": hits}
    os.makedirs(os.path.dirname(CACHE), exist_ok=True)
    json.dump(data, open(CACHE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return CACHE


def cached_key(env, oracles=None, log=lambda *_: None):
    """返回缓存的 key bytes；若样本可用则顺手校验，校验失败返回 None。"""
    ent = cache_load().get(env["wxid"])
    if not ent:
        return None
    kb = bytes.fromhex(ent["key"])
    if oracles:
        hits = oracle_hits(kb, oracles)
        log("缓存密钥校验：%d/%d 命中" % (hits, len(oracles)))
        if hits == 0:
            log("缓存密钥已失效（微信升级或换账号后会变），需要重新扫描")
            return None
    return kb


# ---------------------------------------------------------------- 主流程

def find_key(env, pid=None, seed=None, log=print, save=True):
    oracles = load_oracles(env["wx_root"], log)
    kb = cached_key(env, oracles, log)
    if kb is not None:
        return kb, None

    if seed:
        for fname, fn in FORMULAS.items():
            kb = derive(seed, env["wxid"], fname)
            h = oracle_hits(kb, oracles)
            if h:
                log("seed=%s + %s 命中 %d/%d" % (seed, fname, h, len(oracles)))
                if save:
                    cache_save(env["wxid"], kb.hex(), seed, fname, h)
                return kb, seed
        raise W.WxError("给定 seed=%s 推导不出可用密钥" % seed)

    pids = [pid] if pid else find_pids()
    if not pids:
        raise W.WxError("没找到 %s 进程。表情密钥只在微信运行时存在于内存，"
                        "请先启动并登录微信再执行本步骤。" % "/".join(PROC_NAMES))

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    for p in pids:
        handle = k32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, p)
        if not handle:
            log("OpenProcess pid=%d 失败（err=%d），需要足够的进程权限" % (p, ctypes.get_last_error()))
            continue
        log("已附加 Weixin.exe pid=%d" % p)
        cand = collect_seeds(k32, handle, log)
        # 已知公式优先，缩小搜索面
        order = ["seed+wxid+EMOTICON", "seed+wxid+Emoticon", "seed+wxid",
                 "wxid+seed+EMOTICON", "seed"]
        tested, best = 0, None
        for s in cand:
            for fname in order:
                tested += 1
                kb = derive(s, env["wxid"], fname)
                h = oracle_hits(kb, oracles)
                if h and (best is None or h > best[2]):
                    best = (s, fname, h, kb)
                    if h == len(oracles):          # 全部样本命中，可以收工
                        break
        if best:
            s, fname, h, kb = best
            log("命中！seed=%s formula=%s key=%s（交叉验证 %d/%d，试了 %d 次）"
                % (s, fname, kb.hex(), h, len(oracles), tested))
            if save:
                cache_save(env["wxid"], kb.hex(), s, fname, h)
            return kb, s
        log("pid=%d 扫完未命中（试了 %d 次）" % (p, tested))
    raise W.WxError("内存里没找到表情密钥。请确认：① 微信已登录；② 最近打开过含表情包的聊天；"
                    "③ 微信版本仍是 4.1.x。")


def get_key(env, **kw):
    """给其它脚本用的便捷入口：优先缓存，其次扫描。"""
    return find_key(env, save=True, **kw)[0]


def main():
    ap = argparse.ArgumentParser(description="找出微信表情缓存 AES 密钥")
    ap.add_argument("--pid", type=int, help="指定 Weixin.exe 的 pid")
    ap.add_argument("--seed", help="已知 seed，直接推导")
    ap.add_argument("--db-dir", help="db_storage 路径（默认读 wechat-cli 配置）")
    ap.add_argument("--show", action="store_true", help="只打印缓存内容")
    a = ap.parse_args()

    env = W.discover(a.db_dir)
    if a.show:
        print(json.dumps(cache_load().get(env["wxid"], {}), ensure_ascii=False, indent=1))
        return

    def log(*m):
        print(*m, flush=True)

    kb, seed = find_key(env, pid=a.pid, seed=a.seed, log=log)
    print(json.dumps({"wxid": env["wxid"], "key": kb.hex(), "seed": seed,
                      "cache": CACHE}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except W.WxError as e:
        print("错误：%s" % e, file=sys.stderr)
        sys.exit(2)
