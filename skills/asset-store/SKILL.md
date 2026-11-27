---
name: asset-store
description: Search and fetch game assets (images, sprites, VFX, audio, music, 3D models, Unity/Unreal packages, zip/rar/7z packs) from the user's NAS asset library. Use when the user asks to find, browse, preview or download art/audio/model assets, look for "a sword icon", "explosion effect", "背景音乐", "宝箱", or pull a file from an asset pack into a project. When the library has nothing fitting, also searches free asset sites (Poly Haven, ambientCG, OpenGameArt, itch.io, Sketchfab, Freesound, Poly Pizza, Openverse...) and downloads from them into the library. Also uploads a local file or folder (a pack) into the library. Also starts library scans and picture-description jobs, organizes the library (classify packs, move them, unpack, delete), changes the server's model settings and updates the server.
---

# 素材库（asset-store）

用户的 NAS 上有一个素材库网页服务：图片、序列帧、特效、音频、fbx、Unity/UE 包、zip/rar/7z 素材包都建了索引，可以按文件名、路径、包内文件名、画面描述来搜，也能按意思搜。用 `scripts/asset_store.py` 调用它，这个脚本只需要 Python 3.10+ 标准库，输出 JSON。

## 准备

服务地址放在环境变量 `ASSET_STORE_URL` 里，例如 `http://192.168.1.50:8000`；也可以每次加 `--url`。先确认能连上：

```bash
python scripts/asset_store.py tasks
```

连不上时，提醒用户检查地址，或者看服务（`docker compose ps`）有没有在运行。不要去猜地址。

## 典型流程

0. **先按包找**（找"做某种游戏用的素材"时从这里开始）：`python scripts/asset_store.py packs 蒸汽朋克 --limit 20`
   - 一个素材包（文件夹，或压缩包加它的预览图）只占一条，不会被包里几百个文件刷屏。
   - 排序：包名里有全部搜索词的在前（`match: name`），其次是包里文件命中的（`match: inside`，`hit_count` 多的在前），最后是按意思找到的（`match: meaning`）。不带搜索词时按路径列出全部包（`match: all`）。
   - 每条有 `category`（分类路径）、`dim`（2D/3D/audio）、`engine`（unreal/unity/daz，不知道时为空；daz 是 DAZ Studio 的产品包，下载后要用 DAZ 的安装工具装进内容目录才能用）、`caption`（画面描述）、`tags`（打过标签才有：`view` 视角、`style` 画风、`themes` 题材）、`files`、`size`、`hits`（命中的几个文件）、`links`。
   - `--category 02_3D/Environments` 只看某个分类；分类有 `01_2D/*`、`02_3D/*`、`03_Audio/*`、`04_Tools/*`、`05_Reference`。
   - `--view` 只留这个视角的包：`俯视 等距 横版 第一人称 第三人称 不限`；`--style` 只留这个画风的包：`写实 卡通 像素 低多边形 风格化 手绘 其他`。
   - 标签只打了一小部分包，没打标签的包会被 `--view`/`--style` 直接筛掉。先不带这两个选项搜一次；结果太多、又只要 2D 素材时再用它们缩小范围，找 3D 素材时不要用。3D 场景和道具大多什么视角都能用（标签是「不限」），不要因为名字里没有"俯视"就排除。拿不准时用 `thumb` 看预览图。
   - 想看文件夹包里有什么，用 `search 包名的一段 --mode text`。
1. **搜文件**：`python scripts/asset_store.py search 火焰 爆炸 --limit 10`
   - 多个词之间是"并且"的关系，中英文都可以，例如 `sword icon`、`剑`。
   - 用 `--kind` 限定类型，取值有 `image audio video model archive unreal other`。
   - `--mode auto`（默认）：语义搜索可用时按意思排序，不可用时自动退回关键词搜索。返回里的 `mode` 是实际用了哪种，`note` 说明为什么退回。
   - `--mode text` 是精确的子串匹配，搜文件名片段（`walk_03`、`PM895`）时用它。
   - 搜不到时换同义词，或换中英文再搜，或换更短的词。`total` 是总数，用 `--offset` 翻页。
