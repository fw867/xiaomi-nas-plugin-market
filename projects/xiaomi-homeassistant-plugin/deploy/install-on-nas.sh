#!/usr/bin/env bash
# 把「Home Assistant」安装/更新到小米智能存储上（首次安装的唯一入口）。
#
# 用法（电脑上执行，需要能 SSH 登录 NAS 的私钥）：
#   NAS_IP=192.168.1.8 NAS_SSH_KEY=/path/to/key bash deploy/install-on-nas.sh
#
# 可选环境变量：
#   NAS_USER_ID    小米用户 ID（u3943892），不设则自动识别
#   PLUGIN_ID      插件编号，默认 11022
#   PLUGIN_PORT    插件页面自己的服务端口，默认 18200（只监听回环）
#   HA_PORT        Home Assistant 对外端口，默认 8123（容器用 host 网络，直接占宿主机这个端口）
#
# 安装内容：
#   /data/plugin/homeassistant/releases/<版本>-<时间戳>-<pid>/   程序（engine.py/server.py/web…）
#   /data/plugin/homeassistant/current -> 上面那个目录
#   /data/plugin/homeassistant/data/                            插件私有数据（容器配置目录默认也在这里）
#   /home/<用户>/plugin/homeassistant/                          小米客户端插件结构（INFO/src/ui/scripts）
#   /etc/systemd/system/xiaomi-homeassistant.service            插件服务
#   /etc/nginx/conf.d/luci/xiaomi-homeassistant.conf            客户端入口（443，反代到 127.0.0.1:18200）
#   /data/plugin/<用户>.list                                    注册表条目（先备份）
#
# 明确的副作用（脚本会逐个做，并在最后自检）：
#   1. 渲染并安装 xiaomi-homeassistant.service（__NAS_USER_ID__ 必须被替换干净）
#   2. 注册 homeassistant 到 /data/plugin/<用户>.list（编号冲突直接失败）
#   3. 安装客户端 UI 与 INFO（abstract 摘要自校验，否则开机会被强制卸载）
#   4. systemctl daemon-reload → enable → restart
#   5. nginx -t 通过才 reload（不通过就报错退出）
#   Home Assistant 容器由插件自己在页面「初始化并启动」时创建，本脚本不碰 Docker。
#
# 失败一律非零退出并打印原因，不做静默成功。

set -euo pipefail

NAS_IP="${NAS_IP:-}"
NAS_SSH_KEY="${NAS_SSH_KEY:-}"
NAS_USER_ID="${NAS_USER_ID:-}"
PLUGIN_ID="${PLUGIN_ID:-11022}"
PLUGIN_PORT="${PLUGIN_PORT:-18200}"
HA_PORT="${HA_PORT:-8123}"

if [[ -z "${NAS_IP}" ]]; then
  printf '请设置 NAS_IP，例如 NAS_IP=192.168.1.8 bash deploy/install-on-nas.sh\n' >&2
  exit 2
fi
if [[ ! "${PLUGIN_ID}" =~ ^[1-9][0-9]*$ ]]; then
  printf 'PLUGIN_ID 必须是正整数。\n' >&2
  exit 2
fi
if [[ ! "${PLUGIN_PORT}" =~ ^[1-9][0-9]*$ ]]; then
  printf 'PLUGIN_PORT 必须是正整数。\n' >&2
  exit 2
fi
if [[ ! "${HA_PORT}" =~ ^[1-9][0-9]*$ ]]; then
  printf 'HA_PORT 必须是正整数。\n' >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
if [[ -f "${PROJECT_DIR}/VERSION" ]]; then
  VERSION="$(tr -d ' \t\r\n' < "${PROJECT_DIR}/VERSION")"
else
  VERSION='0.1.0'
