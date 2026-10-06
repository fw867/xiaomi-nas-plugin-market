# Jellyfin（小米智能存储社区插件）

在小米智能存储客户端里用 Docker 运行 **Jellyfin** 开源媒体服务器。

**不是 Jellyfin 或小米的官方插件。**

## 功能

- 初始化：选择**媒体目录**（必填）与**配置目录**（可选，留空用插件私有目录）
- 数据持久化：
  - `/config` — 服务器配置、用户、媒体库元数据
  - `/cache` — 转码与图片缓存（插件私有目录）
  - `/media` — 所选媒体目录
- 端口映射：
  | 用途 | 宿主机 | 容器 |
  | --- | --- | --- |
  | HTTP Web UI | **8097** | 8096 |
  | HTTPS | 8920 | 8920 |
  | DLNA 发现 | 7359/UDP | 7359/UDP |

宿主机使用 **8097** 是为了与 Emby 插件的 8096 错开，可同时安装。

## 基本配置

1. 安装并打开插件，选择媒体目录后「初始化并启动」
2. 首次启动需拉取官方镜像，可能需要较长时间
3. 打开局域网地址 `http://<NAS-IP>:8097`，完成 Jellyfin 向导（管理员账号、媒体库）
4. 媒体库路径在容器内为 `/media` 下的子目录

## 存储位置（存储池 / 外接设备）

目录选择器支持多个**存储位置**：内置存储池（`/nas/pool0/<用户>/data`，是 FUSE）
和外接设备（U 盘，稳定 bind 路径 `/nas/mnt/usb`，或内核挂载点 `/mnt/usb-xxxx`）。

- 由环境变量 `LOCAL_ROOTS` 给出，冒号分隔的绝对路径，顺序即页面上的顺序；
  不设置时退化为单个 `LOCAL_ROOT`（旧部署方式完全不受影响）。
- 页面会显示位置标签（存储池 / 外接设备 / 末段目录名），多于一个位置时出现切换按钮；
  只有一个位置时不显示。
- 目录选择与状态卡片都显示**完整绝对路径**；提交给后端时绝对路径与相对路径都接受，
  绝对路径按最长前缀匹配回到所属位置（`Engine.locate()`）。

## 目录设置（修改目录 / 重新初始化）

配置完成后，状态区下方的「目录设置」提供两个入口：

- **修改目录**：弹窗里**媒体目录与配置目录都能改**，两者都默认「保持当前」，
  点「选择目录」走同一套存储位置选择器（存储池 / 外接设备）；配置目录另有
  「用私有目录」，即改回插件私有目录。插件按新的宿主路径**重建容器**——bind 挂载
  在创建容器时固定，只 restart 的话旧容器会继续挂着旧目录。写新配置前先把
  `settings.json` 备份成 `settings.json.bak-<时间戳>`；重建失败会自动回滚配置并尽力
  按旧配置恢复容器，失败原因原样显示在页面上。只是换位置，**原目录里的文件不会被删除**；
  新位置若没有原来的 Jellyfin 配置，应用会以全新状态启动。
  提交语义：不带 `configPath`＝保持当前配置目录，空串＝插件私有目录，路径＝新位置。
- **重新初始化**：停止并移除容器、把 `settings.json` 挪成带时间戳的备份，
  页面回到初始化表单（`configured` 变 false）。**媒体目录与配置目录里的文件一个都不动**；
  本插件没有路由器端口映射之类的副作用，容器就是唯一的系统改动。

两个动作都要确认（`service/reset` 必须带 `{"confirm": true}`），
`busy` 时一律拒绝（`当前有操作正在进行，请稍后再试`）。对应的引擎方法是
`Engine.reconfigure()` 与 `Engine.reset()`，分发仍走 `POST /api/service/<action>`。

## 镜像

`jellyfin/jellyfin:latest@sha256:78d3ea1207d1322471fcac39a614f004f2ccf7e878f95ab2977d752f07e4dd7e`

## 隔离

- 不挂载 docker.sock，不使用 privileged / host 网络
- 容器以媒体目录属主 `uid:gid` 运行
- 资源上限：2 GiB 内存、2 CPU（`MemorySwap` 与 `Memory` 相同＝容器内不给 swap）
  - 早期是 1 GiB。2026-10-06 实测：扫描媒体库时，单个 `ffprobe` 进程就能吃到
    780 MB 匿名内存，1 GiB 上限下被容器自己的 memcg OOM kill 了两次，而当时宿主机
    还有 2.3 GB 可用——是容器上限不够，不是整机缺内存，所以放宽到 2 GiB。
  - 资源上限只在**创建容器**时生效（Docker 20.10 的 `/containers/<id>/update` 对
    这类上限不生效），所以插件启动时发现旧容器的上限不对会**停 → 删 → 按新配置重建**；
    `/config`、`/cache`、`/media` 都是 bind 挂载，配置与媒体库不受影响。
