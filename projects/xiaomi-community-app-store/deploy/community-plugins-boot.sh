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
# 背景三（Docker 依赖）：本脚本还负责两件全设备共用的 Docker 修复——
#   1) iptables NAT 的 MASQUERADE 规则（否则所有容器出不了网）
#   2) dockerd 的 fd 软上限（否则发布端口的 docker-proxy 会撞 EMFILE 后退出）
# 两件都幂等，且都刻意排在"拉起插件服务"之前，避免与插件的启动流程互相干扰。
#
# 本脚本由 root crontab 每分钟触发；目标服务全部在跑时立即退出，几乎无开销。
#
# 可选配置：/data/plugin/community-plugins-boot.conf
#   BOOT_WINDOW=600        仅在上电后该秒数内尝试启动（默认 600 秒）
#   UNIT_DIR=...           覆盖扫描目录（默认 /data/etc/upper/systemd/system）
#   EXTRA_UNITS="..."      额外需保持运行的服务，空格分隔，例如：
#                          EXTRA_UNITS="dropbear.socket"
#   DOCKER_FD_MIN=65536    dockerd fd 软上限低于此值就修复（默认 65536）
#   DOCKER_FD_RETRY=600    修复失败/被推迟后，隔多少秒才再试（默认 600 秒）
#   DOCKER_FD_LIMITS=       仅测试用：改成读某个假的 limits 文件
#   DOCKER_FD_DRYRUN=1     仅测试用：只记录、不写文件也不重启 docker

LOG=/data/plugin/community-plugins-boot.log
UNIT_DIR=/data/etc/upper/systemd/system
BOOT_WINDOW=600
EXTRA_UNITS=""
# 这几个刻意用 ${VAR:-默认} 取默认值：配置文件（下面 source）能覆盖，
# 环境变量也能覆盖 —— 后者是给测试留的口子（见文件头的 DOCKER_FD_* 说明）。
DOCKER_FD_MIN=${DOCKER_FD_MIN:-65536}
DOCKER_FD_RETRY=${DOCKER_FD_RETRY:-600}
DOCKER_FD_LIMITS=${DOCKER_FD_LIMITS:-}
DOCKER_FD_DRYRUN=${DOCKER_FD_DRYRUN:-0}

[ -f /data/plugin/community-plugins-boot.conf ] && . /data/plugin/community-plugins-boot.conf

DOCKER_DROPIN=/etc/systemd/system/docker.service.d/override.conf
DOCKER_FD_MARK=/data/plugin/.docker-fd-retry
DOCKER_SOCK=/var/run/docker.sock

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $1" >> "$LOG" 2>/dev/null
    logger -t community-plugins-boot "$1" 2>/dev/null || true
}

