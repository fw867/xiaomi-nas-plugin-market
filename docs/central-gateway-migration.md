# 米家中枢网关移植方案：小米智能存储 RP05 → 飞牛 fnOS（Intel N150 / x86_64）

> **本文与 [nas-root-recovery-2026-10.md](nas-root-recovery-2026-10.md) 的关系** 🟢：
> **同属本次固件 / 迁移研究系列**。主报告记录"固件升级导致 SSH 与第三方应用丢失"的排查、
> 安全观察与恢复流程；**本文是该系列中"中枢网关"这一独立成果的延伸** ——
> 主报告 **§5.7 讲"怎么把网关镜像取回并完整拆解"**，**本文讲"把它搬到另一台机器上的方案与边界"**。
> 两文共用同一批证据（镜像、证书、宿主组件、只读共享能力），
> 主报告 **§7.4「相关文档」已把本文列入索引**。
>
> **文档性质**：**纯研究 / 纯文档**。本文**不要求改动任何设备**，也**不包含任何绕过鉴权的做法**。
> 全部内容面向**"迁移自己拥有的这一台设备"**这一正当用途。
>
> **诚实前提（请先读这一句）** 🔴：
> 这台 NAS 上的"中枢网关"**不是**一个能独立搬走的软件包 ——
> 它是 **"容器镜像 + 宿主中介 + 云端设备身份"三件套**，而**第三件（设备身份）不可复制**。
> 因此本文结论是：**容器与宿主中介可以搬（并已在本文给出完整步骤）；
> 但"整机下线、由 N150 完全顶替"在软件层面不可行**，需要小米侧的支持。
> 下面按"能搬的部分 / 不能搬的部分 / 怎么验证"逐层给命令与判据。

---

## 0. 结论速览

| 项 | 可行性 | 说明 |
| --- | --- | --- |
| **容器镜像搬到 N150 并跑起来** | 🟢 **可以**（需 QEMU 用户态模拟）| 镜像 `arm/v7` 32 位；N150 是 `x86_64` ⇒ `docker run --platform linux/arm/v7` + binfmt |
| **把网关的"功能"在 N150 上跑通自检** | 🟡 **很可能可以**（能启动、能进自检态）| 容器自带全部业务服务（`central_service`/`lan_service`/`bt_service` 等）|
| **宿主中介搬到 N150** | 🟡 **可以，但要 QEMU aarch64 chroot**（依赖只有 libc）| `miotcentralctld` / `central_proxy_cli` 都是 aarch64 闭源二进制 |
| **让米家云把这个"新机"认成同一个网关** | 🔴 **不可以（本方案无法做到）** | 设备身份不是文件，而是**云端按 SN/MAC/did 绑定**的；见 §4 |
| **整机下线、N150 完全顶替** | 🔴 **不可行** | 需要小米官方"设备迁移/换机"支持；本文给出申请话术与前置条件 |

**最关键 3 个前置条件**（缺一即不可做整机下线）：
1. 🔴 **必须先拿到小米官方的"中枢网关迁移/换机"支持**（否则云端不会认新机；§4、§5.3）；
2. 🔴 **必须先让原机保持在线**（原机的设备身份与云连接是唯一"活的"那份，关掉就没了；§5.1）；
3. 🟡 **必须先在 N150 上完成容器 + 宿主中介的"离线自检"**（证明镜像与中介能在 x86_64 上跑起来；§3、§6）。

---

## 1. 资产盘点与迁移清单

### 1.1 我们手上已有的资产（🟢 已取回，纯只读）

| 资产 | 位置 | 大小 | 说明 |
| --- | --- | --- | --- |
| **网关容器镜像** | `artifacts/scratch/central-image/default_image.tar.xz` | 7 922 044 B | XZ；解压后是 **OCI 归档** tar（31 548 928 B）|
| ↳ 解开的 OCI 布局 | `artifacts/scratch/central-image/extracted/` | 62 MB | `oci-layout` + `index.json` + `manifest.json` + `repositories` + `blobs/sha256/*` |
| ↳ **镜像根文件系统** | `artifacts/scratch/central-image/rootfs/` | 31 MB / 2472 文件 | 直接可用（`arm/v7`）|
| 版本元数据 | `artifacts/scratch/central-image/central.version` | 173 B | `{"model":"RP05","version":"1.0.0.26","time":"2026-04-20 21:42:54","compiler":"minas team","desc":"central firmware","channel":"release"}` |
| 包签名 | `artifacts/scratch/central-image/central.sign` | 256 B | RSA-2048（设备侧会验签 ⇒ **改包无效**）|
| **设备 mTLS 凭据** | `artifacts/scratch/central-image/device-certs/{device.crt,device.key,ca.crt}` | 729 / 241 / 1157 B | 见 §4（🔴 **路径可复制，但身份不可复制**）|
| **宿主中介二进制** | WSL `/root/rootfs/usr/bin/{miotcentralctld,miotcentralctl,central_proxy_cli}`、`/root/rootfs/usr/lib/libcentral_proxy.so.0.0.1` | 67 496 / 67 472 / 133 952 / 132 976 B | **aarch64**，见 §3 |
| 宿主配置 | `/root/rootfs/etc/central_software_pack/platform.conf` | 272 B | 见 §1.4 |
| 启动脚本 | `/root/rootfs/usr/bin/{central_software_pack.sh,miot_centralctld_hook.sh}` | — | 已全文分析 |
| systemd 单元 | `/root/rootfs/etc/systemd/system/miotcentralctrld.service` | — | 见 §3.1 |

> 🟢 **这些都是"只读"得来的**：镜像与配置来自**已下载的固件包**（离线解包），
> 证书与运行态数据来自**一次只读的共享访问**（见 §1.3）。**全过程未修改设备任何状态。**

