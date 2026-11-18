---
name: asset-store
description: Search and fetch game assets (images, sprites, VFX, audio, music, 3D models, Unity/Unreal packages, zip/rar/7z packs) from the user's NAS asset library. Use when the user asks to find, browse, preview or download art/audio/model assets, look for "a sword icon", "explosion effect", "背景音乐", "宝箱", or pull a file from an asset pack into a project. When the library has nothing fitting, also searches free asset sites (Poly Haven, ambientCG, OpenGameArt, itch.io, Sketchfab, Freesound...) and downloads from them into the library. Also starts library scans and picture-description jobs.
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
   - 每条有 `category`（分类路径）、`dim`（2D/3D/audio）、`engine`（unreal/unity，不知道时为空）、`caption`（画面描述）、`tags`（打过标签才有：`view` 视角、`style` 画风、`themes` 题材）、`files`、`size`、`hits`（命中的几个文件）、`links`。
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
   - `archive`：压缩包的文件数，以及 `engine`（unity 或 unreal）。
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

库里搜不到合适的素材时，可以搜免费素材网站。目前接了 Poly Haven、ambientCG、OpenGameArt、itch.io、Sketchfab、Freesound；以后可能还会加，以返回的 `sources` 为准。

1. **搜**：`python scripts/asset_store.py external treasure chest --limit 10`
   - 这些网站只认英文：中文需求先翻成英文关键词。返回里 `chinese: true` 表示你传的是中文，多半搜不到东西。
   - 默认同时搜所有网站，每个网站留前 `--limit` 条。`--source polyhaven` 只搜一个网站，翻页用 `--offset`，值取那个网站返回的 `next`。
   - 每个网站一段：`label`、`license`（网站整体的授权）、`total`、`error`（这个网站出错时的原因，不影响其他网站）、`items`。
   - 每条有 `source`、`id`、`title`、`kind`、`thumb`（网站上的预览图地址）、`page`（原页面）、`tags`；`in_library` 表示库里已经有这个素材（给出库里的路径），`downloaded: true` 表示已经下过，这两种情况都不要再下。
2. **看**：`python scripts/asset_store.py external-show polyhaven treasure_chest`
   - 有 `description`、`authors`、`license`（这个素材自己的授权）、`previews`、`note`（下载限制等说明），以及 `options`：可以下的规格，每个有 `key`、`label`、`size`（字节，有时没有）和 `ready`（false 表示这个规格现在下不了）。
   - **授权**：Poly Haven、ambientCG 是 CC0，可以随便用；其他网站每个素材的授权都不一样（如 CC-BY 要署名、CC-BY-NC 不可商用），一定要把 `license` 告诉用户。
3. **下到素材库**：`python scripts/asset_store.py external-get polyhaven treasure_chest --option fbx/1k`
   - 会让服务器下载，先和用户确认素材、规格和授权。`--option` 用 `external-show` 里 `ready` 为 true 的 `key`；同一个素材有多种规格时，没有特别要求就选小一点的（比如 1K 贴图）。
   - 文件下到素材库的 `_下载` 文件夹，进度和结果用 `downloads` 查看。下完自动扫描，扫完就能在库里搜到。
   - 409 说要 key 时：Sketchfab、Freesound 下载要先在网页「外部素材」页填 key，告诉用户去填，不要自己设置。

## 其他操作（会让服务器干活，用户要求时才做）

- `scan`：马上重新扫描 NAS，新加的素材要扫描后才搜得到。服务器一分钟内开始，用 `tasks` 看进度。
- `caption --root 根目录名 --prefix 文件夹`：让看图模型给这个文件夹里的图写描述。每张图要几秒，期间会占用用户那台机器上的模型，开始前要和用户确认范围。`--redo` 表示已有描述的也重做。
- `caption-stop`：停止描述任务。
- `scan-stop`：停止正在进行的扫描（还没开始的扫描请求会被撤销）。已扫的保留。
- `tasks`：查看扫描和描述任务在做什么、上次的结果。
- `download 链接 --title 名字`：让服务器把一个 http/https 下载链接下到素材库的 `_下载` 文件夹（用 aria2，大小不限），下完自动扫描。链接必须是文件本身的直链：贴网页地址会失败（下到网页时标成失败，不入库）。下载前要和用户确认；网盘分享链接下不了。
  - 409 表示下载服务没运行，或 NAS 对它是只读的（要在 `.env` 里设 `ASSET_MOVER_ACCESS=rw`）。
- `downloads`：看下载队列。`status` 取值：`queued` 排队、`active` 下载中（`progress` 里有 `done`/`total`/`speed` 字节数）、`done` 完成（`path` 是落地位置）、`duplicate` 完成但库里已有一模一样的文件（`dup_of`），`failed` 失败（`error` 是原因）、`cancelled` 已取消。

## 出错

脚本失败时退出码是 1，stderr 里是 `{"error": "HTTP 4xx: 原因"}`：

| 状态码 | 含义 |
|---|---|
| 404 | id 不存在，或者文件已从 NAS 删除，需要重新扫描 |
| 409 | 功能没启用（比如语义搜索、看图描述），或已有任务在跑；外部网站下载时表示这个网站要 key |
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
| 任务状态 | `GET /api/agent/tasks` |
| 扫描 | `POST /api/scan`，body 为 `{}` |
| 描述图片 | `POST /api/captions`，body 为 `{"root":"nas","prefix":"文件夹","redo":false}` |
| 停止描述 | `POST /api/captions/stop` |
| 停止扫描 | `POST /api/scan/stop` |
| 下载链接到素材库 | `POST /api/downloads`，body 为 `{"url":"https://…/a.zip","title":"可选"}` |
| 下载队列 | `GET /api/downloads` |
| 取消、重试下载 | `POST /api/downloads/{id}/cancel`、`POST /api/downloads/{id}/retry` |
| 搜外部网站 | `GET /api/external/search?q=&source=&offset=0`（source 为空：所有网站） |
| 外部素材详情 | `GET /api/external/{source}/{id}` |
| 下载外部素材到素材库 | `POST /api/external/{source}/{id}/download`，body 为 `{"option":"fbx/1k"}`（可加 `"root"`） |

完整的接口文档在服务的 `/docs` 页面（FastAPI 自动生成）。
