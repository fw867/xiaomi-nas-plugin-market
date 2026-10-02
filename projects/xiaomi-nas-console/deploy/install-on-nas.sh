#!/usr/bin/env bash
# 把「控制台」安装/更新到已授权的自有小米智能存储上。
#
# 用法（在电脑上执行，需要能 SSH 登录 NAS 的私钥）：
#   NAS_IP=192.168.1.8 NAS_SSH_KEY=/path/to/key bash deploy/install-on-nas.sh
#
# 可选环境变量：
#   NAS_USER_ID  小米用户 ID（u3943892），不设则自动识别
#   PLUGIN_ID    插件编号，默认 11020（冲突时换一个未占用的正整数）
#   LAN_PORT     浏览器入口端口，不设则沿用本机已装的端口，首次安装默认 5001（仅局域网）
#
# 安装内容：
#   /data/plugin/xiaomi-nas-console/releases/<版本-时间>/   程序与前端
#   /data/plugin/xiaomi-nas-console/current -> 上面那个目录
#   /data/plugin/xiaomi-nas-console/admin-token             浏览器入口的访问令牌
#   /data/plugin/xiaomi-nas-console/state.json              端口等运行状态
#   /home/<用户>/plugin/nasconsole/                         小米客户端插件结构（INFO/src/ui）
#   /etc/systemd/system/xiaomi-nas-console.service          控制台服务
#   /etc/nginx/conf.d/luci/xiaomi-nas-console.conf          客户端入口（443）
#   /etc/nginx/conf.d/xiaomi-nas-console-lan.conf           浏览器入口（LAN_PORT）
#   /data/plugin/<用户>.list                                新增 nasconsole 条目（先备份）
#
# 写操作只有：桌面入口开关/端口（自己的那一个 nginx 配置文件）、文件窗口的上传 /
# 新建文件夹 / 重命名 / 删除（删除先进各存储根下的 .console-trash 回收站）。

set -euo pipefail

if [[ -z "${NAS_IP:-}" || -z "${NAS_SSH_KEY:-}" ]]; then
  printf '请先设置 NAS_IP 与 NAS_SSH_KEY，例如：\n  NAS_IP=192.168.1.8 NAS_SSH_KEY=~/.ssh/id_ed25519 bash deploy/install-on-nas.sh\n' >&2
  exit 2
fi

NAS_USER_ID="${NAS_USER_ID:-}"
PLUGIN_ID="${PLUGIN_ID:-11020}"
LAN_PORT="${LAN_PORT:-}"

if [[ ! "${PLUGIN_ID}" =~ ^[1-9][0-9]*$ ]]; then
  printf 'PLUGIN_ID 必须是正整数。\n' >&2
  exit 2
fi
# LAN_PORT 允许留空：留空表示"沿用这台机器上已安装的端口"（下面连上 NAS 后再决定）
if [[ -n "${LAN_PORT}" ]] && { [[ ! "${LAN_PORT}" =~ ^[0-9]+$ ]] || (( LAN_PORT < 1024 || LAN_PORT > 65535 )); }; then
  printf 'LAN_PORT 必须是 1024-65535 的端口号。\n' >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
VERSION="$(tr -d ' \t\r\n' < "${PROJECT_DIR}/VERSION")"
RELEASE_ID="v${VERSION}-$(date +%Y%m%d%H%M%S)"
REMOTE_ROOT="/data/plugin/xiaomi-nas-console"
REMOTE_RELEASE="${REMOTE_ROOT}/releases/${RELEASE_ID}"
REMOTE_TARGET="root@${NAS_IP}"
TEMP_DIR="$(mktemp -d -t xiaomi-nas-console.XXXXXX)"

cleanup() { rm -rf "${TEMP_DIR}"; }
trap cleanup EXIT

for required in \
  "${PROJECT_DIR}/engine.py" \
  "${PROJECT_DIR}/server.py" \
  "${PROJECT_DIR}/web/index.html" \
  "${PROJECT_DIR}/web/app.js" \
  "${PROJECT_DIR}/web/styles.css" \
  "${PROJECT_DIR}/assets/xiaomi-nas-console.png"
do
  if [[ ! -f "${required}" ]]; then
    printf '缺少发布文件：%s\n' "${required}" >&2
    exit 2
  fi
done

SSH_OPTIONS=(
  -i "${NAS_SSH_KEY}"
  -o BatchMode=yes
  -o IdentitiesOnly=yes
  -o PreferredAuthentications=publickey
  -o PubkeyAuthentication=yes
  -o PasswordAuthentication=no
  -o KbdInteractiveAuthentication=no
  -o StrictHostKeyChecking=accept-new
)

ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" 'true'

