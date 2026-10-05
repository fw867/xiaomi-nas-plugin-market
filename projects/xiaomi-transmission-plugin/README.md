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

## 修改目录与重新初始化

配置完成后，状态卡片底部的「目录设置」里有这两个入口：

- **修改目录**：只更换下载 / 配置 / 监控目录的位置（存储池或外接设备都行），也可以顺便设置新的
  WebUI 密码（留空表示不改）。容器会**停止并按新的宿主路径重建**——bind 挂载只在创建容器时确定，
  restart 换不了目录，旧容器的挂载不会保留；WebUI 账号与定时设置都在。**原目录里的文件不会被删除
  或移动**；新位置如果本来没有内容，Transmission 就以全新状态启动。改之前会把插件配置与
  `<配置目录>/settings.json` 各备份成 `settings.json.bak-<时间戳>`，重建失败会自动回滚到原目录。
- **重新初始化**：清空插件配置、停止并移除容器、撤掉路由器上的端口映射，页面回到初始化表单，
  之后需要重新初始化才能再次启动服务。**下载 / 配置 / 监控目录里的文件一个都不会被删除、移动或
  改写**（种子与 resume 数据都留在配置目录里）。插件在 `/data/plugin/transmission/data/` 下的
  `settings.json` 与 `credential.json` **只改名归档**、不会删除：
  `settings.json.bak-<时间戳>`、`credential.json.bak-<时间戳>`；daemon 的
  `<配置目录>/settings.json` 只另存一份备份，原文件留在原处。

### WebUI 密码忘了 / 凭据文件不见了

1. **正常做法**：点「重新初始化」→ 页面回到初始化表单 → 重新选择目录并设置新的 WebUI 账号密码。
   （插件不会去动配置目录里的种子与 resume 数据。）
2. **想继续用原密码**：只要 `/data/plugin/transmission/data/credential.json` 不在了，就把
   `credential.json.bak-<时间戳>` 改名回 `credential.json`；如果插件配置也一起被清掉了，再把
   `settings.json.bak-<时间戳>` 改名回 `settings.json`，然后**重启插件服务**（或点「启动服务」）。
3. **只丢了 `credential.json`**（配置还在，插件起不来容器）：插件页会提示「WebUI 凭据文件缺失」。
   点「修改目录」，此时新密码是**必填**的，目录可以不动，确认后容器会按原目录重建并写入新凭据。
   旧密码找不回来的话，走第 1 条重新初始化也是一样效果。

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

## 入站端口自检

容器固定发布三个端口：`9091/tcp`（WebUI）、`51413/tcp` 与 `51413/udp`（BT 入站）。

**Docker 会骗人**：`inspect` 里的 `PortBindings` / `NetworkSettings.Ports` 只是「声明过要发布」。
真正把宿主端口接上的是用户态的 `docker-proxy` 进程，它异常退出后宿主上就没人监听，
而 `inspect` 依然显示端口已绑定——BT 的 **TCP** 入站就是这样悄悄消失的
（UDP 与 WebUI 还正常，所以从页面上看不出来，只有「测试端口」报不可达）。

所以插件在「启动服务」时会直接读 `/proc/net/tcp{,6}`、`/proc/net/udp{,6}`，核对这三个端口
在宿主机上**是否真的有监听**；缺了就重启一次容器把端口绑定重新下发（比重建容器便宜，
不动 `/config` 也不重拉镜像），并在日志里留一行：

```text
transmission: 入站端口未监听 51413/tcp，重启容器修复
```

**只在启动时查一次不够**：实测 `docker-proxy` 会在容器继续运行时悄悄死掉（进程变成僵尸，
dmesg 里既没有 OOM 也没有 segfault），而 dockerd 是带 `iptables: false` 启动的、没有 DNAT 兜底，
端口一死局域网和公网同时不通，页面上那条提示要等到下次手动启动才消失。所以插件服务里还挂着
一条 `port_keeper` 定时器，**每 60 秒**再核对一次，缺了走同样的重启修复；两次修复至少隔 10 分钟，
免得反复重启打断正在下的任务。手动启停进行中、或服务本来就是停的，定时器一律不插手。

仍然缺的话，插件页面的端口那一行下方会显示「入站端口未发布：…」，接口
`/api/status` 的 `ports` 字段里也有 `published` / `missing` / `repaired`。

### docker-proxy 为什么会死：fd 上限（2026-09-28 定位）

