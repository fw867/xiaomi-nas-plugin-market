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
#   DOCKER_REPAIR_RETRY=600   修复失败/被推迟后，隔多少秒才再试（默认 600 秒）
#   DOCKER_READY_WAIT=90   拉起插件服务前最多等 docker 就绪多少秒（默认 90）
#   DOCKER_NAT_ENFORCE=1   把 UCI 的 mi_docker.globals.iptables 保持为 1（默认 0=不管）。
#                          出厂默认是 0（Docker 不碰 iptables，发布端口全走用户态代理）；
#                          显式开了 DNAT 的机器可以置 1：固件升级若把这个开关重置回 0，
#                          钩子会设回 1 并重启 docker，把 DNAT 恢复回来。
#   DOCKER_FD_LIMITS=      仅测试用：改成读某个假的 limits 文件
#   DOCKER_NAT_MISSING=    仅测试用：假装这些发布端口缺 DNAT 规则（如 "9091 51413"）
#   DOCKER_REPAIR_DRYRUN=1 仅测试用：只记录、不写文件也不重启 docker
#   UPTIME_FILE=/proc/uptime  仅测试用：换成假的开机时长文件

LOG=/data/plugin/community-plugins-boot.log
UNIT_DIR=/data/etc/upper/systemd/system
BOOT_WINDOW=600
EXTRA_UNITS=""
# 这几个刻意用 ${VAR:-默认} 取默认值：配置文件（下面 source）能覆盖，
# 环境变量也能覆盖 —— 后者是给测试留的口子（见文件头的 DOCKER_* 说明）。
DOCKER_FD_MIN=${DOCKER_FD_MIN:-65536}
DOCKER_REPAIR_RETRY=${DOCKER_REPAIR_RETRY:-${DOCKER_FD_RETRY:-600}}
DOCKER_READY_WAIT=${DOCKER_READY_WAIT:-90}
DOCKER_NAT_ENFORCE=${DOCKER_NAT_ENFORCE:-0}
DOCKER_FD_LIMITS=${DOCKER_FD_LIMITS:-}
DOCKER_NAT_MISSING=${DOCKER_NAT_MISSING:-}
DOCKER_REPAIR_DRYRUN=${DOCKER_REPAIR_DRYRUN:-${DOCKER_FD_DRYRUN:-0}}
UPTIME_FILE=${UPTIME_FILE:-/proc/uptime}

[ -f /data/plugin/community-plugins-boot.conf ] && . /data/plugin/community-plugins-boot.conf

DOCKER_DROPIN=/etc/systemd/system/docker.service.d/override.conf
DOCKER_REPAIR_MARK=/data/plugin/.docker-repair-retry
DOCKER_SOCK=/var/run/docker.sock
# 上次「在跑」的容器清单：开机后按它把插件容器拉回来。原因见文件末尾 restore_containers()
DOCKER_LAST_RUNNING=/data/plugin/.docker-last-running

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $1" >> "$LOG" 2>/dev/null
    logger -t community-plugins-boot "$1" 2>/dev/null || true
}

