---
name: asset-store
description: Search and fetch game assets (images, sprites, VFX, audio, music, 3D models, Unity/Unreal packages, zip/rar/7z packs) from the user's NAS asset library. Use when the user asks to find, browse, preview or download art/audio/model assets, look for "a sword icon", "explosion effect", "背景音乐", "宝箱", or pull a file from an asset pack into a project. Also starts library scans and picture-description jobs.
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

1. **搜**：`python scripts/asset_store.py search 火焰 爆炸 --limit 10`
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

## 其他操作（会让服务器干活，用户要求时才做）

- `scan`：马上重新扫描 NAS，新加的素材要扫描后才搜得到。服务器一分钟内开始，用 `tasks` 看进度。
- `caption --root 根目录名 --prefix 文件夹`：让看图模型给这个文件夹里的图写描述。每张图要几秒，期间会占用用户那台机器上的模型，开始前要和用户确认范围。`--redo` 表示已有描述的也重做。
- `caption-stop`：停止描述任务。
- `tasks`：查看扫描和描述任务在做什么、上次的结果。

## 出错

脚本失败时退出码是 1，stderr 里是 `{"error": "HTTP 4xx: 原因"}`：

| 状态码 | 含义 |
|---|---|
| 404 | id 不存在，或者文件已从 NAS 删除，需要重新扫描 |
| 409 | 功能没启用（比如语义搜索、看图描述），或已有任务在跑 |
| 413 | 包里的文件太大，只能整包下载 |
| 415 | 这类压缩包不能单独取文件，或者没有缩略图 |
| 422 | 包里的这个文件取不出来，改为整包下载 |
| 429 | 语义搜索正忙（只有 `--mode semantic` 时会出现），稍后再试或改用 `--mode auto` |
| 503 | 带 `Retry-After` 时表示服务器忙，稍等再试；说 bsdtar 没装时，只能整包下载 |

网络超时、下载中断这类问题也会以同样的 `{"error": ...}` 格式报告，出错时不会留下下载了一半的文件。

## 直接用 HTTP（没有 Python 时）

GET 请求不需要认证。POST 请求要带请求头 `X-Requested-With: anything` 和 JSON body。

| 用途 | 请求 |
|---|---|
| 搜索 | `GET /api/agent/search?q=&kind=&mode=auto&limit=20&offset=0` |
| 详情 | `GET /api/agent/files/{id}` |
| 包内文件 | `GET /api/agent/files/{id}/entries?q=&limit=&offset=` |
| 取包内单个文件 | `GET /api/agent/files/{id}/entries/{entry_id}/download` |
| 下载整个文件 | `GET /api/files/{id}/download` |
| 缩略图 | `GET /api/files/{id}/thumb` |
| 任务状态 | `GET /api/agent/tasks` |
| 扫描 | `POST /api/scan`，body 为 `{}` |
| 描述图片 | `POST /api/captions`，body 为 `{"root":"nas","prefix":"文件夹","redo":false}` |
| 停止描述 | `POST /api/captions/stop` |

完整的接口文档在服务的 `/docs` 页面（FastAPI 自动生成）。