一开始只知道「51413/tcp 会消失、UDP 和 9091 一直好、dmesg 里既没有 OOM 也没有
segfault」，所以先加了运行期巡检兜着。后来抓到了完整链条：

1. `/tmp/dockerd/daemon.json` 里是 `"iptables": false`，**发布的端口完全靠用户态
   `docker-proxy`**，没有 DNAT 兜底；
2. `docker-proxy` 每条代理连接占 **2 个 fd**，而 systemd 给 `docker.service` 的
   **软**上限只有 1024（`LimitNOFILESoft=1024`，硬上限 524288，即 systemd 默认值）；
3. transmission 的 `peer-limit-global` 是 1000，入站连接一多就撞上 1024，
   `Accept()` 返回 `EMFILE`，docker-proxy 的 accept 循环**出错后直接 return**
   （进程是正常退出，所以没有任何"被杀"的痕迹），监听端口随之消失；
4. UDP 代理走的是另一套循环、9091 连接数很少，所以**只有 51413/tcp 反复死**。
   实测 01:25–07:13 之间死了 14 次，巡检每次重启容器都能修好，但连接涨回来又死——
   这就是"巡检自动重启还是没用"的真相。

自查（把 `<pid>` 换成 51413/tcp 那个 docker-proxy）：

```bash
systemctl show docker.service -p LimitNOFILESoft          # 1024 就是病根
grep 'Max open files' /proc/<pid>/limits                  # soft / hard
ls /proc/<pid>/fd | wc -l                                 # 当前用量
```

一次性修法（写 drop-in 后重启 dockerd，重启后依然生效）：

```bash
mkdir -p /etc/systemd/system/docker.service.d
cat > /etc/systemd/system/docker.service.d/override.conf <<'EOF'
[Service]
LimitNOFILE=524288
EOF
systemctl daemon-reload
systemctl restart docker          # 容器会全部停下
systemctl restart xiaomi-transmission.service   # RestartPolicy=no，得手动拉回
```

修复后实测：所有 docker-proxy 的 fd 软上限从 1024 变成 **524288**，
51413/tcp 当前用量约 400 fd（原来是上限的 40%），`port-test` 仍返回
`port-is-open: true`。运行期巡检**保留作兜底**，正常情况下不会再被触发；
如果哪天 NAS 固件升级把 drop-in 冲掉了，按上面三条命令再确认一次即可。

## 定时开启 / 关闭全部任务

第二个卡片底部有两个选择框，各有 **25 个选项**：`关闭` + `0 点` … `23 点`。

- **定时开启全部任务**：选中的钟点到了，对**全部任务**发一次 `torrent-start`；
- **定时关闭全部任务**：选中的钟点到了，发一次 `torrent-stop`。

细节：

- **按钟点、每天一次**：选 `3 点` 就是每天 03:00 执行（今天的 03:00 已过就顺延到明天）。
  两个定时互相独立，直接选不同钟点即可（例如 3 点开、23 点关）。
- 页面在两个定时都关闭时不显示提示，有启用的会显示一行「下次：MM-DD HH:mm 开启 · …」。
- 设置存在插件配置里（`schedule.start` / `schedule.stop`），重启插件、重启设备都不丢；
  改设置**不会立刻执行**——想马上生效就进控制台手动开始。
- 执行动作放在插件服务里的一个定时线程（60 秒一轮，和端口巡检、路由器映射续期并列）。
  服务没起来、或账号缺失时只记一行日志并把下一次顺延到明天的同一钟点，不会每分钟重试。
- 接口：`/api/status` 的 `schedule` 段；`POST /api/schedule`，body 为
  `{"kind": "start"|"stop", "value": "off"|"0".."23"}`。

## 页面状态点与提示

状态卡片的「测试端口 / 映射端口」两个按钮并排在**同一行**，按钮前面的状态是**圆点**而不是文字：
绿=正常（公网可达 / 路由器已转发）、红=异常（仅局域网可达 / 转发失败）、灰=未知或未测。
圆点与按钮文字垂直居中、不带外圈光晕；完整说明放在圆点的 `title`（长按/悬停可见）里，
点按钮后的结果用页面底部的**提示条**显示。

## 首页小组件：为什么做不了

试过把 transmission 注册成 app 首页的小组件，结论是**第三方插件做不了**。实测过程：

1. registry 的 `frontend.widget` 字段客户端是认的——写进去之后，app 的「小组件 → 应用」里
   会出现 `Transmission 下载`，点开能看到卡片可以添加，**客户端还会来取卡片图标**
   （`GET /plugin/<user>/transmission/assets/transmission.png` → 200）。
