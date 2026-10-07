# 变更记录（Home Assistant 插件）

本插件**未上架**应用商店，仅保留源码；改动都记在这里。

## 0.1.0（未上架）

- 首个版本：Docker 容器运行 Home Assistant，容器名 `xiaomi-plugin-homeassistant`，
  只挂一路 bind（用户选的**配置目录 → `/config`**），host 网络直接占宿主机 **8123**。
- 沿用同族插件（Jellyfin / Emby）的整套骨架：多存储位置（`LOCAL_ROOTS`）、
  完整绝对路径展示、「修改目录」（停+删容器 → 按新 bind 重建 → 失败回滚）、
  「重新初始化」（必须 `confirm`，只归档插件自己的 `settings.json`，**用户目录零改动**）、
  设备/inode 身份校验与 FUSE 身份号自动更新、`/api/browse`、会话/CSRF、
  关掉镜像自带的 30 秒健康检查（避免每 30 秒重写 SQLite 唤醒机械盘）。
- 版本显示优先读配置目录里的 `.HA_VERSION`（HA 自己写的），读不到才退回镜像口径。
- 一处有意偏离同族模板：官方容器要求以 root 运行（镜像不支持非 root，也没有
  PUID/PGID），所以不设 `User`，但仍要求配置目录属于非 root 的 NAS 用户。
- host 模式下的端口自检：只有「本来停着、这次重新 start」才自检，先给 **60 秒**宽限期，
  宽限期内一直没监听才停 → 删 → 重建一次，重建后仍没监听就如实报错。
- 首次安装脚本 `deploy/install-on-nas.sh`（配套 `uninstall-on-nas.sh`、
  `register_plugin.py`、`native_layout.py`）：渲染 systemd 单元 → 上传 release 到
  `/data/plugin/homeassistant/releases/<版本>-<时间戳>-<pid>` → 切 `current` →
  写注册表 → 铺客户端 UI 与 INFO（摘要自校验）→ `daemon-reload`/enable/restart →
  `nginx -t` 后 reload → 自检（服务状态、两个端口、注册表、页面令牌、占位符残留）。
  失败会汇总失败项并非零退出，不会打印「安装完成」。
- 镜像按 Docker 能力自动选：≥23 用 `stable`（首选），<23 固定到最后一个 gzip 层版本
  `2026.2.0`，拉取失败自动换另一个候选重试一次；Docker 的原始报错会透传到页面
  （截断 400 字符）。
- **未上架原因**：真机实测（Docker 20.10.17 + runc 1.1.2）起不了 HA 官方镜像——
  2026.3.0+ 的 zstd 层拉不下来（可用 2026.2.0 绕过），但容器 runtime 层面报
  `failed to create shim task: … can't get final child's PID from pipe: EOF`，
  穷尽最小配置 / 换入口 / 换旧版镜像 / 特权+无硬化 / cgroupns=host 均失败。
  等厂商升级 Docker 且 runtime 兼容后，把 `scripts/build_apps.py` 的
  `PACKAGE_SPECS` 条目加回来即可。详见 README「未上架」一节（含复现命令）。
