# -*- coding: utf-8 -*-
"""一键把某个微信联系人/群聊的聊天记录导出成自带表情包与语音的 HTML。

**先问位置再动手**：`--out` 必须由用户指定，不要自己塞一个默认目录。
交付目录里最终只有 4 个文件 + 1 个文件夹：

    <对方>_聊天记录.html      主交付物：表情贴图 + 语音播放器 + 搜索/筛选
    <对方>_聊天记录.md        带媒体相对链接
    <对方>_聊天记录.txt       纯文字，便于全文检索
    <对方>_聊天记录_raw.md    一行一条的原始日志（占位符形式，可被 --backfill 回填）
    <对方>_聊天记录_媒体/      表情/ + 语音/ + 两个 manifest

中间产物（解密后的数据库）落在系统临时目录，不进交付目录。

用法：
    python wxchat_export.py list --name 张三            # 先看候选联系人
    python wxchat_export.py all  --name 张三 --out "D:\\备份\\张三_聊天记录"
    python wxchat_export.py all  --peer-username wxid_xxx --name 张三 --out "..."

分步执行（大聊想断点续跑时用）：
    ... db        # 只解密数据库
    ... emojikey  # 只挖表情密钥
    ... voice     # 只提语音
    ... stickers  # 只提表情包
    ... build     # 只出 HTML/MD/TXT/RAW
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_export                                                     # noqa: E402
import extract_stickers                                                 # noqa: E402
import extract_voice                                                    # noqa: E402
import wxcommon as W                                                    # noqa: E402


def log(*m):
    print(*m, flush=True)


def _prepare(a):
    env = W.discover(getattr(a, "db_dir", None))
    out = os.path.abspath(a.out) if getattr(a, "out", None) else None
    if not out:
        raise W.WxError(
            "必须传 --out 指定存放位置（这是用户要选的），例如 --out \"D:\\\\备份\\\\张三_聊天记录\"。\n"
            "不要自己替用户决定：先问清楚放哪个文件夹再执行。")
    workdir = (os.path.abspath(a.workdir) if getattr(a, "workdir", None)
               else W.default_workdir(env))
    skipped = []
    _, skipped = W.decrypt_all(env, workdir)
    return env, out, workdir, skipped


def _peer(env, workdir, a):
    if getattr(a, "peer_username", None):
        return {"username": a.peer_username, "label": a.name}
    return W.pick_peer(env, workdir, a.name)


def media_root(out, peer_label):
    return os.path.join(out, build_export.media_dirname(peer_label))


# ---------------------------------------------------------------- list

def cmd_list(a):
    env = W.discover(getattr(a, "db_dir", None))
    workdir = W.default_workdir(env)
    W.decrypt_all(env, workdir)
    cands = W.resolve_contacts(env, workdir, a.name)
    if not cands:
        log("联系人里没有匹配「%s」的项" % a.name)
        return
    # 附上每个候选的会话消息条数（同名表可能跨 message_0/1 两个库，取条数多的那个）
    counts = {}
    for rel in (r"message\message_0.db", r"message\message_1.db"):
        try:
            db = W.decrypt_db(env, workdir, rel)
        except W.WxError:
            continue
        con = W.open_ro(db)
        for (name,) in con.execute("SELECT name FROM sqlite_master "
                                   "WHERE type='table' AND name LIKE 'Msg_%'"):
            n = con.execute('SELECT COUNT(*) FROM "%s"' % name).fetchone()[0]
            dbname = rel.split("\\")[-1]
            if name not in counts or n > counts[name][1]:
                counts[name] = (dbname, n)
    log("匹配「%s」共 %d 个联系人：" % (a.name, len(cands)))
    for c in cands:
        db, n = counts.get(W.msg_table_name(c["username"]), ("-", 0))
        log("  %s %-22s nick=%-14s alias=%-14s 消息 %5d (%s)"
            % ("★" if c["exact"] else " ", c["username"], c["nick_name"] or "-",
               c["alias"] or "-", n, db))


# ---------------------------------------------------------------- all

def cmd_all(a):
    t0 = time.time()
    env, out, workdir, skipped = _prepare(a)
    for s in skipped:
        log("跳过：%s" % s)
    log("账号 %s，db_storage=%s" % (env["wxid"], env["db_dir"]))

    peer = _peer(env, workdir, a)

    # 动手前先看目标目录有没有同名产物：有就停下来让用户决定，绝不静默覆盖
    W.ensure_dirs(out)
    log("存放位置：%s" % out)
    hits = W.existing_targets(out, peer["label"])
    if hits and not a.force:
        raise W.WxError(
            "该目录已存在本次要产出的文件，未做任何覆盖：\n  " + "\n  ".join(hits) +
            "\n\n请先问用户：① 换个文件夹，② 同意覆盖（加 --force），还是 ③ 他自己先挪走。")

    me = a.me or W.self_profile(env, workdir)["label"]
    tables = W.find_msg_tables(env, workdir, peer["username"])
    log("会话：%s（%s）→ %s，表 %s，共 %d 条消息"
        % (peer["label"], peer["username"],
           " + ".join("%s(%d)" % (t["rel"].split("\\")[-1], t["rows"]) for t in tables),
           tables[0]["table"], sum(t["rows"] for t in tables)))

    media = os.path.join(out, build_export.media_dirname(peer["label"]))
    if not a.no_voice:
        cid = W.chat_name_id(env, workdir, peer["username"])
        if cid is None:
            log("该会话没有语音记录，跳过语音提取")
        else:
            extract_voice.extract(env, workdir, media, peer["label"], me,
                                  peer["username"], cid, tables, log)

    stickers_done = False
    if not a.no_stickers:
        try:
            import find_emoji_key as FEK
            key = FEK.get_key(env, pid=a.pid, log=log)
            extract_stickers.extract(env, workdir, media, tables, key, log)
            stickers_done = True
        except W.WxError as e:
            log("表情包未还原：%s" % e)
            log("→ HTML 里会保留 [表情] 占位；先启动微信并打开几张表情，再单独跑 emojikey/stickers")

    stat = build_export.build(env, workdir, out, peer["label"], me, peer["username"],
                              tables, raw_path=a.backfill, media_root=media, force=True,
                              log=log)
    stat.update({"peer_info": {"label": peer["label"], "username": peer["username"]},
                 "me_label": me, "out": out, "workdir": workdir,
                 "tables": [{"db": t["rel"], "table": t["table"], "rows": t["rows"]}
                            for t in tables],
                 "stickers_extracted": stickers_done,
                 "elapsed_sec": round(time.time() - t0, 1)})
    # 统计落在中间目录，交付目录只留 4 个文件 + 1 个媒体文件夹
    W.write_json(os.path.join(workdir, "_wxchat_summary.json"), stat)

    log("")
    log("完成，用时 %.1f 秒。交付内容（%s）：" % (stat["elapsed_sec"], out))
    for p in stat["artifacts"]:
        log("  %-28s %8.0f KB" % (os.path.basename(p), os.path.getsize(p) / 1024))
    n_media = sum(len(f) for _, _, f in os.walk(stat["media_dir"]))
    log("  %-28s %8d 个文件" % (os.path.basename(stat["media_dir"]) + "/", n_media))
    log("中间产物（可删，不进交付目录）：%s" % workdir)
    if stat["total"]:
        log("HTML 用的是相对路径，给用户看之前先起静态服务：")
        log('  cd "%s" && <PY> -m http.server 8770 --bind 127.0.0.1' % out)


def cmd_db(a):
    env, out, workdir, skipped = _prepare(a)
    for s in skipped:
        log("跳过：%s" % s)
    log("数据库解密到 %s" % workdir)


def cmd_emojikey(a):
    import find_emoji_key as FEK
    env = W.discover(a.db_dir)
    kb, seed = FEK.find_key(env, pid=a.pid, log=log)
    log("KEY=%s seed=%s" % (kb.hex(), seed))


def cmd_voice(a):
    env, out, workdir, _ = _prepare(a)
    peer = _peer(env, workdir, a)
    me = a.me or W.self_profile(env, workdir)["label"]
    tables = W.find_msg_tables(env, workdir, peer["username"])
    cid = W.chat_name_id(env, workdir, peer["username"])
    if cid is None:
        raise W.WxError("该会话没有语音记录")
    extract_voice.extract(env, workdir, media_root(out, peer["label"]), peer["label"], me,
                          peer["username"], cid, tables, log)


def cmd_stickers(a):
    import find_emoji_key as FEK
    env, out, workdir, _ = _prepare(a)
    peer = _peer(env, workdir, a)
    tables = W.find_msg_tables(env, workdir, peer["username"])
    key = FEK.get_key(env, pid=a.pid, log=log)
    extract_stickers.extract(env, workdir, media_root(out, peer["label"]), tables, key, log)


def cmd_build(a):
    env, out, workdir, _ = _prepare(a)
    peer = _peer(env, workdir, a)
    me = a.me or W.self_profile(env, workdir)["label"]
    tables = W.find_msg_tables(env, workdir, peer["username"])
    build_export.build(env, workdir, out, peer["label"], me, peer["username"],
                       tables, raw_path=a.backfill, force=a.force, log=log)


# ---------------------------------------------------------------- CLI

def main():
    ap = argparse.ArgumentParser(description="微信 4.x 聊天记录 → HTML/MD/TXT（含表情包与语音）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, need_name=True, need_out=True):
        if need_name:
            p.add_argument("--name", required=True, help="联系人显示名")
        p.add_argument("--peer-username", help="直接指定对方 username，跳过名称匹配")
        p.add_argument("--me", help="『我』的显示名，默认取本机昵称")
        if need_out:
            p.add_argument("--out", required=True,
                           help="用户指定的存放文件夹（必须先问用户，不要自己假设）")
        p.add_argument("--workdir", help="解密库目录，默认系统临时目录（不进交付目录）")
        p.add_argument("--db-dir", help="db_storage 路径，默认读 wechat-cli 配置")
        p.add_argument("--backfill", help="外部的 raw.md，用来补齐 WAL 里还没落库的最新几条")
        p.add_argument("--force", action="store_true", help="目标目录已有同名产物时允许覆盖")

    p = sub.add_parser("list", help="列出匹配的联系人")
    common(p, need_out=False)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("all", help="一键全流程")
    common(p)
    p.add_argument("--pid", type=int, help="找表情密钥时指定 Weixin.exe 的 pid")
    p.add_argument("--no-voice", action="store_true", help="跳过语音")
    p.add_argument("--no-stickers", action="store_true", help="跳过表情包")
    p.set_defaults(func=cmd_all)

    p = sub.add_parser("db", help="只解密数据库")
    common(p, need_name=False)
    p.set_defaults(func=cmd_db)

    p = sub.add_parser("emojikey", help="只扫内存找表情密钥")
    p.add_argument("--pid", type=int)
    p.add_argument("--db-dir")
    p.set_defaults(func=cmd_emojikey)

    p = sub.add_parser("voice", help="只提语音")
    common(p)
    p.set_defaults(func=cmd_voice)

    p = sub.add_parser("stickers", help="只提表情包")
    common(p)
    p.add_argument("--pid", type=int)
    p.set_defaults(func=cmd_stickers)

    p = sub.add_parser("build", help="只重新生成 HTML/MD/TXT")
    common(p)
    p.set_defaults(func=cmd_build)

    a = ap.parse_args()
    a.func(a)


if __name__ == "__main__":
    try:
        main()
    except W.WxError as e:
        print("错误：%s" % e, file=sys.stderr)
        sys.exit(2)
