# 小米智能存储插件市场

此分享版本为 0.1.2 测试发布，非小米官方产品。Windows 实体安装尚未验收；115 和阿里云盘均需要使用者自己获批的 App ID，不能省略开发者应用授权。WebDAV 多用户版本在列表中标为测试版。

面向已经通过授权流程开启 root SSH 的小米智能存储设备。用户只需部署一次商店，之后可在小米智能存储 Mac/Android 客户端内安装、更新和卸载社区插件。

## 安全边界

- 商店服务仅监听 `127.0.0.1:18119`，不直接暴露到局域网。
- 所有变更请求都要求商店自己的随机会话和 CSRF 令牌。
- 仓库清单和插件包使用 ECDSA P-256 分离签名，并额外校验 SHA-256。
- 插件包是声明式 `bundle-v1`，不允许携带或执行安装 shell。
- 解包时拒绝绝对路径、`..`、符号链接、设备文件和超限文件。
- 安装采用暂存、备份、`nginx -t`、服务健康检查、最后写注册表的顺序。
- 失败自动回滚；卸载默认保留插件数据，清除数据必须单独确认。

## 普通用户流程

1. 使用受信的一键脚本为自己的 NAS 开启密钥 SSH。
2. 下载商店发布包及 `SHA256SUMS.txt`，核对校验值。
3. Windows 用户双击 `install-windows.cmd`；Mac 可使用 `install-macos.command`；Mac/Linux 用户也可在终端运行 `bash install.sh`。安装器会询问 NAS 当前 IP、自动寻找常见位置的 root SSH 密钥，并尽量从小米客户端证书识别当前账号。
4. 重新打开小米智能存储客户端，在“基础应用”中打开“插件市场”。
5. 从小米客户端打开后会自动建立设备会话。后续安装和更新均在商店页面完成，不需要管理码，也不再需要输入 NAS IP。

Windows 小白用户可以直接阅读发布包内的 `WINDOWS-安装说明.txt`。分享时只需提供正式 ZIP 与对应的 `SHA256SUMS.txt`，不要附带任何个人 SSH 私钥、客户端证书或 NAS 会话令牌。

商店运行在 NAS 内部，因此 NAS 的 DHCP 地址变化不会影响插件页面。
商店使用每台 NAS 独立的内部密钥签发 30 天设备会话；密钥不会显示给用户，NAS 重启不会让会话失效。

应用入口使用 `community-store-v4.png`：蓝色下载符号铺满图标画布，四角为真实透明像素，针对手机客户端的小尺寸显示移除了旧版白色外圈。

安装器默认复用 root 脚本生成的 `~/.xiaomi-nas-root/nas-root-key`。密钥或用户注册表不在默认位置时，才需要设置 `NAS_SSH_KEY` 或 `NAS_USER_ID`。

无人值守安装也可以直接传参：

```bash
NAS_IP=192.168.31.100 NAS_SSH_KEY=/path/to/nas-root-key NAS_USER_ID=u123456 bash install.sh
```

Windows PowerShell 无人值守安装：

```powershell
.\install-windows.ps1 -NasIp 192.168.31.100 -NasSshKey C:\path\to\nas-root-key -NasUserId u123456
```

## 开机钩子（deploy/community-plugins-boot.sh）

小米 NAS 的根文件系统是只读 erofs、`/etc` 是 overlay，systemd 在 overlay 挂载前就
读完了单元目录，所以安装时新增到 `/etc/systemd/system/` 的服务开机不会被拉起。
`deploy/install-boot-hook.sh add` 把 `deploy/community-plugins-boot.sh` 装到
`/data/plugin/community-plugins-boot.sh`，并在 root crontab 里加一条**每分钟**的任务
补启动这些服务（目标都在跑时立即退出，几乎无开销；只在开机后
`BOOT_WINDOW`（默认 600 秒）内动手，不会干扰管理员手动停掉的服务）。

同一支脚本顺带维护几件**全设备共用**的 Docker 修复，都幂等、都排在「拉起插件服务」
之前，免得和插件的启动流程互相干扰：

1. **iptables NAT 的 MASQUERADE 规则**：开机早期 `iptables-restore` 会用空规则清链，
   把 Docker 的 MASQUERADE/DOCKER 链一并抹掉，结果所有容器都出不了网；