### 1.2 运行态数据清单（在设备上，需要时用"只读共享"取出）

设备上中枢网关的数据根目录是 **`/data/other/central`**（= 容器内的 `/data`）。🟢 已实测其结构：

```
/data/other/central/
├── default_image.tar        31 548 928 B   ← 上面那个 OCI tar 的**未压缩**版（我们已有 .xz，无需再取）
├── local/
│   └── central_service/     ← ★ 设备身份（挂进容器的 /data/local/central_service）
│       ├── device.crt        729 B
│       ├── device.key        241 B
│       └── ca.crt           1157 B
├── share/
└── log/
```

🟡 **还需进一步盘点（尚未读取）**：`share/` 与 `log/` 的内容；
以及容器运行时可能写入的其它 `/data/**`（例如网关的设备列表、自动化规则）。
**这些是否包含"设备身份之外的状态"（配对过的子设备、自动化配置），决定了"迁移后要不要重新配对"** 🔴
（见 §4.4 与 §6.3）。

### 1.3 ★ 怎么把这些"读出来"：共享只读方式（🟢 已实测的边界）

我们已实测一条**只读**通道：**给任意目录加 Samba 共享**，然后用 `smbclient` 读取。

**能力边界（🟢 实测）**：
- ✅ **可以读**：`/data/other/central/**`（含证书）、`/data/central/**`、`/data/etc/upper/**`（活的 `/etc`）
- ❌ **不可以写**：所有目标目录都是 **root 所有**，共享会话的 `force user` 是池主 ⇒ **一律 `ACCESS_DENIED`**

**取数据的确切命令**（示例：取运行态目录）

```bash
# ① 建立只读共享（HTTPS + 客户端证书 + 服务器 CN 作 SNI）
#    POST /cgi-bin/luci/netshare/add_samba_account
#    {"username":"<任意非 nobody 名>","password":"<临时密码>","share_dir":["/data/other/central"]}
#    ⚠️ 共享名 = 目录 basename + "-" + <uid 数字部分>，例如 central-3943892
#    ⚠️ Samba 账号 id 槽位只有 1/2；id=2 被占用时所有 add 都会返回 2505（假故障）

# ② 读取（只读；不要用 put/rm）
smbclient '//192.168.1.8/central-3943892' -U '<用户名>%<临时密码>' -m SMB3 -c 'ls'
smbclient '//192.168.1.8/central-3943892' -U '<用户名>%<临时密码>' -m SMB3 \
  -c 'get local\central_service\device.crt /tmp/device.crt'

# ③ 用完立刻删除该临时账号，恢复出厂基线
#    POST /cgi-bin/luci/netshare/del_samba_account {"username":"<名>","id":"2"}
```

> ⚠️ **纪律边界（必须遵守）**：**绝对不要**对该共享执行 `put` / `del` / `mkdir`
> —— 实测全部 `ACCESS_DENIED`，而且**尝试写入本身就是在动设备**。
> 本文所有取值动作**只允许 `ls` / `get`**。**不要**去读用户数据目录（照片/视频/下载/共享/相册/备份）。

### 1.4 宿主配置（🟢 全文，直接可搬 —— 不含任何凭据）

```ini
# /etc/central_software_pack/platform.conf
ro.sys.name=NAS
ro.sys.model=xiaomi.station.rp05
ro.sys.vendor=xiaomi
ro.sys.priority=144.0.0
ro.sys.rsyncport=873
ro.sys.mipsport=8884
ro.sys.webcfgport=8086
ro.sys.mqttport=8883
ro.sys.veventsiid=7
ro.sys.phylist=[{"name":"enu1u3","type":"ethernet"}]
ro.sys.loopback=[]
```

🟢 **重要澄清**：用户提到的 "8893" 在配置里**不存在**；实际端口是
**873 (rsync) / 8884 (mips) / 8086 (webcfg) / 8883 (mqtt)**。
🟡 `ro.sys.phylist` 里的 **`enu1u3`** 是本机网卡名 —— 迁到 N150 时**必须改成对方实际的网卡名**
（否则网关的局域网发现/组播会绑错网卡）。

---

## 2. 在 x86_64 上跑 arm/v7 容器

### 2.1 为什么不能直接 `docker load`

🟢 我们取回的是 **OCI 归档**（`oci-layout` + `index.json` + `manifest.json` + `repositories` + `blobs/`），
**不是** `docker save` 的镜像 tar。`docker load` **通常不认** OCI layout。

**正确做法（二选一）**

**方案 A：用 `skopeo` 转成 `docker-archive`（推荐）**
```bash
# 在 N150 上（先把 extracted/ 目录拷过去）
skopeo copy \
  oci:/path/to/extracted:micr.cloud.mioffice.cn/central_software_pack/central_software_pack:v0.10.8 \
  docker-archive:/path/to/central-docker.tar

docker load -i /path/to/central-docker.tar
# 期望看到：
#   Loaded image: micr.cloud.mioffice.cn/central_software_pack/central_software_pack:v0.10.8
```
> 🟡 `oci:` 后面的引用名要与 `index.json` 里的
> `io.containerd.image.name` / `org.opencontainers.image.ref.name`（= `v0.10.8`）一致。
> 若 `skopeo` 报找不到引用，用 `skopeo inspect oci:/path/to/extracted` 先列出可用引用。

**方案 B：直接用 `docker import` 从层 tar 造镜像（跳过 manifest）**
```bash
docker import \
  /path/to/extracted/blobs/sha256/778b1d0c60c63f0508e5cfde6ec945e5a376c5293bc1e603b35cfd4ca43b6b2a \
  central-gateway:v0.10.8
```
> ⚠️ 方案 B 会**丢掉原始 manifest 的 `architecture` 标注**，需要手动 `--platform`。
> 🟢 更好的是：**我们已经有解开的 rootfs**（`artifacts/scratch/central-image/rootfs/`），
> 也可以在 N150 上用 `tar -C rootfs -c . | docker import - central-gateway:v0.10.8`。

