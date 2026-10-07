# Home Assistant（小米智能存储社区插件）

在小米智能存储客户端里用 Docker 运行 **Home Assistant**（开源家庭自动化平台）。

**不是 Home Assistant 或小米的官方插件。**

## 未上架：本机平台暂不支持（2026-10 实测结论）

**代码保留在仓库里，但已从 `scripts/build_apps.py` 的 `PACKAGE_SPECS` 摘除登记、
不在应用商店上架**，因为在这台小米 NAS 上**装不起来**。下面是真机实测的全过程与
结论，留给以后（厂商升级 Docker 之后）回来复验。

### 环境

| 项 | 值 |
| --- | --- |
| dockerd | **20.10.17** |
| runc | **1.1.2** |
| containerd / runc 位置 | `/data/docker/` 下 |
| 同机其它 Docker 插件 | jellyfin / transmission **运行正常** ✓ |

### 两个障础

**① 镜像层是 zstd，Docker 20.10 解不开**

Home Assistant 的 arm64 镜像**从 2026.3.0 起所有层都是**
`application/vnd.oci.image.layer.v1.tar+zstd`，而解包 zstd 需要 **Docker 23.0+**。
拉取到 35 秒后报：

```text
failed to register layer: Error processing tar file(exit status 1): archive/tar: invalid tar header
```

逐个 tag 查过层格式（判定方法见下）：

| tag | 层格式 | 结果 |
| --- | --- | --- |
| `2026.2.0` | gzip | ✓ **最后可用**，715 MB（真机实测拉下来 2.1 GB 解压后） |
| `2026.3.0` 及以后 | zstd | ✗ |
| `stable` | zstd | ✗ |

**② 就算拿到 gzip 版镜像，容器也起不来（runtime 问题，插件侧无解）**

用 `2026.2.0` 成功拉取后，`docker create` 成功（host 网络建容器返回 **201** ✓），
但 `docker start` 失败：

```text
failed to create shim task: OCI runtime create failed: runc create failed:
unable to start container process: can't get final child's PID from pipe: EOF
```

已穷尽排除（全部失败 ✗）：

- 最小配置：`host` 网络 + 一个 bind 挂载、去掉所有硬化参数 ✗
- 覆盖入口为 `/bin/sh`（`--entrypoint /bin/sh`，绕开 HA 的 s6-overlay）✗
- 换更老的镜像 `2025.1.0` ✗
- `--privileged` + `seccomp=unconfined` + `apparmor=unconfined` + `label=disable` ✗
- `--cgroupns=host` ✗
- 镜像架构核对：元数据 `arm64` ✓、镜像内 `/bin/busybox` 的 ELF 头 = **AArch64** ✓
- host 网络本身可用（建容器 201 ✓）、磁盘余量 7.4 GB ✓
- 内核日志里**没有** DENIED / segfault ✓

**结论**：该平台的 container runtime（Docker 20.10.17 + runc 1.1.2）起不了 Home
Assistant 官方镜像，**不是插件能修的**——插件只调 Docker Engine API，runc 这一层
在它之外。真机上已卸载插件、删除容器与两个测试镜像、删除空白配置目录
（**用户目录未动** ✓）。

### 判定用的命令要点（复现用）

```bash
# 1. 层格式：看镜像 manifest 里每层的 mediaType
docker manifest inspect ghcr.io/home-assistant/home-assistant:2026.2.0 \
  | grep -o 'application/vnd[^"]*' | sort -u
#   2026.2.0         → application/vnd.docker.image.rootfs.diff.tar.gzip        ✓
#   2026.3.0 / stable → application/vnd.oci.image.layer.v1.tar+zstd            ✗
# 或者直接看拉取失败时 Docker 的原话（本插件会把原话透传到页面 error 里）
docker pull ghcr.io/home-assistant/home-assistant:stable
#   failed to register layer: … archive/tar: invalid tar header

# 2. runtime 是否真能起：用最小参数启同一个镜像
docker run --rm --network host -v /tmp/ha-test:/config \
  ghcr.io/home-assistant/home-assistant:2026.2.0
#   failed to create shim task: OCI runtime create failed: runc create failed:
#   unable to start container process: can't get final child's PID from pipe: EOF

# 3. 对照：同机 gzip 层镜像与其它插件都正常
docker run --rm hello-world                     # 几秒拉完 ✓
systemctl status xiaomi-jellyfin.service        # 正常 ✓
```