2. **看**：每条结果有这些字段：
   - `path`：形如 `根目录:/相对路径`。
   - `caption`：看图模型写的画面描述，有就优先参考。
   - `frames`：大于 0 表示这是一组序列帧，按一张卡片显示。
   - `archive`：压缩包的文件数，以及 `engine`（unity、unreal 或 daz）和 `encrypted`（要密码才能解开的文件数；空表示还没数过）。
   - 库里哪些压缩包要密码：`encrypted [--limit N --offset N]`，列出有加密文件的包（`encrypted` 是加密文件数），以及连文件名都加密、列不出来的 7z（`names_locked: true`）。
   - `matches_inside`：包里哪些文件命中了搜索词。
   - 要亲眼看图时，用 `thumb ID --out <临时目录>` 存下缩略图（webp），再用读图工具打开。缩略图只有 `links.thumb` 存在时才有。
3. **细看压缩包**：
   - `show ID` 返回前 200 个包内文件。
   - `entries ID --filter 关键词` 在包里筛选。
4. **取文件**：
   - 整个文件：`get ID --out <目标目录>`。
   - 包里的单个文件：`get ID --entry ENTRY_ID --out <目标目录>`。只有带 `download` 字段的条目能单独取（zip/rar/7z，256MB 以内）；`.unitypackage` 只能整包下载。
   - 返回的 `saved` 是本地完整路径。往用户的项目里放文件之前，先确认目标目录。
   - 已有同名文件时不会覆盖，会存成 `name (1).ext`，以 `saved` 为准。
   - 单个文件取不出来（422，常见于文件名是 GBK 编码或用反斜杠的旧压缩包）时，改为整包下载，再在本地解压。

## 库里没有：去外部网站找

库里搜不到合适的素材时，可以搜免费素材网站。以返回的 `sources` 为准，目前有：

| source | 有什么 | 说明 |
|---|---|---|
| `polyhaven` | HDRI、PBR 贴图、模型 | CC0 |
| `ambientcg` | PBR 材质、HDRI、模型 | CC0，每个规格一个 zip |
| `opengameart` | 2D、像素、3D、音乐、音效 | 授权各异；它要求每 10 秒最多请求一次，搜一次常要等 10 秒左右 |
| `itch` | 2D、像素、UI 等免费素材包 | 只按英文标签找（`pixel art` 当成标签 `pixel-art`）；**不能下载**，只能把 `page` 给用户去手动下 |
| `sketchfab` | 3D 模型（最多） | 授权各异；下载要 key |
| `freesound` | 音效 | 授权各异；搜索就要 key；只能下试听版（MP3/OGG），原始文件要用户手动下 |
| `polypizza` | 低模（low poly）模型，GLB | CC0 或 CC-BY；搜索就要 key |
| `openverse_images` | CC 图片（Flickr、Wikimedia 等） | 授权各异；**每天只能请求 200 次**，必须带关键词 |
| `openverse_audio` | CC 音乐、音效 | 同上 |

1. **搜**：`python scripts/asset_store.py external treasure chest --source polypizza --limit 10`
   - 这些网站只认英文：中文需求先翻成英文关键词。返回里 `chinese: true` 表示你传的是中文，多半搜不到东西。
   - **按要找的东西挑网站，用 `--source` 一次搜一个**，不要每次都搜全部：模型找 `polypizza`、`sketchfab`、`polyhaven`；贴图、材质、HDRI 找 `polyhaven`、`ambientcg`；音效找 `freesound`、`openverse_audio`；2D 和像素素材找 `opengameart`、`itch`；参考图找 `openverse_images`。不带 `--source` 会同时搜所有网站（慢，还会用掉 Openverse 的每日额度）。
   - 每个网站留前 `--limit` 条。翻页用 `--offset`，值取那个网站返回的 `next`；`more: true` 表示还有下一页。
   - 每个网站一段：`label`、`license`（网站整体的授权）、`total`（有的网站不给，就没有这个字段，看 `more`）、`error`（这个网站出错的原因，例如要 key、额度用完，不影响其他网站）、`notice`（结果不全时的说明，例如 Poly Pizza 有的词只列出了 CC0 模型）、`items`。
   - 每条有 `source`、`id`、`title`、`kind`、`thumb`（网站上的预览图地址）、`page`（原页面）、`tags`；`in_library` 表示库里已经有这个素材（给出库里的路径），`downloaded: true` 表示已经下过，这两种情况都不要再下。
