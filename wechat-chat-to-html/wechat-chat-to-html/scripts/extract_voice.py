# -*- coding: utf-8 -*-
"""把会话里的语音消息还原成可播放的 WAV。

语音不是独立文件：SILK 音频以 BLOB 形式内联在 media_0.db 的
`VoiceInfo.voice_data` 里（头部标记 `\\x02#!SILK_V3`），需要用 pysilk 解成
24kHz 单声道 PCM 再包 WAV。

发送人用 (create_time, local_id) 作为复合键回查消息表——因为同一会话的表可能
分散在 message_0/1.db 两个库里，local_id 会重号。
"""
from __future__ import annotations

import argparse
import datetime
import io
import os
import sys
import wave

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wxcommon as W                                                    # noqa: E402

import pysilk                                                           # noqa: E402

SILK_MAGIC = b"#!SILK_V3"
RATE = 24000


def voice_key(create_time, local_id):
    """语音条目的稳定键，导出与渲染两侧都用它。"""
    return "%d_%d" % (int(create_time), int(local_id))


def _sender_index(env, workdir, tables, peer_username, peer_label, me_label):
    """(create_time, local_id) -> 发送人显示名。"""
    idx = {}
    for t in tables:
        labels = W.sender_labels(env, workdir, t["path"], peer_username, peer_label, me_label)
        if not labels:
            continue
        con = W.open_ro(t["path"])
        try:
            rows = con.execute('SELECT local_id, create_time, real_sender_id FROM "%s" '
                               'WHERE (local_type & 0xFFFFFFFF)=34' % t["table"]).fetchall()
        except Exception:
            continue
        for lid, ts, rid in rows:
            idx[voice_key(ts, lid)] = labels.get(rid, str(rid))
    return idx


def extract(env, workdir, media_root, peer_label, me_label, peer_username, chat_id,
            tables, log=print):
    """tables 为 wxcommon.find_msg_tables 的返回值。返回 manifest 列表。"""
    out_dir = os.path.join(media_root, "语音")
    os.makedirs(out_dir, exist_ok=True)

    mc = W.open_ro(W.media_dir(env, workdir))
    who = _sender_index(env, workdir, tables, peer_username, peer_label, me_label)

    rows = mc.execute(
        "SELECT local_id, create_time, voice_data FROM VoiceInfo "
        "WHERE chat_name_id=? ORDER BY create_time", (chat_id,)).fetchall()

    manifest, ok, fail = [], 0, 0
    for lid, ts, blob in rows:
        data = bytes(blob or b"")
        i = data.find(SILK_MAGIC)
        if i < 0:
            fail += 1
            manifest.append({"key": voice_key(ts, lid), "local_id": lid,
                             "error": "no SILK header"})
            continue
        payload = data[i:]
        dst = io.BytesIO()
        try:
            pysilk.decode(io.BytesIO(payload), dst, RATE)
            pcm = dst.getvalue()
        except Exception as e:
            fail += 1
            manifest.append({"key": voice_key(ts, lid), "local_id": lid, "error": repr(e)})
            continue

        dt = datetime.datetime.fromtimestamp(ts)
        sender = who.get(voice_key(ts, lid), "?")
        name = "%s_%s.wav" % (dt.strftime("%Y%m%d_%H%M%S"), sender)
        path = os.path.join(out_dir, name)
        n = 1
        while os.path.exists(path):
            name = "%s_%s_%d.wav" % (dt.strftime("%Y%m%d_%H%M%S"), sender, n)
            path = os.path.join(out_dir, name)
            n += 1
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(pcm)
        ok += 1
        manifest.append({
            "key": voice_key(ts, lid),
            "local_id": lid,
            "time": dt.strftime("%Y-%m-%d %H:%M:%S"),
            "sender": sender,
            "file": "语音/" + name,
            "seconds": round(len(pcm) / 2 / RATE, 2),
            "silk_bytes": len(payload),
        })

    W.write_json(os.path.join(media_root, "voice_manifest.json"), manifest)
    log("语音：库内 %d 条 → WAV %d 个，失败 %d 个" % (len(rows), ok, fail))
    return manifest


def main():
    ap = argparse.ArgumentParser(description="提取会话语音为 WAV")
    ap.add_argument("--name", required=True, help="联系人名称")
    ap.add_argument("--peer-username")
    ap.add_argument("--out", required=True, help="用户指定的存放文件夹")
    ap.add_argument("--workdir", help="解密库目录，默认系统临时目录")
    ap.add_argument("--db-dir")
    a = ap.parse_args()

    env = W.discover(a.db_dir)
    workdir = a.workdir or W.default_workdir(env)
    W.decrypt_all(env, workdir)
    peer = ({"label": a.name, "username": a.peer_username} if a.peer_username
            else W.pick_peer(env, workdir, a.name))
    me = W.self_profile(env, workdir)["label"]
    tables = W.find_msg_tables(env, workdir, peer["username"])
    cid = W.chat_name_id(env, workdir, peer["username"])
    if cid is None:
        raise W.WxError("media_0.db 里没有该会话的 chat_name_id，说明没发过语音")
    W.ensure_dirs(a.out)
    extract(env, workdir, os.path.join(a.out, W.media_dirname(peer["label"])),
            peer["label"], me, peer["username"], cid, tables)


if __name__ == "__main__":
    try:
        main()
    except W.WxError as e:
        print("错误：%s" % e, file=sys.stderr)
        sys.exit(2)