### 什么时候可以把它加回来

两个前提**都**满足时，把 `PACKAGE_SPECS` 里 `homeassistant` 那条加回来即可重新上架
（插件代码、测试、安装脚本都还在，`pluginId 11022` / 服务端口 `18200` /
`releaseRoot=/data/plugin/homeassistant` 都空着没人占）：

1. 厂商把 **Docker 升到 23+**（镜像层格式问题自动消失；插件本来就会按 `/version`
   自动选 stable，不需要改代码）；
2. 该平台的 **runc / containerd 能起 HA 官方镜像**（用上面的 `docker run` 最小用例
   验证一次）。

在此之前，插件在**旧 Docker 上会自动固定到 `2026.2.0`**（见「镜像」一节）——
那部分逻辑已经写好并测过，留着不影响别的插件。

## 功能

- 初始化：选择**配置目录**（可选，留空用插件私有目录）
- 数据持久化：只有一路挂载
  - `/config` — Home Assistant 的数据库、集成配置、自动化、密钥；
    `media`、`share`、`ssl`、`backup` 都是它下面的普通子目录
- **网络模式：`host`**（容器与宿主机共享网络栈，不做端口映射）

  | 用途 | 地址 |
  | --- | --- |
  | Home Assistant 界面 | `http://<NAS 局域网 IP>:8123` |
  | 插件页面自己的服务 | `127.0.0.1:18200`（只回环；经 nginx + 小米客户端证书代理） |

  宿主机的 **8123** 就是容器的 8123，手机 App 与浏览器都连它；自动发现
  （mDNS/SSDP）与来源地址判断因此是完整的。
- 版本显示优先读配置目录里的 `.HA_VERSION`（Home Assistant 自己写的版本号），
  读不到（首次启动、目录被清空）才退回镜像口径。

## 基本配置

1. 安装并打开插件，选择配置目录（或不选，用插件私有目录）后「初始化并启动」
2. 首次启动需拉取官方镜像（`ghcr.io`），可能需要较长时间
3. 打开局域网地址 `http://<NAS-IP>:8123`，完成 Home Assistant 初始化（创建管理员账号）

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

- **修改目录**：弹窗里改**配置目录**，默认「保持当前」，点「选择目录」走同一套
  存储位置选择器（存储池 / 外接设备），另有「用私有目录」，即改回插件私有目录。
  插件按新的宿主路径**重建容器**——bind 挂在创建容器时固定，只 restart 的话旧容器
  会继续挂着旧目录。写新配置前先把 `settings.json` 备份成
  `settings.json.bak-<时间戳>`；重建失败会自动回滚配置并尽力按旧配置恢复容器，
  失败原因原样显示在页面上。只是换位置，**原目录里的文件不会被删除**；新位置若没有
  原来的 Home Assistant 配置，应用会以全新状态启动。
  提交语义：不带 `configPath`＝保持当前配置目录，空串＝插件私有目录，路径＝新位置。
- **重新初始化**：停止并移除容器、把 `settings.json` 挪成带时间戳的备份，
  页面回到初始化表单（`configured` 变 false）。**配置目录里的文件一个都不动**
  （`configuration.yaml`、`.storage/`、`home-assistant_v2.db` 都在原地）；
  本插件没有路由器端口映射之类的副作用，容器就是唯一的系统改动。

两个动作都要确认（`service/reset` 必须带 `{"confirm": true}`），
`busy` 时一律拒绝（`当前有操作正在进行，请稍后再试`）。对应的引擎方法是
`Engine.reconfigure()` 与 `Engine.reset()`，分发仍走 `POST /api/service/<action>`。

## 镜像（按 Docker 版本自动选）

