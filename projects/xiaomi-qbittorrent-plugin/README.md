# qB 下载

小米智能存储插件市场的 qBittorrent 插件，版本 **0.1.2**。不是 qBittorrent 或小米官方插件。

## 安装与使用

1. 安装本仓库新版插件市场，在列表中安装「qB 下载」。此步骤只安装插件，不启动下载容器。
2. 使用设备所有者账号打开插件，选择存储里的一个目录作为下载目录。该目录会直接挂载为容器内的 `/downloads`，插件不新建子目录、也不修改其中已有文件的所有权；请选一个已经由该 NAS 用户拥有、且愿意用来存放下载内容的目录。
3. 设置 admin 管理密码（8 至 200 位，大小写、数字、符号至少两种），确认后安装并启动服务。首次须能连接 GitHub Container Registry 下载镜像，可能需要数分钟。
4. 服务就绪后，状态页会显示局域网入口地址（可直接用电脑或手机的浏览器打开 qBittorrent 完整界面）；点「qB Web 控制台」进入下载列表。登录一次后会话会记住，平时不必重复输密码。支持磁力链接、最大 2 MiB 的种子文件、任务进度与速度、暂停/继续、文件清单、限速与并发数。
5. 「移除任务」始终保留已下载文件；插件中没有删除文件操作。

## 隔离与生命周期

- 独立容器 `xiaomi-plugin-qbittorrent`，只挂载插件私有配置目录与所选的下载目录。不挂载 Docker socket，不使用 privileged 或 host 网络。
- Python 管理服务需 root/Docker 权限，仅提供固定操作。它不是不受信任插件的沙箱；安装之前应信任发行者及其签名。
- 512 MiB 容器内存上限、1.5 CPU 上限、128 PID、默认两个并发下载。是上限，不是恒定资源占用。
- WebUI 监听 18123 并对局域网开放，可直接用 qBittorrent 官方客户端或网页连接；插件服务绑定 18122，通过现有设备同源入口访问，不写死 NAS IP。
- BT 入站端口固定为 36754（TCP + UDP）并映射到宿主机，其他 peer 才能主动连进来，做种才有上传。要让公网上的 peer 连入，还需要在路由器上把 36754 转发到这台 NAS；插件本身未启用 UPnP/NAT-PMP。
- qB API 的密码、CSRF、Host 校验保持开启；插件本身另要求设备所有者会话及 CSRF。密码只保存为 qB PBKDF2-SHA512 哈希；登录 Cookie 仅在服务内存保留。
- 固定 LinuxServer 镜像版本 `5.2.3_v2.0.14-ls474` 及多架构 digest `sha256:a00b6a597a3832a1814cde0ef60abc55c94644f3f80902c3432f6af6de8d4a96`，上游清单包含 ARM64。LinuxServer 是第三方镜像维护者，不是 qB 官方。
- 停止或卸载插件时，systemd `ExecStopPost` 只停止带本插件私有所有权标签的容器。容器与配置、下载文件均保留；不执行 `docker rm` 或批量清理。重装后可恢复已配置服务。
- 服务启动前核对目录 inode/device 与路径，存储变化时拒绝启动。OTA 仍可能移除系统入口/服务，不能保证永不受影响；不要在正在下载时升级固件。

## 验证与限制

本地自动化测试和模拟 API UI 测试不能替代真实 RP05 镜像启动、磁力下载、Mac/手机入口验收。未在你的 NAS 上自动部署或创建下载任务。

本机预览不调用 Docker、不修改 NAS：

```bash
python3 scripts/build_ui.py
python3 -m unittest discover -s tests -v
python3 server.py --dev
```

打开 `http://127.0.0.1:18122/`。未授权 API 不能获取任务列表。真实共享包不包含密码、令牌、个人 SSH 密钥或下载文件。

## 上游与资产

- [qBittorrent](https://www.qbittorrent.org/) / [Web API 文档](https://github.com/qbittorrent/qBittorrent/wiki/WebUI-API-(qBittorrent-5.0))
- [LinuxServer 镜像说明](https://docs.linuxserver.io/images/docker-qbittorrent/)
- 应用图标取自 qBittorrent `release-5.2.3/src/icons/qbittorrent.ico`，只转为 PNG；原 ICO 保存在 licenses，保留其上游 GPL 许可与作者信息。
- 按钮图标复用生态已有 Radix Icons PNG，保留对应许可证。不混淆上游品牌或暗示背书。
