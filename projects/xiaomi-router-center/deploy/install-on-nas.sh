#!/usr/bin/env bash
# 把「路由器软件中心」安装/更新到小米智能存储上。
#
# 用法（电脑上执行，需要能 SSH 登录 NAS 的私钥）：
#   NAS_IP=192.168.1.8 NAS_SSH_KEY=/path/to/key bash deploy/install-on-nas.sh
#
# 可选环境变量：
#   NAS_USER_ID    小米用户 ID（u3943892），不设则自动识别
#   PLUGIN_ID      插件编号，默认 11021
#   ROUTER_TARGET  路由器软件中心地址，默认 http://192.168.1.1:9958/
#
# 安装内容：
#   /data/plugin/router-center/releases/<版本-时间>/   程序与前端
#   /data/plugin/router-center/current -> 上面那个目录
#   /data/plugin/router-center/settings.json           目标地址（0600 的 router-token 存令牌）
#   /home/<用户>/plugin/rtrcenter/                     小米客户端插件结构（INFO/src/ui/scripts）
#   /etc/systemd/system/xiaomi-router-center.service   插件服务
#   /etc/nginx/conf.d/luci/xiaomi-router-center.conf   客户端入口（443，反代到路由器）
#   /data/plugin/<用户>.list                           注册 nasconsole 之外的 rtrcenter 条目（先备份）
#
# 唯一的写操作：渲染上面那一个 nginx 配置文件，且必须 nginx -t 通过才 reload。

set -euo pipefail

NAS_IP="${NAS_IP:-}"
NAS_SSH_KEY="${NAS_SSH_KEY:-}"
NAS_USER_ID="${NAS_USER_ID:-}"
PLUGIN_ID="${PLUGIN_ID:-11021}"
ROUTER_TARGET="${ROUTER_TARGET:-}"
PLUGIN_PORT="${PLUGIN_PORT:-18101}"

if [[ -z "${NAS_IP}" ]]; then
  printf '请设置 NAS_IP，例如 NAS_IP=192.168.1.8 bash deploy/install-on-nas.sh\n' >&2
  exit 2
fi
if [[ ! "${PLUGIN_ID}" =~ ^[1-9][0-9]*$ ]]; then
  printf 'PLUGIN_ID 必须是正整数。\n' >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
VERSION="$(tr -d ' \t\r\n' < "${PROJECT_DIR}/VERSION")"
RELEASE_ID="v${VERSION}-$(date +%Y%m%d%H%M%S)"
REMOTE_ROOT="/data/plugin/router-center"
REMOTE_RELEASE="${REMOTE_ROOT}/releases/${RELEASE_ID}"
REMOTE_TARGET="root@${NAS_IP}"
TEMP_DIR="$(mktemp -d -t xiaomi-router-center.XXXXXX)"

cleanup() { rm -rf "${TEMP_DIR}"; }
trap cleanup EXIT

for required in \
  "${PROJECT_DIR}/engine.py" \
  "${PROJECT_DIR}/server.py" \
  "${PROJECT_DIR}/web/index.html" \
  "${PROJECT_DIR}/web/app.js" \
  "${PROJECT_DIR}/web/styles.css" \
  "${PROJECT_DIR}/assets/xiaomi-router-center.png"; do
  [[ -f "${required}" ]] || { printf '缺少文件：%s\n' "${required}" >&2; exit 2; }
done

SSH_OPTIONS=(-o BatchMode=yes -o StrictHostKeyChecking=accept-new)
if [[ -n "${NAS_SSH_KEY}" ]]; then
  SSH_OPTIONS+=(-i "${NAS_SSH_KEY}")
fi

printf '1/8 连接并准备目录 …\n'
ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" 'true'
if [[ -z "${NAS_USER_ID}" ]]; then
  if ! NAS_USER_ID="$(ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" 'set -- /data/plugin/u*.list; if [ "$#" -eq 1 ] && [ -f "$1" ]; then name=${1##*/}; printf "%s\n" "${name%.list}"; else exit 1; fi')"; then
    printf '无法自动识别小米用户（存在多个 u*.list）。请显式设置 NAS_USER_ID。\n' >&2
    exit 2
  fi
  printf '自动识别小米用户：%s\n' "${NAS_USER_ID}"
fi