fi
# 目录名必须是 <版本>-<时间戳>-<pid>：插件用 installed_version() 从发布目录名
# 解析实际安装的版本号（商店安装器用的是同一套命名），格式不对页面就会一直显示
# 代码里的兜底版本。
RELEASE_ID="${VERSION}-$(date +%Y%m%d%H%M%S)-$$"
REMOTE_ROOT="/data/plugin/homeassistant"
REMOTE_RELEASE="${REMOTE_ROOT}/releases/${RELEASE_ID}"
REMOTE_DATA="${REMOTE_ROOT}/data"
REMOTE_TARGET="root@${NAS_IP}"
TEMP_DIR="$(mktemp -d -t xiaomi-homeassistant.XXXXXX)"
PLUGIN_KEY='homeassistant'
SERVICE_NAME='xiaomi-homeassistant.service'

cleanup() { rm -rf "${TEMP_DIR}"; }
trap cleanup EXIT

for required in \
  "${PROJECT_DIR}/engine.py" \
  "${PROJECT_DIR}/server.py" \
  "${PROJECT_DIR}/web/index.html" \
  "${PROJECT_DIR}/web/app.js" \
  "${PROJECT_DIR}/web/styles.css" \
  "${PROJECT_DIR}/web/assets/homeassistant.png" \
  "${PROJECT_DIR}/assets/homeassistant.png" \
  "${PROJECT_DIR}/deploy/xiaomi-homeassistant.nginx.conf" \
  "${PROJECT_DIR}/deploy/plugin-meta.json" \
  "${PROJECT_DIR}/deploy/control" \
  "${SCRIPT_DIR}/register_plugin.py" \
  "${SCRIPT_DIR}/native_layout.py"; do
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
NAS_USER_NUM="${NAS_USER_ID#u}"
REMOTE_UI_HOME="/home/${NAS_USER_ID}/plugin/${PLUGIN_KEY}"
REMOTE_UI_SRC="${REMOTE_UI_HOME}/src/ui"
printf '  插件目录：%s\n  客户端 UI：%s\n' "${REMOTE_RELEASE}" "${REMOTE_UI_SRC}"

printf '2/8 渲染 systemd 单元并检查占位符 …\n'
sed -e "s|__NAS_USER_ID__|${NAS_USER_ID}|g" \
  "${PROJECT_DIR}/deploy/xiaomi-homeassistant.service" > "${TEMP_DIR}/homeassistant.service"
if grep -qE '__[A-Z0-9_]+__' "${TEMP_DIR}/homeassistant.service"; then
  printf '渲染后的 systemd 单元里还有未替换的占位符：\n' >&2
  grep -nE '__[A-Z0-9_]+__' "${TEMP_DIR}/homeassistant.service" >&2
  exit 2
fi
# nginx 入口是固定模板（没有占位符），但仍然校验一遍，防止以后加占位符忘了渲染
if grep -qE '__[A-Z0-9_]+__' "${PROJECT_DIR}/deploy/xiaomi-homeassistant.nginx.conf"; then
  printf 'nginx 模板里出现占位符，但脚本没有渲染它：%s\n' \
    "${PROJECT_DIR}/deploy/xiaomi-homeassistant.nginx.conf" >&2
  grep -nE '__[A-Z0-9_]+__' "${PROJECT_DIR}/deploy/xiaomi-homeassistant.nginx.conf" >&2
  exit 2
fi
install -m 0644 "${PROJECT_DIR}/deploy/xiaomi-homeassistant.nginx.conf" "${TEMP_DIR}/homeassistant.conf"

printf '3/8 上传程序、前端与模板 …\n'
ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" \
  "mkdir -p '${REMOTE_RELEASE}/web' '${REMOTE_RELEASE}/deploy' '${REMOTE_DATA}' '${REMOTE_UI_SRC}' /data/plugin/www/icon"
scp "${SSH_OPTIONS[@]}" "${PROJECT_DIR}/engine.py" "${PROJECT_DIR}/server.py" "${PROJECT_DIR}/VERSION" \
  "${REMOTE_TARGET}:${REMOTE_RELEASE}/"
