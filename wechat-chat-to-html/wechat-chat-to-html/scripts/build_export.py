# -*- coding: utf-8 -*-
"""重建会话并产出 HTML / Markdown / 纯文本三份导出版本。

HTML 版自带：真实表情包贴图、语音播放器、全文搜索、类型筛选、日期跳转。
表情与语音路径取自同目录下的 媒体/ 文件夹（相对路径，需要本地起个静态服务看）。
"""
from __future__ import annotations

import argparse
import datetime
import html
import json
import os
import re
import sqlite3
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import wxcommon as W                                                    # noqa: E402

import zstandard                                                        # noqa: E402

WD = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

base_name = W.base_name
media_dirname = W.media_dirname
deliverables = W.deliverables


# ---------------------------------------------------------------- 内容解析

def make_decoder():
    dctx = zstandard.ZstdDecompressor()

    def dec(mc, ct):
        if mc is None:
            return ""
        if ct == 4 and isinstance(mc, (bytes, bytearray)):
            try:
                return dctx.decompress(bytes(mc)).decode("utf-8", "replace")
            except Exception:
                return ""
        if isinstance(mc, (bytes, bytearray)):
            return bytes(mc).decode("utf-8", "replace")
        return mc

    return dec


def strip_prefix(s):
    """群聊里消息体会带 'wxid_xxx:\\n' 前缀。"""
    return s.split(":\n", 1)[1] if ":\n" in s[:80] else s


def parse_appmsg(s):
    """type=49 应用消息（引用/文件/链接/小程序/转账…）。"""
    try:
        root = ET.fromstring(s)
    except Exception:
        return None
    am = root.find(".//appmsg")
    if am is None:
        return None
    title = (am.findtext("title") or "").strip()
    atype = (am.findtext("type") or "").strip()
    if atype == "57":
        ref = am.find(".//refermsg")
        out = title or "[引用消息]"
        if ref is not None:
            dn = (ref.findtext("displayname") or "").strip()
            rc = re.sub(r"\s+", " ", (ref.findtext("content") or "")).strip()
            if len(rc) > 160:
                rc = rc[:160] + "…"
            if rc:
                out += ("\n  ↳ 回复 %s：%s" % (dn, rc)) if dn else ("\n  ↳ %s" % rc)
        return out
    if atype == "6":
        return "[文件] %s" % title if title else "[文件]"
    if atype == "5":
        return "[链接] %s" % title if title else "[链接]"
    if atype in ("33", "36", "44"):
        return "[小程序] %s" % title if title else "[小程序]"
    if atype == "19":
        return "[聊天记录] %s" % title if title else "[聊天记录]"
    if atype == "2000":
        return "[转账]"
    if atype == "2001":
        return "[红包]"
    return "[链接/文件] %s" % title if title else "[链接/文件]"


def parse_location(s):
    m = re.search(r'poiname="([^"]*)"', s) or re.search(r'label="([^"]*)"', s)
    return "[位置] " + (m.group(1) if m and m.group(1) else "")


