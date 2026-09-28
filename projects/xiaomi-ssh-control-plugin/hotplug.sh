#!/bin/sh
# 存储池挂载钩子：由厂商的 /usr/sbin/syshotplug 在存储池挂载后调用（ACTION=mounted）。
#
# 这条路（社区工具 kid0114/xiaomi-nas-ssh-root-tool 用的也是它）比每分钟 cron 更
# "事件驱动"：池一挂上就跑，不依赖轮询。但**本机实测它比厂商关 SSH 还早几秒**
# （2026-09-28 14:42 那次开机）：
#
#     pool mounted 事件        14:42:17   ← 本钩子被调用的时刻
#     minas.boot_check 判定    14:42:22   ← ssh_check 在这里 stop dropbear.socket
#     dropbear 重新可用        14:43:01   ← 是插件每分钟的 keepalive 拉回来的
#
# 所以不能立刻 start（会被随后的 ssh_check 关掉），而是 fork 一个后台任务：等
# ssh_check 跑完再拉起，并在随后几分钟里复查几次。后台执行是刻意的——syshotplug
# 默认并行调用各钩子，串行模式（-w/-s）下也不该让我们这几十秒拖慢别的钩子。
#
# 与插件状态文件联动：用户关掉「开机自动启动」时直接退出，不擅自拉起 SSH。

STATE=/data/plugin/ssh-control/state.json
UNIT=dropbear.socket

[ "${ACTION:-}" = "mounted" ] || exit 0

grep -q '"autostart"[[:space:]]*:[[:space:]]*true' "$STATE" 2>/dev/null || exit 0
systemctl is-active --quiet "$UNIT" && exit 0

(
    # 等厂商的 boot_check 跑完（本机约在池挂载后 5 秒~30 秒之间）
    sleep 20
    for _ in 1 2 3 4 5 6; do
        systemctl is-active --quiet "$UNIT" && exit 0
        if systemctl start "$UNIT" >/dev/null 2>&1; then
            logger -t sshcontrol.hotplug "started $UNIT after pool mount" 2>/dev/null || true
        fi
        sleep 20
    done
) &

exit 0