tar -C "${PROJECT_DIR}/web" -czf - . | ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" "tar -xzf - -C '${REMOTE_RELEASE}/web'"
# 插件目录里留一份 nginx 模板、control、plugin-meta.json 与说明：与商店包的 runtime
# 结构保持一致，也让以后重新渲染入口 / 重算 INFO 有据可依。
scp "${SSH_OPTIONS[@]}" "${PROJECT_DIR}/deploy/xiaomi-homeassistant.nginx.conf" \
  "${REMOTE_TARGET}:${REMOTE_RELEASE}/deploy/xiaomi-homeassistant.nginx.conf"
scp "${SSH_OPTIONS[@]}" "${PROJECT_DIR}/deploy/plugin-meta.json" \
  "${REMOTE_TARGET}:${REMOTE_RELEASE}/deploy/plugin-meta.json"
scp "${SSH_OPTIONS[@]}" "${PROJECT_DIR}/deploy/control" \
  "${REMOTE_TARGET}:${REMOTE_RELEASE}/control"
if [[ -f "${PROJECT_DIR}/README.md" ]]; then
  scp "${SSH_OPTIONS[@]}" "${PROJECT_DIR}/README.md" "${REMOTE_TARGET}:${REMOTE_RELEASE}/README.md"
fi
# 客户端 UI：src/ui 下必须有 index.html 与静态资源，否则插件页白屏
tar -C "${PROJECT_DIR}/web" -czf - . | ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" "tar -xzf - -C '${REMOTE_UI_SRC}'"
scp "${SSH_OPTIONS[@]}" "${PROJECT_DIR}/assets/homeassistant.png" \
  "${REMOTE_TARGET}:/data/plugin/www/icon/${PLUGIN_KEY}.icon"
ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" "
set -eu
test -s '${REMOTE_UI_SRC}/index.html' || { echo '客户端 UI 上传失败：缺 index.html' >&2; exit 1; }
test -s '${REMOTE_UI_SRC}/assets/homeassistant.png' || { echo '客户端 UI 上传失败：缺图标' >&2; exit 1; }
test -s '${REMOTE_RELEASE}/engine.py' || { echo '程序上传失败：缺 engine.py' >&2; exit 1; }
test -s '${REMOTE_RELEASE}/deploy/plugin-meta.json' || { echo '元数据上传失败：缺 plugin-meta.json' >&2; exit 1; }
chmod 0700 '${REMOTE_DATA}'
"

printf '4/8 安装 systemd 服务与 nginx 入口 …\n'
scp "${SSH_OPTIONS[@]}" "${TEMP_DIR}/homeassistant.service" "${REMOTE_TARGET}:/tmp/${SERVICE_NAME}"
scp "${SSH_OPTIONS[@]}" "${TEMP_DIR}/homeassistant.conf" "${REMOTE_TARGET}:/tmp/xiaomi-homeassistant.conf"
ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" "
set -eu
# 先装 unit 再切 current：万一切链接后服务起不来，单元里指的是新目录，便于排查
install -m 0644 /tmp/${SERVICE_NAME} /etc/systemd/system/${SERVICE_NAME}
ln -sfn '${REMOTE_RELEASE}' '${REMOTE_ROOT}/current'
install -m 0644 /tmp/xiaomi-homeassistant.conf /etc/nginx/conf.d/luci/xiaomi-homeassistant.conf
rm -f /tmp/${SERVICE_NAME} /tmp/xiaomi-homeassistant.conf
# 插件私有数据目录：容器配置目录默认就在这里，属主给运行插件的用户
chown -R '${NAS_USER_ID}:${NAS_USER_ID}' '${REMOTE_DATA}' 2>/dev/null || true
"

printf '5/8 检查 Home Assistant 端口 %s 是否空闲（host 网络要靠它）…\n' "${HA_PORT}"
# host 网络下容器直接占宿主机端口：已经被别人听着的话，容器起来也绑不上，
# 与其到页面上才发现，不如装的时候就明确告诉用户。
PORT_STATE="$(ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" "
if command -v ss >/dev/null 2>&1; then
  ss -ltn 2>/dev/null | awk '{print \$4}' | grep -E '[:.]${HA_PORT}\$' || true
elif command -v netstat >/dev/null 2>&1; then
  netstat -ltn 2>/dev/null | awk '{print \$4}' | grep -E '[:.]${HA_PORT}\$' || true