2. **看**：`python scripts/asset_store.py external-show polyhaven treasure_chest`
   - 有 `description`、`authors`、`license`（这个素材自己的授权）、`attribution`（要署名的素材给出的署名文字，CC0 的没有）、`previews`、`note`（下载限制等说明），以及 `options`：可以下的规格，每个有 `key`、`label`、`size`（字节，有时没有）和 `ready`（false 表示这个规格现在下不了）。`options` 为空时（如 itch.io）只能让用户去 `page` 手动下。
   - **授权**：Poly Haven、ambientCG 是 CC0，可以随便用；其他网站每个素材的授权都不一样（如 CC-BY 要署名、带 NC 的不可商用），一定要把 `license` 告诉用户，有 `attribution` 时一并给出，提醒用户放进游戏的鸣谢里。
3. **下到素材库**：`python scripts/asset_store.py external-get polyhaven treasure_chest --option fbx/1k`
   - 会让服务器下载，先和用户确认素材、规格和授权。`--option` 用 `external-show` 里 `ready` 为 true 的 `key`；同一个素材有多种规格时，没有特别要求就选小一点的（比如 1K 贴图）。
   - 文件下到素材库的 `_下载` 文件夹，进度和结果用 `downloads` 查看。下完自动扫描，扫完就能在库里搜到。
   - 409 或 `error` 说要 key 时：Sketchfab、Freesound、Poly Pizza 要用户自己账号的 key，告诉用户去网页「外部素材」页右上角「网站账号」里填，不要自己设置。`external-keys` 可以看哪些已经填了。

## 其他操作（会让服务器干活，用户要求时才做）

- `scan`：马上重新扫描 NAS，新加的素材要扫描后才搜得到。服务器一分钟内开始，用 `tasks` 看进度。
- `caption --root 根目录名 --prefix 文件夹`：让看图模型给这个文件夹里的图写描述。每张图要几秒，期间会占用用户那台机器上的模型，开始前要和用户确认范围。`--redo` 表示已有描述的也重做。
- `caption-stop`：停止描述任务。
- `scan-stop`：停止正在进行的扫描（还没开始的扫描请求会被撤销）。已扫的保留。
- `tasks`：查看扫描和描述任务在做什么、上次的结果。
- `download 链接 --title 名字`：让服务器把一个 http/https 下载链接下到素材库的 `_下载` 文件夹（用 aria2，大小不限），下完自动扫描。链接必须是文件本身的直链：贴网页地址会失败（下到网页时标成失败，不入库）。下载前要和用户确认；网盘分享链接下不了。
  - 409 表示下载服务没运行，或 NAS 对它是只读的（要在 `.env` 里设 `ASSET_MOVER_ACCESS=rw`）。
- `upload 本地路径 [--name 包名] [--root 根目录名]`：把用户电脑上的一个文件或整个文件夹传到素材库的 `_下载` 文件夹（文件夹整体算一个包，保留子文件夹；以 `.` 开头的隐藏文件不传，结果的 `skipped` 列出它们）。传完自动扫描（单个文件还会查库里有没有一模一样的）。**这会往 NAS 写文件：每次都要用户明确同意**，先告诉用户传什么、多大。中途失败会自动取消，已传的部分不留；要再传就重新跑一次（没有续传）。
  - 409 表示 NAS 对网页服务是只读的（要在 `.env` 里设 `ASSET_MOVER_ACCESS=rw`）或空间不够；整理在移动文件时会自动等它结束。
