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
nano .env               # 至少改 ASSET_BIND（LXC 的局域网 IP），并确认 ASSET_NAS_PATH 和 ASSET_ROOTS
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
| 立刻扫一次 | `docker compose exec scanner python -m assetindex scan` |
| 重试坏包和没封面的包 | `docker compose exec scanner python -m assetindex scan --retry-errors` |
| 看扫描日志 | `docker compose logs --tail 100 scanner` |
| 升级 | 更新项目文件后 `docker compose up -d --build` |
| 重建索引 | `docker compose down && docker volume rm assetindex_asset-data && docker compose up -d` |

- 定时扫描正在跑时，手动扫描会直接退出（退出码 3，日志里有 "another scan is running"），等它跑完再试。
- 某个包连续两次把扫描进程撑爆内存，会被标记为坏包并跳过，日志里能看到 "killed the scanner"。调高 `SCANNER_MEM_LIMIT` 后，用 `--retry-errors` 重试。
- 索引数据卷可以随时删掉重建，只是要重新跑一遍首次扫描。NAS 上的东西不受任何影响。

## 安全

网页**没有登录**，任何能访问端口的人都能浏览和下载全部素材。

只在局域网使用，不要在路由器上做端口转发。需要外网访问时，前面加一个带认证的反向代理，或者走 VPN。
