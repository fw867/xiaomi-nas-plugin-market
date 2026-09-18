# Transmission 下载（小米智能存储社区插件）

在小米智能存储客户端里管理 BT 下载：启停/重启 transmission-daemon、设置下载目录与并发、
配置连接数与上传下载限速，并内置一套完整的 Web 管理界面
（[transmission-web-control](https://github.com/ronggang/transmission-web-control)）。

**不是 Transmission 或小米的官方插件。** 上游是 [Transmission](https://transmissionbt.com/)
（GPL-2.0-or-later）与 transmission-web-control（MIT）。

## 与其他下载插件的区别

`qB 下载`（qBittorrent）依赖 Docker 拉镜像；本插件**不需要 Docker**：transmission-daemon
是可执行的静态依赖，由 `scripts/fetch_runtime.py` 从 Entware 的 aarch64 仓库提取后
随插件一起分发，连 Entware 自己的 glibc 与加载器一起带上。
设备上**不需要**安装 Entware，也不需要联网安装任何东西。

Entware 二进制的 ELF 解释器写死为 `/opt/lib/ld-linux-aarch64.so.1`（Entware 的安装
路径），设备上没有 `/opt`，所以直接执行会报「无法执行：找不到需要的文件」。
插件用随包加载器显式调用：

```bash
lib/ld-linux-aarch64.so.1 --library-path lib bin/transmission-daemon --version
```

`server.py` 的 `runtime_argv()` 负责拼这条命令（`start_daemon` 与 `daemon_version`
都走它）；`daemon_env()` 里仍导出的 `LD_LIBRARY_PATH` 只是加载器缺失时的兜底，
`--library-path` 会覆盖它。

代价是插件包体较大（见下），且二进制由第三方仓库（Entware）构建，不是我们自己编译的。

## 架构

```text
界面（小米客户端内嵌页，同源）
  │  /plugin/<小米用户>/transmission/...
  ▼
nginx  location 规则（deploy/xiaomi-transmission.nginx.conf）
  ├── /transmission/api/...  → 反代 http://127.0.0.1:18140/api/...
  ├── /transmission/rpc      → 反代 http://127.0.0.1:18140/rpc → daemon
  └── 其余                    → 静态文件（/home/<用户>/plugin/transmission/src/ui/）
  ▼
xiaomi-transmission.service   127.0.0.1:18140（server.py，仅标准库）
  ├── 启停/重启：Popen(transmission-daemon -f -g /data/plugin/transmission/data)
  ├── 设置：读写 <config-dir>/settings.json，按需要重启 daemon
  └── 状态：pid 文件 + RPC session-stats
  ▼
transmission-daemon   127.0.0.1:19191（RPC 只绑回环，不开鉴权）
```

`server.py` 只监听回环、只提供固定操作，它不是不受信任插件的沙箱。
RPC 端口刻意用 19191 而不是 transmission 默认的 9091，避免与用户自己装的
transmission 冲突（可用 `RPC_PORT` 环境变量调整）。

## 设置项与 settings.json 字段

界面上的每一项都对应 transmission 4.x `settings.json` 里的真实键名。
依据是 **transmission 4.0.6 的 `docs/Editing-Configuration-Files.md`**
（4.1 起文档改用 snake_case，但要 `TR_SAVE_VERSION_FORMAT=5` 才是默认；
4.x 默认仍是 kebab-case）。插件会读磁盘上现有 `settings.json` 判断风格，
没有文件时按 4.x 默认写 kebab-case；也可以写 snake_case，插件能跟上。

| 界面上的设置项 | 类型 | settings.json 键（4.x kebab-case） | 单位 | 说明 |
| --- | --- | --- | --- | --- |
| 下载目录 | 路径 | `download-dir` | — | 必须绝对路径；目录不存在时插件会创建 |
| 同时上传数（每任务上传槽位） | 整数 1–64 | `upload-slots-per-torrent` | 个 | 默认 14 |
| 限制同时做种任务数 | 开关 | `seed-queue-enabled` | — | 关时下面的上限不生效 |
| 同时做种任务数上限 | 整数 1–1000 | `seed-queue-size` | 个 | 默认 10 |
| 限制同时下载任务数 | 开关 | `download-queue-enabled` | — | 默认开 |
| 同时下载任务数上限 | 整数 1–1000 | `download-queue-size` | 个 | 默认 5 |
| 不把低速任务计入并发上限 | 开关 | `queue-stalled-enabled` | — | 默认开 |
| 低速判定时间 | 整数 1–1440 | `queue-stalled-minutes` | 分钟 | 默认 30 |
| 全局连接数 | 整数 1–10000 | `peer-limit-global` | 个 | 默认 240 |
| 单种连接数 | 整数 1–2000 | `peer-limit-per-torrent` | 个 | 默认 60 |
| 启用上传限速 | 开关 | `speed-limit-up-enabled` | — | |
| 上传限速 | 整数 1–1048576 | `speed-limit-up` | KB/s | 默认 100 |
| 启用下载限速 | 开关 | `speed-limit-down-enabled` | — | |
| 下载限速 | 整数 1–1048576 | `speed-limit-down` | KB/s | 默认 100 |
| 启用时段限速（限速模式） | 开关 | `alt-speed-enabled` | — | 手动进入限速模式 |
| 时段限速·上传达限 | 整数 1–1048576 | `alt-speed-up` | KB/s | 默认 50 |
| 时段限速·下传达限 | 整数 1–1048576 | `alt-speed-down` | KB/s | 默认 50 |
| 启用限速时段计划 | 开关 | `alt-speed-time-enabled` | — | |
| 时段开始 | 整数 0–1439 | `alt-speed-time-begin` | 零点起分钟 | 界面按 `HH:MM` 输入，默认 540=09:00 |
| 时段结束 | 整数 0–1439 | `alt-speed-time-end` | 零点起分钟 | 默认 1020=17:00 |
| 生效星期 | 整数 0–127 | `alt-speed-time-day` | 位图 | 周日 1 周一 2 周二 4 周三 8 周四 16 周五 32 周六 64；全年 127、工作日 62、周末 65 |

**关于「同时上传数」的字段名**：Transmission 4.x 里**没有** `max-peers-global` /
`max-peers-per-torrent`——那是 1.4x 的旧名，早已改名为 `peer-limit-global` /
`peer-limit-per-torrent`。真正表示「同时上传数」的是 `upload-slots-per-torrent`
（每个任务同时上传给多少个连接），再叠加 `seed-queue-enabled` + `seed-queue-size`
限制同时做种的任务数。

### 写入方式与生效时机

界面上点「保存并应用」后，插件按固定顺序执行：

1. **先停止** transmission-daemon（transmission 只在退出时回写 settings.json，
   运行中直接改文件会被它覆盖）；
2. 原子写入 `settings.json`（临时文件 + rename，权限 0644）；
3. 再启动 daemon。

daemon 原本没在运行时不会把它拉起来，界面会提示「下次启动生效」。
`validate_settings()` 是白名单校验：请求体里出现未声明的键直接 400，
所以界面（或任何调用方）无法往 `settings.json` 里塞任意字段。

### 开机自动拉起

点「启动」或「重启」时插件会把「用户希望 daemon 运行」写进
`/data/plugin/transmission/data/plugin-state.json`，点「停止」时清掉。
`server.py` 启动时读这个标志，为真就自动把 daemon 拉起来——
所以设备重启或插件服务重启后，下载会自动恢复，不需要再进界面点一次。
transmission 自己不管这件事，这是插件补上的行为。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/healthz` | 存活探测，商城安装时用它判断服务就绪 |
| GET | `/api/status` | daemon 状态、版本、pid、RPC 端口、任务数与实时速度 |
| GET | `/api/settings` | 分组、字段（中文标签/单位/范围/help）、当前值、存储键名 |
| POST | `/api/settings` | `{"values": {...}, "restart": true}`，返回更新后的值 |
| POST | `/api/action` | `{"action": "start"｜"stop"｜"restart"}`，同时维护开机自启标志 |
| POST | `/rpc` | 转发到 daemon 的 `/transmission/rpc`（含 409 session-id 透传） |
| GET | `/<静态路径>` | `web/` 目录下的文件，含 `twc/`（transmission-web-control） |

`/api/settings` 的 `values` 用**逻辑名**（即上表的 kebab-case 键名）。
界面上「时段开始/结束」按 `HH:MM` 显示，存储仍是零点起分钟数；
「生效星期」7 个复选框合成位图，两者都在前端换算。

## transmission-web-control 是怎么拿到的

上游仓库把**构建产物直接提交在 `src/`**（它自己的安装脚本就是下载源码包后把
`src/.` 整体拷进 transmission 的 web 目录），所以这里同样取 `src/`，不自行构建：

* 仓库：`https://github.com/ronggang/transmission-web-control`
* release/tag：**`v1.6.1-update1`**（2020-09-13，最新 release）
* commit：`c26a0761e3a8fe3cff2480735ec363dc253c5105`
* 源码包：`https://github.com/ronggang/transmission-web-control/archive/refs/tags/v1.6.1-update1.tar.gz`
* 解出的内容：`src/` 下的 `index.html`、`index.mobile.html`、`favicon.ico`、
  `tr-web-control/{config.js,i18n.json,i18n/,logo.png,logo-white.png,plugin.js,script/,style/,template/}`
* 落盘位置：`web/twc/`，随插件一起打包；`scripts/fetch_web_control.py` 会把源码包的
  SHA-256 与每个文件的 SHA-256 写进 `web/twc/PROVENANCE.json`。

### 为什么必须是 `web/twc/` 这一层

transmission-web-control 的 RPC 地址写死为相对页面的 `../rpc`
（见上游 `src/tr-web-control/script/transmission.js` 的 `rpcpath: "../rpc"`）。
`src/` 放进 `web/twc/` 后：

```text
页面  <base>/twc/index.html
  →   ../rpc  =  <base>/rpc
  →   nginx 反代  →  server.py /rpc  →  daemon /transmission/rpc
```

如果把 `index.html` 再往下一层（例如 `web/twc/html/`），`../rpc` 就会落到
`<base>/rpc` 以外的位置，界面的所有请求都会 404。上游自带的
`index.original.html` 跳转与 GitHub 更新检查在插件环境下不可用，属已知限制。

## 运行时依赖：来源与校验值

来源仓库：**Entware `http://bin.entware.net/aarch64-k3.10/`**
（`Packages` 索引，生成时间 31-Aug-2026 17:20；索引里 `Filename` + `SHA256sum` 即下表）。

根包（`scripts/fetch_runtime.py` 的 `ROOTS`）：`transmission-daemon`、
`transmission-remote`、`transmission-cli`。其余按 `Depends` **递归解析**得到。

Entware 自己的 glibc 也必须随包携带（`libc` / `libpthread` / `librt` / `libssp`，
以及动态加载器 `ld-linux-aarch64.so.1`）：因为二进制的 ELF 解释器指向 Entware 的
`/opt/lib/`，设备上那个路径不存在，必须用随包加载器显式调用，而随包加载器要配
随包的 libc 才能工作。加载器文件本身也是从这些包里的 `/opt/lib/*.so*` 收集来的
（文件名含 `.so.`，会被同一个收集规则捞到），具体来源包见 `runtime-manifest.json`。
下表是 `fetch_runtime.py` 解析出的全部 **.ipk（27 个）**；`runtime-manifest.json`
记录的是解包后**每个文件**的来源包与 SHA-256，下面只是包一级的来源快照。

| 包 | 版本 | .ipk | 大小 | SHA-256（Entware 索引） |
| --- | --- | --- | --- | --- |
| `transmission-daemon` | 4.0.6-2 | `transmission-daemon_4.0.6-2_aarch64-3.10.ipk` | 583856 | `9a4eccdc6a9f20e34f3461416bb8a2f09c3fe735a54fd6822981c9b5139758b5` |
| `transmission-remote` | 4.0.6-2 | `transmission-remote_4.0.6-2_aarch64-3.10.ipk` | 158291 | `7437402fe01918122a61669649365634193ecf297043ed905c7c69f25c193fa2` |
| `transmission-cli` | 4.0.6-2 | `transmission-cli_4.0.6-2_aarch64-3.10.ipk` | 913812 | `bd3a3e067d76b6bb8463dcf50f1bf7106a0c6a9c9b7a814e7ed91a39e01b588f` |
| `libc` | 2.27-12 | `libc_2.27-12_aarch64-3.10.ipk` | 1367072 | `4d7825a9ddf7a382a985a44ec85c4ab458f8ebe12c3e3b5fb25da814f71167bb` |
| `libpthread` | 2.27-12 | `libpthread_2.27-12_aarch64-3.10.ipk` | 44699 | `a2af364e6e139069f8b37dd9e7f2accaf529efd35738c6d381d2e43e78b876d4` |
| `librt` | 2.27-12 | `librt_2.27-12_aarch64-3.10.ipk` | 13307 | `86d4adc05b939793f4fb7d54ecc6c34b867a1fe9c449983393f708f245b72b43` |
| `libssp` | 8.4.0-12 | `libssp_8.4.0-12_aarch64-3.10.ipk` | 4092 | `8d85e466962569005ec604a64b2ffa739454a9db017a9a5e8dbe23b3c4bf2c25` |
| `libatomic` | 8.4.0-12 | `libatomic_8.4.0-12_aarch64-3.10.ipk` | 10747 | `2a3ab50ae1173bc85d16fe17cfff87f3e983f87beecb11a13c15775d9e9bedb4` |
| `libgcc` | 8.4.0-12 | `libgcc_8.4.0-12_aarch64-3.10.ipk` | 36583 | `123882cd0063342b8cd058ea97541601d793ef1f847920e1da6be4c3a0b8da8b` |
| `libcurl` | 8.15.0-2 | `libcurl_8.15.0-2_aarch64-3.10.ipk` | 338900 | `d73bc4d0cb428fb5c46a0eae1e111f6d0c7282334f5970d6e189c24ea687ebfe` |
| `libopenssl` | 3.5.5-1 | `libopenssl_3.5.5-1_aarch64-3.10.ipk` | 2471054 | `d1b3738612336121d5510eb5f212f1154e30db1653be827bc09721a53ac2d260` |
| `libnghttp2` | 1.66.0-1 | `libnghttp2_1.66.0-1_aarch64-3.10.ipk` | 77681 | `2739a9f955baf30fe3559512dd9ea8b5295ddb752748c287f9f2f8ea8037f8fe` |
| `ca-bundle` | 20250419-2 | `ca-bundle_20250419-2_all.ipk` | 131045 | `e4206917b1aa53a1a365fb21909da79d6b2d0b6b15fa50f79a4221fcb6d6192d` |
| `zlib` | 1.3.1-1 | `zlib_1.3.1-1_aarch64-3.10.ipk` | 43197 | `30470ce83557e2438cac7a0a874a1fb7e50570c4a7cbbe3f2ac1b4a2d1efb372` |
| `libdeflate` | 1.25-1 | `libdeflate_1.25-1_aarch64-3.10.ipk` | 42270 | `c9fa5057e42cc864b732561f3c4f5365f673cb5ef6c7346fcb8ddbb60e5355a5` |
| `libevent2` | 2.1.12-2 | `libevent2_2.1.12-2_aarch64-3.10.ipk` | 129402 | `5575875f98977abe492606c236102f9dcc24e6cccf6ce889670336bdb68de285` |
| `libevent2-core` | 2.1.12-2 | `libevent2-core_2.1.12-2_aarch64-3.10.ipk` | 77752 | `61e98c84fbaf8493d92f301b79f05ac03031c6d09ddd37418b4733a15c0e397c` |
| `libevent2-pthreads` | 2.1.12-2 | `libevent2-pthreads_2.1.12-2_aarch64-3.10.ipk` | 3635 | `db524e54002759c269ded83f7c6bbede585ae1ee8426209a4e7365fa3a02e81c` |
| `libminiupnpc` | 2.2.8-1 | `libminiupnpc_2.2.8-1_aarch64-3.10.ipk` | 25317 | `555473717e7e3e9fa248e4be6825224c208a7f6cbd2e9989ddf0539937a04649` |
| `libnatpmp` | 20230423-2 | `libnatpmp_20230423-2_aarch64-3.10.ipk` | 4915 | `3aa4e23403e0a28730484f81d92a7fde350b58a540f2d784f389c8e74eec27fa` |
| `libpsl` | 0.21.5-1 | `libpsl_0.21.5-1_aarch64-3.10.ipk` | 54777 | `6f33ac738ae993a9297621d058e2a3f8e1a7fa03c500728ff47795679239cd67` |
| `libutp` | 2024.11.16~490874c4-1 | `libutp_2024.11.16~490874c4-1_aarch64-3.10.ipk` | 25832 | `61e7b6568cec8d6b3d0d93f5dc0d0730a9764ef66fff355c1ecfb98452730687` |
| `libstdcpp` | 8.4.0-12 | `libstdcpp_8.4.0-12_aarch64-3.10.ipk` | 389225 | `7b945502a7e7eef3e172b0343a533f4cce71db1579a372a4cee6e4c209b1bdc1` |
| `libidn2` | 2.3.7-1 | `libidn2_2.3.7-1_aarch64-3.10.ipk` | 105344 | `c6f08e42f4f50e326b7d15af3a13a95b4af540de0708f5921b087626346db1ba` |
| `libunistring` | 1.4.1-1 | `libunistring_1.4.1-1_aarch64-3.10.ipk` | 744577 | `e4ffab1982e603343f5ea9d05b7f603bfdba4a4f389de92623f23ddc258799d2` |
| `libiconv-full` | 1.18-1 | `libiconv-full_1.18-1_aarch64-3.10.ipk` | 678103 | `e2b0bfafd5438fd79d533599eef7fb2493369ca7b3a22186dee6542ccb4e7bf7` |
| `libintl-full` | 0.24.1-1 | `libintl-full_0.24.1-1_aarch64-3.10.ipk` | 33279 | `c1a5f199089158fa6b2e8d03be22ae7477665d9db65af9cbb8d73334379506fc` |

依赖是怎么串起来的：

```text
transmission-daemon/-remote/-cli
  → libatomic → libgcc
  → libcurl → libopenssl, zlib, libnghttp2, ca-bundle
  → libdeflate, libevent2, libminiupnpc, libnatpmp, libutp → libstdcpp
  → libevent2-pthreads → libevent2-core
  → libpsl → libidn2, libunistring, libintl-full
  → libidn2 → libunistring, libiconv-full, libintl-full
  → libc → libgcc；libpthread → libgcc；librt → libpthread；libssp（无依赖）
```

`.ipk` 合计约 8.2 MiB；解出的 `bin/` + `lib/` 合计约 20 MiB
（`libopenssl` 6.7 MiB、`libc` 2.9 MiB 两家占了大头）。`lib/` 里的符号链接
（如 `libcrypto.so.3`）在打包前会被解析成真实文件——发布包解压时符号链接不被支持。
包体偏大是「不依赖 Docker、不依赖设备上的 glibc」的直接代价。

### 每个文件的 SHA-256

`bin/` 与 `lib/` 里**每个文件**的相对路径、来源 .ipk、大小与 SHA-256 由
`scripts/fetch_runtime.py` 写进 `runtime-manifest.json`；许可证文本的 URL 与
SHA-256 也在同一个文件里。查看方式：

```bash
cd projects/xiaomi-transmission-plugin
python3 scripts/fetch_runtime.py --check        # 按清单校验现有文件
python3 scripts/fetch_runtime.py --print-table  # 重新打印上面的 .ipk 表格
```

## 生成运行时要跑的命令

`bin/`、`lib/`、`licenses/`、`runtime-manifest.json` 与 `web/twc/` 都由脚本生成，
它们是 `scripts/build_apps.py` 的打包输入，缺失时打包会直接失败：

```bash
cd projects/xiaomi-transmission-plugin
python3 scripts/fetch_runtime.py         # Entware → bin/ lib/ licenses/ runtime-manifest.json
python3 scripts/fetch_web_control.py     # transmission-web-control → web/twc/
```

图标 `web/assets/transmission.png` 不在这里生成：它是 Transmission 官方图标
（512×512、8-bit RGBA），随仓库一起存放。来源与许可见
`licenses/TRANSMISSION-ICON-LICENSE`。

`LICENSE` 层面：`licenses/TRANSMISSION-COPYING`（GPL-2.0）与
`licenses/TRANSMISSION-WEB-CONTROL-LICENSE`（MIT）由 `fetch_runtime.py` 下载，
随插件包一起分发，满足再分发时的许可要求。

## 已知限制

* **本仓库当前检出中尚未包含 vendored 运行时**：`bin/`、`lib/`、`licenses/`、
  `runtime-manifest.json` 与 `web/twc/` 需要跑上面两条命令生成。
  因此 `python3 scripts/build_apps.py --only transmission` 在跑脚本之前会报缺少源文件。
* 二进制来自第三方（Entware），不是本项目自行编译。Entware 的 aarch64 目标按
  glibc 2.27 构建，随包连 Entware 自己的 glibc 和加载器一起带上，运行时不依赖
  设备上的 `/lib`；本插件已在 RP05（aarch64 / Cortex-A55×4 / erofs 只读根 +
  可写 `/data`）这一代设备上实测六个二进制都能跑（`--version` 通过）。
* 只支持主动出站连接（不含 UPnP/NAT-PMP 的内网端口映射也可以开，但
  `port-forwarding-enabled` 默认开着，能否成功取决于路由器）。没有单独映射
  BT 入站端口，部分网络下的连接数和速度会受限。
* daemon 以 root 运行，与其它社区插件一致。插件服务本身需要 root 才能启停进程。
  它不是不受信任插件的沙箱。
* `settings.json` 的持久化只有「停 daemon → 写文件 → 启 daemon」这一条路径；
  直接停掉整个插件服务不会停止 daemon，`ExecStopPost` 才会兜底 SIGTERM。
* transmission-web-control 是 2020 年的界面（tag `v1.6.1-update1`），
  在 transmission 4.0.6 上可用，但不再跟进上游新特性与安全修复；
  它自带的「检查更新」「切回原版界面（index.original.html）」在插件环境下不可用。
* 不承诺官方 OTA 后插件入口与服务始终存在。
* 不附带任何个人密钥、云盘令牌或 NAS 配置。

## 安装

通过插件市场安装：

1. 在小米智能存储客户端打开「插件市场」。
2. 找到「Transmission 下载」，点击安装。
3. 重新打开客户端，在「全部应用」中打开「Transmission 下载」。
4. 在「下载目录」里选一个设备上看得见的目录，保存后点「启动」。

## 状态与日志

* 配置目录：`/data/plugin/transmission/data/`（`settings.json`、`torrents/`、`resume/`、
  `plugin-state.json` 开机自启标志）
* daemon 日志：`/data/plugin/transmission/data/transmission.log`
* 插件日志：`journalctl -u xiaomi-transmission.service`
* 插件数据目录：`/data/plugin/transmission/data/`（升级时不会动它）

## 开发

```bash
cd projects/xiaomi-transmission-plugin
python3 -m unittest discover -s tests -v
```

测试全部用 `unittest.mock` 打桩，不会真的启动 transmission-daemon，
也不要求存在 `/data` 或 Entware。

### 在设备上手工验证运行时能不能跑

`bin/` 是 aarch64 的 ELF，只能在设备上验证。把整个插件目录拷到 `/data` 再执行：

```bash
# 在本机打包后，把 runtime 目录传到 NAS
scp -r projects/xiaomi-transmission-plugin/bin \
       projects/xiaomi-transmission-plugin/lib root@<NAS>:/tmp/tr-test/

# 在 NAS 上：必须用随包加载器显式调用（ELF 解释器指向 Entware 的 /opt/lib）
ssh root@<NAS>
cd /tmp/tr-test
chmod 755 bin/* lib/ld-linux-aarch64.so.1
lib/ld-linux-aarch64.so.1 --library-path lib bin/transmission-daemon --version
lib/ld-linux-aarch64.so.1 --library-path lib bin/transmission-daemon -d -g /tmp/tr-test/cfg | head -40
```

`--version` 打印 `transmission-daemon 4.0.6 (38c164933e)` 即运行时可用；
`-d`（dump settings）能进一步确认默认配置与字段名。

> 直接执行 `./bin/transmission-daemon` 会报「无法执行：找不到需要的文件」——
> 这是 ELF 解释器 `/opt/lib/ld-linux-aarch64.so.1` 不存在导致的，不是缺 .so；
> 换加载器调用即可，`LD_LIBRARY_PATH` 也救不了它。

### 上游与资产

* [Transmission 4.0.6](https://github.com/transmission/transmission/tree/4.0.6) /
  [settings.json 字段文档](https://github.com/transmission/transmission/blob/4.0.6/docs/Editing-Configuration-Files.md)
* [transmission-daemon(1) 手册](https://github.com/transmission/transmission/blob/4.0.6/daemon/transmission-daemon.1)
* [Entware](https://entware.net/)（`aarch64-k3.10` 仓库）
* [transmission-web-control](https://github.com/ronggang/transmission-web-control)
* 应用图标为本项目自绘，不复用 Transmission 上游商标，也不暗示背书。