- `download-cancel ID`：取消排队中、下载中或上传中的条目；`download-retry ID`：重试失败或取消了的下载（上传的不能重试，要重新上传）。用户要求时才做。
- `downloads`：看下载队列（上传也在里面，`source` 为 `upload`）。`status` 取值：`queued` 排队、`active` 下载中（`progress` 里有 `done`/`total`/`speed` 字节数）、`uploading` 上传中（`progress.done` 是已收到的完整文件的字节数）、`done` 完成（`path` 是落地位置）、`duplicate` 完成但库里已有一模一样的文件（`dup_of`），`failed` 失败（`error` 是原因）、`cancelled` 已取消。

## 整理素材库、设置、更新（用户要求时才做）

这些命令和网页上「整理」页、右上角「设置」、「更新」做的是同一件事。分三档：

**只读，随时可以用**：`stats`（各类文件数）、`organize`（分析进度、上次报告、`categories` 可用的分类、`counts` 各状态的包数）、`units`、`unit-pictures`、`splits`、`moves`（文件任务：正在做的、历史、`mount` 里 NAS 是否可写、`unsettled` 中断未收尾的次数）、`move-plan`、`trash`、`locked`、`tags`、`settings`、`update`。

**只改整理建议、不动文件**：照用户说的做，做完告诉用户改了什么。
- 流程：`organize-start --root nas [--prefix 文件夹]` 让看图模型分析（找重复的包，给每个包提议分类和名字；很慢、占用模型，先和用户确认范围）→ `units --root nas --show proposed` 看提议（每条有 `path`、`category`、`name`、`target` 目标路径、`reason`、`status`、`dup_of`）→ 审核 → 接受（接受就马上移动，见下）。
- `decide reject|reset|redo --root nas 路径…`：拒绝、撤回决定、让模型重新分析。

**接受提议 = 马上移动文件**（mover 可写时，服务器一接受就开始移动；正在移动时接受的，移完这批接着移）：和下面一样，每次都要用户明确同意，先告诉用户哪些包移到哪里。
- `decide accept --root nas 路径…`：接受提议。
- `accept-all --root nas --category 01_2D/UI`：接受列表里这一类的全部提议。先用 `units` 告诉用户有多少个。
- `retarget --root nas 路径 01_2D/UI/新名字`：改目标并接受（分类用 `organize` 返回的 `categories`）。目标写 `_待删除/名字` 就是马上移进 `_待删除`。
- 返回里 `moving` 为 true 表示已开始移动；进度看 `moves`。
- `keep`（重复的包改为保留这一份）、`split`（文件夹里每个子文件夹各算一个包）、`join`（整个文件夹算一个包）、`unsplit`（撤销拆开）。
- `tag --root nas 包路径…` 让模型给包打视角、画风、题材标签（也占模型）。