### 2.2 让 x86_64 能跑 arm/v7：binfmt + QEMU

**三种环境，任选**

| 环境 | 要做什么 |
| --- | --- |
| **fnOS 原生 Docker** 🟢 最直接 | `docker run --privileged --rm tonistiigi/binfmt --install arm`（注册 arm/v7 的 binfmt handler），或安装发行版包 `qemu-user-static` + `binfmt-support` 后 `systemctl restart systemd-binfmt` |
| **WSL2** | 同上（`qemu-user-static` 在 Ubuntu 仓库里有；WSL2 内核支持 binfmt_misc）|
| **Docker Desktop** | 默认已内置 QEMU 模拟，直接 `--platform linux/arm/v7` 即可 |

**验证 binfmt 是否就绪**
```bash
# 应能看到 arm 的条目
ls /proc/sys/fs/binfmt_misc/ | grep -i arm
# 快速自测：跑一个 arm/v7 的 busybox
docker run --rm --platform linux/arm/v7 arm32v7/busybox uname -m
# 期望输出: armv7l
```

### 2.3 依 `central_software_pack.sh` 的真实参数启动容器

🟢 原机启动命令（来自 `/usr/bin/central_software_pack.sh`，可直接参照）：

```bash
CONTAINER_NAME=miot_central
IMAGE_NAME=micr.cloud.mioffice.cn/central_software_pack/central_software_pack
EXTERNAL_SOCK_DIR=/tmp/miot_central
CONTAINER_DATA_DIR=/data/other/central
SHARED_TMP_DIR=/tmp/shared_container_tmp
EXTRA_CONTAINER_OPTIONS="--memory=256m --cpus=1"
# 原机：docker run -d --network=host -v ... --name miot_central "$image_id" busybox init
```

**在 N150 上的等价启动（含 arm/v7 模拟与中介挂载）**

```bash
# 0) 准备目录
sudo mkdir -p /opt/central/{external,data,platform,mdnsd,shared_tmp}
#    external   ← 容器内 /tmp/external（与宿主中介的 UDS 通道）
#    data       ← 容器内 /data（运行态；把原机 /data/other/central 的内容放这里）
#    platform   ← 放 platform.conf
#    mdnsd      ← 容器内 /var/run/mdnsd
#    shared_tmp ← 容器内 /tmp

# 1) 放 platform.conf（记得把 phylist 改成 N150 的实际网卡名！）
ip -o link show | awk -F': ' '{print $2}'      # 先查网卡名，例如 enp1s0
sudo cp artifacts/scratch/central-image/../.../platform.conf /opt/central/platform/platform.conf
sudo sed -i 's/"enu1u3"/"enp1s0"/' /opt/central/platform/platform.conf   # ← 按实际网卡名改

# 2) 放运行态数据（从原机只读共享取回的 /data/other/central/**）
sudo cp -a <取回的 central 数据>/* /opt/central/data/

# 3) 启动（--platform 必须显式指定；--network=host 与原机一致）
docker run -d \
  --name miot_central \
  --platform linux/arm/v7 \
  --network=host \
  --memory=256m --cpus=1 \
  -v /opt/central/external:/tmp/external \
  -v /opt/central/data:/data \
  -v /opt/central/platform/platform.conf:/etc/platform.conf:ro \
  -v /opt/central/mdnsd:/var/run/mdnsd \
  -v /opt/central/shared_tmp:/tmp \
  --privileged \
  central-gateway:v0.10.8 \
  busybox init
```

> 🔴 **注意 `--privileged` 与 `--network=host`**：
> 网关需要蓝牙（`bt_service`）、组播发现（`lan_service`）、mDNS。
> 在 N150 上**没有蓝牙适配器** ⇒ **`bt_service` 必定起不来**（见 §3.4）。
> 组播/发现**可以**工作（N150 有以太网）。
> ⚠️ **先不要加 `--privileged`**，用最小权限试；只有确认需要时才加。

### 2.4 性能评估（Intel N150 + QEMU 用户态）🟡

| 维度 | 判断 |
| --- | --- |
| **CPU** | N150 = 4× Gracemont（E-core，无超线程）。**QEMU 用户态模拟 arm/v7 大约损失 3–8×**，即等效 ~0.5–1.3 个现代核 |
| **网关负载** | 🟡 中枢网关的负载**主要由"事件驱动 + 少量设备轮询"构成**，稳态 CPU 占用通常**很低**（原机限制是 `--cpus=1 --memory=256m`，说明官方也认为它是轻负载）|
| **结论** | 🟡 **稳态很可能够用**；但**设备发现/批量状态同步/OTA 等突发场景会明显变慢**。**必须实测**（§6.4 给了压测方法）|
| **内存** | 原机给 256 MB ⇒ N150 上给 **512 MB 更稳**（QEMU 有额外开销）|
| **⚠️ 最大不确定性** | QEMU 用户态对 **`ioctl` / 原始套接字 / 蓝牙 HCI** 的模拟**不完整** ⇒ 蓝牙类功能**基本不可能**，组播**可能**有问题 |

---

## 3. ★ 宿主中介怎么办（本方案的关键难点）

### 3.1 中介是什么、要做什么（🟢 静态已理清）

