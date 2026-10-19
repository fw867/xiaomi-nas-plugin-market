# SSH 开关（小米智能存储社区插件）

在小米智能存储客户端里启停 SSH 远程登录，并可设置开机自动保持运行。

## 功能

- **启动 / 停止 SSH**：直接控制 `dropbear.socket`，无需登录终端。
- **开机自动启动**：开启后每次开机自动保持 SSH 可用，两种守护方式可选（见下）。
- 状态实时显示：当前 SSH 是否在监听。

## 两种守护方式

| 方式 | 原理 | 特点 |
| --- | --- | --- |
| **每分钟巡检**（默认） | crontab 里一条 `* * * * * sh keepalive.sh #@sshcontrol` | 兼容性最好；关机/开机后最多一分钟内恢复；每分钟一次检查 |
| **存储池挂载时** | 往厂商的 `syshotplug` 框架放钩子 `/etc/syshotplug/pool/98.ssh-control`，池挂载（`ACTION=mounted`）时被调用 | 事件驱动、不轮询；本机实测它比厂商关 SSH 还早几秒，所以钩子内部会 fork 后台任务、等 `ssh_check` 跑完再拉起并复查几分钟 |
| **两者都开**（推荐） | 上面两条同时启用 | 事件驱动 + 轮询兜底 |

## 这个钩子为什么不能"挂载时立刻 start"

`/etc/syshotplug/<事件>/` 是小米自己的热插拔框架：`/usr/sbin/syshotplug <事件>` 会按
文件名（`LC_ALL=C`）依次执行目录里**带可执行位**的脚本，并把 `ACTION` 传进去。
`/etc/syshotplug/pool/` 就是存储池事件组，厂商自己在里面挂了 27 个钩子
（`00.fs_prepare`、`40.plugin`（`plugincenter boot --third --pool`）、`40.distfs` …），
阶段划分是 `00-19 基础 / 20-39 业务服务 / 40-59 模块 / 70-79 数据库重载 / 80-89 慢活 /
90-99 收尾`，所以钩子取名 `98.ssh-control` 落在收尾阶段。

但本机实测（2026-09-28 14:42 那次开机）**池挂载事件比厂商关 SSH 还早**：

```
pool mounted 事件        14:42:17   ← 钩子被调用（/var/run/distfs_pool_ready 的 mtime）
minas.boot_check 判定    14:42:22   ← ssh_check 在这里 stop dropbear.socket
dropbear 重新可用        14:43:01   ← 插件每分钟的 keepalive 拉回来的
```

