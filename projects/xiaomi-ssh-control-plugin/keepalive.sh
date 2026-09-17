#!/bin/sh
# 保持 SSH 常驻（由 crontab 每分钟触发，仅在开关打开时存在）。
#
# 背景：/lib/minas/boot_check.sh 的 ssh_check() 每次开机检查
# sysmode=factory / channel=develop / RPMB 标志 ssh_en=true，
# 三者皆不满足时执行 `systemctl stop dropbear.socket`。
# 部分设备 RPMB 写入失效，ssh_en 无法持久化，开机后 SSH 会被关掉，
# 本脚本负责把它拉回来。

LOG=/data/plugin/ssh-control/keepalive.log
UNIT=dropbear.socket

# 开关关闭则不动
if ! grep -q '"autostart"[[:space:]]*:[[:space:]]*true' /data/plugin/ssh-control/state.json 2>/dev/null; then
    exit 0
fi

systemctl is-active --quiet "$UNIT" && exit 0

if systemctl start "$UNIT" >/dev/null 2>&1; then
    mkdir -p "$(dirname "$LOG")" 2>/dev/null
    echo "$(date '+%Y-%m-%d %H:%M:%S') started $UNIT" >> "$LOG" 2>/dev/null
    logger -t sshcontrol "started $UNIT" 2>/dev/null || true
fi

exit 0