- 停止/卸载只停容器，配置与媒体保留

## 已关闭容器健康检查

官方 `jellyfin/jellyfin` 镜像自带一个 **30 秒一次**的健康检查
（`curl --noproxy localhost -Lk -fsS ${HEALTHCHECK_URL}`，指向 `/health`）。
每打一次 `/health`，Jellyfin 都会重写 SQLite 的 `jellyfin.db-shm` / `-wal`；
文件一改，系统的 findex 索引服务（fanotify）立刻写 `/nas/sys`，而 `/nas/sys`
建在**跨两块盘的 RAID1**（md0）上——两块机械盘每 30 秒被唤醒一次，永远进不了休眠，
每天多出约 1.4 GB/盘的无谓写入。

所以插件在建容器时显式写入 `Healthcheck: {"Test": ["NONE"]}`。

- **旧版本插件建的容器**（已经带着镜像的健康检查）会在插件服务重启、或点
  「启动服务」时**自动重建一次**去掉它。`/config`、`/cache`、`/media` 都是 bind
  挂载，配置与媒体库不受影响，只有一次短暂的容器重启。
- 健康检查只能在创建容器时决定：Docker 20.10 的 `POST /containers/<id>/update`
  虽然接受 `Healthcheck` 字段并返回 200，但**实际不生效**，所以只能重建。
- 页面「说明与限制」里写了这一点（状态栏只显示 Jellyfin/镜像版本等运行信息）；
  接口 `/api/status` 的 `healthcheckOff` 字段可以程序化判断。
- 想知道是不是别的程序在唤醒硬盘，用硬盘休眠插件的体检脚本：
  `projects/xiaomi-disk-sleep-plugin/tools/disk-activity-report.py`。

**实测效果**（关闭健康检查并重建容器后）：`sdb2`（Jellyfin 配置盘）60 秒写入从
3632 扇区降到 **0**，`md0`（`/nas/sys`，跨两块盘的 RAID1）写入下降约 57%；
系统排空剩余事件后，两块机械盘在 12:27:29 同时进入 `standby`，此后 60 多分钟
`hdidle` 没有再记录任何 activity。也就是说这条 30 秒心跳原本就是硬盘不休眠的
根本原因，关掉它之后不需要停用系统索引/相册/媒体库服务。

## 不再写死 JELLYFIN_PublishedServerUrl

早先的版本给容器设了 `JELLYFIN_PublishedServerUrl=http://__NAS_IP__:8097`，
而那个 `__NAS_IP__` **从来没有被替换过**（全仓库只此一处）。后果是 Jellyfin 把
它当成自己的对外地址：

```bash
$ curl http://127.0.0.1:8097/System/Info/Public
{"LocalAddress":"http://__NAS_IP__:8097", ...}     # 客户端拿到的就是这个解析不了的域名
```

客户端拿到解析不了的地址就会连接失败、会话中断（Jellyfin 日志里的
`WS ... The remote party closed the WebSocket connection`、
`Unexpected end of request content` 都是这种客户端侧断开），远程访问时表现为
「用着用着就退出了」。设成局域网地址对远程访问同样是错的——**该用什么地址，
取决于客户端是从哪个地址连上来的**，所以交给 Jellyfin 自己判断，插件不再插手。

环境变量只在创建容器时生效，因此旧容器里残留这个键时，`start()` 会**自动重建一次**
（判断逻辑见 `Engine.stale_env()`，与去掉健康检查走同一条重建路径）。

不设这个变量之后，Jellyfin 默认会报**容器自己的地址**（`http://172.17.0.2:8096`），
客户端同样用不了。正确做法是在 Jellyfin 里打开**按请求头发布地址**：

- 控制台 → 网络 → 勾上「允许通过请求头发布服务器地址」
  （对应 `config/network.xml` 的 `EnablePublishedServerUriByRequest`，改成 `true` 后
  需要重启 Jellyfin）；这样地址跟着客户端实际访问用的 Host 走——局域网访问得到
  `http://<NAS-IP>:8097`，走内网穿透访问得到隧道域名，两边都对。
- 改 `network.xml` 要在容器**停止**状态下改：Jellyfin 关闭时会把内存里的配置写回文件。
  改的时候原地写入（不要用 `sed -i`，那会换掉 inode 和属主）。


## 本地预览

```bash
cd projects/xiaomi-jellyfin-plugin
python -m unittest discover -s tests -v
python server.py --dev
```

## 上游

- [Jellyfin](https://jellyfin.org/)
- [容器安装文档](https://jellyfin.org/docs/general/installation/container/)
- 图标来自 Jellyfin 官方 GitHub 组织头像，仅用于识别兼容软件
