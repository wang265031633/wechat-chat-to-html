---
name: wechat-chat-to-html
description: 把本机微信里某个联系人（或群聊）的聊天记录导出成可浏览的 HTML，内嵌真实表情包图片与语音播放器，同时产出 Markdown、纯文本与原始日志版本。适用于微信 4.x（Windows），需要本机已用 wechat-cli 提取过数据库密钥。当用户说"导出我和某人的聊天记录""把聊天记录整理成网页/HTML""查看并保存某人的聊天""聊天记录里的表情包和语音怎么一起导出"时，务必使用本技能，而不是只调 wechat-cli 的 export（后者在 4.x 上只能输出 [表情]/[语音] 占位符）。执行前必须先向用户确认存放文件夹。
agent_created: true
---

# 微信聊天记录 → HTML（含表情包与语音）

## 这个技能解决什么

`wechat-cli export` 在微信 4.x 上只能给出 `[表情]`、`[语音]`、`[图片]` 占位符——因为
4.x 把语音以 SILK BLOB 内联进 `media_0.db`，把表情包以「AES-128-CBC、IV=Key」
的私有格式散在 `cache/Emoticon`、`persist` 与商店包里，还额外套了一层私有 `wxgf`
（HEVC）容器。本技能把这些全部还原，再拼回一条时间线，输出一份能直接看的 HTML。

## 第 0 步（不可跳过）：先问用户存到哪个文件夹

**不要自己假设位置**，也不要默认写到当前工作目录。动手之前明确问用户，然后停下等他回答。

用 `AskUserQuestion` 提问，选项按用户实际情况给（示例）：

| 选项 | 说明 |
| --- | --- |
| 桌面 | `C:\Users\<用户>\Desktop\<对方>_聊天记录` |
| 文档 | `C:\Users\<用户>\Documents\<对方>_聊天记录` |
| 当前工作区 | `<workspace>\<对方>_聊天记录` |
| （自由填写） | 用户直接给绝对路径 |

提问时把这些一起讲清楚，避免用户误判：

- 最终会在这个文件夹里放 **4 个文件 + 1 个文件夹**（见下表），体积取决于聊天里有多少
  语音和表情，可能几十到几百 MB。
- 内容是**私密聊天**，提醒用户挑一个放心的位置（不要放到会被同步/共享的目录）。
- 中间产物（解密后的数据库）放在系统临时目录，**不会**进这个文件夹。

拿到路径后再执行。如果用户说"你看着办"，就建议一个明确路径并再确认一次再动手。

## 交付物（固定 5 项）

| 条目 | 内容 |
| --- | --- |
| `<对方>_聊天记录.html` | 主交付物：表情贴图 + 语音播放器 + 全文搜索 + 类型筛选 + 日期跳转 |
| `<对方>_聊天记录.md` | 带媒体相对链接的 Markdown |
| `<对方>_聊天记录.txt` | 纯文字，便于全文检索 |
| `<对方>_聊天记录_raw.md` | 一行一条的原始日志（媒体为 `[表情]` / `[语音 Ns]` 占位），供二次处理 |
| `<对方>_聊天记录_媒体/` | `表情/`（png/gif/jpg/webp）+ `语音/`（wav）+ 两个 manifest |

交付文件夹里**只应有这 5 项**。解密后的数据库与统计落在
`%TEMP%\wxchat_work\<wxid>\`，不要往交付目录里塞中间产物或日志。

> 媒体文件夹与 HTML 靠**相对路径**绑定：要挪就整份一起挪，别只挪 HTML；
> 挪完位置后重新跑一次自检（第 3 步）。

## 前置条件（不满足就先解决，不要硬跑）

1. **本机装好 wechat-cli 且已提取过密钥**：`~/.wechat-cli/config.json` 有 `db_dir`，
   `~/.wechat-cli/all_keys.json` 有各库 `enc_key`。缺一个就先跑 wechat-cli 的初始化/密钥提取。
2. **微信 4.1.x Windows，账号已在本机登录过**（数据在 `xwechat_files/<wxid>_xxxx/db_storage`）。
3. **解释器**：必须用装了依赖的那个 Python（下称 `<PY>`）。首次使用按
   `references/环境与依赖.md` 建 venv 装 `wechat-cli pycryptodome pysilk av Pillow zstandard`。
4. 想还原**表情包**时微信得处于**运行状态**（表情密钥只在进程内存里）；还原**文字/语音**则不需要。

## 标准流程

```bash
# 0) 先问用户要存到哪里；拿到 <存放文件夹> 之后才继续