```
┌─ 容器 miot_central (arm/v7) ────────────────┐
│  mosquitto 8883 (mTLS, require_certificate) │
│  mosquitto 8884 (listen localhost)          │
│  /tmp/external/centralCtl.sock  ←───────────┼── UDS ──┐
│  /tmp/external/central_proxy.sock           │         │
│  /var/run/mdnsd  ←──────────────────────────┼──────┐  │
└─────────────────────────────────────────────┘      │  │
                                                     │  │
┌─ 宿主中介 (aarch64) ────────────────────────────────┼──┘
│  miotcentralctrld.service: User=root                │
│    ExecStart=miotcentralctld -c /tmp/miot_central/centralCtl.sock
│    After=/Requires=miio_client.service              │
│  central_proxy_cli -a <sock> -c -n <method>         │
│  libcentral_proxy.so  ← 代理库（提供 UDS RPC）       │
│  mips 桥 8884 ⇄ miio_client（米家云会话）           │
└─────────────────────────────────────────────────────┘
```

🟢 **中介的最小依赖（实测 `readelf -d`）**：
| 二进制 | NEEDED |
| --- | --- |
| `miotcentralctld` | **仅 `libc.so.6`** |
| `miotcentralctl` | **仅 `libc.so.6`** |
| `central_proxy_cli` | `libpthread.so.0`、`libc.so.6`、`ld-linux-aarch64.so.1` |
| `libcentral_proxy.so.0.0.1` | `libc.so.6`、`ld-linux-aarch64.so.1` |

⇒ 🟢 **依赖极简** ⇒ **`qemu-aarch64-static` + chroot 是完全可行的**（路线 (a) 代价很低）。

### 3.2 路线 (a)：在 N150 上用 `qemu-aarch64-static` + chroot 跑中介 🟡 **推荐先试这条**

**步骤**

```bash
# 1) 装 qemu 用户态
sudo apt install -y qemu-user-static binfmt-support

# 2) 建一个 aarch64 chroot（用固件里的真实 rootfs，保证 glibc 版本匹配）
sudo mkdir -p /opt/central-arm64
#   🟢 用我们已解包的固件 rootfs（最稳）：把最小必要集合拷进去
sudo cp -a <rootfs>/lib      /opt/central-arm64/
sudo cp -a <rootfs>/usr/lib  /opt/central-arm64/usr/
sudo cp -a <rootfs>/usr/bin/{miotcentralctld,miotcentralctl,central_proxy_cli} /opt/central-arm64/usr/bin/
sudo cp -a <rootfs>/usr/lib/libcentral_proxy.so.0.0.1 /opt/central-arm64/usr/lib/
sudo cp /usr/bin/qemu-aarch64-static /opt/central-arm64/usr/bin/

# 3) 验证能跑（先跑 --help/usage）
sudo chroot /opt/central-arm64 /usr/bin/qemu-aarch64-static /usr/bin/miotcentralctl
#   期望：打印 usage（不是 "cannot execute binary file"）

# 4) 关键点：把容器看到的中介路径桥接过去
#   容器内 /tmp/external 需要与中介看到的 /tmp/miot_central 是**同一个目录**
sudo mkdir -p /opt/central-arm64/tmp/miot_central
sudo mount --bind /opt/central/external /opt/central-arm64/tmp/miot_central
#   ⚠️ 每次重启后要重新 bind（建议写成 systemd unit 的 ExecStartPre）
```

**代价与风险**
- 🟢 依赖只有 libc ⇒ 拷贝量小、成功率高
- 🟡 **要解决"路径不一致"**：容器内是 `/tmp/external`，中介的 unit 里写死 `/tmp/miot_central`
  ⇒ 用 `--bind` 或 `symlink` 对齐
- 🔴 **`miotcentralctrld.service` 里有 `Requires=miio_client.service`**
  ⇒ **中介本身依赖 `miio_client`**，而 `miio_client` **才是真正持有米家云会话（设备身份）的那一层**
  ⇒ **即使中介跑起来，缺 `miio_client` 也连不上云**（这是本方案的核心断点，见 §4）

**要拷哪些 `.so`、怎么定位**（通用方法）
```bash
# 对任一 aarch64 二进制，列出它需要的库
readelf -d <binary> | grep NEEDED
# 对需要的每个库，找到它的真实路径（含符号链接链）
ldd <binary>          # 在 aarch64 环境里跑；或手工在 rootfs 里 find
find <rootfs> -name 'libc.so.6' -o -name 'libpthread.so.0'
```

### 3.3 路线 (b)：只桩掉不需要的部分 🟡 **推荐与 (a) 并行评估**

**必须有的**（缺了功能就不完整）：
| 组件 | 为什么要 | 能否自写 |
| --- | --- | --- |
| **米家云 MiIO 会话** | 网关的"在线"完全依赖它；**设备身份在这一层** | 🔴 **不能**（见 §4）|
| `/tmp/external` UDS 协议 | 容器 ↔ 中介的唯一控制通道 | 🟡 可以逆（见 (c)）|
| `mdnsd` | 局域网内 App/设备发现网关 | 🟢 可以用标准 `avahi-daemon` 替代（N150 上现成）|
| `platform.conf` | 设备身份/端口声明 | 🟢 已有，直接给 |
| 网卡（组播） | 设备发现 | 🟢 N150 自带 |

**可以不做的**：
| 组件 | 原因 |
| --- | --- |
| **蓝牙（`bt_service`）** | N150 无蓝牙适配器；且 QEMU 用户态对 HCI 模拟不完整 ⇒ **直接放弃** |
| `rsync 873` | 仅内部同步用，迁移初期不需要 |
| `webcfg 8086` | 极客版配置页；可后续再补 |
| OTA（`miio_ota`） | 🔴 **不要搬**：OTA 会去拉官方包并**可能重刷/覆盖** —— 迁移期间**应禁掉** |
| `fds_backup_service` | 云备份；非核心 |

