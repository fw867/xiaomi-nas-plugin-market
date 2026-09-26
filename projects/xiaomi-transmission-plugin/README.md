# Transmission 下载（Docker）

小米智能存储插件市场的 Transmission 插件，版本 **0.2.0**。  
本版改为 **Docker 容器** 运行 LinuxServer 的 Transmission 镜像，配置页参考 qB 下载。

**不是 Transmission 或小米的官方插件。**

## 功能

- 配置页可设置：
  - 下载目录 → 容器 `/downloads`
  - 配置文件夹目录 → 容器 `/config`
  - 监控目录 → 容器 `/watch`
  - WebUI 用户名 / 密码
- 端口映射：
  - WebUI / RPC：`tcp 9091`
  - BT 入站：`tcp/udp 51413`
- 服务启停、局域网 WebUI 入口展示

## 安装与使用

1. 安装本插件后，用设备所有者账号打开插件页。
2. 分别选择下载、配置、监控三个目录（必须同属一个 NAS 用户，且互不相同）。
3. 设置 WebUI 用户名与密码（密码至少 8 位，大小写/数字/符号至少两类）。
4. 同意并「安装并启动」。首次需拉取镜像，可能需要数分钟。
5. 服务就绪后，点状态页上方的「打开控制台」进入完整 WebUI（走插件同源路径，局域网与外网都能用）；局域网设备也可以直接打开卡片里的 `http://<NAS-IP>:9091`，更快、不经过插件中转。两种入口都用安装时的 WebUI 用户名和密码登录。

## 隔离与生命周期

- 容器名：`xiaomi-plugin-transmission`
- 只挂载所选三个目录，不挂载 Docker socket，不使用 privileged / host 网络
- 资源上限：512 MiB 内存、1.5 CPU、128 PID
- WebUI 账号密码通过镜像 `USER`/`PASS` 环境变量注入；`settings.json` 只写目录与端口等非鉴权项
- **控制台**：默认安装 [Transmission Web Control](https://github.com/ronggang/transmission-web-control)
  （`v1.6.1-update1`）到所选配置目录的 `webui/` 下，容器以
  `TRANSMISSION_WEB_HOME=/config/webui` 使用它。
  **想换成别的 WebUI**：把界面文件放进 `<配置目录>/webui/`（保证 `index.html` 在该目录根部，
  参照官方安装脚本 `cp -r <包>/src/. $TRANSMISSION_WEB_HOME/` 的做法），然后重启服务即可；
  插件不会覆盖你放进去的文件。
  - 插件页的「打开控制台」走**插件同源路径** `/plugin/<你>/transmission/console/`：插件把
    `webui/` 里的文件发出去，并把控制台的 RPC 请求（相对路径 `../rpc`）转发给容器 9091，
    所以局域网之外（客户端远程通道）也能打开。该入口只对设备所有者开放：所有者客户端证书，
    或用插件页令牌换来的签名 Cookie（12 小时有效，HttpOnly）；令牌只出现在首次跳转的地址上，
    随即 302 到不带令牌的地址。
  - 卡片里的 `http://<NAS-IP>:9091` 是**局域网直连**入口（更快、不经过插件中转），外网打不开。
- **配置文件**：只有 `<配置目录>/settings.json`（容器内 `/config/settings.json`）生效，
  见下方「配置文件改哪里」
- 停止/卸载插件只停止容器，不删除配置与下载文件

## 配置文件改哪里

- **只有 `<配置目录>/settings.json`**（即容器内 `/config/settings.json`）会被读取：镜像写死
  `transmission-daemon -g /config`，没有环境变量能改这个位置。插件页的「配置目录」下方会显示
  这个文件的完整路径。
- **改配置的顺序是「停止服务」→ 改 `settings.json` → 「启动服务」**：daemon 只在启动时读一次
  配置，而它在停止时会把内存里的配置写回文件——插件还在运行时改，改动会被它覆盖掉。
  写在 `transmission-daemon/` 子目录里的配置不会被读取，daemon 会回落到镜像默认值
  （例如 `rpc-bind-address` 的 `[::]` 在无 IPv6 的容器里绑不上 9091，Web 界面起不来）。
- **插件不会覆盖你改过的值**：启动时只补「缺失」的键；另外把 `rpc-enabled`、`rpc-port`、
  `rpc-bind-address` 纠正为容器映射要求的值（9091 固定，改这三个键会让 WebUI 和插件控制台都
  打不开）。缓存、连接数、限速、DHT/PEX、队列、做种规则等键改了就一直是你的值。
- **镜像每次启动都会重写下面这些键**（它们来自容器环境变量，改 `settings.json` 无效）：
  `rpc-authentication-required`、`rpc-username`、`rpc-password`、`rpc-whitelist`、
  `rpc-whitelist-enabled`、`rpc-host-whitelist`、`rpc-host-whitelist-enabled`、`peer-port`、
  `peer-port-random-on-start`、`umask`。换 WebUI 账号密码需要重新初始化插件。
- **旧版原生插件的配置会自动迁移一次**：`<配置目录>/transmission-daemon/settings.json` 是原生版
  （Entware 随包二进制）的路径，Docker 版 daemon 不读它。升级后第一次启动时，插件把其中与容器
  无关的可调项并进真正生效的 `settings.json`，并把旧文件改名成 `settings.json.legacy` 留在原处
  （避免继续改一个不生效的文件）；路径、端口、账号、绑定、脚本文件名这类键不会搬过来。

## 镜像

- `lscr.io/linuxserver/transmission@sha256:1a12fef3c89eca48b7be9e7d36b17b4eb4e1bcf5e1ee7fbf372e3a38b562939d`
- 版本：Transmission **4.1.3** / LinuxServer **ls362**（多架构，含 arm64）

## 本地预览

```bash
python3 scripts/build_ui.py
python3 -m unittest discover -s tests -v
python3 server.py --dev
```

打开 `http://127.0.0.1:18140/`。预览模式不会操作 Docker、不会修改 NAS。

## 上游

- [Transmission](https://transmissionbt.com/)
- [LinuxServer Transmission 镜像](https://docs.linuxserver.io/images/docker-transmission/)