| Docker 版本 | 使用的镜像 | 说明 |
| --- | --- | --- |
| ≥ 23（能解 zstd 层） | `ghcr.io/home-assistant/home-assistant:stable` | 最新版，**首选** |
| < 23（如厂商的 20.10.17） | `ghcr.io/home-assistant/home-assistant:2026.2.0` | 最后一个 gzip 层版本，自动固定 |
| `/version` 查不到 | 先试 `stable` | 拉不动会自动改用 `2026.2.0` |

**为什么需要这套选择**：厂商的 dockerd 是 **20.10.17**，而 Home Assistant 的 arm64 镜像
**从 2026.3.0 起所有层都改成 zstd**（`application/vnd.oci.image.layer.v1.tar+zstd`），
解包 zstd 需要 **Docker 23.0+**。在 20.10 上拉 stable 会在 35 秒后失败：

```text
failed to register layer: Error processing tar file(exit status 1): archive/tar: invalid tar header
```

所以插件在拉镜像前先查一次 `/version`：

- 版本号按**数字元组**比较（`parse_docker_version()` / `compare_versions()`，
  `20.10.17` 与 `23.0.0` 这种用字符串比会出错，`26.1` 与 `26.1.0` 视为相等）；
- ≥ 23 → stable；< 23 → 固定 `2026.2.0`；
- 拉取失败时**自动改用另一个候选重试一次**（stable ↔ 2026.2.0），
  两个都失败才报错，错误里保留 Docker 的原话（截断到 400 字符）；
- 成功后把实际用的镜像记进 `settings.json` 的 `image` 字段，重启后继续用它
  （Docker 没升级就不必再试错一遍）；**等厂商把 Docker 升到 23+，插件会自动回到
  `stable`**，不需要改任何配置。

状态卡片会显示「当前使用镜像 + 为什么是它 + Docker 版本」，接口
`/api/status` 暴露 `image` / `imageVersion` / `imageReason` / `dockerVersion` 四个字段。

**没有写死 digest**：Home Assistant 每个补丁版都会重新推送 `stable`，锁定 digest 反而
会让插件长期停在旧版本上。要固定版本，把 `engine.py` 里的 `IMAGE` / `IMAGE_LEGACY`
换成具体标签即可。

## 隔离

- 不挂载 docker.sock，不使用 privileged
- **使用 host 网络**（有意为之，见下），只挂载所选配置目录一路（→ `/config`），不引入媒体目录
- 资源上限：2 GiB 内存、2 CPU（`MemorySwap` 与 `Memory` 相同＝容器内不给 swap）
  - 资源上限只在**创建容器**时生效（Docker 20.10 的 `/containers/<id>/update` 对
    这类上限不生效），所以插件启动时发现旧容器的上限不对会**停 → 删 → 按新配置重建**；
    `/config` 是 bind 挂载，Home Assistant 的配置与数据不受影响。
- 停止/卸载只停容器，配置保留

## 网络模式是 host（有意为之）

容器用 `NetworkMode: "host"`，**不做端口映射**（`PortBindings`/`ExposedPorts` 在 host
模式下不生效，写上只会让人误以为做了映射）。原因：

- Home Assistant 的**自动发现**（mDNS/SSDP）与**来源地址判断**在桥接下不完整，
  官方文档本身就建议 host 网络。
- NAS 上 8123 实测空闲（没有监听、没有进程），host 模式不会与厂商服务抢端口。

代价与边界：

- 容器与宿主机共享网络栈，隔离性弱于桥接；本插件仍然不挂 docker.sock、不加
  privileged，保留全部资源上限、`no-new-privileges` 与 json-file 日志限额。
- 插件页面自己的服务仍然只在 `127.0.0.1:18200` 上监听（`PORT` 环境变量），
  与 8123 不在一个号段。
- **旧版本插件建的桥接容器会被自动重建**：`Engine.network_stale()` 发现
  `NetworkMode` 不是 `host`（含"老容器没有这个键"）就停 → 删 → 按新配置重建，
  与去掉健康检查走同一条重建路径。