# 端口：显式给了 LAN_PORT 就用它；否则沿用已安装的端口（App 里改过就保留），
# 都没有才用默认 5001。这样重装不会把用户在 App 里设的端口改回去。
if [[ -z "${LAN_PORT}" ]]; then
  REMOTE_PORT="$(printf '%s\n' \
    'import json, pathlib' \
    'path = pathlib.Path("/data/plugin/xiaomi-nas-console/state.json")' \
    'try:' \
    '    print(int(json.loads(path.read_text(encoding="utf-8"))["lan_port"]))' \
    'except Exception:' \
    '    print("")' \
    | ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" 'python3 -' || true)"
  LAN_PORT="${REMOTE_PORT:-5001}"
fi
if [[ ! "${LAN_PORT}" =~ ^[0-9]+$ ]] || (( LAN_PORT < 1024 || LAN_PORT > 65535 )); then
  printf 'LAN_PORT 必须是 1024-65535 的端口号。\n' >&2
  exit 2
fi
printf '桌面入口端口：%s\n' "${LAN_PORT}"

if [[ -z "${NAS_USER_ID}" ]]; then
  if ! NAS_USER_ID="$(ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" 'set -- /data/plugin/u*.list; if [ "$#" -eq 1 ] && [ -f "$1" ]; then name=${1##*/}; printf "%s\n" "${name%.list}"; else exit 1; fi')"; then
    printf '无法自动识别小米用户（存在多个 u*.list）。请显式设置 NAS_USER_ID。\n' >&2
    exit 2
  fi
  printf '自动识别小米用户：%s\n' "${NAS_USER_ID}"
fi

sed -e "s|__NAS_USER_ID__|${NAS_USER_ID}|g" \
  "${SCRIPT_DIR}/xiaomi-nas-console.nginx.conf" > "${TEMP_DIR}/nasconsole-client.conf"
# 带占位符的原文存到插件目录（App 里改端口时用它重新渲染）；
# 渲染好的这一份装到 /etc/nginx/conf.d/
cp "${SCRIPT_DIR}/xiaomi-nas-console-lan.nginx.conf" "${TEMP_DIR}/nasconsole-lan.template.conf"
sed -e "s|__LAN_PORT__|${LAN_PORT}|g" \
  "${SCRIPT_DIR}/xiaomi-nas-console-lan.nginx.conf" > "${TEMP_DIR}/nasconsole-lan.conf"
sed -e "s|__LAN_PORT__|${LAN_PORT}|g" \
  "${SCRIPT_DIR}/xiaomi-nas-console.service" > "${TEMP_DIR}/nasconsole.service"
sed -e "s|__PLUGIN_ID__|${PLUGIN_ID}|g" \
  "${SCRIPT_DIR}/plugin-meta.json" > "${TEMP_DIR}/plugin-meta.json"

REMOTE_UI_ROOT="/home/${NAS_USER_ID}/plugin/nasconsole"

printf '1/8 创建目录 …\n'
ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" \
  "mkdir -p '${REMOTE_RELEASE}/web' '${REMOTE_RELEASE}/lan' '${REMOTE_ROOT}/releases' '${REMOTE_UI_ROOT}/src/ui' /data/plugin/www/icon"

printf '2/8 上传程序与前端 …\n'
scp "${SSH_OPTIONS[@]}" "${PROJECT_DIR}/engine.py" "${PROJECT_DIR}/server.py" "${PROJECT_DIR}/VERSION" \
  "${REMOTE_TARGET}:${REMOTE_RELEASE}/"
tar -C "${PROJECT_DIR}/web" -czf - . | ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" "tar -xzf - -C '${REMOTE_RELEASE}/web'"
tar -C "${PROJECT_DIR}/web" -czf - . | ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" "tar -xzf - -C '${REMOTE_UI_ROOT}/src/ui'"
scp "${SSH_OPTIONS[@]}" "${PROJECT_DIR}/assets/xiaomi-nas-console.png" \
  "${REMOTE_TARGET}:/data/plugin/www/icon/nas-console.icon"

printf '3/8 安装 systemd 服务 …\n'
scp "${SSH_OPTIONS[@]}" "${TEMP_DIR}/nasconsole.service" "${REMOTE_TARGET}:/tmp/xiaomi-nas-console.service"
ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" \
  "ln -sfn '${REMOTE_RELEASE}' '${REMOTE_ROOT}/current' && install -m 0644 /tmp/xiaomi-nas-console.service /etc/systemd/system/xiaomi-nas-console.service"