# 目标地址：显式给了就用，否则沿用已装的设置，最后才用默认值
if [[ -z "${ROUTER_TARGET}" ]]; then
  REMOTE_TARGET_URL="$(printf '%s\n' \
    'import json, pathlib' \
    'path = pathlib.Path("/data/plugin/router-center/settings.json")' \
    'try:' \
    '    print(json.loads(path.read_text(encoding="utf-8")).get("target", ""))' \
    'except Exception:' \
    '    print("")' \
    | ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" 'python3 -' || true)"
  ROUTER_TARGET="${REMOTE_TARGET_URL:-http://192.168.1.1:9958/}"
fi
case "${ROUTER_TARGET}" in
  http://*|https://*) ;;
  *) ROUTER_TARGET="http://${ROUTER_TARGET}" ;;
esac
case "${ROUTER_TARGET}" in
  */) ;;
  *) ROUTER_TARGET="${ROUTER_TARGET}/" ;;
esac
printf '路由器软件中心地址：%s\n' "${ROUTER_TARGET}"

REMOTE_UI_ROOT="/home/${NAS_USER_ID}/plugin/rtrcenter"

printf '2/8 渲染客户端入口配置 …\n'
# 正则 location 里不能写带 URI 的 proxy_pass（nginx 会直接报 emerg），所以模板用
# rewrite ... break + 不带 URI 的 proxy_pass；也正因如此目标地址要去掉结尾斜杠。
NAS_USER_NUM="${NAS_USER_ID#u}"
ROUTER_TARGET_NO_SLASH="${ROUTER_TARGET%/}"
sed -e "s|__NAS_USER_ID__|${NAS_USER_ID}|g" \
    -e "s|__NAS_USER_NUM__|${NAS_USER_NUM}|g" \
    -e "s|__PLUGIN_PORT__|${PLUGIN_PORT}|g" \
    -e "s|__ROUTER_TARGET__|${ROUTER_TARGET_NO_SLASH}|g" \
  "${SCRIPT_DIR}/xiaomi-router-center.nginx.conf" > "${TEMP_DIR}/rtrcenter.conf"
sed -e "s|__NAS_USER_ID__|${NAS_USER_ID}|g" \
  "${SCRIPT_DIR}/xiaomi-router-center.service" > "${TEMP_DIR}/rtrcenter.service"

for rendered in "${TEMP_DIR}/rtrcenter.conf" "${TEMP_DIR}/rtrcenter.service"; do
  if grep -qE '__[A-Z_]+__' "${rendered}"; then
    printf '渲染后的 %s 里还有未替换的占位符：\n' "$(basename "${rendered}")" >&2
    grep -nE '__[A-Z_]+__' "${rendered}" >&2
    exit 2
  fi
done

printf '3/8 上传程序、前端与模板 …\n'
ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" \
  "mkdir -p '${REMOTE_RELEASE}/web' '${REMOTE_RELEASE}/deploy' '${REMOTE_ROOT}' '${REMOTE_UI_ROOT}/src/ui' /data/plugin/www/icon"
scp "${SSH_OPTIONS[@]}" "${PROJECT_DIR}/engine.py" "${PROJECT_DIR}/server.py" "${PROJECT_DIR}/VERSION" \
  "${REMOTE_TARGET}:${REMOTE_RELEASE}/"
tar -C "${PROJECT_DIR}/web" -czf - . | ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" "tar -xzf - -C '${REMOTE_RELEASE}/web'"
# 插件目录里的 nginx 模板：服务改目标地址时用它重新渲染
scp "${SSH_OPTIONS[@]}" "${PROJECT_DIR}/deploy/xiaomi-router-center.nginx.conf" \
  "${REMOTE_TARGET}:${REMOTE_RELEASE}/deploy/xiaomi-router-center.nginx.conf"
cp "${PROJECT_DIR}/deploy/control" "${TEMP_DIR}/control"
scp "${SSH_OPTIONS[@]}" "${TEMP_DIR}/control" "${REMOTE_TARGET}:${REMOTE_RELEASE}/control"
tar -C "${PROJECT_DIR}/web" -czf - . | ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" "tar -xzf - -C '${REMOTE_UI_ROOT}/src/ui'"
scp "${SSH_OPTIONS[@]}" "${PROJECT_DIR}/assets/xiaomi-router-center.png" \
  "${REMOTE_TARGET}:/data/plugin/www/icon/router-center.icon"

