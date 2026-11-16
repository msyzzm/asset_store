# 部署：PVE LXC + Docker + NFS（只读）

结构：

```
NAS ──NFS(只读)──> PVE 宿主机 /mnt/nas-assets
                     └─ bind mount(ro) ──> LXC /mnt/assets
                                             └─ docker volume(ro) ──> 容器 /assets
LXC 本地盘 ──> docker 卷 asset-data ──> 容器 /data（索引 + 缩略图）
```

两个容器共用一个镜像：
- `web`：网页和 API，端口 8000。
- `scanner`：每 6 小时扫描一次。

从 PVE 到容器这一侧全程只读，索引和缩略图只写在 LXC 本地盘上。NAS 那边也要做相应配置（见第 1 步），否则这里的只读只是"本机不写"，挡不住局域网里的其他机器。SQLite 的 WAL 和扫描锁在 NFS 上不可靠，所以数据卷不能放到 NAS。

每一步后面都附了检查命令，检查通过再做下一步。

## 1. PVE 宿主机：挂载 NAS（只读）

先在 **NAS 上**设置这个 NFS 导出：
- **只读**（RO）。
- 允许的客户端**只填 PVE 宿主机的 IP**，不要填 `*` 或整个网段。NFSv3 不验证客户端声称的用户身份，开放给整个网段等于开放给所有设备。

然后在宿主机上挂载，而不是在 LXC 里挂。非特权 LXC 不能自己挂 NFS。

```bash
mkdir -p /mnt/nas-assets
# /etc/fstab 加一行（按你的 NAS 地址和导出路径改）：
# nas.lan:/volume1/assets  /mnt/nas-assets  nfs  ro,nosuid,nodev,noexec,soft,timeo=150,retrans=3,_netdev  0  0
mount /mnt/nas-assets
```

选 `soft`，是为了 NAS 掉线时让读取报错，而不是让扫描永远卡住。这里只读，报错只会让这一轮扫描跳过，不会损坏数据。

检查：

```bash
ls /mnt/nas-assets | head
```

## 2. 创建 LXC 并开启 Docker 所需特性

1. 用 Debian 12 模板创建一个**非特权** LXC。
   - 内存至少 6 GB：web 2 GB，scanner 3 GB，再留出余量。swap 设为 0。这个 LXC 的内存上限是最后一道防线，第 6 步会说明原因。
   - 系统盘按缩略图上限 4 GB 加索引来定，建议 20 GB 以上。
2. 在宿主机上执行（`<ID>` 是 LXC 编号）：

```bash
pct set <ID> -features nesting=1,keyctl=1
pct set <ID> -mp0 /mnt/nas-assets,mp=/mnt/assets,ro=1
pct reboot <ID>
```

`nesting` 和 `keyctl` 会削弱 LXC 的隔离，这是在 LXC 里跑 Docker 的代价。所以这台 LXC 只用来跑这个服务，不要放别的东西。

检查（LXC 里）：

```bash
ls /mnt/assets | head          # 能看到素材
touch /mnt/assets/x            # 应该失败：Read-only file system
```

## 3. 权限：UID 映射

非特权 LXC 的 UID 在宿主机上会整体偏移 100000。容器里的 `app` 用户（uid 1000）在宿主机和 NAS 看来是 **uid 101000**。

所以 NAS 上的素材必须对这个 uid 可读。二选一：
- 文件对"其他人"可读（文件 `r`，目录 `r-x`）。大多数 NAS 默认就是这样。
- 或者在 NAS 的 NFS 导出里设置 squash：把所有用户映射到一个**专用账号**，这个账号在 NAS 上只对素材共享有只读权限。可以用 guest 或单独建一个账号，**不要映射成 admin**。

优先用第一种。

检查（宿主机上，模拟容器用户）：

```bash
setpriv --reuid=101000 --regid=101000 --clear-groups ls /mnt/nas-assets | head
```

`101000` 是默认映射下的值。如果给这个 LXC 配过自定义 `lxc.idmap`，按实际映射换算。

这条能列出文件就没问题。如果报 `Permission denied`，先改 NAS 的权限或 squash 设置，再继续。

## 4. LXC 里安装 Docker