### 3.4 路线 (c)：逆出协议后自写轻量中介 🟡 **可行性中等，工作量大**

🟢 **已有的抓手**：
- `central_proxy_cli -a <sock> -c -n <method>` ⇒ **方法名是字符串**，
  我们已经知道一个：**`autoWebConfig/getCentralLinkInfo`**
- `libcentral_proxy.so` 里有 **`%s, peer did: %lu`** ⇒ 协议里有 **`did` 字段**（设备标识）
- 协议载体是 **Unix domain socket**（`/tmp/miot_central/central_proxy.sock`）

**要做的逆向工作**
```bash
# 1) 抓协议：在**自己的**设备上，让 cli 与中介通信，用 strace/ltrace 记录 socket 读写
strace -f -e trace=network,read,write -s 4096 \
  central_proxy_cli -a /tmp/miot_central/central_proxy.sock -c -n autoWebConfig/getCentralLinkInfo

# 2) 静态：把方法表挖出来（字符串交叉引用）
llvm-objdump-18 -d /path/to/central_proxy_cli | less
strings -a /path/to/libcentral_proxy.so.0.0.1 | sort -u

# 3) 归纳：请求/响应是 JSON 还是 TLV？先看 cli 打印的原始字节
```
**工作量估计** 🟡：
- 若能抓到明文 JSON ⇒ **1–3 天**可写一个能应答 `getCentralLinkInfo` 等少量方法的桩
- 若要**完整实现**中介（设备管理、自动化下发、OTA 触发…）⇒ **数周到数月**，且**每个固件版本都要跟**
- 🔴 **但即使写完，也解决不了"云端设备身份"这个根本问题**（§4）

---

## 4. 设备身份与绑定（诚实评估）🔴 **这是本方案的硬边界**

### 4.1 我们已知的设备身份是什么

🟢 **实测拿到的 mTLS 凭据**（在 `/data/other/central/local/central_service/`）：

| 文件 | 要点 |
| --- | --- |
| `device.crt` | subject = `C=CN, O=Mijia Device, CN=mips.3943892.786f7468cfb0e7390dc4a7085a35e7814d4f471a.14` |
| | issuer = `C=CN, O=MIOT CENTRAL GATEWAY`；有效期 `2026-09-19` → **`2056-09-11`** |
| | sha256 指纹 `F9:A7:94:…:E5:82` |
| `ca.crt` | subject = `O=MIOT CENTRAL GATEWAY, C=CN`；issuer = **`O=Mijia Root, C=CN`** |
| `device.key` | **PKCS#8 EC 私钥（P-256）**，241 B |

🟢 另外 `platform.conf` 声明了设备身份字段：`ro.sys.model=xiaomi.station.rp05`、`ro.sys.vendor=xiaomi`。

### 4.2 证书"能不能搬过去用"—— 分三层看

| 层次 | 能否复制 | 说明 |
| --- | --- | --- |
| **文件本身** | 🟢 **能**：`device.crt` / `device.key` / `ca.crt` 都是普通文件，**可以拷到 N150** | 我们已经在本地有一份（`artifacts/scratch/central-image/device-certs/`）|
| **证书里的 CN** | 🔴 **绑死在 NAS 的 did**：`mips.3943892.<hash>.14` | `<hash>` 是设备身份的派生值，`.3943892` 就是你的 NAS 用户/设备号 |
| **云端认可的"N127 是这台网关"** | 🔴 **做不到** | 米家云按 **SN / MAC / did** 绑定；见下 |

### 4.3 为什么"换硬件还认同一身份"做不到（🔴）

1. **中枢网关的"在线"不是容器自己说的** —— 它依赖宿主 **`miio_client`** 建立的**米家云 MiIO 会话**；
   而这个会话用的是**设备级身份**（SN `68894/P5WP11456`、MAC `D4:53:2A:C7:1A:B1`、`did`）。
2. **图中没有任何"可搬走的身份文件"** —— 我们**全盘搜过**：
   固件里**没有**任何脚本/配置读取或生成 `/data/local/central_service/*`（0 命中）。
   这说明凭据是**由 `miio_client` / 云端在运行期协商生成的**，而不是一份可拷贝的配置文件。
3. **可能的硬件绑定** 🔴：
   - 本机有 **RPMB**（`mitee_tool rpmb get downgrade_en` 曾被用来判断降级许可 ⇒ 说明 RPMB 在用）
   - A/B 槽与防回滚走 **U-Boot env in eMMC boot0/boot1**
   - 🟡 **我们没能证明**证书的密钥材料是否与 RPMB/安全启动绑定（**未验证**，不要当成结论）
   - 🔴 **但可以确定的是**：即使密钥不绑硬件，**云端仍按 SN/MAC/did 认定设备**，
     而 N150 **没有** RP05 的 SN/MAC ⇒ **云端不会把它当成同一台网关**
4. **证书有效期到 2056** ⇒ 🟡 说明米家把它当**长期设备凭据**，
   这类凭据**几乎肯定**伴随**服务端侧的设备注册记录**（不是自签就有效）。

### 4.4 需要从设备额外导出哪些标识（如走"官方换机"流程）

🟡 **先只读盘点、不要动设备**。建议向小米申请时提供/确认以下项
（**其中前 3 项我们已知，后几项需要时再用只读共享盘点**）：

| 标识 | 我们是否已知 | 来源 |
| --- | --- | --- |
| 设备型号 | 🟢 已知 | `xiaomi.station.rp05` |
| SN | 🟢 已知 | `68894/P5WP11456` |
| MAC | 🟢 已知 | `D4:53:2A:C7:1A:B1` |
| 用户 uid / did | 🟢 部分 | cert CN 里的 `3943892` / `<hash>` |
| 固件版本 | 🟢 已知 | `1.0.9.462.F24C26D24-REL` |
| 网关自身版本 | 🟢 已知 | `central.version` = `1.0.0.26` |
| **已配对的子设备清单** | 🟡 **待盘点** | 容器 `/data/**`（我们尚未读 `share/`、`log/`）|
| **自动化/场景规则** | 🟡 **待盘点** | 同上 |
| **云端设备注册记录** | 🔴 **无法从设备侧取得** | 只在米家云侧 |

