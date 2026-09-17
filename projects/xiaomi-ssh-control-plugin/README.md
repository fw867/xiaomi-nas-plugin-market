# SSH 开关（小米智能存储社区插件）

在小米智能存储客户端里启停 SSH 远程登录，并可设置开机自动保持运行。

## 功能

- **启动 / 停止 SSH**：直接控制 `dropbear.socket`，无需登录终端。
- **开机自动启动**：开启后每次开机自动保持 SSH 可用。
- 状态实时显示：当前 SSH 是否在监听。

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
   └─ 自启：写 /data/plugin/ssh-control/state.json
            + 增删 crontab 条目
                 * * * * * .../current/keepalive.sh #@sshcontrol
```

`keepalive.sh` 每分钟检查一次：开关为开且 SSH 未运行时，执行
`systemctl start dropbear.socket`。

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

- 状态文件：`/data/plugin/ssh-control/state.json`
- 守护日志：`/data/plugin/ssh-control/keepalive.log`
- 系统日志：`journalctl -u xiaomi-ssh-control.service`
- 本插件注册的 crontab 行带 `#@sshcontrol` 标记，可据此定位或清理。

## 开发

```bash
python3 -m unittest discover -s tests -v
```
