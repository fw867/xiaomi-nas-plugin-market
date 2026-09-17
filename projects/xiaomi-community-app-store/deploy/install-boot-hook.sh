#!/bin/sh
# 安装/卸载开机自启钩子（幂等）。
#
# 用法：
#   sh install-boot-hook.sh add      安装（写入启动脚本 + crontab 条目）
#   sh install-boot-hook.sh remove   卸载
#
# 说明见 community-plugins-boot.sh 顶部：小米 NAS 的 /etc 是 overlay，
# systemd 在 overlay 挂载前读取单元目录，安装时新增的 unit 开机不会被拉起，
# 因此用 root crontab 的每分钟任务在开机后自动补启动。

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BOOT_SCRIPT="/data/plugin/community-plugins-boot.sh"
CRON_CMD="* * * * * $BOOT_SCRIPT #@community.plugins"

ACTION="${1:-add}"

install_hook() {
    if [ ! -f "${SCRIPT_DIR}/community-plugins-boot.sh" ]; then
        echo "缺少 community-plugins-boot.sh" >&2
        exit 1
    fi
    mkdir -p /data/plugin
    cp "${SCRIPT_DIR}/community-plugins-boot.sh" "$BOOT_SCRIPT"
    chmod 755 "$BOOT_SCRIPT"

    current="$(crontab -l 2>/dev/null || true)"
    if printf '%s\n' "$current" | grep -qF "$BOOT_SCRIPT"; then
        echo "crontab 条目已存在"
    else
        { printf '%s\n' "$current"; echo "$CRON_CMD"; } | sed '/^$/d' | crontab -
        echo "已添加 crontab 开机钩子"
    fi
}

remove_hook() {
    crontab -l 2>/dev/null | grep -vF "$BOOT_SCRIPT" | crontab - || true
    rm -f "$BOOT_SCRIPT" /data/plugin/community-plugins-boot.log
    echo "已移除开机钩子"
}

case "$ACTION" in
    add)    install_hook ;;
    remove) remove_hook ;;
    *)      echo "用法：$0 {add|remove}" >&2; exit 2 ;;
esac
