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