> 🔴 **本文不提供、也不讨论**任何绕过云端鉴权、伪造其它设备身份、或重放凭据的做法。
> 若云端不接受，唯一正当路径是**官方换机/迁移支持**。

---

## 4A. 🟡 不依赖云的本地用途可行性（**未验证**）

> **本节全部内容为 🟡 推断 + 可验证方案，未做任何实测。**
> 目的：回答"如果 N150 上**只跑容器、桩掉云端上行**，还有没有可用的东西？"
> 🔴 **本节不含任何"连云"或"伪造身份"的步骤** —— 只讨论**纯局域网侧**能力。

### 4A.1 为什么可能有本地能力（🟡 推断依据）

🟢 **镜像里确实带着一批纯局域网侧的服务**（已从镜像 rootfs 确认存在）：

| 服务 | 推测职责 | 需要云吗 |
| --- | --- | --- |
| `lan_service` | **局域网内设备/网关的发现与本地控制** | 🟡 **推测不需要**（名字即"LAN"）|
| `autoLocal_service` | **本地自动化 / 本地场景执行** | 🟡 **推测不需要** |
| `autoWebConfig_service` | 极客版本地 Web 配置（`webcfg 8086`）| 🟡 **推测不需要** |
| `devinfo_service`、`property_service` | 设备属性/属性变更（多为本地总线）| 🟡 部分 |
| `lightGroup_service`、`bridge_service` | 灯组、子设备桥接 | 🟡 需要有人下发 |
| `bt_service` | 蓝牙 Mesh | 🔴 N150 无蓝牙 + QEMU 不完整 ⇒ **基本不可能** |
| `automation_service` | 自动化（可能**云规则下发**）| 🔴 很可能需要云 |
| `central_service` | 核心（云连接 + 总控）| 🔴 需要云 |

🟡 **推断**：容器里的服务是**按"本地优先"设计的**（网关断网时局域网控制应仍可用），
所以**桩掉云端上行后，本地那部分可能仍能起来并暴露本地接口**。

### 4A.2 能做什么（🟡 未验证，可能）

| 可能可用 | 说明 |
| --- | --- |
| 🟡 **容器内各本地服务正常起进程** | `busybox init` + `perp` 拉起 `lan_service` / `autoLocal_service` 等 |
| 🟡 **本地管理接口可访问** | `webcfg 8086`（极客版配置页）**或许**能在局域网打开 |
| 🟡 **局域网设备发现** | `lan_service` 可能仍会做 mDNS/组播宣告与探测（**取决于它是否要先拿到云端设备表**）|
| 🟡 **本地 UDS/RPC 可用** | `central_proxy_cli` 对 `/tmp/external/*.sock` 的调用**可能**能拿到本地信息（如 `autoWebConfig/getCentralLinkInfo`）|
| 🟡 **为未来"自写中介"打基础** | 能跑起来就能**抓协议、试方法名**（§3.4）|

### 4A.3 不能做什么（🟡→🔴）

| 做不到 | 原因 |
| --- | --- |
| 🔴 **米家 App 里不会显示这个网关** | App 的"网关在线"来自**云端**；桩掉上行 ⇒ **云端不知道它存在** |
| 🔴 **不能用 App 控制任何子设备** | 同上（App 的指令路径是云 → 网关）|
| 🔴 **不能同步/接收云端自动化与场景** | `automation_service` 的规则通常来自云 |
| 🔴 **不能 OTA** | 也无意义（且**应主动禁掉**，见 §3.3）|
| 🔴 **不能配对新的子设备** | 配对流程要经云端下发凭据 |
| 🔴 **蓝牙类功能全废** | N150 无蓝牙 + QEMU 用户态 HCI 模拟不完整 |
| 🔴 **"原机下线后由它顶上"** | 这是 §4 的硬边界，**与本节无关** |

### 4A.4 现实定位（一句实话）

🟡 **"不依赖云的本地用途"更像是"技术研究与自检平台"，而不是"可用的网关替代品"。**
它的真实价值是：**让网关的全部本地代码在一台你能完全掌控的 x86_64 机器上跑起来**，
从而可以**观察、抓协议、做实验** —— 这也是走向 §3.4（自写中介）的必要一步。
🔴 **但它替代不了"米家 App 里的那个网关"。**

### 4A.5 如何验证（🟡 全部为纯离线自检，**不涉及云**）

```bash
# ① 关键：先把"云端上行"桩掉/挡掉，避免它真的去连云
#    （做法：在容器网络层阻断出站 8883/443，或用 hosts 把云域名指向黑洞地址）
#    ⚠️ 目的只是"让自检不依赖云"，不是绕过任何鉴权。

# ② 看哪些服务起来了
docker exec miot_central sh -c 'ps | grep -E "service|mosquitto" | grep -v grep'

# ③ 看本地监听端口（8884 / 8086 / 8883）
docker exec miot_central sh -c 'busybox netstat -tlnp'

# ④ 试本地管理页（在 N150 的浏览器打开）
#    http://<N150-IP>:8086/          ← 极客版配置页（🟡 能否打开 = 关键判据）

# ⑤ 试 UDS RPC（已知方法名）
sudo chroot /opt/central-arm64 /usr/bin/qemu-aarch64-static \
  /usr/bin/central_proxy_cli -a /tmp/miot_central/central_proxy.sock \
  -c -n autoWebConfig/getCentralLinkInfo
#    🟡 若能返回本地信息 ⇒ 证明"本地协议层可独立工作"，这是自写中介的起点

# ⑥ 看日志里"本地 vs 云"的分界
docker logs miot_central 2>&1 | grep -iE 'cloud|login|token|bind|offline|retry|lan|local'
#    🟡 关注：是否有明确的 "network unreachable / cloud connect failed" 后
#       仍继续跑本地服务 —— 若有，说明本地路径是独立的
```