printf '4/8 安装 systemd 服务与 nginx 入口 …\n'
scp "${SSH_OPTIONS[@]}" "${TEMP_DIR}/rtrcenter.service" "${REMOTE_TARGET}:/tmp/xiaomi-router-center.service"
scp "${SSH_OPTIONS[@]}" "${TEMP_DIR}/rtrcenter.conf" "${REMOTE_TARGET}:/tmp/xiaomi-router-center.conf"
ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" "
set -eu
ln -sfn '${REMOTE_RELEASE}' '${REMOTE_ROOT}/current'
install -m 0644 /tmp/xiaomi-router-center.service /etc/systemd/system/xiaomi-router-center.service
install -m 0644 /tmp/xiaomi-router-center.conf /etc/nginx/conf.d/luci/xiaomi-router-center.conf
rm -f /tmp/xiaomi-router-center.service /tmp/xiaomi-router-center.conf
"

printf '5/8 写入默认设置（不改已有设置）…\n'
ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" "
set -eu
state='${REMOTE_ROOT}/settings.json'
if [ ! -s \"\${state}\" ]; then
  printf '{\n  \"target\": \"${ROUTER_TARGET}\"\n}\n' > \"\${state}\"
fi
chmod 600 \"\${state}\" 2>/dev/null || chmod 644 \"\${state}\"
"

printf '6/8 注册插件并补齐小米插件结构 …\n'
# 客户端入口是 nginx 直连 UI 目录，所以这里只需要：注册表条目 + INFO/src/ui/scripts 结构 + abstract
scp "${SSH_OPTIONS[@]}" "${SCRIPT_DIR}/register_plugin.py" "${SCRIPT_DIR}/native_layout.py" \
  "${SCRIPT_DIR}/control" "${REMOTE_TARGET}:/tmp/" 
scp "${SSH_OPTIONS[@]}" "${SCRIPT_DIR}/control" "${REMOTE_TARGET}:/tmp/rtrcenter-control"
ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" "
set -eu
python3 /tmp/register_plugin.py --user '${NAS_USER_ID}' --plugin-id '${PLUGIN_ID}' --version '${VERSION}' --quiet
python3 /tmp/native_layout.py --user '${NAS_USER_ID}' --name rtrcenter \
  --service xiaomi-router-center.service --title 'Unifi' \
  --plugin-id '${PLUGIN_ID}' --version '${VERSION}' \
  --desc '把局域网里 UniFi SoftCenter 搬进小米存储，手机 App 远程管理路由器插件' \
  --tags 'tool,network' --control /tmp/rtrcenter-control --quiet
rm -f /tmp/register_plugin.py /tmp/native_layout.py /tmp/rtrcenter-control
"

printf '7/8 启动服务并热加载 nginx …\n'
ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" "
set -eu
systemctl daemon-reload
systemctl enable xiaomi-router-center.service >/dev/null 2>&1 || true
systemctl restart xiaomi-router-center.service
nginx -t
systemctl reload nginx
"

printf '8/8 自检 …\n'
# 服务刚 restart，端口可能还没起来：重试几次再判定
HEALTH=""
for attempt in 1 2 3 4 5 6 7 8 9 10; do
  HEALTH="$(ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" \
    "curl -s --max-time 5 http://127.0.0.1:${PLUGIN_PORT}/healthz" || true)"
  case "${HEALTH}" in
    *'"ok"'*) break ;;
  esac
  sleep 1
done
printf '  插件服务自检：%s\n' "${HEALTH:-（无响应）}"
case "${HEALTH}" in
  *'"ok"'*) ;;
  *)
    printf '插件服务健康检查未通过，请看 systemctl status xiaomi-router-center.service\n' >&2
    exit 1
    ;;
esac
STATUS="$(ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" \
  "curl -s --max-time 20 http://127.0.0.1:${PLUGIN_PORT}/api/status" || true)"
printf '  路由器探测：%s\n' "$(printf '%s' "${STATUS}" | head -c 400)"

printf '\n安装完成。\n'
printf '  小米客户端：完全退出并重新打开小米智能存储，在「全部应用」里打开「Unifi」\n'
printf '  直连地址（局域网）：https://%s/plugin/%s/rtrcenter/\n' "${NAS_IP}" "${NAS_USER_ID}"
printf '  目标软件中心：%s\n' "${ROUTER_TARGET}"
printf '  首次打开请在插件页右上角「设置」里填一次 AdminToken（会存在 NAS 上，0600）。\n'