按 Docker 官方文档在 Debian 上安装：https://docs.docker.com/engine/install/debian/

需要装 `docker-ce` 和 `docker-compose-plugin`。不要用 Debian 自带的 `docker-compose` v1。

检查：

```bash
docker run --rm hello-world
docker compose version
```

## 5. 部署

把项目目录拷到 LXC，比如 `/opt/assetindex`，然后：

```bash
cd /opt/assetindex
cp .env.example .env
nano .env               # 确认 ASSET_NAS_PATH 和 ASSET_ROOTS；ASSET_BIND 默认 0.0.0.0（所有网卡）
docker compose build
docker compose run --rm web python -m assetindex check
docker compose run --rm web touch /assets/x     # 应该失败：Read-only file system
```

`check` 每一行都应该是 `OK`：

| 检查项 | 失败时 |
|---|---|
| data dir writable | 数据卷权限不对 |
| database | SQLite 打不开；看报错 |
| root … readable | NAS 没挂上，或 uid 101000 读不了（见第 3 步） |
| bsdtar | 镜像里缺 libarchive-tools（不该发生） |
| bsdtar non-ASCII names | 中文包内文件名匹配失败：locale 问题，把输出发给我 |
| 3D viewer files | 前端文件缺失或 MIME 不对 |

全部 OK 后启动：

```bash
docker compose up -d
docker compose logs -f scanner     # 首次扫描：看到 "thumbnail cache: removed" 就是扫完一轮
```

启动（包括重启、更新）时不会自动扫描。第一次装好后，在网页任务栏「扫描」一行点「立即扫描」开始首次扫描，同一行会显示扫到哪一步、进度多少。之后定时扫描接着上一次扫描的时间走，默认每 6 小时一次（`SCAN_INTERVAL_SECONDS`）；如果停机期间错过了，就从启动时起重新计时。

检查网页和 3D 预览需要的 JS：

```bash
curl -sI http://localhost:8000/ | head -1
curl -sI http://localhost:8000/vendor/three/build/three.module.min.js | grep -i content-type   # 应含 javascript
```

浏览器打开 `http://<LXC 的 IP>:8000`。

## 6. 确认内存上限真的生效

在嵌套的 LXC 里，Docker 不一定能设置内存限制，要实际确认：

```bash
docker stats --no-stream
```

`MEM USAGE / LIMIT` 那一列应显示 2GiB（web）和 3GiB（scanner）。

如果显示的是整个 LXC 的内存，说明限制没生效。这时只能靠 LXC 本身的内存上限兜底。最坏情况是两个容器一起被杀，然后被 `restart: unless-stopped` 拉起来。把输出发给我，我们再看怎么处理。

## 日常操作

| 要做的事 | 命令 |
|---|---|
| 立刻扫一次 | 网页任务栏「扫描」一行点「立即扫描」，或 `docker compose exec scanner python -m assetindex scan` |
| 停止扫描 | 任务栏「扫描」一行点「停止」：处理完手上这个文件夹、压缩包或这批向量就停。已扫的保留，没扫完的下一轮定时扫描接着做 |
| 重试坏包和没封面的包 | `docker compose exec scanner python -m assetindex scan --retry-errors` |
| 看扫描日志 | `docker compose logs --tail 100 scanner` |
| 升级 | 网页任务栏「更新」一行点「更新」（需先装一次更新服务，见下文）；或手动 `git pull && docker compose up -d --build` |
| 重建索引 | `docker compose down && docker volume rm assetindex_asset-data && docker compose up -d` |

- 定时扫描正在跑时，手动扫描会直接退出（退出码 3，日志里有 "another scan is running"），等它跑完再试。
- 某个包连续两次把扫描进程撑爆内存，会被标记为坏包并跳过，日志里能看到 "killed the scanner"。调高 `SCANNER_MEM_LIMIT` 后，用 `--retry-errors` 重试。
- 索引数据卷可以随时删掉重建，只是要重新跑一遍首次扫描。NAS 上的东西不受任何影响。

## 网页一键更新

页面任务栏里的「更新」一行会显示当前版本和 GitHub 上的新提交，点「更新」就会自动拉代码、重建并重启容器，不用再 ssh 上去。

