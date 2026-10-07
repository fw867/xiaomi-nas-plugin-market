#!/usr/bin/env bash
# 卸载「Home Assistant」：停服务、删 nginx 入口、摘注册表条目、删插件程序与客户端 UI。
#
# 用法：
#   NAS_IP=192.168.1.8 NAS_SSH_KEY=/path/to/key bash deploy/uninstall-on-nas.sh
#
# 可选环境变量：
#   NAS_USER_ID   小米用户 ID，默认 u3943892
#   PURGE         1 = 连 /data/plugin/homeassistant 一起删
#                 （默认 0：保留 data/，也就保住了默认落在插件私有目录里的
#                  Home Assistant 配置；**用户存储里的配置目录永远不碰**）
#   DROP_CONTAINER 1 = 同时删除本插件的 Home Assistant 容器
#                 （默认 0：只停服务，容器留给用户自己决定；容器里的 /config
#                  是 bind 挂载，删容器不会删数据）
set -euo pipefail

NAS_IP="${NAS_IP:-}"
NAS_SSH_KEY="${NAS_SSH_KEY:-}"
NAS_USER_ID="${NAS_USER_ID:-u3943892}"
PURGE="${PURGE:-0}"
DROP_CONTAINER="${DROP_CONTAINER:-0}"
SERVICE_NAME='xiaomi-homeassistant.service'
CONTAINER_NAME='xiaomi-plugin-homeassistant'

[[ -n "${NAS_IP}" ]] || { printf '请设置 NAS_IP，例如 NAS_IP=192.168.1.8 bash deploy/uninstall-on-nas.sh\n' >&2; exit 2; }

SSH_OPTIONS=(-o BatchMode=yes -o StrictHostKeyChecking=accept-new)
[[ -n "${NAS_SSH_KEY}" ]] && SSH_OPTIONS+=(-i "${NAS_SSH_KEY}")
REMOTE_TARGET="root@${NAS_IP}"

ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" "
set -eu
# 先停服务：它会带 --stop-owned 停掉自己建的容器，并收好 enabled=false
systemctl disable --now ${SERVICE_NAME} 2>/dev/null || true
rm -f /etc/systemd/system/${SERVICE_NAME}
rm -f /etc/nginx/conf.d/luci/xiaomi-homeassistant.conf
systemctl daemon-reload
nginx -t && systemctl reload nginx
rm -rf /home/${NAS_USER_ID}/plugin/homeassistant
rm -f /data/plugin/www/icon/homeassistant.icon
python3 - <<'PY'
import json, pathlib
path = pathlib.Path('/data/plugin/${NAS_USER_ID}.list')
if path.is_file():
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        data = {}
    if isinstance(data, dict) and data.pop('homeassistant', None) is not None:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print('已从注册表移除 homeassistant')
PY
if [ '${DROP_CONTAINER}' = '1' ]; then
  if command -v docker >/dev/null 2>&1; then
    docker rm -f '${CONTAINER_NAME}' >/dev/null 2>&1 || true
    echo '已删除容器 ${CONTAINER_NAME}（/config 是 bind 挂载，数据未删）'
  else
    echo '警告：设备上没有 docker 命令，容器 ${CONTAINER_NAME} 可能还在（可用插件页「重新初始化」清理）' >&2
  fi
fi
if [ '${PURGE}' = '1' ]; then
  rm -rf /data/plugin/homeassistant
  echo '已删除 /data/plugin/homeassistant（含插件私有数据；用户存储里的配置目录未动）'
else
  rm -rf /data/plugin/homeassistant/releases /data/plugin/homeassistant/current
  echo '保留 /data/plugin/homeassistant/data（PURGE=1 可一并删除）'
fi
"
printf '卸载完成。\n'
printf '  注意：用户存储里的 Home Assistant 配置目录（初始化时选的那个）一个文件都没动。\n'