### host 模式下的端口自检

没有「端口发布」可查，所以判断 Home Assistant 有没有真的把界面提供出来，只能看
**宿主机 8123 是否在监听**（`engine.port_is_listening()`，连一次本机回环）：

- 容器本来就跑着（刷新页面 / 插件服务重启）→ 不自检、不碰 Docker。
- 新建容器（首次初始化 / 换目录）→ **不**自检：Home Assistant 首次要装配配置、
  建数据库，可能几分钟才监听，误判成"要重建"只会白折腾；就绪与否由
  `homeassistant_info()`（打 `/manifest.json`）轮询反映。
- 容器本来就是停着、这次只是重新 `start` → 自检，但**先给 60 秒宽限期**
  （`PORT_GRACE_SECONDS`，按 `window / PORT_GRACE_POLL` 次数轮询）。取 60 秒而不是
  20 秒：真机上（机械盘 + 首次初始化 / 刚升级完 / 正在装配集成）Home Assistant 从
  启动到 bind 8123 超过 20 秒并不罕见，宽限期太短会把它误判成"绑定坏了"，白白做一次
  停 → 删 → 重建（重建又要再等一遍启动）。宽限期内一直没监听才**停 → 删 → 重建**
  一次（`_ensure_port(restart=True)`）；重建后仍没监听就如实报错
  （`宿主机 8123 端口没有监听；请检查 8123 是否被其它程序占用`）。

## 容器以 root 运行（有意为之）