真正执行更新的是 LXC 主机上一个很小的 systemd 服务，容器本身拿不到 Docker 的控制权。这个服务只需在 LXC 里装一次，前提是容器已经在运行：

```bash
cd /opt/asset_store            # 你 clone 仓库的位置
sudo deploy/updater/install.sh
```

它会做这些事：

- 从正在运行的 web 容器上找出数据卷在主机上的路径；
- 安装 `asset-updater.path`（网页一提交请求就执行更新）和 `asset-updater-check.timer`（每小时 `git fetch` 一次，看有没有新版本）；
- 以仓库所有者的身份执行 git 命令，用的是你平时 `git pull` 时的凭据；`docker compose` 以 root 身份执行。

说明：

- 更新执行的是 `git pull --ff-only` 加 `docker compose up -d --build`。如果 LXC 里的仓库有本地改动或分叉，pull 会失败，页面上显示红点，鼠标悬停能看到报错。完整输出在数据卷里的 `update.log`。
- 重建期间网页会断开一会儿，完成后页面自动刷新到新版本。正在跑的扫描或看图描述会被打断，重启后点一下就能接着做，已经完成的部分不会丢。
- 拉代码和重建镜像期间可以点「停止」，服务继续跑原来的版本。进入「重启服务」这一步后就不能停了，免得网页起不来。停止功能从装上这个版本之后的下一次更新开始生效（主机上的更新脚本在每次更新成功后自动换成新的）。
- 卸载：`sudo deploy/updater/install.sh --uninstall`。
- 查看服务日志：`journalctl -u asset-updater -u asset-updater-check`。

## 语义搜索（可选）

接上一个 OpenAI 兼容的 embeddings 接口后，可以用描述来搜，比如「蓝色的冰魔法特效」。不接也不影响其他功能。

1. 网页右上角点「设置」，填写：
   - 接口地址：填到 `/v1` 为止，例如 `http://192.168.1.10:8000/v1`，程序会自动加上 `/embeddings`；
   - 模型名；
   - API key：没有就留空。
2. 选择「图片格式」，这一项决定能不能按画面搜：

   | 选项 | 请求体里图片的写法 | 适用 |
   |---|---|---|
   | 不发送图片 | — | 只用路径和文件名，解决中英互通和近义词 |
   | input 对象 | `"input": [{"image": "data:image/jpeg;base64,..."}]` | Jina 等 |
   | input 字符串 | `"input": ["data:image/jpeg;base64,..."]` | 部分自建服务 |
   | messages | `"messages": [{"role": "user", "content": [{"type": "image_url", ...}]}]`，每次一张 | vLLM 等 |

3. 依次点「保存」和「测试连接」。文字和图片都显示「成功，向量 N 维」就说明可用。图片显示失败时，换一种格式再试。
4. 勾选「启用」并保存，然后点「扫描」。扫描结束后会自动计算向量：
   - 首次要处理全部素材，耗时取决于接口速度；之后每次只处理新增和改动的。
   - 设置里会显示进度。
5. 搜索框旁边会出现「按描述搜」开关。勾上以后，结果按相关度排序，最多显示 200 个。

说明：

- **发出去的内容**：每张卡片发一段文字（路径，压缩包还附带包内的目录名和文件名）。开启图片后，每张卡片再发一张 256px 的小图（压缩包是 384px 的拼图）。序列帧只发第一帧。
- **换模型**：已有的向量作废，下次扫描全部重算。
- **接口挂了**：只会跳过这一轮的向量计算，扫描照常完成，日志里有 `embedding stopped early`，下次扫描接着算。
- **被接口拒绝的素材**（比如图片格式不支持）：会记下来，不会每次都重发。修好之后用 `--retry-errors` 重试。
- **命令行检查**：`docker compose run --rm web python -m assetindex check` 也会测试这个接口。
- **key 的存放**：保存在数据卷的 `embedding.json` 里（文件权限 600），网页上不会再显示。没有登录，所以局域网里的人都能改这些设置。为了不让别人把 key 骗到他自己的机器上，改了接口地址就必须重新填 key。

## 看图描述（可选）

