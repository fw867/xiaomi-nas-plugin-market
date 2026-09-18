# 第三方组件与品牌

- 小米、115、阿里云盘名称及标识属于各自权利人，仅用于识别兼容设备和服务，不表示官方背书。
- 115 与阿里云盘图标保持官方来源，详见各插件 README；不得将其单独宣传为本项目拥有的商标。
- WebDAV 随包分发 rclone、WsgiDAV、Cheroot 及 Python 依赖，保留 `projects/xiaomi-webdav-plugin/licenses/` 和 `vendor/*.dist-info/` 中的原始许可证及版权声明。
- 设备管家使用 React、Vite、Radix、Motion、Roboto 等组件，依赖版本锁定于 package-lock.json，适用各上游许可证。
- 图标和界面中使用的 Radix Icons 按其 MIT 许可证分发。

本项目自编代码的统一开源许可证尚待作者确定；公开提供源码不表示第三方商标、素材或依赖被重新授权。第三方组件始终遵循其原有许可。
# qBittorrent 资产与容器

`projects/xiaomi-qbittorrent-plugin/licenses` 保留 qBittorrent COPYING、GPLv3、AUTHORS 和原始 ICO 图标；PNG 仅为格式转换，上游资产继续适用其 GPL 许可，不代表品牌背书。qB 应用本体未嵌入 ZIP，使用者明确启动时从 LinuxServer 的 GHCR 仓库拉取锁定 digest 的镜像。Radix 按钮图标保留原许可证。
# Transmission 资产与二进制

`projects/xiaomi-transmission-plugin` 随包分发 **transmission-daemon / transmission-remote / transmission-cli**（Transmission 4.0.6，GPL-2.0-or-later）及其共享库依赖，二进制取自 Entware 的 `aarch64-k3.10` 仓库，不是本项目自行编译。`licenses/TRANSMISSION-COPYING` 保留上游 GPL-2.0 全文，`licenses/TRANSMISSION-WEB-CONTROL-LICENSE` 保留 transmission-web-control（MIT）的许可证。`runtime-manifest.json` 记录每个文件的来源 .ipk、下载 URL 与 SHA-256。Transmission 名称仅用于识别兼容软件，不表示官方背书；插件图标为本项目自绘，不复用上游商标。