uptime_seconds() {
    cut -d. -f1 "$UPTIME_FILE" 2>/dev/null || echo 0
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

# ===== Docker 依赖修复（两项，都需要重启 dockerd 才能生效）=====
#
# 一、发布端口的 DNAT 规则
# 本机把 dockerd 配成托管 iptables 后（UCI mi_docker.globals.iptables=1），
# 发布端口由内核 DNAT（nat 表的 DOCKER 链）转发——外部流量不再经过用户态
# docker-proxy，省掉每连接 2 个 fd 与两次用户态拷贝；回环流量仍由 docker-proxy
# 兜着（Docker 的 OUTPUT 规则排除 127.0.0.0/8），所以远程访问隧道依赖的
# 127.0.0.1:<端口> 不受影响。
# 风险：开机早期 iptables.service 会 `iptables-restore` 一个 0 字节规则文件
# （等于清空所有链）。本机的顺序是先清链再起 docker（docker 依赖 network-online
# → network-pre → iptables.service），所以正常情况没问题；但厂商脚本若在 docker
# 之后又清一次，DOCKER 链就永久缺失——表现是**容器出不了网、所有发布端口不通**。
# 判据用「正在运行且声明了发布端口的容器，是否有对应 DNAT 规则」，没有容器发布
# 端口时不判为故障（Docker 只在第一个端口映射时创建 DOCKER 链）。
#
# 二、dockerd 的 fd 软上限
# systemd 默认软上限只有 1024。DNS/NAT 都在内核时，用户态代理只服务回环流量，
# 压力小得多；但若哪天又退回 `iptables: false`（发布端口全走代理），一个代理连接
# 占 2 个 fd，BT 把 peer 上限开到 1000 就会撞穿 1024 → `Accept()` 报 EMFILE →
# docker-proxy 的 accept 循环出错即 return（进程正常退出，dmesg 里没有痕迹）→
# 宿主端口消失。定位过程见 transmission 插件 README。
#
# 共同处置：位置刻意排在"拉起插件服务"**之前**（这时插件容器要么还没建、要么
# 还没起，重启不打断插件启动流程）；重启前记下在跑的容器、重启后按原样拉起
# （插件容器 RestartPolicy 是 no）；有插件服务在 activating、或容器在 restarting
# 时推迟到下一分钟；失败/推迟后 DOCKER_REPAIR_RETRY 秒内不重试，避免反复重启。
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

# Docker 现在是否托管 iptables（daemon.json 里 "iptables" 为 false 才算不托管；缺省=托管）
docker_manages_iptables() {
    config=/tmp/dockerd/daemon.json
    [ -f "$config" ] || return 0
    value="$(python3 -c '
import json, sys
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    print("true")
    raise SystemExit
print("false" if data.get("iptables") is False else "true")' "$config" 2>/dev/null)"
    [ "$value" != "false" ]
}

# 把 UCI 的 iptables 开关设回 1（只有 DOCKER_NAT_ENFORCE=1 时才做）
# 输出：set / would-set / 空
enforce_docker_nat() {
    [ "$DOCKER_NAT_ENFORCE" = "1" ] || return 0
    command -v uci >/dev/null 2>&1 || return 0
    current="$(uci -q get mi_docker.globals.iptables 2>/dev/null)"
    [ "$current" = "1" ] && return 0
    if [ "$DOCKER_REPAIR_DRYRUN" = "1" ]; then
        printf 'would-set'
        return 0
    fi
    if uci set mi_docker.globals.iptables='1' 2>/dev/null && uci commit mi_docker 2>/dev/null; then
        printf 'set'
    fi
}

# 返回"正在运行且发布了端口、却没有对应 DNAT 规则"的端口；没有则输出空
docker_nat_missing_ports() {
    if [ -n "$DOCKER_NAT_MISSING" ]; then          # 仅测试用：假装这些端口缺规则
        printf '%s' "$DOCKER_NAT_MISSING"
        return
    fi
    [ -S "$DOCKER_SOCK" ] || return
    docker_api /containers/json | python3 -c '
import json, subprocess, sys
try:
    data = json.load(sys.stdin)
except Exception:
    raise SystemExit
want = set()
for item in data:
    if item.get("State") != "running":
        continue
    for port in item.get("Ports") or []:
        if port.get("PublicPort"):
            want.add(int(port["PublicPort"]))
if not want:
    raise SystemExit
try:
    rules = subprocess.run(["iptables", "-t", "nat", "-S", "DOCKER"],
                           capture_output=True, text=True, timeout=15).stdout
except Exception:
    raise SystemExit
have = set()
for line in rules.splitlines():
    if "DNAT" in line and "--dport" in line:
        try:
            have.add(int(line.split("--dport")[1].split()[0]))
        except (IndexError, ValueError):
            pass
missing = sorted(want - have)
if missing:
    print(" ".join(str(port) for port in missing))' 2>/dev/null
}

# 需要重启 dockerd 的原因（空 = 不用动）
docker_repair_reason() {
    reason=""
    current="$(dockerd_fd_soft_limit)"
    case "$current" in
        ''|*[!0-9]*) ;;                                # 读不到就不折腾
        *)
            if [ "$current" -lt "$DOCKER_FD_MIN" ]; then
                write_docker_fd_dropin
                reason="dockerd fd 软上限 $current 偏低"
            fi
            ;;
    esac
    nat_state="$(enforce_docker_nat)"
    if [ "$nat_state" = "set" ]; then
        reason="${reason:+$reason；}iptables 开关曾被重置，已设回 mi_docker.globals.iptables=1"
    elif [ "$nat_state" = "would-set" ]; then
        reason="${reason:+$reason；}iptables 开关被重置（dry-run：会设回 1）"
    fi
    # 只有 Docker 确实托管 iptables 时才要求 DNAT 规则：否则出厂默认（iptables=0）的
    # 机器会被误判成"端口缺规则"，每分钟重启一次 docker。
    if [ "$nat_state" = "set" ] || docker_manages_iptables; then
        missing="$(docker_nat_missing_ports 2>/dev/null)"
        if [ -n "$missing" ]; then
            reason="${reason:+$reason；}发布端口 $missing 缺少 DNAT 规则"
        fi
    fi
    printf '%s' "$reason"
}