fi
" || true)"
if [[ -n "${PORT_STATE}" ]]; then
  printf '  警告：宿主机 %s 已经有监听（%s）。\n' "${HA_PORT}" "$(printf '%s' "${PORT_STATE}" | tr '\n' ' ')" >&2
  printf '  如果那不是本插件的容器，Home Assistant 起来后可能绑不上端口；\n' >&2
  printf '  确认后再继续（例如先停掉占用者，或用 HA_PORT 换一个端口）。\n' >&2
else
  printf '  %s 空闲 ✓\n' "${HA_PORT}"
fi

printf '6/8 注册插件并补齐小米插件结构 …\n'
# 客户端入口是 nginx 直连 UI 目录，所以这里只需要：注册表条目 + INFO/src/ui/scripts 结构
scp "${SSH_OPTIONS[@]}" "${SCRIPT_DIR}/register_plugin.py" "${SCRIPT_DIR}/native_layout.py" \
  "${REMOTE_TARGET}:/tmp/"
scp "${SSH_OPTIONS[@]}" "${SCRIPT_DIR}/control" "${REMOTE_TARGET}:/tmp/homeassistant-control"
# native_layout.py 必须在 UI 就位后最后执行：abstract 覆盖 src/ 下全部文件
ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" "
set -eu
python3 /tmp/register_plugin.py --user '${NAS_USER_ID}' --plugin-id '${PLUGIN_ID}' \
  --version '${VERSION}' --port '${PLUGIN_PORT}' --quiet
python3 /tmp/native_layout.py --user '${NAS_USER_ID}' --name '${PLUGIN_KEY}' \
  --service '${SERVICE_NAME}' --title 'Home Assistant' \
  --plugin-id '${PLUGIN_ID}' --version '${VERSION}' \
  --desc '开源家庭自动化平台；配置目录持久化，端口 ${HA_PORT} 对局域网开放' \
  --tags 'tool,home' --control /tmp/homeassistant-control \
  --meta '${REMOTE_RELEASE}/deploy/plugin-meta.json' --quiet
rm -f /tmp/register_plugin.py /tmp/native_layout.py /tmp/homeassistant-control
"

printf '7/8 启动服务并热加载 nginx …\n'
ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" "
set -eu
systemctl daemon-reload
systemctl enable ${SERVICE_NAME} >/dev/null 2>&1 || true
systemctl restart ${SERVICE_NAME}
nginx -t
systemctl reload nginx
"

printf '8/8 自检 …\n'
# 自检分两类：
#   · 硬检查（部署产物里残留占位符、注册表条目、命令本身失败）→ 立即报错并非零退出，
#     而且**绝不**再打印「安装完成」，免得给人"装好了"的错觉；
#   · 软检查（健康检查、监听、结构）→ 收集起来，最后一次性汇总失败项再非零退出。
FAILURES=''
fail_check() {
  FAILURES="${FAILURES}${FAILURES:+, }$1"
  printf '  ✗ %s：%s\n' "$1" "$2" >&2
}

# 渲染后的 systemd 单元
if ! ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" \
  "grep -qE '__[A-Z0-9_]+__' /etc/systemd/system/${SERVICE_NAME}"; then
  printf '  单元无占位符 ✓\n'
else
  ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" \
    "grep -nE '__[A-Z0-9_]+__' /etc/systemd/system/${SERVICE_NAME}" >&2 || true
  printf '渲染后的 systemd 单元里还有未替换的占位符，终止安装（不打印"安装完成"）。\n' >&2
  exit 1
fi

