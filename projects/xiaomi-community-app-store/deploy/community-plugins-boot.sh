#!/bin/sh
# 开机后自动拉起运行时新增的社区插件服务，并可保持指定服务常驻。
#
# 背景一（插件）：小米智能存储的根文件系统是只读 erofs，/etc 是 overlay，
# upperdir 位于 /data/etc/upper。systemd 在 overlay 挂载之前就已读取单元目录，
# 因此安装时新增到 /etc/systemd/system/ 的 unit 虽然 enabled、符号链接也正确，
# 开机时却不在 multi-user.target 的依赖集合里，不会被自动拉起。
#
# 背景二（SSH）：/lib/minas/boot_check.sh 的 ssh_check() 每次开机都会检查
#   sysmode=factory / channel=develop / RPMB 标志 ssh_en=true
# 三者皆不满足时执行 `systemctl stop dropbear.socket` 关闭 SSH。
# 若设备 RPMB 写入失效（mitee_tool rpmb set ssh_en true 报 "rpmb set verify failed"），
# 该标志无法持久化，SSH 每次开机都会被关掉；本脚本可在其后重新拉起。
#
# 本脚本由 root crontab 每分钟触发；目标服务全部在跑时立即退出，几乎无开销。
#
# 可选配置：/data/plugin/community-plugins-boot.conf
#   BOOT_WINDOW=600   仅在上电后该秒数内尝试启动（默认 600 秒）
#   UNIT_DIR=...      覆盖扫描目录（默认 /data/etc/upper/systemd/system）
#   EXTRA_UNITS="..." 额外需保持运行的服务，空格分隔，例如：
#                      EXTRA_UNITS="dropbear.socket"

LOG=/data/plugin/community-plugins-boot.log
UNIT_DIR=/data/etc/upper/systemd/system
BOOT_WINDOW=600
EXTRA_UNITS=""

[ -f /data/plugin/community-plugins-boot.conf ] && . /data/plugin/community-plugins-boot.conf

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $1" >> "$LOG" 2>/dev/null
    logger -t community-plugins-boot "$1" 2>/dev/null || true
}

# 恢复被 plugincenter 强制卸载的注册表条目与 UI。
# plugincenter boot 会对已装插件跑小米签名校验，第三方插件必然失败并被
# 「force uninstall」：删除 /home/<用户>/plugin/<key>/ 并从注册表移除条目。
# 该脚本幂等，条目与 UI 都在位时立即退出。
RESTORE=/data/plugin/community-store/current/deploy/restore-plugins.py
if [ -f "$RESTORE" ]; then
    if python3 "$RESTORE" --quiet 2>/dev/null; then
        :
    fi
fi

# 修复 Docker 的 NAT 规则（幂等）。
# /etc/iptables/iptables.rules 是个 0 字节空文件，而 iptables.service 在开机早期
# 执行 `iptables-restore -w -- /etc/iptables/iptables.rules` —— 该命令默认清空所有链，
# Docker 自己建的 MASQUERADE / DOCKER 链被一并抹掉；docker.service 晚两秒启动时
# 因 docker0 已存在而没有重建，规则就此永久缺失，后果是所有容器都出不了网
# （容器内域名解析失败、连不上任何外网地址），而**入站端口映射因为另有
# docker-proxy 兜着、看起来仍然正常**，所以很容易被误判成「只有某个插件坏了」。
# 这是全设备所有容器共用的依赖，因此放在这里统一维护。
if command -v iptables >/dev/null 2>&1; then
    if ! iptables -t nat -C POSTROUTING -s 172.17.0.0/16 ! -o docker0 -j MASQUERADE 2>/dev/null; then
        if iptables -t nat -A POSTROUTING -s 172.17.0.0/16 ! -o docker0 -j MASQUERADE 2>/dev/null; then
            log "restored docker bridge MASQUERADE rule"
        else
            log "failed to restore docker bridge MASQUERADE rule"
        fi
    fi
fi

# 收集运行时新增的服务（只查文件系统，不依赖 systemd 状态）
services=""
for unit in "$UNIT_DIR"/*.service; do
    [ -f "$unit" ] || continue
    services="$services ${unit##*/}"
done
services="$services $EXTRA_UNITS"
services="$(printf '%s' "$services" | xargs 2>/dev/null)"
[ -z "$services" ] && exit 0

# 目标全部在跑 → 立即退出（常态路径）
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