让一个能看图的对话模型（OpenAI 兼容的 `/chat/completions`，比如 Strata 上的 Qwen）给图片写一句中文描述和几个关键词。写好以后，搜索框能按画面内容搜到图片（比如搜「宝箱」「爆炸」）；开着语义搜索的话，这张卡片的文字向量会带上描述重新计算。

它由单独的 `captioner` 容器执行，平时空闲。**只有在网页上点「开始」才会运行**，定时扫描不会触发它。

1. 网页任务栏「看图描述」一行点「打开」，展开「接口设置」，填写：
   - 接口地址：填到 `/v1` 为止，例如 `http://192.168.1.234:8080/v1`，程序会自动加上 `/chat/completions`；
   - 模型名：Strata 不看这个字段，随便填；其他服务要填真实的模型名；
   - API key。
2. 点「保存」，再点「测试」。模型会描述一张红色圆形的示例图，返回内容和耗时都会显示出来。
3. 选好根目录和文件夹（留空表示整个根目录，输入时会提示子文件夹），对话框会显示要处理多少张，然后点「开始」。
   - 默认只处理还没有描述的卡片，勾选「已经有描述的也重新描述」就全部重做。
   - 随时可以点「停止」，当前这张描述完就停。已经写好的描述会保留，下次开始时接着处理剩下的。
4. 描述会显示在大图下方的「画面描述」里，鼠标悬停在卡片上也能看到。

说明：

- **速度**：一次只处理一张。本地模型通常每张几秒，几万张第一次要跑好几个小时，期间那台机器上的模型会被占着。建议按文件夹分批跑。
- **发出去的内容**：每张卡片一张 512px 的 JPEG（压缩包发它的拼图），外加固定的提示词。请求里带 `reasoning_effort: "none"`，让模型不先"思考"再回答。
- **出错处理**：
  - 接口连不上或超时：任务立即停止。
  - 单张图被拒绝或模型没回答：这张记为失败，继续下一张。
  - 连续 5 张都被拒绝：说明设置有问题，任务停止。
  - 失败的图下次开始时会再试一次。
- **读不出图的卡片**：eps 没有内嵌预览、超过 5000 万像素的超大 JPG 等，没有图可以发，记为失败。
- **看日志**：`docker compose logs --tail 100 captioner`。
- **命令行直接跑**：`docker compose exec captioner python -m assetindex caption --root nas --prefix 某个文件夹`。
- **key 的存放**：与语义搜索相同，保存在 `caption.json` 里（权限 600），改了接口地址必须重新填 key。

## 素材整理（第一步：只出建议）

主页任务栏的「整理」一行点「打开」，进入整理页。分析会做这几件事：

1. **把素材库分成"包"**：压缩包是一个包，文件夹按目录结构判断。
   - 顶层是 `2D`、`3D`、`Audio`、`itch`、`UE` 这类分类名或来源名的，下面每一项各算一个包。
   - 名字里带"合集""系列""Collection"的文件夹，会再往下展开一层。
   - 一个文件夹里（或下一层子文件夹里）有 3 个以上名字不同的压缩包，就当成"放包的文件夹"展开，比如 `UE/道具/` 里每个 `.rar` 各算一个包，再深的层也一样。名字开头都一样的（`Cabin_Unity.zip`、`Cabin_Unreal.zip`，或者 `4.27/`、`5.0/` 里放同一个包）算同一个包的几个版本，不展开。
   - 和压缩包同名的预览图、说明文件（`Castle.rar` 旁边的 `Castle.png`），以及分卷（`x.part1.rar`、`x.part2.rar`），跟压缩包算同一个包，一起移动。名字里的空格、下划线、大小写、版本号不影响配对。专门放预览图的子文件夹（比如 `PM895/JPEG/(1)_xx.jpg` 对应 `PM895/(1)_xx.zip`，里面全是和上层压缩包同名的图）也一样，图跟着各自的压缩包走。
   - 判断错了，在页面上点「拆开」，这个包的子项就会各自成为一个包；点「合并」，它所在的整个文件夹就当成一个包。
   - 已经确认或拒绝过的文件夹包，规则改了也不会被拆开。