# 1) 确认是哪个联系人（同名会有多个，★ 是精确匹配；这一步不写文件）
<PY> scripts/wxchat_export.py list --name 张三

# 2) 一键全流程，产物直接落到用户指定的文件夹
<PY> scripts/wxchat_export.py all --name 张三 --out "<存放文件夹>"

# 3) 自检：4 个文件是否齐、媒体引用是否都能落到磁盘
<PY> scripts/verify_export.py "<存放文件夹>"

# 4) 让用户真正看到图、听到声音（在存放文件夹里执行）
<PY> -m http.server 8770 --bind 127.0.0.1
```

大聊（几万条）或中途失败时按步拆开跑，每步幂等、复用中间产物，`--out` 都传同一个文件夹：

```bash
<PY> scripts/wxchat_export.py db        --name 张三 --out "<存放文件夹>"   # 只解密数据库
<PY> scripts/wxchat_export.py emojikey                                  # 只扫内存找表情密钥
<PY> scripts/wxchat_export.py voice     --name 张三 --out "<存放文件夹>"   # 只提语音
<PY> scripts/wxchat_export.py stickers  --name 张三 --out "<存放文件夹>"   # 只提表情包
<PY> scripts/wxchat_export.py build     --name 张三 --out "<存放文件夹>"   # 只重出 4 个文件
```

常用参数：

| 参数 | 说明 |
| --- | --- |
| `--name` | 联系人显示名，在备注/昵称/微信号/别名里精确匹配，不唯一时报错并列出候选 |
| `--peer-username` | 直接指定对方 username（群聊是 `xxx@chatroom`），跳过名称匹配 |
| `--me` | 「我」在导出里的显示名，默认取本机昵称；想显示成「我」就传 `--me 我` |
| `--out` | **用户指定的存放文件夹（必填，不许默认）** |
| `--workdir` | 解密库目录，默认 `%TEMP%\wxchat_work\<wxid>`，不进交付目录 |
| `--db-dir` | db_storage 路径，默认读 wechat-cli 配置 |
| `--backfill` | 外部 raw.md，用来补齐 WAL 里还没落库的最新几条（可选） |
| `--force` | 目标目录已有同名产物时允许覆盖；**只有用户明确同意覆盖后才能加** |
| `--no-voice` / `--no-stickers` | 跳过对应步骤 |

## 覆盖保护（不要绕过）

`all` / `build` 在动手前会检查目标文件夹里是否已有同名产物。若有且没给 `--force`，
脚本会**直接中止、不覆盖任何文件**，并列出冲突项。此时必须回去问用户：
① 换个文件夹，② 同意覆盖，还是 ③ 他自己先挪走。**不要为了"跑通"擅自加 `--force`。**

## 实现要点（改代码前必读）

- **产物命名只有一处定义**：`wxcommon.base_name / file_names / media_dirname / deliverables`，
  导出、自检、覆盖检查都从这里取，改名不会漏。
- **会话表会跨库分片**：同一个会话的 `Msg_<md5(对方username)>` 表**可能同时存在于
  `message_0.db` 和 `message_1.db`**（老消息在旧库、新消息写进新库）。
  必须**并集**两个库再按 `server_id`（无则 `库+local_id`）去重；只取一个库会丢消息。
- **`real_sender_id` 必须逐库解析**：每个消息库有**自己的** `Name2Id` 表，
  `real_sender_id` 是那里的 rowid。同一个号在不同库里含义不同（实测 message_0 里
  `3`=我、message_1 里 `1`=我），**绝不能硬编码 1=我 / 2=对方**。
  用 `wxcommon.sender_labels()`，它拿 `user_name` 跟本机 wxid / 对方 wxid 比对。
- **语音的稳定键是 `(create_time, local_id)`**：local_id 跨库会重号，语音清单里用
  `"<ts>_<lid>"` 做键，导出与渲染两侧共用。
- **表情密钥**由 `find_emoji_key.py` 从 `Weixin.exe` 内存里扫出来（`seed` 是 6~14 位十进制数），
  缓存到 `~/.wechat-cli/emoji_key.json`；每次读取都会做 oracle 校验，失效会自动重扫。
- 详细格式说明（SILK / wxgf / AES IV=Key / 商店包切片 / 图片 .dat）见
  `references/微信4x格式笔记.md`。

## 已知局限（如实告知用户，不要假装完整）

- **图片（`local_type`=3）仍是 `[图片]` 占位**：4.x 的 `.dat` 图片用另一把 AES 密钥，
  且该密钥只在"看过图之后"才进内存。要解需先在微信里把图片点开浏览一遍再扫内存。
- **群聊成员显示为 wxid**：只有对方那一侧的 `Name2Id` 能映射到昵称，其余成员未做昵称补全。
- 极少数表情解不动（HEVC 解码报错）会保留原始 `.wxgf`，HTML 里显示为占位符。
- 只有本机库里有的消息才会导出；手机端独有的历史记录需先在手机上用微信「迁移」到电脑。

## 故障排查

| 现象 | 原因 / 处理 |
| --- | --- |
| `必须传 --out 指定存放位置` | 还没问用户。回到第 0 步，别自己编一个目录 |
| `该目录已存在本次要产出的文件` | 覆盖保护生效。问用户：换目录 / 同意覆盖(`--force`) / 他自己挪走 |
| `无法导入 wechat_cli.core.crypto` | 用错解释器了。换 `references/环境与依赖.md` 里的 venv `<PY>` |
| `all_keys.json 里没有 xxx.db 的密钥` | 该库还没被 wechat-cli 解过，重跑 wechat-cli 的密钥提取 |
| `内存里没找到表情密钥` | 微信没开 / 没打开过含表情的聊天 / 版本不是 4.1.x。先开微信翻几张表情再跑 `emojikey` |
| `匹配到多个联系人` | 按输出的候选加 `--peer-username wxid_xxx` 或写全昵称 |
| 打开 HTML 看不到表情、点语音没反应 | 没用静态服务（相对路径）。在存放文件夹里跑 `python -m http.server` |
| 消息条数比预期少 | 检查是否只读到了一个库；`all` 的日志会打印 `message_0.db(…) + message_1.db(…)` |
| `PermissionError: avcodec_send_packet()` | 单个动图解码异常，已兜底为保留 `.wxgf`，不影响整体 |

## 隐私与文件安全

- 全流程只读本机微信数据、只往 `--out` 指定目录和临时目录写文件，**不联网、不上传**。
- 导出的是私人聊天。不要把内容发到任何外部服务，也不要擅自复制到共享/同步目录。
- 需要挪动或清理已有导出时，**先征得用户同意**；清理中间产物只需清 `%TEMP%\wxchat_work`，
  不要碰用户的其它文件。
- **本技能本身不含任何个人数据**：`scripts/`、`references/` 里的 wxid、表名、消息条数、
  路径、昵称都是脱敏占位符（如示例名统一用 `张三`）。要发布到公开仓库时，仓库里只应有
  `SKILL.md` / `scripts/` / `references/` / `README.md` / `.gitignore` 这类代码与文档。
- **绝不能提交**：导出产物（`*_聊天记录*`、`*_媒体/`）、密钥（`all_keys.json`、
  `emoji_key.json`、`*_keys.json`）、解密后的数据库（`*.db` / `*-wal` / `*-shm`）、
  中间目录（`%TEMP%\wxchat_work`）。`.gitignore` 已对上述做拦截，但**不保证万无一失**——
  推送前务必自己 `git status` 核一遍，确认没有私密内容混进暂存区。