printf '4/8 生成访问令牌与状态文件 …\n'
ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" "
set -eu
token_file='${REMOTE_ROOT}/admin-token'
if [ ! -s \"\${token_file}\" ]; then
  umask 077
  openssl rand -hex 16 > \"\${token_file}\"
fi
chmod 600 \"\${token_file}\"
state='${REMOTE_ROOT}/state.json'
# 端口状态每次安装都写齐，保证和刚装好的 nginx 配置一致
printf '{\n  \"lan_port\": ${LAN_PORT}\n}\n' > \"\${state}\"
"

printf '5/8 安装 nginx 入口 …\n'
scp "${SSH_OPTIONS[@]}" "${TEMP_DIR}/nasconsole-client.conf" "${REMOTE_TARGET}:/tmp/nasconsole-client.conf"
scp "${SSH_OPTIONS[@]}" "${TEMP_DIR}/nasconsole-lan.conf" "${REMOTE_TARGET}:/tmp/nasconsole-lan.conf"
# 再把带占位符的入口模板放进 release 目录（和商店安装包的 runtime/lan 一致）：
# App 里的开关/端口设置用它重新渲染
scp "${SSH_OPTIONS[@]}" "${TEMP_DIR}/nasconsole-lan.template.conf" \
  "${REMOTE_TARGET}:${REMOTE_RELEASE}/lan/xiaomi-nas-console-lan.conf"
ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" 'sh -s' <<'REMOTE_NGINX'
set -eu
install_conf() {
  source_file="$1"
  target="$2"
  backup="${target}.before-$(date +%s).bak"
  if [ -f "${target}" ]; then cp -p "${target}" "${backup}"; fi
  install -m 0644 "${source_file}" "${target}"
  if ! nginx -t; then
    if [ -f "${backup}" ]; then mv "${backup}" "${target}"; fi
    nginx -t || true
    return 1
  fi
  return 0
}
install_conf /tmp/nasconsole-client.conf /etc/nginx/conf.d/luci/xiaomi-nas-console.conf
install_conf /tmp/nasconsole-lan.conf /etc/nginx/conf.d/xiaomi-nas-console-lan.conf
REMOTE_NGINX

printf '6/8 注册小米客户端入口 …\n'
ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" \
  "python3 - --user-id '${NAS_USER_ID}' --plugin-id '${PLUGIN_ID}' --version '${VERSION}'" \
  < "${SCRIPT_DIR}/register_plugin.py"

printf '7/8 补齐小米插件结构 …\n'
scp "${SSH_OPTIONS[@]}" "${SCRIPT_DIR}/control" "${REMOTE_TARGET}:/tmp/nasconsole-control"
ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" \
  "python3 - --user '${NAS_USER_ID}' --name nasconsole --plugin-id '${PLUGIN_ID}' --version '${VERSION}' --control /tmp/nasconsole-control" \
  < "${SCRIPT_DIR}/native_layout.py"

printf '8/8 启动并自检 …\n'
ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" "systemctl daemon-reload && systemctl enable xiaomi-nas-console.service >/dev/null 2>&1 && systemctl restart xiaomi-nas-console.service && systemctl reload nginx"

for attempt in $(seq 1 20); do
  if ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" 'curl -fsS --max-time 5 http://127.0.0.1:18100/healthz >/dev/null 2>&1'; then
    break
  fi
  if [[ "${attempt}" == "20" ]]; then
    printf '服务健康检查超时。请查看：journalctl -u xiaomi-nas-console.service -n 50\n' >&2
    exit 1
  fi
  sleep 1
done

TOKEN="$(ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" "cat '${REMOTE_ROOT}/admin-token'")"
LAN_CHECK="$(ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" "curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:${LAN_PORT}/")"

printf '\n安装完成。\n'
printf '  桌面版（电脑浏览器）：http://%s:%s/      访问令牌：%s\n' "${NAS_IP}" "${LAN_PORT}" "${TOKEN}"
printf '  小米客户端：完全退出并重新打开小米智能存储，在「全部应用」里打开「控制台」\n'
printf '              （手机端只有开关/地址/令牌，可在那里启用或停用桌面入口）\n'
printf '  服务状态： systemctl status xiaomi-nas-console.service\n'
printf '  浏览器入口首页自检： HTTP %s\n' "${LAN_CHECK}"
printf '  请勿把 %s 端口转发到公网。\n' "${LAN_PORT}"
