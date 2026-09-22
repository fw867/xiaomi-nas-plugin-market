# 第三方组件与品牌

- 小米、115、阿里云盘名称及标识属于各自权利人，仅用于识别兼容设备和服务，不表示官方背书。
- 115 与阿里云盘图标保持官方来源，详见各插件 README；不得将其单独宣传为本项目拥有的商标。
- WebDAV 随包分发 rclone、WsgiDAV、Cheroot 及 Python 依赖，保留 `projects/xiaomi-webdav-plugin/licenses/` 和 `vendor/*.dist-info/` 中的原始许可证及版权声明。
- 设备管家使用 React、Vite、Radix、Motion、Roboto 等组件，依赖版本锁定于 package-lock.json，适用各上游许可证。
- 图标和界面中使用的 Radix Icons 按其 MIT 许可证分发。

本项目自编代码的统一开源许可证尚待作者确定；公开提供源码不表示第三方商标、素材或依赖被重新授权。第三方组件始终遵循其原有许可。
# qBittorrent 资产与容器

`projects/xiaomi-qbittorrent-plugin/licenses` 保留 qBittorrent COPYING、GPLv3、AUTHORS 和原始 ICO 图标；PNG 仅为格式转换，上游资产继续适用其 GPL 许可，不代表品牌背书。qB 应用本体未嵌入 ZIP，使用者明确启动时从 LinuxServer 的 GHCR 仓库拉取锁定 digest 的镜像。Radix 按钮图标保留原许可证。
# Transmission 资产与容器

`projects/xiaomi-transmission-plugin` **0.2.0 起不再随包分发** transmission 二进制，改为使用者确认后从 LinuxServer 镜像仓库拉取锁定 digest 的镜像 `lscr.io/linuxserver/transmission@sha256:1a12fef3c89eca48b7be9e7d36b17b4eb4e1bcf5e1ee7fbf372e3a38b562939d`（Transmission 4.1.3 / ls362）。`licenses/TRANSMISSION-COPYING` 仍保留 Transmission（GPL-2.0-or-later）许可证全文。Transmission 名称仅用于识别兼容软件，不表示官方背书。LinuxServer 为第三方镜像维护者，不是 Transmission 官方。
# Emby 图标与容器

`projects/xiaomi-emby-plugin` 不随包分发 Emby 二进制，使用者确认后由插件从 Docker Hub 拉取官方镜像 `emby/embyserver:4.10.0.40`（多架构 manifest digest `sha256:3aafff933d3f28d23ed0bc201022abe71c0aa80deb17177566c726b9bbc686c6`）。Emby 为第三方专有软件，其许可与商标归 Emby 官方所有，本插件不代表官方背书，也不包含 Emby Premiere 相关授权。插件图标来自 Icon-Icons 的 Emby 图标（`emby_macos_bigsur_icon_190203.png`），仅用于识别兼容软件。

# DPanel 图标与容器

`projects/xiaomi-dpanel-plugin` 不随包分发 DPanel 二进制，使用者确认后由插件拉取 `dpanel/dpanel:lite`（digest `sha256:befa4221aeebbeac9148cceca06f8b474f412b2de68b5f3968a68e04a0e632c1`）。DPanel 名称与商标归其开发团队所有，本插件不代表官方背书。插件图标取自 DPanel CDN（`https://cdn.w7.cc/dpanel/dpanel-logo-small.png`），仅用于识别兼容软件。DPanel 面板需要访问 Docker socket 才能管理容器，请使用者知悉相关权限风险。

# Jellyfin 图标与容器

`projects/xiaomi-jellyfin-plugin` 不随包分发 Jellyfin 二进制，使用者确认后由插件拉取官方镜像 `jellyfin/jellyfin:latest`（digest `sha256:78d3ea1207d1322471fcac39a614f004f2ccf7e878f95ab2977d752f07e4dd7e`）。Jellyfin 为开源项目（GPL），名称与商标归 Jellyfin 项目所有，本插件不代表官方背书。插件图标取自 Jellyfin 官方 GitHub 组织头像，仅用于识别兼容软件。

# fwclient 内网穿透

`projects/xiaomi-fwclient-plugin` 随包分发 **fwclient-linux-arm64**（内网穿透客户端，构建自本项目关联的 fwserver 源码）。使用者需自备合法的网关域名与访问令牌；隧道流量与规则由网关侧配置。插件图标为本项目自绘，仅用于识别。
