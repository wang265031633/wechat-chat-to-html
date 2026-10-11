# -*- coding: utf-8 -*-
"""微信 4.x 本地数据访问层。

职责：账号/密钥发现 → 库解密 → 联系人检索 → 会话表定位。

约定（微信 4.1.x Windows）：
  * 数据库是 SQLCipher 4（AES-256-CBC, page=4096, reserve=80），每个库独立密钥；
    `~/.wechat-cli/all_keys.json` 里的 enc_key 是**裸密钥**，不需要再跑 PBKDF2。
  * 每个会话一张表：`Msg_<md5(对方 username)>`，散落在 message_0.db / message_1.db。
  * media_0.db 的 `Name2Id` 表：rowid 即 VoiceInfo.chat_name_id。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile

CONFIG_JSON = os.path.expanduser("~/.wechat-cli/config.json")
KEYS_JSON = os.path.expanduser("~/.wechat-cli/all_keys.json")

# 导出流程需要解密的库（相对 db_storage）
NEEDED_DBS = [
    r"contact\contact.db",
    r"message\message_0.db",
    r"message\message_1.db",
    r"message\media_0.db",
    r"message\message_resource.db",
    r"emoticon\emoticon.db",
]


class WxError(RuntimeError):
    """环境不满足或无法定位数据时抛出，消息面向使用者可读。"""


# ---------------------------------------------------------------- 基础设施

def uri(path):
    """sqlite3 只读 URI。Windows 盘符路径必须转成正斜杠。"""
    return "file:%s?mode=ro" % os.path.abspath(path).replace("\\", "/")


def open_ro(path):
    con = sqlite3.connect(uri(path), uri=True)
    return con


def load_json(path, what):
    if not os.path.exists(path):
        raise WxError("缺少%s：%s" % (what, path))
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def cli_crypto():
    """复用 wechat-cli 已验证的 SQLCipher 解密实现（本技能唯一外部依赖）。"""
    try:
        from wechat_cli.core.crypto import full_decrypt, decrypt_wal
    except Exception as e:                                    # pragma: no cover
        raise WxError(
            "无法导入 wechat_cli.core.crypto（%s）。请改用装了 wechat-cli 的解释器，"
            "见 references/环境与依赖.md" % e)
    return full_decrypt, decrypt_wal


def ensure_dirs(*paths):
    for p in paths:
        if p:
            os.makedirs(p, exist_ok=True)


def default_workdir(env):
    """解密库的落地目录。

    故意放在系统临时目录，而不是用户指定的导出目录里——交付目录只应包含
    4 个文件 + 1 个媒体文件夹，中间产物（几十~几百 MB 的解密库）不进去。
    """
    p = os.path.join(tempfile.gettempdir(), "wxchat_work", env["wxid"])
    os.makedirs(p, exist_ok=True)
    return p


# ---------------------------------------------------------------- 产物命名

def base_name(peer_label):
    """4 个交付文件共用的主名：<对方>_聊天记录.{html,md,txt,raw.md}"""
    return "%s_聊天记录" % peer_label


def file_names(peer_label):
    b = base_name(peer_label)
    return [b + ".html", b + ".md", b + ".txt", b + "_raw.md"]


def media_dirname(peer_label):
    """媒体文件夹名：<对方>_聊天记录_媒体/"""
    return "%s_聊天记录_媒体" % peer_label


def deliverables(peer_label):
    """交付目录里应当出现的全部顶层条目（4 个文件 + 1 个文件夹）。"""
    return file_names(peer_label) + [media_dirname(peer_label)]


def existing_targets(out_dir, peer_label):
    """目标目录里已存在的同名产物（用来在动手前问用户要不要覆盖）。"""
    if not os.path.isdir(out_dir):
        return []
    hits = []
    for n in deliverables(peer_label):
        p = os.path.join(out_dir, n)
        if os.path.isdir(p):
            cnt = sum(len(f) for _, _, f in os.walk(p))
            hits.append("%s\\（文件夹，已含 %d 个文件）" % (p, cnt))
        elif os.path.exists(p):
            hits.append("%s（%.0f KB）" % (p, os.path.getsize(p) / 1024))
    return hits


def write_json(path, obj):
    ensure_dirs(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    return path


# ---------------------------------------------------------------- 账号发现

def discover(db_dir=None):
    """返回 {db_dir, wx_root, wxid, keys}。"""
    if db_dir is None:
        db_dir = load_json(CONFIG_JSON, "wechat-cli 配置")["db_dir"]
    db_dir = os.path.abspath(db_dir)
    if not os.path.isdir(db_dir):
        raise WxError("db_storage 目录不存在：%s" % db_dir)
    root = os.path.dirname(db_dir)                  # ...\xwechat_files\wxid_xxx_abcd
    wxid = re.sub(r"_[0-9a-f]{4}$", "", os.path.basename(root))
    return {
        "db_dir": db_dir,
        "wx_root": root,                            # 含 cache/ business/ msg/ 等
        "wxid": wxid,
        "keys": load_json(KEYS_JSON, "数据库密钥表 all_keys.json"),
    }


# ---------------------------------------------------------------- 库解密

def decrypt_db(env, workdir, rel, force=False):
    """解密单个库（含 WAL 回放），返回解密后路径。"""
    full_decrypt, decrypt_wal = cli_crypto()
    src = os.path.join(env["db_dir"], rel)
    if not os.path.exists(src):
        raise WxError("源库不存在：%s" % src)
    entry = env["keys"].get(rel)
    if not entry:
        raise WxError("all_keys.json 里没有 %s 的密钥，请重新运行 wechat-cli 提取密钥" % rel)
    dst = os.path.join(workdir, rel.replace("\\", "_"))
    if force or not os.path.exists(dst):
        key = bytes.fromhex(entry["enc_key"])
        full_decrypt(src, dst, key)
        wal = src + "-wal"
        if os.path.exists(wal):
            decrypt_wal(wal, dst, key)
    return dst


def decrypt_all(env, workdir, rels=None, force=False):
    """批量解密，返回 {rel: 解密路径}；缺失的库跳过并记录。"""
    rels = rels or NEEDED_DBS
    out, skipped = {}, []
    for rel in rels:
        try:
            out[rel] = decrypt_db(env, workdir, rel, force=force)
        except WxError as e:
            skipped.append("%s：%s" % (rel, e))
    return out, skipped


# ---------------------------------------------------------------- 联系人

def resolve_contacts(env, workdir, name):
    """按备注/昵称/微信号/别名检索联系人，精确匹配排前面。"""
    db = decrypt_db(env, workdir, r"contact\contact.db")
    con = open_ro(db)
    like = "%" + name + "%"
    rows = con.execute(
        "SELECT username, alias, nick_name, remark FROM contact "
        "WHERE username=? OR alias=? OR nick_name=? OR remark=? "
        "   OR nick_name LIKE ? OR remark LIKE ? OR alias LIKE ? OR username LIKE ?",
        (name, name, name, name, like, like, like, like)).fetchall()
    out = []
    for un, al, nk, rm in rows:
        label = rm or nk or al or un
        out.append({
            "username": un,
            "alias": al or "",
            "nick_name": nk or "",
            "remark": rm or "",
            "label": label,
            "exact": name in (un, al, nk, rm),
        })
    out.sort(key=lambda c: (not c["exact"], c["username"].endswith("@chatroom"), c["label"]))
    return out


def self_profile(env, workdir):
    """本机账号的昵称/微信号（用于 HTML 里『我』的显示名）。"""
    db = decrypt_db(env, workdir, r"contact\contact.db")
    con = open_ro(db)
    r = con.execute("SELECT alias, nick_name, remark FROM contact WHERE username=?",
                    (env["wxid"],)).fetchone()
    alias, nick, remark = (r or ("", "", ""))
    return {"wxid": env["wxid"], "alias": alias or "", "nick_name": nick or "",
            "label": nick or alias or env["wxid"]}


def pick_peer(env, workdir, name):
    """把用户给的名字收敛到一个联系人；不唯一时抛错并附带候选清单。"""
    cands = resolve_contacts(env, workdir, name)
    exact = [c for c in cands if c["exact"]]
    pool = exact or cands
    if not pool:
        raise WxError("联系人里找不到「%s」，先用 `--list` 看看有哪些候选" % name)
    if len(pool) > 1:
        lines = ["「%s」匹配到多个联系人，请用完整昵称/微信号再试：" % name]
        for c in pool[:20]:
            lines.append("  - %s（nick=%s username=%s）" % (c["label"], c["nick_name"], c["username"]))
        raise WxError("\n".join(lines))
    if not exact:
        lines = ["「%s」没有精确匹配，最接近的是：" % name]
        for c in cands[:10]:
            lines.append("  - %s（nick=%s username=%s）" % (c["label"], c["nick_name"], c["username"]))
        raise WxError("\n".join(lines) + "\n请用上面完整名字重试。")
    return pool[0]


# ---------------------------------------------------------------- 会话表

def msg_table_name(username):
    """微信为每个会话建的 Msg 表名。"""
    return "Msg_" + hashlib.md5(username.encode("utf-8")).hexdigest()


def find_msg_tables(env, workdir, peer_username):
    """找出该会话的 Msg 表。

    重要：同一个会话的表可能**同时存在于 message_0.db 和 message_1.db**——
    微信按活跃度分片，老消息留在旧库、新消息写进新库。必须并集，不能只取一个。
    返回 [{'rel', 'table', 'rows', 'path'}]，按行数降序。
    """
    tab = msg_table_name(peer_username)
    found = []
    for rel in (r"message\message_0.db", r"message\message_1.db"):
        try:
            db = decrypt_db(env, workdir, rel)
        except WxError:
            continue
        con = open_ro(db)
        if con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                       (tab,)).fetchone():
            found.append({"rel": rel, "table": tab, "path": db,
                          "rows": con.execute('SELECT COUNT(*) FROM "%s"' % tab).fetchone()[0]})
    if not found:
        raise WxError("在 message_0/1.db 里都没找到会话表 %s（对方 username=%s）；"
                      "可能这段聊天只存在于手机端" % (tab, peer_username))
    found.sort(key=lambda d: -d["rows"])
    return found


def find_msg_table(env, workdir, peer_username):
    """只要一个库时用（消息条数最多的那个）。返回 (rel, table, rows)。"""
    f = find_msg_tables(env, workdir, peer_username)[0]
    return f["rel"], f["table"], f["rows"]


def sender_labels(env, workdir, db_path, peer_username, peer_label, me_label):
    """real_sender_id → 显示名。

    每个消息库有**自己的** Name2Id 表，rowid 才是 real_sender_id；
    所以必须按库解析，不能硬编码 1=我 / 2=对方（不同库里编号会变，群里还会更多）。
    """
    labels = {}
    try:
        con = open_ro(db_path)
        for rid, un in con.execute("SELECT rowid, user_name FROM Name2Id"):
            if un == env["wxid"]:
                labels[rid] = me_label
            elif un == peer_username:
                labels[rid] = peer_label
            else:
                labels[rid] = un or str(rid)
    except sqlite3.Error:
        pass
    return labels


def merge_sender_labels(env, workdir, peer_username, peer_label, me_label, tables=None):
    """多库合并：{"<rel>|<rid>": label}，避免不同库的 rowid 冲突。"""
    out = {}
    for t in (tables or find_msg_tables(env, workdir, peer_username)):
        for rid, lab in sender_labels(env, workdir, t["path"], peer_username,
                                      peer_label, me_label).items():
            out["%s|%d" % (t["rel"], rid)] = lab
    return out


def chat_name_id(env, workdir, peer_username):
    """media_0.db 里给语音用的 chat_name_id。"""
    db = decrypt_db(env, workdir, r"message\media_0.db")
    con = open_ro(db)
    r = con.execute("SELECT rowid FROM Name2Id WHERE user_name=?", (peer_username,)).fetchone()
    return r[0] if r else None


def media_dir(env, workdir):
    """media_0.db 解密后的路径。"""
    return decrypt_db(env, workdir, r"message\media_0.db")


def emoticon_db(env, workdir):
    return decrypt_db(env, workdir, r"emoticon\emoticon.db")
