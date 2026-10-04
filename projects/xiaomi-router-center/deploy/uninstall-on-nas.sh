#!/usr/bin/env bash
# 卸载「路由器软件中心」：停服务、删 nginx 入口、摘注册表条目、删插件目录。
# 默认保留 /data/plugin/router-center/settings.json 与 router-token；PURGE=1 时一并删除。
set -euo pipefail

NAS_IP="${NAS_IP:-}"
NAS_SSH_KEY="${NAS_SSH_KEY:-}"
NAS_USER_ID="${NAS_USER_ID:-u3943892}"
PURGE="${PURGE:-0}"

[[ -n "${NAS_IP}" ]] || { printf '请设置 NAS_IP\n' >&2; exit 2; }

SSH_OPTIONS=(-o BatchMode=yes -o StrictHostKeyChecking=accept-new)
[[ -n "${NAS_SSH_KEY}" ]] && SSH_OPTIONS+=(-i "${NAS_SSH_KEY}")
REMOTE_TARGET="root@${NAS_IP}"

ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" "
set -eu
systemctl disable --now xiaomi-router-center.service 2>/dev/null || true
rm -f /etc/systemd/system/xiaomi-router-center.service
rm -f /etc/nginx/conf.d/luci/xiaomi-router-center.conf
systemctl daemon-reload
nginx -t && systemctl reload nginx
rm -rf /home/${NAS_USER_ID}/plugin/rtrcenter
rm -f /data/plugin/www/icon/router-center.icon
python3 - <<'PY'
import json, pathlib
path = pathlib.Path('/data/plugin/${NAS_USER_ID}.list')
if path.is_file():
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        data = {}
    if isinstance(data, dict) and data.pop('rtrcenter', None) is not None:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print('已从注册表移除 rtrcenter')
PY
if [ '${PURGE}' = '1' ]; then
  rm -rf /data/plugin/router-center
  echo '已删除 /data/plugin/router-center（含设置与令牌）'
else
  rm -rf /data/plugin/router-center/releases /data/plugin/router-center/current
  echo '保留 /data/plugin/router-center 下的设置与令牌（PURGE=1 可一并删除）'
fi
"
printf '卸载完成。\n'
