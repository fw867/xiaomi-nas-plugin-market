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
