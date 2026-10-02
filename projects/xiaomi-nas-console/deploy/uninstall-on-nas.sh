#!/usr/bin/env bash
# 卸载「控制台」：停服务、删入口、清注册表条目。
#
#   NAS_IP=192.168.1.8 NAS_SSH_KEY=/path/to/key bash deploy/uninstall-on-nas.sh
#
# 可选环境变量：
#   PURGE=1   同时删除 /data/plugin/xiaomi-nas-console（含访问令牌）
#   默认保留该目录，方便改主意时重装。

set -euo pipefail

if [[ -z "${NAS_IP:-}" || -z "${NAS_SSH_KEY:-}" ]]; then
  printf '请先设置 NAS_IP 与 NAS_SSH_KEY。\n' >&2
  exit 2
fi

NAS_USER_ID="${NAS_USER_ID:-}"
PURGE="${PURGE:-0}"
REMOTE_TARGET="root@${NAS_IP}"

SSH_OPTIONS=(
  -i "${NAS_SSH_KEY}"
  -o BatchMode=yes
  -o IdentitiesOnly=yes
  -o StrictHostKeyChecking=accept-new
)

if [[ -z "${NAS_USER_ID}" ]]; then
  NAS_USER_ID="$(ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" \
    'set -- /data/plugin/u*.list; for path in "$@"; do name=${path##*/}; printf "%s\n" "${name%.list}"; done')"
  if [[ -z "${NAS_USER_ID}" ]]; then
    printf '无法识别小米用户，请设置 NAS_USER_ID。\n' >&2
    exit 2
  fi
  printf '识别到小米用户：%s\n' "${NAS_USER_ID}"
fi

ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" "NAS_USER_ID='${NAS_USER_ID}' PURGE='${PURGE}' sh -s" <<'REMOTE'
set -eu

systemctl stop xiaomi-nas-console.service 2>/dev/null || true
systemctl disable xiaomi-nas-console.service 2>/dev/null || true
rm -f /etc/systemd/system/xiaomi-nas-console.service
systemctl daemon-reload 2>/dev/null || true

for target in /etc/nginx/conf.d/luci/xiaomi-nas-console.conf /etc/nginx/conf.d/xiaomi-nas-console-lan.conf; do
  if [ -f "${target}" ]; then
    rm -f "${target}"
    echo "已删除 ${target}"
  fi
done
if nginx -t >/dev/null 2>&1; then
  systemctl reload nginx || true
else
  echo '警告：nginx -t 失败，请手动检查 /etc/nginx' >&2
fi

# 清注册表条目（先备份）
python3 - "${NAS_USER_ID}" <<'PY'
import json, os, sys, time
from pathlib import Path

path = Path(f'/data/plugin/{sys.argv[1]}.list')
if path.is_file():
    registry = json.loads(path.read_text(encoding='utf-8'))
    if 'nasconsole' in registry:
        backup = path.with_name(f'{path.name}.before-remove-nasconsole-{int(time.time())}.bak')
        backup.write_bytes(path.read_bytes())
        os.chmod(backup, 0o600)
        registry.pop('nasconsole')
        temporary = path.with_suffix(path.suffix + '.nasconsole.tmp')
        temporary.write_text(json.dumps(registry, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        os.chmod(temporary, 0o644)
        temporary.replace(path)
        print(f'已从 {path} 移除 nasconsole 条目')
    else:
        print(f'{path} 里没有 nasconsole 条目')
PY

rm -rf "/home/${NAS_USER_ID}/plugin/nasconsole"

if [ "${PURGE}" = "1" ]; then
  rm -rf /data/plugin/xiaomi-nas-console
  echo '已删除 /data/plugin/xiaomi-nas-console'
else
  echo '保留 /data/plugin/xiaomi-nas-console（含访问令牌）；PURGE=1 可一并删除'
fi

echo '卸载完成。请完全退出并重新打开小米智能存储客户端。'
REMOTE