**动 NAS 上的文件、改服务器：每次都要用户明确同意**
- `move apply --root nas`：把已接受但还没移动的包（比如上次移动出错、被停止，或当时 NAS 只读）移到目标位置。`move-plan --root nas` 看有哪些和 `problems`（移不了的原因）。
- `move undo --root nas --journal 20261118-101500.jsonl`：撤销某一次移动（名字在 `moves` 的 `history` 里）。
- `move recover --root nas`：`unsettled` 大于 0 时，把上次中断的移动收尾。
- `move unpack --root nas --password 密码 [--limit N] [路径…]`：把带密码的压缩包解成文件夹（`locked` 列出哪些可以解），原压缩包放进 `_待删除`。
- `move verify --root nas 路径…`：只读，检查压缩包有没有损坏。用户问某个包坏没坏时可以直接用。
- `move repair --root nas 副本1 副本2…`：用几份各坏一处的副本拼出一份完好的。`move lift --root nas 路径…`：外层压缩包里只套着另一个压缩包时，拆掉外层；套着好几个压缩包时（合集，如一个 rar 里有十几个 DAZ 产品的 zip 和预览图），把它们和图片解到一个和外层同名的文件夹里，每对同名的 zip 和图片各算一个包；外层旁边的同名封面跟着放进文件夹当 `Preview`（合集里每个产品已有图片时移进 `_待删除`）。里面还有别的素材时不动。路径给的是一张图片 `X.jpg`、旁边有同名文件夹 `X` 时，按同样的规则把这张封面收进文件夹。外层带密码时：不给 `--password` 就先试不用密码，再依次试服务器上存的密码（见下面的 `settings-set passwords`）；给了 `--password` 就只用它。
- **`move purge --root nas`：永久删除 `_待删除` 文件夹里的全部文件，不进 NAS 回收站，删了找不回来。** 先跑 `trash --root nas`，把文件数和总大小告诉用户；只有用户明确说“永久删除”才执行，绝不自己决定删除。
- `move-stop`：做完手上这个包后停下。文件任务进行时，扫描和分析会等它做完。
- `settings-set caption '{"model": "qwen3-vl"}'`、`settings-set embedding '{"enabled": true}'`：改看图模型、语义搜索的设置，只传要改的字段（看图模型：`base_url`、`model`、`api_key`、`workers`、`retries`、`retry_wait`、`max_failures`；语义搜索：`enabled`、`base_url`、`model`、`api_key`、`image_format`、`batch_size`）。key 只能写不能读；改了接口地址却没给新 key 时，旧 key 会被清掉。改完用 `settings-test caption`（描述一张示例图）或 `settings-test embedding` 试一下。改前告诉用户要改成什么。
- `settings-set passwords '{"add": ["密码"]}'`：存一个拆外层时自动尝试的压缩包密码（`{"passwords": [...]}` 整个替换，`[]` 清空）。存在服务器的数据目录里，只能写不能读：`settings` 只显示有几个。密码只在用户给出时才存。
- `update-run check` 查有没有新版本（`update` 里的 `behind`、`pending`）；`update-run update` 安装新版本并重启所有服务，约一分钟。重启会打断正在进行的扫描和分析（下载会续传），先用 `tasks` 看有没有扫描在跑，告诉用户再更新；更新完扫描不会自动接着做，需要时再 `scan`。`update-stop` 在重启前停下。

## 出错

脚本失败时退出码是 1，stderr 里是 `{"error": "HTTP 4xx: 原因"}`：

| 状态码 | 含义 |
|---|---|
| 404 | id 不存在，或者文件已从 NAS 删除，需要重新扫描 |
| 409 | 功能没启用（比如语义搜索、看图描述），或已有任务在跑，或下载服务没运行；外部网站下载时表示这个网站要 key |
| 413 | 包里的文件太大，只能整包下载 |
| 415 | 这类压缩包不能单独取文件，或者没有缩略图 |
| 422 | 包里的这个文件取不出来，改为整包下载 |
| 429 | 语义搜索正忙（只有 `--mode semantic` 时会出现），稍后再试或改用 `--mode auto` |
| 502 | 外部网站出错或接口改了，稍后再试或换个网站 |
| 503 | 带 `Retry-After` 时表示服务器忙，稍等再试；说 bsdtar 没装时，只能整包下载 |

网络超时、下载中断这类问题也会以同样的 `{"error": ...}` 格式报告，出错时不会留下下载了一半的文件。

## 直接用 HTTP（没有 Python 时）

GET 请求不需要认证。POST 请求要带请求头 `X-Requested-With: anything` 和 JSON body。