所以钩子不能直接 `systemctl start`（会被几秒后的 `ssh_check` 关掉），而是
**fork 一个后台任务**：先等 20 秒让 `ssh_check` 跑完，再拉起并在随后几分钟里复查几次。
fork 是刻意的——`syshotplug` 默认并行调用各钩子，串行模式（`-w`/`-s`）下也不该让我们
这几十秒拖慢别的钩子。社区工具
[kid0114/xiaomi-nas-ssh-root-tool](https://github.com/kid0114/xiaomi-nas-ssh-root-tool)
用的是同一个挂载点，本插件在此基础上加了"尊重开关"和"延迟复查"。

## 为什么需要这个插件

`/lib/minas/boot_check.sh` 的 `ssh_check()` 每次开机都会执行：

```sh
ssh_check() {
    channel="$(jq -r '.channel' /usr/share/minas.version)"
    [ "factory" = "$SYSMODE" ] && open="1"                     # 工厂模式
    [ "develop" = "$channel" ] && open="1"                     # develop 通道
    [ "true" = "$(mitee_tool rpmb get ssh_en)" ] && open="1"   # RPMB 标志
    if [ "1" = "$open" ]; then systemctl start dropbear.socket
    else                       systemctl stop  dropbear.socket; fi
}
```

普通用户的设备三项都不满足（sysmode 为 `user`、channel 为 `release`、RPMB 标志为空），
所以**每次开机 SSH 都会被关掉**。

官方设计的持久化方式是写 RPMB 标志：

```sh
mitee_tool rpmb set ssh_en true
```

但**部分设备的 RPMB 写入失效**，该命令会报错：

```
rpmb set verify failed
```

此时标志无法持久化，只能靠开机后重新拉起 `dropbear.socket`。本插件即为此提供
一个可开关的图形界面：开启「开机自动启动」后，插件会注册一个每分钟的守护任务，
在系统关掉 SSH 之后把它拉回来。

## 工作原理

```text
界面（小米客户端内嵌页）
   │  /plugin/<用户>/sshcontrol/api/...
   ▼
xiaomi-ssh-control.service  127.0.0.1:18130
   │
   ├─ 启停：systemctl start/stop dropbear.socket
   └─ 自启：写 /data/plugin/ssh-control/state.json（autostart + method）
            ├─ 方式含 cron：增删 crontab 条目
            │     * * * * * sh .../current/keepalive.sh #@sshcontrol
            └─ 方式含 hotplug：写 /etc/syshotplug/pool/98.ssh-control（0755）
                  → overlay 落到 /data/etc/upper/syshotplug/pool/98.ssh-control
```

`keepalive.sh` 每分钟检查一次：开关为开且 SSH 未运行时，执行 `systemctl start dropbear.socket`。
`hotplug.sh` 只在 `ACTION=mounted` 且开关为开时动手，且是后台延迟复查（原因见上）。

## 安全边界

- 服务只监听 `127.0.0.1:18130`，不暴露到局域网。
- 插件自身需要 root 才能控制系统服务，与其它社区插件一致。
- **开启开机自启等于让 SSH 长期可用**，请确保已使用密钥登录并妥善保管私钥。
- 手动点「停止 SSH」会同时关闭开机自启，避免守护进程立刻把它拉回来。

## 安装

通过插件市场安装：

1. 在小米智能存储客户端打开「插件市场」。
2. 在列表中找到「SSH 开关」，点击安装。
3. 重新打开客户端，在「全部应用」中打开「SSH 开关」。

## 状态与日志

- 状态文件：`/data/plugin/ssh-control/state.json`（`autostart`、`method`）
- 巡检日志：`/data/plugin/ssh-control/keepalive.log`
- 钩子路径：`/etc/syshotplug/pool/98.ssh-control`（持久层
  `/data/etc/upper/syshotplug/pool/98.ssh-control`）
- 钩子日志：`logger -t sshcontrol.hotplug`（系统日志里搜 `sshcontrol.hotplug`）
- 系统日志：`journalctl -u xiaomi-ssh-control.service`
- 本插件注册的 crontab 行带 `#@sshcontrol` 标记，可据此定位或清理。

## 开发

```bash
python3 -m unittest discover -s tests -v
```

## SSH 执行通道修复（v0.1.8 起）

### 问题

这台固件的 `dropbear` 被厂商改过：二进制里**硬编码**了子程序路径
`/usr/sbin/mi-shell`（`strings /usr/sbin/dropbearmulti | grep mi-shell` 可见）。
而 `mi-shell` 是个 **JSON 协议受限壳**（`cJSON_Parse` + 读 stdin，含 `illegal cmd [%s]`），
它**忽略 argv 里的 `-c 命令`**。结果就是：

```bash
ssh root@nas 'id'      # 认证成功、能看到登录 banner，但命令根本没执行
ssh root@nas 'id > /tmp/x'   # 连 /tmp/x 都不会被创建
```

实测结论（2026-10-19）：与「socket 激活 / 独立进程」无关，与公钥/密码认证方式也无关 ——
是**二进制层面**的行为，`-s`、`-B`、`-p 2222` 都改变不了。

### 修法（可逆，不改原文件）

根分区是 **只读 erofs**，改不了二进制。所以用 **bind-mount** 把 `mi-shell` 顶替成
一个 30 字节的垫片：

```sh
#!/bin/sh
exec /bin/bash "$@"
```

- 插件的 `deploy/mi-shell-shim` 会被复制到 `/data/plugin/ssh-control/state/mi-shell-shim`；
- 挂载后 `ssh root@nas 'id'` 立刻正常（`uid=0(root)`）；
- **`umount /usr/sbin/mi-shell` 立即完全还原**，原文件从未被修改（md5 不变）。

### 三层内容（都由插件的 `deploy/98.ssh-restore` 承担）

固化成 `/etc/syshotplug/pool/98.ssh-restore`（**每次池挂载 / 开机**执行一次，幂等）：

1. 补齐两处 `authorized_keys`（`/etc/dropbear/` 与 `root` 的 `~/.ssh/`），`0600 root`；
2. root 密码哈希被清空/锁死时，从母本 `/data/plugin/ssh-control/state/root-shadow` 写回；
3. 挂上 `mi-shell` 垫片；顺带确保 `dropbear.socket` 在跑。

母本（公钥、哈希、垫片）都在 `/data/plugin/ssh-control/state/` 下，`0700` 目录、文件 `0600/0755`。
钩子里的路径与用户 id **由插件按本机实际情况生成**，不写死任何设备。

### 怎么用

- 插件页面「SSH 执行通道」卡片显示 **已修复 / 未修复 / 不适用**；
- **一键修复**：装垫片 + 收公钥/哈希母本 + 装钩子 + 立刻挂载；
- **一键还原**：`umount` + 删钩子。**不影响公钥登录本身**，也不会删除你配置的公钥。

### 怎么彻底卸载

在 App 里卸载本插件即可 —— 卸载时会执行 `scripts/control disable`，里面已经做了：

```sh
umount /usr/sbin/mi-shell
rm -f /etc/syshotplug/pool/98.ssh-restore
rm -f /data/plugin/ssh-control/state/mi-shell-shim
```

手工卸载等价于上面三行。**注意**：`/etc/dropbear/authorized_keys` 与 root 的
`~/.ssh/authorized_keys` **不会**被删除——那是你自己的登录凭据。

### 验证

```bash
ssh root@nas 'id'          # 修复后：uid=0(root) gid=0(root) ...
umount /usr/sbin/mi-shell  # 还原后：又变成"认证成功但无输出"
```

### 已知边界

- 只在本固件（`1.0.9.462`，`dropbear v2022.83`）上验证过；若以后厂商改回正常 dropbear，
  状态会显示「不适用」（`/usr/sbin/mi-shell` 不存在时不做任何事）。
- 垫片只把 `mi-shell` 这一条路径顶替掉；其它厂商功能不受影响。
- 重启后靠 `/etc/syshotplug/pool/98.ssh-restore` 自动恢复（已实测过一次真实重启）。

### 实现细节：为什么必须用 `nsenter`

第一版是直接在插件进程里 `mount --bind`，结果**看起来成功、实际无效**：

```
插件自己的 status ：shimMounted = true
主机（PID1）视角   ：/proc/1/mounts 里没有这一条  ⇒ dropbear 仍然拿到 mi-shell
```

原因：`xiaomi-ssh-control.service` 里有 `PrivateTmp=true`，systemd 据此给服务建了
**独立的 mount namespace**（实测 `mnt:[4026532708]` vs PID1 的 `mnt:[4026531841]`），
服务内的挂载对主机不可见。

所以现在：

- **挂载 / 卸载 / 跑钩子** 都走 `nsenter -t 1 -m -- <cmd>`，落到 PID1 的命名空间；
- **"是否已挂载"的判断**读 `/proc/1/mounts`（不是 `/proc/mounts`，后者是服务自己的视角）；
- 保留 `PrivateTmp=true`（不为了这功能牺牲硬化）。

另外还有一层**主机侧兜底**：应用店的开机钩子 `community-plugins-boot.sh` 每分钟在
**cron（主机命名空间）** 里检查一次，发现没挂就自己挂上（日志
`ssh: mounted mi-shell shim (host namespace)`）。实测手动 `umount` 后 **≤60 秒**自动恢复。

> 这一层很重要：syshotplug 的 pool 钩子**并非每次开机都会跑到**（实测 22:13 那次重启就没有
> 任何 `98.ssh-restore` 日志），只靠它会出现"重启后 SSH 又不能执行命令"的缺口。

用户点「一键还原」后 `state.json` 会写 `execFix: false`，上述两层都会**尊重该意图**不再挂回去。