write_docker_fd_dropin() {
    grep -qE '^[[:space:]]*LimitNOFILE=' "$DOCKER_DROPIN" 2>/dev/null && return 0
    [ "$DOCKER_REPAIR_DRYRUN" = "1" ] && return 0
    mkdir -p "$(dirname "$DOCKER_DROPIN")" 2>/dev/null
    printf '%s\n' '[Service]' 'LimitNOFILE=524288' > "$DOCKER_DROPIN" 2>/dev/null
    chmod 644 "$DOCKER_DROPIN" 2>/dev/null
    systemctl daemon-reload 2>/dev/null
}

repair_settle() {
    # 有插件服务在启动、或容器在 restarting 时不适合重启 dockerd
    for svc in $services; do
        [ "$(systemctl is-active "$svc" 2>/dev/null)" = "activating" ] && return 1
    done
    if [ "$DOCKER_REPAIR_DRYRUN" != "1" ] && [ -S "$DOCKER_SOCK" ]; then
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

repair_cooldown_ok() {
    # 刚开机时忽略上一轮开机留下的冷却标记：systemd 在 /etc overlay 挂载前就读完了
    # 单元目录，所以**开机时看不到 drop-in**，dockerd 会带着旧上限（1024）起来——
    # 这种"开机才发现要修"的情况必须立刻修，不能先等十分钟冷却。
    [ "$(uptime_seconds)" -lt "$BOOT_WINDOW" ] && return 0
    [ -f "$DOCKER_REPAIR_MARK" ] || return 0
    last="$(cat "$DOCKER_REPAIR_MARK" 2>/dev/null)"
    case "$last" in ''|*[!0-9]*) return 0 ;; esac
    now="$(date +%s)"
    [ $((now - last)) -ge "$DOCKER_REPAIR_RETRY" ]
}

wait_docker_ready() {
    # 拉插件服务之前先等 docker 起来：插件服务一启动就会去 start 自己的容器，
    # docker 还没就绪就会失败一次；没有自愈巡逻的插件（例如 Jellyfin）于是一直停着。
    waited=0
    while [ "$waited" -lt "$DOCKER_READY_WAIT" ]; do
        docker_api /_ping >/dev/null 2>&1 && return 0
        sleep 3
        waited=$((waited + 3))
    done
    return 1
}

snapshot_running_containers() {
    # 记下当前在跑的容器名。每分钟刷新一次，所以"用户手动停掉"的状态会在一分钟内
    # 被如实记录；只有最后一次刷新之后才停的才会被误判成"该跑"。
    [ -S "$DOCKER_SOCK" ] || return 0
    if names="$(docker_api /containers/json | python3 -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    raise SystemExit(3)
print(" ".join(c["Names"][0].lstrip("/") for c in data if c.get("State") == "running"))' 2>/dev/null)"; then
        printf '%s\n' "$names" > "$DOCKER_LAST_RUNNING.tmp" 2>/dev/null && \
            mv -f "$DOCKER_LAST_RUNNING.tmp" "$DOCKER_LAST_RUNNING" 2>/dev/null
    fi
}

restore_containers() {
    # 按上次在跑的清单把容器拉回来。关机时 docker 会给容器发 SIGTERM，它们的
    # RestartPolicy 是 no，所以重启后不会自己回来；插件服务只会启动自己的 HTTP 服务，
    # 不一定去 start 容器（各插件行为不一样），因此统一在这里兜底。
    [ -f "$DOCKER_LAST_RUNNING" ] || return 0
    for name in $(cat "$DOCKER_LAST_RUNNING" 2>/dev/null); do
        [ -n "$name" ] || continue
        state="$(docker_api "/containers/$name/json" | python3 -c '
import json, sys
try:
    print(json.load(sys.stdin).get("State", {}).get("Running"))
except Exception:
    print("unknown")' 2>/dev/null)"
        [ "$state" = "True" ] && continue
        if docker_api "/containers/$name/start" post >/dev/null 2>&1; then
            log "restored container $name"
        else
            log "failed to restore container $name"
        fi
    done
}