Home Assistant 官方容器**不支持以非 root 用户运行**：镜像里的 s6-overlay 与 hass
启动流程都假定 uid 0，上游也明确不提供 PUID/PGID（见
[官方镜像以 root 运行](https://community.home-assistant.io/t/official-docker-image-runs-hass-as-root/956857)、
[不支持非 root 账号](https://community.home-assistant.io/t/wth-its-not-possible-to-use-a-non-root-account-for-docker-image/806208)）。
同族插件（Jellyfin / Emby）会设 `User=<所选目录属主>:<属主组>`，本插件刻意**不设**。

代价与补偿：

- 初始化时仍然要求配置目录属于**非 root 的 NAS 用户**（`st_uid`/`st_gid` 不能为 0），
  否则拒绝初始化。HA 在容器里以 root 写入的文件，在宿主机上看到的属主仍是那个用户，
  小米客户端的文件管理才能正常显示与操作。
- 因此**不要在 Home Assistant 里**手动改 `/config` 的属主为别的用户；那会让宿主机的
  文件管理出现权限不一致。

## 已关闭容器健康检查

官方镜像自带一个 **30 秒一次**的健康检查（打 HTTP 探针）。
每打一次 Home Assistant 都会重写 SQLite 的 `-shm` / `-wal`；文件一改，系统的 findex
索引服务（fanotify）立刻写 `/nas/sys`，而 `/nas/sys` 建在**跨两块盘的 RAID1**（md0）上
——两块机械盘每 30 秒被唤醒一次，永远进不了休眠，每天多出约 1.4 GB/盘的无谓写入
（同类问题的完整定位见 `projects/xiaomi-disk-sleep-plugin/tools/disk-activity-report.py`）。

所以插件在建容器时显式写入 `Healthcheck: {"Test": ["NONE"]}`。

- **旧版本插件建的容器**（已经带着镜像的健康检查）会在插件服务重启、或点
  「启动服务」时**自动重建一次**去掉它。`/config` 是 bind 挂载，数据不受影响，
  只有一次短暂的容器重启。
- 健康检查只能在创建容器时决定：Docker 20.10 的 `POST /containers/<id>/update`
  虽然接受 `Healthcheck` 字段并返回 200，但**实际不生效**，所以只能重建。
- 页面「说明与限制」里写了这一点（状态栏只显示 HA 版本与镜像口径）；
  接口 `/api/status` 的 `healthcheckOff` 字段可以程序化判断。

## 目录身份校验（存储挂载）

启动前会校验配置目录：绝对路径必须与记录一致，`st_dev`/`st_ino` 不一致时，若目录确实
落在某个挂载点下且文件系统是 FUSE（厂商存储池 `fuse.cfs` 每次挂载都会换设备号与
inode 号），就**更新记录**而不是拒绝启动；普通盘上身份变化一律拒绝（`请先检查存储挂载`）。
配置在插件私有目录时不参与这套校验——它不随存储挂载变化。

## 本地预览

```bash
cd projects/xiaomi-homeassistant-plugin
python -m unittest discover -s tests -v
python server.py --dev
```

图标由 `assets/make_icon.py` 生成（只依赖 Pillow，不进插件运行时）：

```bash
python3 assets/make_icon.py     # 重新生成 assets/homeassistant.png
```

## 安装（首次安装的唯一入口）

商店里还没有这个插件，所以第一套装在电脑上跑 `deploy/install-on-nas.sh`
（需要能 SSH 登录 NAS 的私钥）：

```bash
cd projects/xiaomi-homeassistant-plugin
NAS_IP=192.168.1.8 NAS_SSH_KEY=~/.ssh/nas_rsa bash deploy/install-on-nas.sh
```

可选环境变量：`NAS_USER_ID`（不设自动识别）、`PLUGIN_ID`（默认 11022）、
`PLUGIN_PORT`（插件服务端口，默认 18200）、`HA_PORT`（Home Assistant 端口，默认 8123）。

脚本按顺序做这些事（任何一步失败都非零退出并打印原因）：

1. 渲染 `deploy/xiaomi-homeassistant.service`（替换 `__NAS_USER_ID__`）并校验没有残留占位符
2. 上传程序与前端到 `/data/plugin/homeassistant/releases/<版本>-<时间戳>-<pid>/`，
   客户端 UI 到 `/home/<用户>/plugin/homeassistant/src/ui/`
3. 安装 systemd 单元与 nginx 入口（`/etc/nginx/conf.d/luci/xiaomi-homeassistant.conf`），
   切换 `/data/plugin/homeassistant/current` 软链
4. 检查宿主机 `HA_PORT` 是否空闲（host 网络下容器要靠它），被占用会明确警告
5. 写注册表条目（`register_plugin.py`，编号冲突即失败）并补齐客户端插件结构
   （`native_layout.py`：`INFO` + `etc/var/tmp/scripts` + 摘要 `abstract`，缺了开机会被强制卸载）
6. `systemctl daemon-reload` → `enable` → `restart`；`nginx -t` 通过才 `reload`
7. 自检并打印：健康检查、服务状态、插件端口 18200、Home Assistant 端口 8123、
   注册表是否含本插件、`src/` 与 `INFO` 是否就位
8. **占位符检查分两半**：部署产物（渲染后的 systemd 单元、nginx 入口与模板、
   `plugin-meta.json`、`control`、`INFO`、注册表条目）必须**零残留**；而页面文件
   `src/ui/index.html` 反过来必须**保留** `__SESSION_TOKEN__` / `__CSRF_TOKEN__` /
   `__PLUGIN_VERSION__` —— 它们是插件服务端在返回页面时替换的**运行时**占位符
   （与 jellyfin / transmission / 网络邻居一致），安装时替换掉页面就拿不到会话令牌
9. 自检失败会打印失败项汇总并**非零退出**，**不会**再打印「安装完成」

卸载（默认保留 `data/`，用户存储里的配置目录永远不碰）：

```bash
NAS_IP=192.168.1.8 NAS_SSH_KEY=~/.ssh/nas_rsa bash deploy/uninstall-on-nas.sh
# PURGE=1 连 /data/plugin/homeassistant 一起删；DROP_CONTAINER=1 同时删容器
```

## 上游

- [Home Assistant](https://www.home-assistant.io/)
- [容器安装文档](https://www.home-assistant.io/installation/linux#install-home-assistant-container)
- [官方容器镜像仓库](https://github.com/home-assistant/docker)
- 图标为自绘（蓝底 + 白色房子），不复制 Home Assistant 官方标识