# 部署产物（原样下发、客户端/商店直接使用的文件）里不许有 __XXX__。
# 注意：/home/<用户>/plugin/<key>/src/ui/ 下的页面文件不参与这项检查——index.html /
# app.js 里的 __SESSION_TOKEN__ / __CSRF_TOKEN__ / __PLUGIN_VERSION__ 是**运行时**
# 由插件服务端替换的（与 jellyfin / transmission / 网络邻居等插件完全一致），
# 它们必须留在文件里，安装时替换掉反而会让页面拿不到会话令牌。
PLACEHOLDER_REPORT="$(ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" "
set -eu
found=''
for f in /etc/nginx/conf.d/luci/xiaomi-homeassistant.conf \
         '${REMOTE_RELEASE}/deploy/xiaomi-homeassistant.nginx.conf' \
         '${REMOTE_RELEASE}/deploy/plugin-meta.json' \
         '${REMOTE_RELEASE}/control' \
         '${REMOTE_UI_HOME}/scripts/control' \
         '${REMOTE_UI_HOME}/INFO'; do
  if [ -f \"\$f\" ] && LC_ALL=C grep -qE '__[A-Z0-9_]+__' \"\$f\"; then
    found=\"\${found}\${found:+, }\$f\"
  fi
done
if [ -n \"\$found\" ]; then printf 'FOUND %s' \"\$found\"; else printf 'CLEAN'; fi
")" || { printf '占位符检查命令本身失败（SSH/远端命令出错），终止安装。\n' >&2; exit 1; }
case "${PLACEHOLDER_REPORT}" in
  CLEAN*) printf '  部署产物无占位符 ✓（已检查：nginx 入口与模板、plugin-meta.json、control、INFO）\n' ;;
  FOUND*)
    printf '部署产物里还有未替换的占位符：%s\n' "${PLACEHOLDER_REPORT#FOUND }" >&2
    printf '终止安装（不打印"安装完成"）。\n' >&2
    exit 1
    ;;
  *)
    printf '占位符检查返回了意外结果：%s\n' "${PLACEHOLDER_REPORT}" >&2
    exit 1
    ;;
esac

# 反向断言：页面文件**必须**留着服务端要替换的占位符（误删了要红）
if ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" \
  "grep -qF '__SESSION_TOKEN__' '${REMOTE_UI_SRC}/index.html' && grep -qF '__CSRF_TOKEN__' '${REMOTE_UI_SRC}/index.html' && grep -qF '__PLUGIN_VERSION__' '${REMOTE_UI_SRC}/index.html'"; then
  printf '  页面模板保留会话/CSRF/版本占位符 ✓（由插件服务端在返回页面时替换）\n'
else
  fail_check '页面模板占位符' 'index.html 里缺 __SESSION_TOKEN__ / __CSRF_TOKEN__ / __PLUGIN_VERSION__（服务端就没法注入会话，页面会 401）'
fi

# 注册表条目里不许有 __XXX__（商店/客户端原样使用）
if ! ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" \
  "python3 -c \"
import json, re, sys
raw = open('/data/plugin/${NAS_USER_ID}.list', encoding='utf-8').read()
found = re.findall(r'__[A-Z0-9_]+__', raw)
if found:
    sys.exit('注册表里残留占位符: ' + ','.join(sorted(set(found))))
\""; then
  printf '注册表条目里还有未替换的占位符，终止安装（不打印"安装完成"）。\n' >&2
  exit 1
fi
printf '  注册表条目无占位符 ✓\n'

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
printf '  插件服务健康检查：%s\n' "${HEALTH:-（无响应）}"
case "${HEALTH}" in
  *'"ok"'*) ;;
  *) fail_check '插件服务健康检查' "未通过，请看 systemctl status ${SERVICE_NAME}" ;;
esac

ACTIVE="$(ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" \
  "systemctl is-active ${SERVICE_NAME} 2>/dev/null || true")"
printf '  服务状态：%s\n' "${ACTIVE}"
case "${ACTIVE}" in
  active) ;;
  *) fail_check '服务状态' "systemctl is-active 返回 ${ACTIVE:-（空）}" ;;
esac

LISTENING="$(ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" \
  "if command -v ss >/dev/null 2>&1; then ss -ltn 2>/dev/null | grep -E '[:.]${PLUGIN_PORT}\$' || true; fi" \
  | tr -d '\r' | tr '\n' ' ')"