2. **查重**：只看大小撞车、256KB 以上的文件。先「抽样比对」：每个文件只读开头、中间、结尾各 16KB；抽样也一样、而且所在的包文件数和总大小都和另一个包相同的，才「细比」读整个文件。样例里要读的数据从 2.2GB 降到 62MB。结果按文件缓存，下次只看新增或改过的。完全相同的包，多出来的副本建议移到 `_Duplicates/`。不同包之间重复的单个文件，以及包内的重复文件，只在顶部统计里列出来，不建议移动，免得把包拆坏。
3. **归类和起名**：用「看图描述」里设置的同一个看图模型（比如 Strata 上的 Qwen），把每个包的拼图、文件名和画面描述发给它，让它从目录方案里选一个分类，并起一个「来源 - 内容」形式的名字。原名是纯编号时，编号会保留在新名字后面。

目录方案：

| 目录 | 放什么 |
|---|---|
| `01_2D/Characters` `Environments` `Props` `UI` `VFX` `Textures` | 2D 角色、场景、道具、界面图标字体、特效、贴图 |
| `02_3D/Characters` `Environments` `Props` `Vehicles` `Vegetation` `Animations` | 3D 角色、场景、道具、载具、植物、动作 |
| `03_Audio/Music` `SFX` `Ambient` `Voice` | 音乐、音效、环境音、语音 |
| `04_Tools/Shaders` `Plugins` `Templates` | Shader、引擎插件和工具、项目模板 |
| `05_Reference` | 设定集、原画、照片、教程 |
| `_Unsorted` / `_Duplicates` | 看不出来的 / 重复的副本 |

引擎（Unity、UE）不单独设目录。卡片上本来就有引擎角标，按内容分类之后，同一类的 Unity 包和 UE 包放在一起。

页面上可以：

- 按状态查看（待确认、重复、已确认、已拒绝、未分析、失败），按分类筛选；
- 逐条或批量确认、拒绝，点「改」手动修改目标路径；
- 在重复的几份里点「改为保留这份」；
- 点「拆开」把一个文件夹包拆成几个包；点「合并」把一个包所在的文件夹整个当成一个包（里面有确认或拒绝过的包时不让合并，先撤回）。

说明：

- **这一步不会移动任何文件**，NAS 仍然是只读的。确认过的建议会在下一步统一执行，到时候再开写权限，并且可以撤销。
- 分析由 `captioner` 容器执行，和看图描述共用同一个模型，同一时间只跑一个任务；两个都提交了，就排队。
- 每个包要问一次模型，大约和描述一张图一样慢。可以先填一个文件夹，分批分析。
- 整理页和主页任务栏都可以点「停止」，手上这个包问完就停，已经出的建议会保留。
- 只有一个文件的包（一个压缩包、一张图），如果还没有看图描述，会在归类的同一次提问里顺便写好描述，之后跑看图描述时就跳过它，不用再问一次。文件夹包不会这样做。
- 某个包里的文件变了（新增、删除、修改），它的建议会作废，下次分析时重新问。已经确认或拒绝的决定，在文件没变的情况下会一直保留。
- 命令行：`docker compose exec captioner python -m assetindex organize --root nas --prefix 某个文件夹`。

## 给 AI agent 用

`/api/agent/*` 是一组给 agent 用的接口，返回内容精简，每条结果都带下载链接，还能从 zip/rar/7z 里单独取出一个文件。仓库的 `skills/asset-store/` 是配套的 skill：`SKILL.md` 加一个只用 Python 标准库的命令行脚本。

在 Claude Code 里使用：

1. 把 `skills/asset-store` 整个文件夹复制到 `~/.claude/skills/`（所有项目都能用），或者复制到某个项目的 `.claude/skills/`。
2. 设置环境变量 `ASSET_STORE_URL=http://<LXC 的 IP>:8000`。
3. 之后直接说"去素材库找一个火焰爆炸的序列帧放到 Assets/VFX"，agent 会自己搜索、查看缩略图、下载。

别的 agent 可以直接调用 HTTP 接口，接口清单在 `SKILL.md` 末尾，完整文档在服务的 `/docs` 页面。

## 安全

网页**没有登录**，任何能访问端口的人都能浏览和下载全部素材。

只在局域网使用，不要在路由器上做端口转发。需要外网访问时，前面加一个带认证的反向代理，或者走 VPN。