def load_messages(env, workdir, tables, peer_username, peer_label, me_label,
                  sticker_man, voice_man, log=print):
    """把同一会话分散在多个库里的消息合并成一条时间线。

    坑：同一张 Msg_ 表可能同时存在于 message_0.db 和 message_1.db，
    且各库自己的 Name2Id 决定了 real_sender_id 的含义，必须逐库解析并去重。
    """
    dec = make_decoder()
    merged = {}
    for t in tables:
        labels = W.sender_labels(env, workdir, t["path"], peer_username, peer_label, me_label)
        con = W.open_ro(t["path"])
        for lid, sid, lt, ts, snd, mc, ctf in con.execute(
                'SELECT local_id, server_id, local_type, create_time, real_sender_id, '
                'message_content, WCDB_CT_message_content FROM "%s" '
                'ORDER BY create_time, local_id' % t["table"]):
            bt = lt & 0xFFFFFFFF
            who = labels.get(snd, str(snd))
            dt = datetime.datetime.fromtimestamp(ts)
            content = dec(mc, ctf)
            rec = {"dt": dt, "who": who, "kind": "text", "text": "", "media": None,
                   "vkey": "%d_%d" % (int(ts), int(lid))}

            if bt == 1:
                rec["text"] = strip_prefix(content)
            elif bt == 47:
                md5 = re.search(r'md5="([0-9a-f]{32})"', content)
                ent = sticker_man.get(md5.group(1)) if md5 else None
                rec["kind"] = "sticker"
                if ent and not ent.get("missing"):
                    rec["media"] = ent["file"]
                else:
                    rec["text"] = "[表情]"
            elif bt == 34:
                v = voice_man.get(rec["vkey"])
                rec["kind"] = "voice"
                if v:
                    rec["media"] = v["file"]
                    rec["dur"] = v["seconds"]
                    rec["text"] = "语音 %ss" % v["seconds"]
                else:
                    rec["text"] = "[语音]"
            elif bt == 3:
                rec["kind"] = "image"
                rec["text"] = "[图片]"
            elif bt == 48:
                rec["kind"] = "sys"
                rec["text"] = parse_location(strip_prefix(content))
            elif bt == 49:
                rec["kind"] = "app"
                rec["text"] = parse_appmsg(strip_prefix(content)) or "[链接/文件]"
            elif bt == 50:
                rec["kind"] = "app"
                rec["text"] = "[通话]"
            elif bt == 10000:
                rec["kind"] = "sys"
                rec["text"] = strip_prefix(content).strip()
            else:
                rec["kind"] = "app"
                rec["text"] = "[type=%d]" % bt

            key = ("s" + str(sid)) if sid else ("%s|%d" % (t["rel"], lid))
            merged.setdefault(key, rec)
    msgs = sorted(merged.values(), key=lambda r: r["dt"])
    if len(tables) > 1:
        log("合并 %d 个库的会话表：%d 条（去重后）" % (len(tables), len(msgs)))
    return msgs


def backfill_from_raw(msgs, raw_path, peer_label, me_label):
    """微信 WAL 里还没落库的最新消息：从 wechat-cli 的原始导出里补齐。"""
    if not raw_path or not os.path.exists(raw_path):
        return 0
    pat = re.compile(r"^- \[(\d{4}-\d{2}-\d{2} \d{2}:\d{2})\] (me|[^:]+): (.*)$")
    last = msgs[-1]["dt"] if msgs else datetime.datetime(1970, 1, 1)
    extra = []
    for line in open(raw_path, encoding="utf-8"):
        m = pat.match(line.rstrip("\n"))
        if not m:
            continue
        dt = datetime.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M")
        if dt <= last:
            continue
        who = me_label if m.group(2).strip() == "me" else m.group(2).strip()
        text = m.group(3).strip()
        if text.startswith("[图片]"):
            kind, text = "image", "[图片]"
        elif text.startswith("[语音]"):
            kind, text = "voice", "[语音]"
        elif text.startswith("[表情]"):
            kind, text = "sticker", "[表情]"
        else:
            kind = "text"
        extra.append({"dt": dt, "who": who, "kind": kind, "text": text, "media": None})
    if extra:
        msgs.extend(extra)
        msgs.sort(key=lambda r: r["dt"])
    return len(extra)


# ---------------------------------------------------------------- 渲染

PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
*{box-sizing:border-box}
body{margin:0;font-family:"PingFang SC","Microsoft YaHei",system-ui,sans-serif;
background:#f5f6f8;color:#1f2328;line-height:1.65}
header{position:sticky;top:0;z-index:20;background:#fff;border-bottom:1px solid #e4e6eb;
padding:12px 20px;box-shadow:0 1px 6px rgba(0,0,0,.05)}
h1{margin:0 0 6px;font-size:17px;font-weight:600}
.stats{display:flex;flex-wrap:wrap;gap:4px 18px;font-size:13px;color:#5a6270}
.stats b{color:#1f2328}
.kinds{display:flex;flex-wrap:wrap;gap:10px;font-size:12.5px;color:#4a5262;margin-top:6px}
.tools{margin-top:9px;display:flex;flex-wrap:wrap;gap:8px;align-items:center}
input[type=search]{flex:1;min-width:180px;max-width:380px;padding:7px 12px;border:1px solid #d7dae0;
border-radius:8px;font-size:14px;outline:none}
input[type=search]:focus{border-color:#4a7dff;box-shadow:0 0 0 3px rgba(74,125,255,.12)}
.btn{padding:6px 11px;border:1px solid #d7dae0;background:#fff;border-radius:8px;font-size:13px;
cursor:pointer;color:#3a4150}
.btn:hover{background:#f0f2f5}
.btn.on{background:#4a7dff;color:#fff;border-color:#4a7dff}
.hit{font-size:12.5px;color:#5a6270}
nav{margin-top:7px;max-height:58px;overflow:auto;display:flex;flex-wrap:wrap;gap:5px}
nav a{font-size:12px;color:#4a7dff;text-decoration:none;background:#eef3ff;border-radius:6px;
padding:1px 7px;white-space:nowrap}
nav a:hover{background:#dfe8ff}
main{max-width:920px;margin:0 auto;padding:18px 16px 90px}
.day{text-align:center;margin:24px 0 12px;font-size:12px;color:#7a8290;position:relative}
.day::before{content:"";display:block;height:1px;background:#e4e6eb;margin-bottom:-9px}
.day>span{background:#f5f6f8}
.row{display:flex;flex-direction:column;margin:8px 0;align-items:flex-start}
.row.me{align-items:flex-end}
.meta{font-size:11px;color:#9aa1ad;margin:0 4px 2px}
.bub{max-width:76%;padding:7px 12px;border-radius:12px;background:#fff;border:1px solid #e6e8ec;
font-size:14.5px;word-break:break-word;box-shadow:0 1px 2px rgba(0,0,0,.03)}
.row.me .bub{background:#d6e8ff;border-color:#c3dcff}
.bub img.sticker{display:block;max-width:160px;max-height:160px;border-radius:6px;cursor:zoom-in;
background:transparent}
.bub:has(img.sticker){background:transparent!important;border:none!important;box-shadow:none!important;
padding:0!important}
audio{height:34px;vertical-align:middle}
.dur{font-size:12px;color:#6b7280;margin-left:6px}
.sys{text-align:center;margin:12px 0;font-size:12px;color:#8b929d}
.sys span{background:#eceef1;border-radius:10px;padding:2px 10px}
.sys i{font-style:normal;margin-left:6px;color:#b3b8c2}
.hidden{display:none!important}
mark{background:#ffe58f;padding:0 1px;border-radius:2px}
footer{text-align:center;color:#9aa1ad;font-size:12px;padding:18px}
#lb{position:fixed;inset:0;background:rgba(0,0,0,.72);display:none;align-items:center;
justify-content:center;z-index:99;cursor:zoom-out}
#lb img{max-width:88vw;max-height:88vh;border-radius:8px;background:#fff}
</style></head><body>
<header>
  <h1>__H1__</h1>
  <div class="stats">__STATS__</div>
  <div class="kinds">__KINDS__</div>
  <div class="tools">
    <input type="search" id="q" placeholder="搜索聊天内容 / 表情包 / 语音…">
    <button class="btn f on" data-k="all">全部</button>
    <button class="btn f" data-k="text">文字</button>
    <button class="btn f" data-k="sticker">表情包</button>
    <button class="btn f" data-k="voice">语音</button>
    <button class="btn f" data-k="me">只看__ME__</button>
    <button class="btn f" data-k="peer">只看__PEER__</button>
    <span class="hit" id="hit"></span>
  </div>
  <nav>__NAV__</nav>
</header>
<main id="main">
__ROWS__
</main>
<footer>表情包与语音由本地微信数据库还原（表情 AES-128-CBC / IV=Key + HEVC 解码 · 语音 SILK→WAV）<br>
仅本地处理，未联网上传</footer>
<div id="lb" onclick="this.style.display='none'"><img id="lbi" src=""></div>
<script>
const q=document.getElementById('q'),hit=document.getElementById('hit');
const rows=[...document.querySelectorAll('.row')];
const days=[...document.querySelectorAll('.day')];
let kind='all';
function esc(s){return s.replace(/[.*+?^${}()|[\\]\\\\]/g,'\\\\$&');}
function apply(){
  const k=q.value.trim().toLowerCase();let n=0;
  rows.forEach(r=>{
    let ok=(kind==='all')||(kind==='me'&&r.classList.contains('me'))||
           (kind==='peer'&&r.classList.contains('peer'))||(r.dataset.k===kind);
    const b=r.querySelector('.bub');
    const t=b.dataset.text||'';
    if(ok&&k) ok=t.includes(k);
    r.classList.toggle('hidden',!ok);
    const raw=b.textContent;
    if(ok&&k&&r.dataset.k!=='sticker'&&r.dataset.k!=='voice'){
      const i=raw.toLowerCase().indexOf(k);
      b.innerHTML=esc(raw.slice(0,i))+'<mark>'+esc(raw.slice(i,i+k.length))+'</mark>'+esc(raw.slice(i+k.length));
    }else if(r.dataset.k!=='sticker'&&r.dataset.k!=='voice'&&b.querySelector('mark')){
      b.textContent=raw;
    }
    if(ok)n++;
  });
  days.forEach(d=>{
    let s=d.nextElementSibling,any=false;
    while(s&&!s.classList.contains('day')){
      if(s.classList.contains('row')&&!s.classList.contains('hidden')){any=true;break;}
      s=s.nextElementSibling;
    }
    d.classList.toggle('hidden',!any);
  });
  hit.textContent=(k||kind!=='all')?('命中 '+n+' 条'):'';
}
q.addEventListener('input',apply);
document.querySelectorAll('.btn.f').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('.btn.f').forEach(x=>x.classList.remove('on'));
  b.classList.add('on');kind=b.dataset.k;apply();
});
document.querySelectorAll('nav a').forEach(a=>a.onclick=e=>{
  e.preventDefault();
  const el=document.getElementById('d'+a.getAttribute('href').slice(2));
  if(el)window.scrollTo({top:el.getBoundingClientRect().top+scrollY-200,behavior:'smooth'});
});
function zoom(img){document.getElementById('lbi').src=img.src;
  document.getElementById('lb').style.display='flex';}
</script></body></html>"""

ICONS = {"text": "💬", "sticker": "😀", "voice": "🔊", "image": "🖼", "app": "🔗", "sys": "•"}


def esc_html(s):
    return html.escape(s).replace("\n", "<br>")


def render(msgs, out_dir, title, peer_label, me_label, fname=None, use_media=True):
    fname = fname or base_name(peer_label)
    media_prefix = (media_dirname(peer_label) + "/") if use_media else ""
    total = len(msgs)
    c_me = sum(1 for m in msgs if m["who"] == me_label)
    kinds = {}
    for m in msgs:
        kinds[m["kind"]] = kinds.get(m["kind"], 0) + 1
    t0, t1 = msgs[0]["dt"], msgs[-1]["dt"]
    active = len({m["dt"].date() for m in msgs})
    span = (t1.date() - t0.date()).days + 1
    stickers_shown = sum(1 for m in msgs if m["kind"] == "sticker" and m["media"])
    voices_shown = sum(1 for m in msgs if m["kind"] == "voice" and m["media"])

    info = [
        ("对话双方", "%s（我） ⇄ %s" % (me_label, peer_label)),
        ("时间范围", "%s ~ %s" % (t0.strftime("%Y-%m-%d %H:%M"), t1.strftime("%Y-%m-%d %H:%M"))),
        ("跨度", "%d 天（实际聊天 %d 天）" % (span, active)),
        ("消息总数", "%d 条（我 %d / %s %d）" % (total, c_me, peer_label, total - c_me)),
        ("语音", "%d 条，已提取 %d 条可播放 WAV" % (kinds.get("voice", 0), voices_shown)),
        ("表情", "%d 条，已还原 %d 条图片/GIF" % (kinds.get("sticker", 0), stickers_shown)),
        ("图片", "%d 条（微信 .dat 加密，未解密）" % kinds.get("image", 0)),
        ("排序", "按时间升序（最早 → 最新）"),
    ]

    # ---- Markdown ----
    md = ["# %s" % title, "", "| 项目 | 内容 |", "| --- | --- |"]
    md += ["| %s | %s |" % kv for kv in info]
    md += ["", "> 表情/语音已还原到 `媒体/`，在支持的 Markdown 阅读器里可直接看到表情图与语音链接。",
           "", "---", ""]
    cur, first = None, True
    for m in msgs:
        if m["dt"].date() != cur:
            cur = m["dt"].date()
            if not first:
                md.append("")
            first = False
            md += ["## %s %s" % (cur.strftime("%Y-%m-%d"), WD[cur.weekday()]), ""]
        who = "**%s**" % m["who"]
        t = "`%s`" % m["dt"].strftime("%H:%M")
        if m["kind"] == "sticker" and m["media"]:
            md.append("- %s %s：![表情](%s%s)" % (t, who, media_prefix, m["media"]))
        elif m["kind"] == "voice" and m["media"]:
            md.append("- %s %s：🔊 [%ss 语音](%s%s)" % (t, who, m["dur"], media_prefix, m["media"]))
        else:
            md.append("- %s %s：%s" % (t, who, m["text"].replace("\n", "  \n    ")))
    md.append("")
    with open(os.path.join(out_dir, "%s.md" % fname), "w", encoding="utf-8") as f:
        f.write("\n".join(md))

    # ---- TXT ----
    tx = [title, "=" * 40] + ["%s：%s" % kv for kv in info] + ["", "-" * 40, ""]
    cur = None
    for m in msgs:
        if m["dt"].date() != cur:
            cur = m["dt"].date()
            tx += ["", "【%s %s】" % (cur.strftime("%Y-%m-%d"), WD[cur.weekday()]), ""]
        if m["kind"] == "sticker" and m["media"]:
            body = "[表情] %s" % m["media"]
        elif m["kind"] == "voice" and m["media"]:
            body = "[语音 %ss] %s" % (m["dur"], m["media"])
        else:
            body = m["text"]
        for i, seg in enumerate(str(body).split("\n")):
            prefix = "[%s] " % m["dt"].strftime("%H:%M") if i == 0 else "        "
            tx.append("%s%s：%s" % (prefix, m["who"], seg))
    with open(os.path.join(out_dir, "%s.txt" % fname), "w", encoding="utf-8") as f:
        f.write("\n".join(tx))

    # ---- RAW（一行一条，供二次处理；格式可被 --backfill 回填） ----
    raw = ["# %s（原始导出：一行一条，媒体为占位符）" % title, ""]
    for m in msgs:
        if m["kind"] == "sticker" and m["media"]:
            body = "[表情]"
        elif m["kind"] == "voice" and m["media"]:
            body = "[语音 %ss]" % m["dur"]
        else:
            body = m["text"]
        body = str(body).replace("\n", " ⏎ ").strip()
        sender = "me" if m["who"] == me_label else m["who"]
        raw.append("- [%s] %s: %s" % (m["dt"].strftime("%Y-%m-%d %H:%M"), sender, body))
    raw.append("")
    with open(os.path.join(out_dir, "%s_raw.md" % fname), "w", encoding="utf-8") as f:
        f.write("\n".join(raw))

    # ---- HTML ----
    rows, day_list, cur = [], [], None
    for m in msgs:
        if m["dt"].date() != cur:
            cur = m["dt"].date()
            day_list.append(cur)
            rows.append('<div class="day" id="d%s">%s %s</div>'
                        % (cur.strftime("%Y%m%d"), cur.strftime("%Y年%m月%d日"), WD[cur.weekday()]))
        who = esc_html(m["who"])
        ts = m["dt"].strftime("%H:%M")
        if m["kind"] == "sys":
            rows.append('<div class="sys"><span>%s</span><i>%s</i></div>' % (esc_html(m["text"]), ts))
            continue
        side = "me" if m["who"] == me_label else "peer"
        if m["kind"] == "sticker" and m["media"]:
            inner = ('<img class="sticker" loading="lazy" src="%s%s" alt="表情包" onclick="zoom(this)">'
                     % (media_prefix, m["media"]))
            search = "表情包 sticker"
        elif m["kind"] == "voice" and m["media"]:
            inner = ('<audio controls preload="none" src="%s%s"></audio><span class="dur">%ss</span>'
                     % (media_prefix, m["media"], m["dur"]))
            search = "语音 voice %ss" % m["dur"]
        else:
            search = m["text"]
            inner = esc_html(m["text"])
        rows.append('<div class="row %s" data-k="%s"><div class="meta">%s · %s</div>'
                    '<div class="bub" data-text="%s">%s</div></div>'
                    % (side, m["kind"], who, ts,
                       html.escape((m["who"] + " " + search).lower()), inner))

    nav = "".join('<a href="#d%s">%s</a>' % (d.strftime("%Y%m%d"), d.strftime("%m-%d"))
                  for d in day_list)
    kinds_html = "".join('<span>%s %d</span>' % (ICONS[k], kinds.get(k, 0))
                         for k in ("text", "sticker", "voice", "image", "app", "sys"))
    stats_html = (
        '<span>时间范围：<b>%s ~ %s</b></span>'
        '<span>消息总数：<b>%d</b></span>'
        '<span>%s <b>%d</b> · %s <b>%d</b></span>'
        '<span>跨度 <b>%d</b> 天 / 活跃 <b>%d</b> 天</span>'
        % (t0.strftime("%Y-%m-%d %H:%M"), t1.strftime("%Y-%m-%d %H:%M"), total,
           me_label, c_me, peer_label, total - c_me, span, active))

    page = (PAGE.replace("__TITLE__", html.escape(title))
                .replace("__H1__", html.escape(title))
                .replace("__STATS__", stats_html)
                .replace("__KINDS__", kinds_html)
                .replace("__ME__", html.escape(me_label))
                .replace("__PEER__", html.escape(peer_label))
                .replace("__NAV__", nav)
                .replace("__ROWS__", "".join(rows)))
    with open(os.path.join(out_dir, "%s.html" % fname), "w", encoding="utf-8") as f:
        f.write(page)

    return {"total": total, "me": c_me, "peer": total - c_me, "kinds": kinds,
            "stickers_shown": stickers_shown, "voices_shown": voices_shown,
            "file": os.path.join(out_dir, "%s.html" % fname),
            "artifacts": [os.path.join(out_dir, n) for n in file_names(fname)],
            "media_dir": os.path.join(out_dir, media_dirname(peer_label)),
            "range": [t0.strftime("%Y-%m-%d %H:%M"), t1.strftime("%Y-%m-%d %H:%M")]}


def file_names(fname):
    return ["%s.html" % fname, "%s.md" % fname, "%s.txt" % fname, "%s_raw.md" % fname]


def build(env, workdir, out_dir, peer_label, me_label, peer_username, tables,
          raw_path=None, media_root=None, force=False, log=print):
    hits = W.existing_targets(out_dir, peer_label)
    if hits and not force:
        raise W.WxError(
            "目标目录里已经有同名产物，没有覆盖：\n  " + "\n  ".join(hits) +
            "\n\n请先问用户：① 换个目录，② 允许覆盖（加 --force），还是 ③ 他自己先挪走。")

    media_root = media_root or os.path.join(out_dir, media_dirname(peer_label))
    sticker_man = {}
    voice_man = {}
    sp = os.path.join(media_root, "sticker_manifest.json")
    vp = os.path.join(media_root, "voice_manifest.json")
    if os.path.exists(sp):
        sticker_man = json.load(open(sp, encoding="utf-8"))
    if os.path.exists(vp):
        voice_man = {v["key"]: v for v in json.load(open(vp, encoding="utf-8"))
                     if isinstance(v, dict) and "file" in v and "key" in v}

    msgs = load_messages(env, workdir, tables, peer_username, peer_label, me_label,
                         sticker_man, voice_man, log=log)
    added = backfill_from_raw(msgs, raw_path, peer_label, me_label)
    if added:
        log("从外部原始导出补齐 WAL 未落库消息 %d 条" % added)

    title = "与「%s」的聊天记录" % peer_label
    stat = render(msgs, out_dir, title, peer_label, me_label,
                  use_media=os.path.isdir(media_root))
    log("导出：%s（%d 条；我 %d / %s %d；表情贴图 %d；语音播放器 %d）"
        % (stat["file"], stat["total"], stat["me"],
           peer_label, stat["peer"], stat["stickers_shown"], stat["voices_shown"]))
    return stat


def main():
    ap = argparse.ArgumentParser(description="重建会话并导出 HTML/MD/TXT/RAW")
    ap.add_argument("--name", required=True, help="联系人显示名（用于 HTML 里的对方称呼）")
    ap.add_argument("--peer-username", help="对方 username，省略则用 --name 去联系人表精确匹配")
    ap.add_argument("--me", help="『我』的显示名，默认取本机账号昵称")
    ap.add_argument("--out", required=True, help="导出目录（用户指定的位置）")
    ap.add_argument("--workdir")
    ap.add_argument("--db-dir")
    ap.add_argument("--backfill", help="外部的 raw.md，用于补齐 WAL 里最新几条")
    ap.add_argument("--force", action="store_true", help="允许覆盖同名产物")
    a = ap.parse_args()

    env = W.discover(a.db_dir)
    workdir = a.workdir or W.default_workdir(env)
    W.decrypt_all(env, workdir)
    peer = ({"label": a.name, "username": a.peer_username} if a.peer_username
            else W.pick_peer(env, workdir, a.name))
    me = a.me or W.self_profile(env, workdir)["label"]
    tables = W.find_msg_tables(env, workdir, peer["username"])
    W.ensure_dirs(a.out)
    build(env, workdir, a.out, peer["label"], me, peer["username"], tables,
          raw_path=a.backfill, force=a.force)


if __name__ == "__main__":
    try:
        main()
    except W.WxError as e:
        print("错误：%s" % e, file=sys.stderr)
        sys.exit(2)