printf '  插件服务端口 %s：%s\n' "${PLUGIN_PORT}" "${LISTENING:-（未监听）}"
case "${LISTENING}" in
  *"${PLUGIN_PORT}"*) ;;
  *) fail_check '插件服务端口' "${PLUGIN_PORT} 上没有监听" ;;
esac

printf '  Home Assistant 端口 %s：%s\n' "${HA_PORT}" "$(ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" \
  "if command -v ss >/dev/null 2>&1; then ss -ltn 2>/dev/null | grep -E '[:.]${HA_PORT}\$' || true; fi" \
  | tr -d '\r' | tr '\n' ' ' | sed 's/^$/(容器尚未创建，页面初始化后才会监听)/')"
printf '  提示：容器要在插件页面点「初始化并启动」时才会创建，所以 %s 现在没监听是正常的。\n' "${HA_PORT}"

REGISTRY="$(ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" \
  "python3 -c \"
import json, sys
path = '/data/plugin/${NAS_USER_ID}.list'
try:
    data = json.load(open(path, encoding='utf-8'))
except Exception as error:
    sys.exit('读取注册表失败: %s' % error)
record = data.get('${PLUGIN_KEY}')
if not isinstance(record, dict):
    sys.exit('注册表里没有 ${PLUGIN_KEY} 条目')
info = record.get('info') or {}
print('含 ${PLUGIN_KEY} 条目：编号 %s、版本 %s、端口 %s、类型 %s' % (
    info.get('id'), info.get('version'), info.get('port'), record.get('frontend', {}).get('type')))
\" 2>&1" || true)"
printf '  注册表：%s\n' "${REGISTRY}"
case "${REGISTRY}" in
  *"含 ${PLUGIN_KEY} 条目"*"编号 ${PLUGIN_ID}"*"端口 ${PLUGIN_PORT}"*) ;;
  *) fail_check '注册表条目' "${REGISTRY:-（读取失败）}" ;;
esac

STRUCTURE="$(ssh "${SSH_OPTIONS[@]}" "${REMOTE_TARGET}" \
  "test -d '${REMOTE_UI_HOME}/src' && test -d '${REMOTE_UI_HOME}/etc' && test -d '${REMOTE_UI_HOME}/var' \
     && test -d '${REMOTE_UI_HOME}/tmp' && test -d '${REMOTE_UI_HOME}/scripts' && test -f '${REMOTE_UI_HOME}/INFO' \
     && test -x '${REMOTE_UI_HOME}/scripts/control' \
     && echo 'src/etc/var/tmp/scripts 与 INFO/control 就位（abstract 已按当前文件计算）' || echo 'MISSING'" || true)"
printf '  插件结构：%s\n' "${STRUCTURE}"
case "${STRUCTURE}" in
  *就位*) ;;
  *) fail_check '插件结构' '缺 src/ 或 INFO 或 scripts/control（开机会被 plugincenter 强制卸载）' ;;
esac

if [ -n "${FAILURES}" ]; then
  printf '\n安装未通过自检，失败项：%s\n' "${FAILURES}" >&2
  printf '已安装的文件仍在原处，但**不要**当作安装成功；修好后重跑本脚本。\n' >&2
  exit 1
fi

printf '\n自检全部通过，安装完成。\n'
printf '  小米客户端：完全退出并重新打开小米智能存储，在「全部应用」里打开「Home Assistant」\n'
printf '  直连地址（局域网）：https://%s/plugin/%s/%s/\n' "${NAS_IP}" "${NAS_USER_ID}" "${PLUGIN_KEY}"
printf '  插件页面：先在「初始化」里选配置目录（可留空用插件私有目录），再点「初始化并启动」\n'
printf '  Home Assistant 界面：http://%s:%s （容器用 host 网络，首次启动要拉镜像，可能数分钟）\n' "${NAS_IP}" "${HA_PORT}"
printf '  提醒：容器起来后若 %s 被别的程序占用，页面会提示「宿主机 %s 端口没有监听」。\n' "${HA_PORT}" "${HA_PORT}"
printf '  卸载：bash deploy/uninstall-on-nas.sh（只删程序与注册，用户配置目录不动）\n'