| 用途 | 请求 |
|---|---|
| 按包搜索 | `GET /api/agent/packs?q=&category=&mode=auto&limit=20&offset=0`（q 为空：按路径列出全部包，可配 category 逛某一类；view=俯视、style=像素 等只留打过这个标签的包） |
| 搜索文件 | `GET /api/agent/search?q=&kind=&mode=auto&limit=20&offset=0` |
| 详情 | `GET /api/agent/files/{id}` |
| 包内文件 | `GET /api/agent/files/{id}/entries?q=&limit=&offset=` |
| 取包内单个文件 | `GET /api/agent/files/{id}/entries/{entry_id}/download` |
| 下载整个文件 | `GET /api/files/{id}/download` |
| 缩略图 | `GET /api/files/{id}/thumb` |
| 包封面 | 包的 `links.thumb` 照原样请求：可能是 `GET /api/files/{id}/thumb`，没有图片的 UE 包可能是 `GET /api/fab-covers/{name}`（Fab 商城上同名商品的宣传图）或包里 .uasset 缩略图拼成的九宫格；`GET /api/fab-covers/status` 看上次扫描后 Fab 查了几个、配上几个、出了什么错 |
| 任务状态 | `GET /api/agent/tasks` |
| 要密码的压缩包 | `GET /api/agent/encrypted?limit=50&offset=0` |
| 扫描 | `POST /api/scan`，body 为 `{}` |
| 描述图片 | `POST /api/captions`，body 为 `{"root":"nas","prefix":"文件夹","redo":false}` |
| 停止描述 | `POST /api/captions/stop` |
| 停止扫描 | `POST /api/scan/stop` |
| 下载链接到素材库 | `POST /api/downloads`，body 为 `{"url":"https://…/a.zip","title":"可选"}` |
| 下载队列 | `GET /api/downloads` |
| 上传文件或文件夹 | `POST /api/uploads`，body 为 `{"files":[{"path":"a.zip","size":123}]}`，文件夹为 `{"folder":true,"name":"包名","files":[{"path":"sub/a.png","size":1},…]}`；再逐个 `PUT /api/uploads/{id}/files/{序号}`（请求体是文件原始字节），最后 `POST /api/uploads/{id}/finish` |
| 取消、重试下载 | `POST /api/downloads/{id}/cancel`、`POST /api/downloads/{id}/retry` |
| 搜外部网站 | `GET /api/external/search?q=&source=&offset=0`（source 为空：所有网站） |
| 外部素材详情 | `GET /api/external/{source}/{id}` |
| 下载外部素材到素材库 | `POST /api/external/{source}/{id}/download`，body 为 `{"option":"fbx/1k"}`（可加 `"root"`） |
| 哪些网站的 key 已填 | `GET /api/external/keys` |
| 各类文件数 | `GET /api/stats` |
| 整理分析 | `GET /api/organize`；开始 `POST /api/organize`，body 为 `{"root":"nas","prefix":"","redo":false}`；停止 `POST /api/organize/stop` |
| 整理建议 | `GET /api/organize/units?root=&show=proposed&category=&q=&limit=&offset=`；包的图片 `GET /api/organize/units/pictures?root=&path=` |
| 审核建议 | `POST /api/organize/units/decide` `{"root","paths":[…],"action":"accept"}`、`/units/accept-all` `{"root","show","category","q"}`、`/units/target` `{"root","path","target"}`、`/units/keep`、`/api/organize/split`、`/join`、`/unsplit` `{"root","path"}` |
| 文件任务 | `GET /api/organize/moves`；预览 `GET /api/organize/moves/plan?root=`、`/trash?root=`、`/locked?root=`；开始 `POST /api/organize/moves` `{"action":"apply","root":"nas"}`（undo 加 `journal`，unpack 加 `password`，verify/repair/lift 加 `paths`）；停止 `POST /api/organize/moves/stop` |
| 打标签 | `GET /api/organize/tags`；`POST /api/organize/tags` `{"root","paths":[…]}`；`POST /api/organize/tags/stop` |
| 设置 | `GET /api/settings/caption`、`/api/settings/embedding`；修改 `PUT` 同一地址，body 为要改的字段；测试 `POST …/test` |
| 更新 | `GET /api/update`；`POST /api/update` `{"action":"check"}` 或 `{"action":"update"}`；`POST /api/update/stop` |

完整的接口文档在服务的 `/docs` 页面（FastAPI 自动生成）。