2. 但**卡片正文不从插件取**：`url` 填 `/widget.html` 时，客户端一次都没请求过这个路径；
   `url` 填插件页面时卡片是空白；`url` 留空时同样报错。三种都失败，报的是「连接超时」，
   也就是正文另有来源（客户端/云端按 widget 类型提供），第三方没有这一环。
3. 官方唯一的先例是 `mediacenter` 的两张卡片：`url` 全是空串（原生绘制），数据走它自己的
   LuCI 路由 `POST /cgi-bin/luci/mediacenter/media_recently_watched`——而 LuCI 路由是
   nginx 里写死的 location + 系统二进制，第三方插件注册不了。

所以数字放在插件页顶部的统计栏：**下载/上传实时速度、做种与下载中的任务数、
本次运行（daemon 启动以来）的累计上传/下载量**，随页面 3 秒一轮刷新。
数据来自 `/api/status` 的 `transfer` 段（`downloaded`/`uploaded` 取 `current-stats`，
即本次启动以来，不是历史总量），取不到时该段为 `null`，不影响状态接口本身。

## 路由器端口映射（UPnP / NAT-PMP）

BT 要能从外面连上，路由器上得有 `51413` 的转发。**容器里 Transmission 自带的 UPnP 不管用**：
它上报给路由器的 internal client 是 Docker 网桥地址（172.17.0.x），路由器路由不到。
所以由插件**从宿主机**发起映射：

- 「启动服务」时自动试一次（`ensure_port_forward()`）：顺序是
  **UPnP**（SSDP 发现 → 读设备描述 → `AddPortMapping`，TCP 与 UDP 各一条），
  不行再退到 **NAT-PMP**（RFC 6886）。
- **尽力而为**：拿不到映射只记录原因，绝不阻断启动。页面上那一行会写清楚卡在哪一步
  （例如 `UPnP 错误 501（Action Failed）`），旁边「映射端口」按钮可随时重试。
- 映射的内网目标固定是 NAS 的局域网地址（按默认路由探测，不会错拿 docker0 的地址），
  端口为 `51413` TCP + UDP；`/api/status` 的 `forward` 段给出 `ok` / `method` /
  `gateway` / `external` / `mapped` / `detail`。
- 成不成功以 Transmission 自己的 `port-test` 为准（它问外部检测服务，返回
  `port-is-open`），别只看路由器说「建好了」。
- **续期**：UPnP 用 `lease=0`（永久映射，建成后不再打扰路由器）；NAT-PMP 的租期由路由器
  给定（实测 7200 秒），插件在**到期一半**时自动重建；失败的话每 30 分钟重试一次
  （路由器重启、UPnP 刚被打开这类情况能自愈）。定时器跑在插件服务里，60 秒一轮。
- **停止服务时会把映射删掉**：容器都停了，那条转发没人应答；而且 UPnP 用的是永久映射，
  不删的话卸载插件后它会一直留在路由器上。页面那行会变成「路由器映射已移除」，
  下次「启动服务」再建回来。
- 删除按**记录下来的方式**做（UPnP `DeletePortMapping` / NAT-PMP opcode 3、4），所以映射
  结果要落盘（`<插件数据目录>/forward.json`）：插件服务停止时 systemd 的 `ExecStopPost`
  是**另一个进程**，只靠内存状态删不掉。存下来的 UPnP 控制地址可能过期（路由器重启会换
  临时端口，实测 41795 → 33299），删失败会重新发现一次再试；「本来就没有这条」（714）
  不算失败。

### 实测踩到的坑（UniFi / UCG-Fiber）

- **UPnP 的 Secure Mode 开着时，`AddPortMapping` 一律返回 `501 Action Failed`**，
  连 NAT-PMP 也返回 `result=3`（网络故障）。现象很有迷惑性：能 SSDP 发现路由器、能读到
  外网 IP、能查映射表，但**任何端口的映射都建不了**（换 50000/12345/6881 一样 501）。
  把 UPnP 的 Secure Mode 关掉就通了。
- 同一个设置页里的 **NAT-PMP 建议一起打开**，UPnP 被拒时才有兜底可退。

实现只用标准库（`upnp.py`），不发任何东西到第三方服务；失败原因都来自路由器返回的
SOAP fault（如 `501 Action Failed`）或 NAT-PMP 的 result code。

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
