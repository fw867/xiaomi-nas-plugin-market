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

## 镜像

`jellyfin/jellyfin:latest@sha256:78d3ea1207d1322471fcac39a614f004f2ccf7e878f95ab2977d752f07e4dd7e`

## 隔离

- 不挂载 docker.sock，不使用 privileged / host 网络
- 容器以媒体目录属主 `uid:gid` 运行
- 资源上限：1 GiB 内存、2 CPU
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
- 页面状态栏出现「健康检查已关」即表示已生效；接口 `/api/status` 的
  `healthcheckOff` 字段同理。
- 想知道是不是别的程序在唤醒硬盘，用硬盘休眠插件的体检脚本：
  `projects/xiaomi-disk-sleep-plugin/tools/disk-activity-report.py`。

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