**判据小结**：
- ✅ 若 ③ 能看到 `8884`/`8086` 监听、④ 能打开本地页、⑤ 能拿到本地 JSON
  ⇒ 🟡 **"本地能力可独立工作"得到支持**（仍**不能**出现在米家 App）
- ❌ 若所有本地服务都因"连云失败"而退出 ⇒ 🟡 说明**云是硬依赖**，本地路径不独立

### 4A.6 明确不做的事（合规边界）

🔴 本节**不提供**：伪造设备身份、重放/复用云端凭据、逆向云端鉴权协议、
或任何"让 N150 冒充这台设备去连云"的做法。
本地自检**只在**容器与 N150 之间进行，**不向米家云发包**。

---

## 5. 风险与前置条件

### 5.1 ★ 铁律：**先验证克隆可用，再下线原机**

```
❌ 错误顺序：关掉 NAS → 在 N150 上折腾 → 发现不行 → 两边都不可用
✅ 正确顺序：
   ① 原机**保持在线不动**（它是唯一"活的"设备身份）
   ② 在 N150 上把容器 + 中介跑起来（**离线自检**，不碰原机）
   ③ 验证 N150 能被米家云认成网关（**这一步是硬门槛**，§6.2）
   ④ 只有 ③ 通过，才考虑下线原机
   ⑤ 若 ③ 不通过 ⇒ **放弃整机下线**，改走官方支持
```
🔴 **在 ③ 通过之前，绝对不要：格式化原机 / 恢复出厂 / 关掉原机的自动升级保护 / 停掉 `miio_client`。**

### 5.2 数据备份建议

| 对象 | 建议 |
| --- | --- |
| **原机的用户数据** | 🟢 上策：**用现成的 SMB/WebDAV 把照片/文档/下载/共享完整拷到 N150**（这是原本就该做的）|
| **中枢网关运行态** | 🟢 用 §1.3 的**只读共享**把 `/data/other/central/**` 整个 `get` 下来存档 |
| **设备证书** | 🟢 已存档（`artifacts/scratch/central-image/device-certs/`）—— ⚠️ **按敏感凭据对待**，不要提交、不要外传 |
| **固件包** | 🟢 已有 1.0.9.462 与 1.0.9.450 两个包（`artifacts/scratch/firmware/`）|
| **恢复手册** | 🟢 `docs/nas-root-recovery-2026-10.md`（含 A/B 槽与 10 分钟恢复流程）|

### 5.3 若云端只允许"单设备连接"会发生什么 🟡

🟡 **三种可能（我们无法从设备侧验证，属未知）**：
| 情形 | 现象 | 应对 |
| --- | --- | --- |
| 后连的顶掉先连的 | N150 上线后 **原机的网关掉线** | 说明只允许一台 ⇒ **这就是"迁移"**，若 N150 工作正常则可下线原机 |
| 两台互踢（反复掉线） | App 里网关**反复上下线** | 🔴 **立即停掉 N150 的容器**，恢复原机在线；改走官方支持 |
| 都连不上 | 两台都不在线 | 🔴 **立即停掉 N150 的容器**；检查原机是否恢复 |

> ⚠️ **注意**：由于 §4 的结论，**N150 实际上很可能连"被认成网关"这一步都到不了**
> ⇒ 上述"互踢"风险**在小概率下才出现**。但**测试时务必准备"一键停掉 N150 容器"的脚本**。

---

## 6. 验收清单

### 6.1 阶段一：容器能在 N150 上起来（🟢 可独立完成）

```bash
# ① binfmt 就绪
ls /proc/sys/fs/binfmt_misc/ | grep -i arm
docker run --rm --platform linux/arm/v7 arm32v7/busybox uname -m   # 期望 armv7l

# ② 镜像已载入
docker images | grep central_software_pack

# ③ 容器在跑，且没立刻退出
docker ps --filter name=miot_central
docker logs --tail 80 miot_central

# ④ 容器内关键服务存在
docker exec miot_central ls /usr/bin | grep -E 'central_service|lan_service|mqtt_server_service'

# ⑤ 容器内 mosquitto 起来了
docker exec miot_central sh -c 'busybox netstat -tlnp 2>/dev/null | grep -E "8883|8884"'
```
✅ **判据**：`docker ps` 显示 `Up`，`logs` 里无 `cannot execute binary file` / `Exec format error`。

### 6.2 阶段二：中介与云端（🟡→🔴 硬门槛）

```bash
# ⑥ 中介进程在跑
ps aux | grep -E 'miotcentralctld|central_proxy_cli' | grep -v grep

# ⑦ UDS 已建立（两侧路径要能对上）
ls -l /opt/central/external/          # 期望有 centralCtl.sock / central_proxy.sock
ls -l /opt/central-arm64/tmp/miot_central/

# ⑧ 通过 cli 问一次中介（用已知方法名）
sudo chroot /opt/central-arm64 /usr/bin/qemu-aarch64-static \
  /usr/bin/central_proxy_cli -a /tmp/miot_central/central_proxy.sock \
  -c -n autoWebConfig/getCentralLinkInfo
#   期望：返回 JSON（说明 UDS 协议通了）

# ⑨ ★ 硬门槛：米家云是否接受
#   在米家 App 里查看"中枢网关"是否在线 —— **这一步无法用命令替代**
```
✅ **判据（全中才算成功）**：App 里网关**在线** 且 **显示的是原设备**（同一 SN/名称）。
❌ **任一条不满足** ⇒ 见 §6.3。

