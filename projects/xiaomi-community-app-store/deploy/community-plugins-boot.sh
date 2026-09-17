#!/bin/sh
# 开机后自动拉起运行时新增的社区插件服务。
#
# 背景：小米智能存储的根文件系统是只读 erofs，/etc 是 overlay，upperdir 位于
# /data/etc/upper。systemd 在 overlay 挂载之前就已读取单元目录，因此安装时新增到
# /etc/systemd/system/ 的 unit 虽然 enabled、符号链接也正确，开机时却不在
# multi-user.target 的依赖集合里，不会被自动拉起。
# 本脚本由 root crontab 每分钟触发：自动发现 overlay 中运行时新增且已启用的
# 服务，在开机窗口内重新加载并启动；全部在跑时立即退出，几乎无开销。
#
# 可选配置：/data/plugin/community-plugins-boot.conf
#   BOOT_WINDOW=600   仅在上电后该秒数内尝试启动（默认 600 秒）
#   UNIT_DIR=...      覆盖 unit 扫描目录

LOG=/data/plugin/community-plugins-boot.log
UNIT_DIR=/data/etc/upper/systemd/system
BOOT_WINDOW=600

[ -f /data/plugin/community-plugins-boot.conf ] && . /data/plugin/community-plugins-boot.conf

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $1" >> "$LOG" 2>/dev/null
    logger -t community-plugins-boot "$1" 2>/dev/null || true
}

# 收集运行时新增的服务（只查文件系统，不依赖 systemd 状态）
services=""
for unit in "$UNIT_DIR"/*.service; do
    [ -f "$unit" ] || continue
    services="$services ${unit##*/}"
done
[ -z "$services" ] && exit 0

# 全部在跑 → 立即退出（常态路径）
all_up=1
for svc in $services; do
    systemctl is-active --quiet "$svc" || { all_up=0; break; }
done
[ "$all_up" -eq 1 ] && exit 0

# 只在开机窗口内动手，避免干扰管理员手动停止的服务
uptime_sec="$(cut -d. -f1 /proc/uptime 2>/dev/null || echo 0)"
[ "$uptime_sec" -gt "$BOOT_WINDOW" ] && exit 0

# 让 systemd 重新读取 overlay 上的 unit 文件
systemctl daemon-reload 2>/dev/null

for svc in $services; do
    systemctl is-enabled --quiet "$svc" 2>/dev/null || continue
    systemctl is-active --quiet "$svc" && continue
    if systemctl start "$svc" >/dev/null 2>&1; then
        log "started $svc"
    else
        log "failed to start $svc"
    fi
done

exit 0