2. **发布端口的 DNAT 规则**：本机把 dockerd 配成托管 iptables 后（UCI
   `mi_docker.globals.iptables=1`），外部流量走内核 DNAT，不再经过用户态 docker-proxy
   （省掉每连接 2 个 fd 与两次用户态拷贝）；回环流量仍由 docker-proxy 兜着，所以
   `127.0.0.1:<端口>` 不受影响。脚本用「正在运行且发布了端口的容器，是否有对应 DNAT
   规则」作判据，缺了就重启 docker 修（重启会自动重建规则）；
3. **dockerd 的 fd 软上限**：systemd 默认软上限只有 1024，而发布端口退回用户态代理时
   一条连接占 2 个 fd，BT 把 peer 上限开大就会撞穿（`Accept()` 报 EMFILE，代理进程正常
   退出、宿主端口消失）。脚本发现软上限低于 `DOCKER_FD_MIN`（默认 65536）就写
   `/etc/systemd/system/docker.service.d/override.conf`。

修复动作（重启 dockerd）会停掉所有容器，因此：

- 位置排在「拉起插件服务」**之前**，并且先等 docker 就绪（`DOCKER_READY_WAIT`，默认
  90 秒）——插件服务一起来就要启动自己的容器，docker 没就绪会让它失败一次；
- 有插件服务在 activating、或容器在 restarting 时推迟到下一分钟；
- 失败/推迟后 `DOCKER_REPAIR_RETRY` 秒内不重试，但**开机窗口内忽略这个冷却**：
  systemd 在 `/etc` overlay 挂载前就读完了单元目录，所以开机时看不到那个 drop-in，
  dockerd 会带着旧上限起来，这种情况必须立刻修；
- 重启前记下在跑的容器、重启后按原样拉起，并**重启对应的插件服务**让它重新同步
  （容器名 `xiaomi-plugin-xxx` ↔ 服务名 `xiaomi-xxx.service`）——插件不知道容器被换过，
  路由器端口映射这类状态不会自己回来；
- 另外每分钟把「当前在跑的容器」记到 `/data/plugin/.docker-last-running`，开机后按这份
  清单兜底恢复：容器 `RestartPolicy` 是 `no`，重启后不会自己回来，而插件服务只启动自己的
  HTTP 服务、不一定去启动容器（例如 Jellyfin）。

市场服务启动时会比对 `/data/plugin/community-plugins-boot.sh` 与发行包里这份副本，
哈希不一致就重装一次（`server.py` 的 `sync_boot_hook()`）——市场升级只替换发行目录、
不会动那个副本，所以钩子里新增的修复要靠这一步才能第一时间生效。

手动重装：`sh deploy/install-boot-hook.sh add`；卸载：`sh deploy/install-boot-hook.sh remove`。
脚本日志在 `/data/plugin/community-plugins-boot.log`。

## 开发与验证

内置仓库包含设备管家、115 云备份、阿里云盘备份、WebDAV 文件桥和 qB 下载。WebDAV 文件桥自带经过官方 SHA-256 校验的 rclone ARM64 引擎，安装后共享默认关闭，远程连接需由用户填写自己的 WebDAV 账号。它不修改小米原有 5000 端口服务。

qB 下载为开发候选版，需要 Docker。首次由用户选择目录和密码后拉取锁定的 LinuxServer 镜像；尚未完成真实 NAS 下载或客户端验收。卸载插件仅停止它自己的容器，保留下载和配置。发行包不包含镜像本体。

```bash
python3 scripts/build_repository.py \
  --signing-key "$HOME/.local/share/xiaomi-community-store/signing-key.pem" \
  --create-key
python3 -m unittest discover -s tests -v
python3 server.py --dev
```

开发预览默认打开 `http://127.0.0.1:18119/`。`--dev` 只用于本机预览，不启用安装、更新或卸载动作。

## 发布原则

不要传播 `curl URL | sh`。正式发布应提供固定版本 ZIP、校验和、签名公钥指纹和可审阅源码。私钥只保存在离线发布机，不进入项目目录、ZIP 或 NAS。

自定义仓库不是“粘贴一个 URL 就信任”。用户必须同时导入该仓库公钥指纹；仓库换钥时需要再次人工确认。