resync_plugin_service() {
    # 容器名 xiaomi-plugin-xxx ↔ 服务名 xiaomi-xxx.service。
    # 重启 docker 会把容器换掉，而插件服务并不知情：它的内存状态还停在老容器上，
    # 路由器端口映射（transmission 这类插件在 start() 里建立）也不会自己回来——
    # 实测少了这一步，公网 BT 端口会一直关到插件的重试间隔（半小时）才恢复。
    # 重启插件服务是幂等的：容器已经在跑时 start() 只做端口与映射的同步，不会重建。
    case "$1" in
        xiaomi-plugin-*) unit="xiaomi-${1#xiaomi-plugin-}.service" ;;
        *) return 0 ;;
    esac
    systemctl is-active --quiet "$unit" || return 0
    if systemctl restart "$unit" >/dev/null 2>&1; then
        log "restarted $unit（重新同步容器与路由器映射）"
    else
        log "failed to restart $unit"
    fi
}

restart_docker_safely() {
    # $1 = 原因（写进日志）
    date +%s > "$DOCKER_REPAIR_MARK" 2>/dev/null
    # 重启前一定 daemon-reload：systemd 是在 /etc overlay 挂载之前读的单元目录，开机时
    # 它并不知道 /etc/systemd/system/docker.service.d/override.conf（fd 上限）存在；
    # 少了这一步，这一轮重启拿到的还是旧上限，下一轮才发现没修好、又重启一次
    # （2026-09-28 实测：14:42:08 那次重启后仍是 1024，14:43:19 才修成 524288）。
    systemctl daemon-reload 2>/dev/null
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
        log "重启 docker 修复：$1（重启后 fd 软上限 $(dockerd_fd_soft_limit)，缺 DNAT 的端口 $(docker_nat_missing_ports)）"
        for id in $running; do
            short="$(printf '%s' "$id" | cut -c1-12)"
            if docker_api "/containers/$id/start" post >/dev/null 2>&1; then
                log "restarted container $short"
            else
                log "container $short 未由本脚本拉起（可能已随重启策略自行恢复，或已不存在）"
            fi
            # 用容器名去重后同步对应的插件服务
            name="$(docker_api "/containers/$id/json" | python3 -c '
import json, sys
try:
    print(json.load(sys.stdin).get("Name", "").lstrip("/"))
except Exception:
    pass' 2>/dev/null)"
            [ -n "$name" ] && resync_plugin_service "$name"
        done
    else
        log "重启 docker 失败（原因：$1），下一轮再试"
    fi
}

docker_fix() {
    fix="$(docker_repair_reason)"
    [ -n "$fix" ] || return 0
    if [ "$DOCKER_REPAIR_DRYRUN" = "1" ]; then
        log "需要重启 docker 修复：$fix（dry-run，未执行）"
        return 0
    fi
    repair_cooldown_ok || return 0
    if ! repair_settle; then
        log "需要重启 docker 修复：$fix，但此刻有插件服务在启动，推迟"
        return 0
    fi
    restart_docker_safely "$fix"
}

docker_fix

[ -z "$services" ] && exit 0

uptime_sec="$(uptime_seconds)"

# 过了开机窗口 → 只刷新"在跑容器"清单就退出（不干扰管理员手动停掉的服务）
if [ "$uptime_sec" -gt "$BOOT_WINDOW" ]; then
    snapshot_running_containers
    exit 0
fi

# ---- 开机窗口内：补启动运行时新增的服务，并把上次在跑的容器拉回来 ----
systemctl daemon-reload 2>/dev/null

# docker 没就绪时先等一会：插件服务一起来就要 start 自己的容器
wait_docker_ready || log "等待 docker 就绪超时，仍继续拉起插件服务"

for svc in $services; do
    systemctl is-enabled --quiet "$svc" 2>/dev/null || continue
    systemctl is-active --quiet "$svc" && continue
    if systemctl start "$svc" >/dev/null 2>&1; then
        log "started $svc"
    else
        log "failed to start $svc"
    fi
done

# 还有插件服务在启动（可能正在重建自己的容器）→ 本轮先不动容器，交给下一分钟
if repair_settle; then
    restore_containers
    snapshot_running_containers
else
    log "有插件服务正在启动，本轮跳过容器恢复"
fi

exit 0
