# qB 下载

小米智能存储插件市场的 qBittorrent 插件，版本 **0.1.2**。不是 qBittorrent 或小米官方插件。

## 安装与使用

1. 安装本仓库新版插件市场，在列表中安装「qB 下载」。此步骤只安装插件，不启动下载容器。
2. 使用设备所有者账号打开插件，选择存储里的一个目录作为下载目录。该目录会直接挂载为容器内的 `/downloads`，插件不新建子目录、也不修改其中已有文件的所有权；请选一个已经由该 NAS 用户拥有、且愿意用来存放下载内容的目录。
3. 设置 admin 管理密码（8 至 200 位，大小写、数字、符号至少两种），确认后安装并启动服务。首次须能连接 GitHub Container Registry 下载镜像，可能需要数分钟。
4. 服务就绪后，状态页会显示局域网入口地址（可直接用电脑或手机的浏览器打开 qBittorrent 完整界面）；点「qB Web 控制台」直接进入下载列表，不需要再输密码。支持磁力链接、最大 2 MiB 的种子文件、任务进度与速度、暂停/继续、文件清单、限速与并发数。
5. 「移除任务」始终保留已下载文件；插件中没有删除文件操作。

## 隔离与生命周期

- 独立容器 `xiaomi-plugin-qbittorrent`，只挂载插件私有配置目录与所选的下载目录。不挂载 Docker socket，不使用 privileged 或 host 网络。
- Python 管理服务需 root/Docker 权限，仅提供固定操作。它不是不受信任插件的沙箱；安装之前应信任发行者及其签名。
- 512 MiB 容器内存上限、1.5 CPU 上限、128 PID、默认两个并发下载。是上限，不是恒定资源占用。
- WebUI 监听 18123 并对局域网开放，可直接用 qBittorrent 官方客户端或网页连接；插件服务绑定 18122，通过现有设备同源入口访问，不写死 NAS IP。
- BT 入站端口固定为 36754（TCP + UDP）并映射到宿主机，其他 peer 才能主动连进来，做种才有上传。要让公网上的 peer 连入，还需要在路由器上把 36754 转发到这台 NAS；插件本身未启用 UPnP/NAT-PMP。
- qB API 的密码、CSRF、Host 校验保持开启；插件本身另要求设备所有者会话及 CSRF。qB 侧的密码只保存为 PBKDF2-SHA512 哈希；安装时输入的密码另存一份在插件私有目录（仅 root 可读），用于会话失效时自动重新登录，免去重复输入。登录 Cookie 持久化在同一目录。
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

## 路由器端口映射（UPnP / NAT-PMP）

BT 要能从外面连上，路由器上得有 `36754`（TCP + UDP）的转发到 NAS。**容器里 qB 自己不做这件事**
（初始化时写死 `PortForwardingEnabled=false`，而且它上报的 internal client 是 Docker 网桥地址，
路由器路由不到），所以由插件**从宿主机**发起，和 transmission 插件是同一套实现：

- 「启动服务」时自动试一次（`ensure_port_forward()`）：先 **UPnP**（SSDP 发现 → 读设备描述 →
  `AddPortMapping`，TCP 与 UDP 各一条），不行再退到 **NAT-PMP**（RFC 6886）。
- **尽力而为**：拿不到映射只记录原因，绝不阻断启动。页面上那一行写清楚卡在哪一步
  （例如 `UPnP 错误 501（Action Failed）`），旁边「映射端口」按钮可随时重试。
- 映射的内网目标固定是 NAS 的局域网地址（按默认路由探测，不会错拿 docker0 的地址）；
  `/api/status` 的 `forward` 段给出 `ok` / `method` / `gateway` / `external` / `mapped` / `detail`。
- **续期**：UPnP 用 `lease=0`（永久映射，不用续）；NAT-PMP 的租期由路由器给定（实测 7200 秒），
  插件在到期一半时重建；失败的话每 30 分钟重试一次。定时器跑在插件服务里，60 秒一轮。
- **停止服务时会把映射删掉**（容器停了那条转发也没人应答，UPnP 又用的是永久映射，不删会一直
  留在路由器上）。删除按记录下来的方式做，映射结果**落盘**在
  `<插件数据目录>/forward.json`——插件服务停止时 systemd 的 `ExecStopPost` 是**另一个进程**，
  只靠内存状态删不掉。存下来的 UPnP 控制地址可能过期（路由器重启会换临时端口），失败会重新
  发现一次再试；「本来就没有这条」（714）不算失败。

> UniFi / UCG-Fiber 实测：UPnP 的 **Secure Mode 开着时 `AddPortMapping` 一律返回
> `501 Action Failed`**（换任何端口都一样，NAT-PMP 也 `result=3`），关掉即通；
> 建议把同一设置页里的 NAT-PMP 一起打开作为兜底。

实现只用标准库（`upnp.py`，与 transmission 插件里是同一份代码，改动请两边同步）。

## 上游与资产

- [qBittorrent](https://www.qbittorrent.org/) / [Web API 文档](https://github.com/qbittorrent/qBittorrent/wiki/WebUI-API-(qBittorrent-5.0))
- [LinuxServer 镜像说明](https://docs.linuxserver.io/images/docker-qbittorrent/)
- 应用图标取自 qBittorrent `release-5.2.3/src/icons/qbittorrent.ico`，只转为 PNG；原 ICO 保存在 licenses，保留其上游 GPL 许可与作者信息。
- 按钮图标复用生态已有 Radix Icons PNG，保留对应许可证。不混淆上游品牌或暗示背书。