### 6.3 失败时的分步排查

| 现象 | 最可能原因 | 排查 |
| --- | --- | --- |
| `Exec format error` | binfmt 未注册 | 回到 §2.2；`ls /proc/sys/fs/binfmt_misc/` |
| 容器起来就退出 | `/data` 不可写 / 缺 `platform.conf` | `docker logs`；检查挂载与权限；`platform.conf` 里的 `phylist` 网卡名 |
| 容器在跑但无 8883/8884 | `busybox init` 没拉起服务 | 进容器手跑 `central_service`；看 `/etc/inittab`、`/etc/perp` |
| UDS 建不起来 | 两侧路径不一致 | 检查 `--bind`；容器内 `/tmp/external` vs 中介 `-c /tmp/miot_central/centralCtl.sock` |
| `central_proxy_cli` 报 `No such file` | chroot 里缺库 | `readelf -d` 看 NEEDED，逐个补齐（应只有 libc/libpthread）|
| **中介在跑但云端不认** | 🔴 **设备身份问题（§4）** | **这是本方案的硬边界** ⇒ 走官方支持，不要试图伪造 |
| 原机被顶掉线 | 云端单设备限制 | **立刻停 N150 容器**：`docker stop miot_central` |
| 局域网发现不到设备 | 组播/网卡绑定 | 检查 `platform.conf` 的 `phylist`；`--network=host`；防火墙 |
| 蓝牙相关功能全废 | N150 无蓝牙 + QEMU 不完整 | 🔴 **预期内，放弃蓝牙** |

### 6.4 性能压测（建议在"阶段一"就做）

```bash
# 容器稳态 CPU / 内存
docker stats --no-stream miot_central

# 宿主的 QEMU 开销
top -b -n1 | head -20            # 看 qemu-arm 进程占用
# 观察 5 分钟：稳态 CPU 若长期 > 60%（单核），说明 QEMU 开销过大，需重新评估
```

---

## 7. 建议的推进顺序（含"止损点"）

| 阶段 | 动作 | 止损点 |
| --- | --- | --- |
| **P0** | 只读盘点运行态数据（§1.3），存档镜像/证书/配置（§1.1）| 无需止损（纯读）|
| **P1** | 在 N150 上装 binfmt、载入镜像、把容器跑起来（§2）| `Exec format error` 且无法解决 ⇒ 停 |
| **P2** | QEMU chroot 跑中介，跑通 UDS（§3.2、§6.1⑥–⑧）| 中介无法与容器建 UDS ⇒ 评估 §3.3/§3.4 |
| **P3** | **★ 尝试让云端认可（§6.2⑨）** | 🔴 **不通过 ⇒ 就此止损**，走官方支持（§4.4）|
| **P4** | 仅当 P3 通过：完整数据迁移 → 观察期 → 才考虑下线原机 | 出现互踢 ⇒ 立即回滚 |

### 🔴 最后一句实话
**P3 大概率不会通过**，原因不是技术不够，而是**设备身份是云端按 SN/MAC/did 发放并绑定的**，
它**不是一个可以拷贝的文件**。所以这份方案现实的价值是：
1. 🟢 让你**完整理解**这台网关的构成（镜像 / 中介 / 身份三层）；
2. 🟢 让你**能在 N150 上把它跑起来做研究/自检**；
3. 🟡 让你**带着确切的技术信息**去找小米谈"设备迁移"（§4.4 给了清单）；
4. 🔴 但**不要**指望"关掉原机、N150 顶替"这一步能靠软件完成。

---

## 附录 A：本文引用的证据位置

| 证据 | 路径 |
| --- | --- |
| 网关镜像（OCI 归档） | `artifacts/scratch/central-image/extracted/` |
| 镜像 rootfs（arm/v7） | `artifacts/scratch/central-image/rootfs/` |
| 版本元数据 / 签名 | `artifacts/scratch/central-image/{central.version,central.sign}` |
| 设备 mTLS 凭据（**敏感**）| `artifacts/scratch/central-image/device-certs/` |
| 宿主配置原文 | 固件解包 `/root/rootfs/etc/central_software_pack/platform.conf` |
| 启动脚本原文 | 固件解包 `/root/rootfs/usr/bin/central_software_pack.sh`、`miot_centralctld_hook.sh` |
| 中介 systemd 单元 | 固件解包 `/root/rootfs/etc/systemd/system/miotcentralctrld.service` |
| mosquitto 配置（8883/8884）| 镜像内 `/etc/mqtt_server_service/mosquitto.conf`、`/etc/mips/mosquitto.conf` |
| OTA / 钩子配置 | 镜像内 `/etc/othook_config.json` |
| 设备 / 固件事实 | `docs/nas-root-recovery-2026-10.md` §2、§3 |
| 只读共享的**能力与边界** | `docs/nas-root-recovery-2026-10.md` §5.7.7 (A) |
| **本文在主报告中的索引位置** | `docs/nas-root-recovery-2026-10.md` **§7.4「相关文档」** |

## 附录 B：本文的合规声明

- 本文**只涉及迁移设备所有者本人的这一台设备**；
- **不含**任何绕过鉴权、伪造身份、重放凭据、或访问他人设备的内容；
- 所有设备侧动作**仅限只读**（且已在前序工作中实测并记录边界）；
- 🔴 **`device.key` 等凭据按敏感文件对待：不要提交进仓库、不要外传**；
- 本文为**纯研究文档**，未对任何设备做改动，也未 commit。