docker_api() {
    # $1 = 路径（如 /containers/json），$2 非空时用 POST
    if [ -n "$2" ]; then
        curl -s -m 20 -X POST --unix-socket "$DOCKER_SOCK" "http://localhost$1" 2>/dev/null
    else
        curl -s -m 20 --unix-socket "$DOCKER_SOCK" "http://localhost$1" 2>/dev/null
    fi
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

# 修复 dockerd 的 fd 软上限（幂等）。
# systemd 默认给 docker.service 的软上限只有 1024，而 `/tmp/dockerd/daemon.json`
# 里是 `"iptables": false`——发布的端口完全靠用户态 docker-proxy，没有 DNAT 兜底，
# 而**一个代理连接占 2 个 fd**。BT 类插件把 peer 上限开到 1000 后代理会撞上 1024，
# `Accept()` 返回 EMFILE，docker-proxy 的 accept 循环出错即 return（进程正常退出，
# 所以 dmesg 里没有 OOM、dockerd 也没有 bind 失败），宿主上的入站端口随之消失，
# 只有 UDP 与连接数少的 WebUI 端口活着。定位过程见 transmission 插件 README。
#
# 处置顺序刻意排在"拉起插件服务"**之前**：
#   1) 先写 drop-in（幂等、无副作用；写好后任何一次 docker 启动都会生效，
#      包括设备重启，所以即使本轮不重启 docker 也不会白写）；
#   2) 让**正在运行的** dockerd 也拿到新上限只能重启它，而重启会停掉所有容器。
#      这时候插件容器要么还没建、要么还没起（本脚本接下来才去拉起插件服务），
#      所以在这个位置重启不打断任何插件的启动流程；
#   3) 万一此刻已有插件服务在 activating、或有容器在 restarting（例如管理员正在
#      手动操作、或上一次修复还没落定），就跳过本轮，交给下一分钟；
#   4) 重启前记下在跑的容器，重启后按原样拉起——插件容器的 RestartPolicy 是 no，
#      不拉回来它们会一直停着。
# 只在需要时才动，且失败/推迟后 DOCKER_FD_RETRY 秒内不重试，避免反复重启 docker。
dockerd_fd_soft_limit() {
    if [ -n "$DOCKER_FD_LIMITS" ]; then
        awk '/Max open files/ {print $4}' "$DOCKER_FD_LIMITS" 2>/dev/null
        return
    fi
    pid="$(systemctl show -p MainPID --value docker.service 2>/dev/null)"
    if [ -z "$pid" ] || [ "$pid" = "0" ] || [ ! -r "/proc/$pid/limits" ]; then
        pid="$(pgrep -f '/data/docker/dockerd' 2>/dev/null | head -1)"
    fi
    [ -n "$pid" ] || return
    awk '/Max open files/ {print $4}' "/proc/$pid/limits" 2>/dev/null
}

fd_settle() {
    # 判断现在适不适合重启 dockerd：有插件服务在启动、或容器在 restarting 就不行
    for svc in $services; do
        [ "$(systemctl is-active "$svc" 2>/dev/null)" = "activating" ] && return 1
    done
    if [ "$DOCKER_FD_DRYRUN" != "1" ] && [ -S "$DOCKER_SOCK" ]; then
        unstable="$(docker_api /containers/json | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    raise SystemExit
print(sum(1 for c in data if c.get("State") == "restarting"))' 2>/dev/null)"
        [ "${unstable:-0}" != "0" ] && return 1
    fi
    return 0
}

fd_cooldown_ok() {
    [ -f "$DOCKER_FD_MARK" ] || return 0
    last="$(cat "$DOCKER_FD_MARK" 2>/dev/null)"
    case "$last" in ''|*[!0-9]*) return 0 ;; esac
    now="$(date +%s)"
    [ $((now - last)) -ge "$DOCKER_FD_RETRY" ]
}

fd_fix_dockerd() {
    current="$(dockerd_fd_soft_limit)"
    case "$current" in ''|*[!0-9]*) return 0 ;; esac       # 读不到就不折腾
    [ "$current" -ge "$DOCKER_FD_MIN" ] && return 0       # 已经够大

    wrote=0
    if ! grep -qE '^[[:space:]]*LimitNOFILE=' "$DOCKER_DROPIN" 2>/dev/null; then
        if [ "$DOCKER_FD_DRYRUN" = "1" ]; then
            wrote=2
        else
            mkdir -p "$(dirname "$DOCKER_DROPIN")" 2>/dev/null
            printf '%s\n' '[Service]' 'LimitNOFILE=524288' > "$DOCKER_DROPIN" 2>/dev/null
            chmod 644 "$DOCKER_DROPIN" 2>/dev/null
            systemctl daemon-reload 2>/dev/null
            wrote=1
        fi
    fi

    if ! fd_cooldown_ok; then
        return 0
    fi
    if ! fd_settle; then
        log "dockerd fd 软上限 ${current} 偏低，但此刻有插件服务在启动，推迟修复"
        return 0
    fi
    if [ "$DOCKER_FD_DRYRUN" = "1" ]; then
        log "dockerd fd 软上限 ${current} 偏低：将写 drop-in 并重启 docker（dry-run，未执行）"
        return 0
    fi

    date +%s > "$DOCKER_FD_MARK" 2>/dev/null
    running="$(docker_api /containers/json | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    raise SystemExit
print(" ".join(c.get("Id", "") for c in data if c.get("State") == "running"))' 2>/dev/null)"

    if systemctl restart docker >/dev/null 2>&1; then
        for _ in $(seq 1 30); do
            docker_api /_ping >/dev/null 2>&1 && break
            sleep 2
        done
        now_limit="$(dockerd_fd_soft_limit)"
        log "dockerd fd 软上限 ${current} → ${now_limit:-未知}（drop-in 写入=$wrote，重启 docker）"
        for id in $running; do
            short="$(printf '%s' "$id" | cut -c1-12)"
            if docker_api "/containers/$id/start" post >/dev/null 2>&1; then
                log "restarted container $short"
            else
                log "container $short 未由本脚本拉起（可能已随重启策略自行恢复，或已不存在）"
            fi
        done
    else
        log "重启 docker 失败（drop-in 写入=$wrote）；下次 docker 启动时 drop-in 仍会生效"
    fi
}

fd_fix_dockerd

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
