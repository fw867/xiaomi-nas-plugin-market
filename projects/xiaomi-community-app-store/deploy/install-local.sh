#!/usr/bin/env bash
# 在小米存储 NAS 本机（已 SSH 登录 root）直接安装应用商店。
# 不需要 NAS_IP / NAS_SSH_KEY —— 脚本就在 NAS 上运行。
#
# 用法：
#   1. 把安装包解压到 NAS 任意目录（如 /tmp/store）
#   2. cd 到包含 server.py 的目录（或其父目录）
#   3. bash install-local.sh
#
# 环境变量（可选）：
#   NAS_USER_ID   小米用户 ID（如 u123456），不设则自动检测
#   PLUGIN_ID     商店插件编号，默认 11002

set -euo pipefail

PLUGIN_ID="${PLUGIN_ID:-11002}"
RELEASE_ID="0.2.0-$(date +%Y%m%d%H%M%S)"
STORE_ROOT="/data/plugin/community-store"
RELEASE_DIR="${STORE_ROOT}/releases/${RELEASE_ID}"
CURRENT_LINK="${STORE_ROOT}/current"

# ---------- 定位项目目录 ----------
# 脚本可能在 deploy/ 下，也可能在项目根目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "${SCRIPT_DIR}/server.py" ]]; then
  PROJECT_DIR="${SCRIPT_DIR}"
elif [[ -f "${SCRIPT_DIR}/../server.py" ]]; then
  PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
elif [[ -f "${SCRIPT_DIR}/../../server.py" ]]; then
  PROJECT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
else
  printf '找不到 server.py。请在安装包解压后的目录中运行此脚本。\n' >&2
  exit 2
fi

# ---------- 校验必要文件 ----------
REQUIRED=(
  server.py
  storelib.py
  web/index.html
  web/assets/community-store-v4.png
  catalog/catalog.json
  catalog/catalog.json.sig
  catalog/repository-public.pem
)
DEPLOY_DIR=""
for candidate in "${PROJECT_DIR}/deploy" "${SCRIPT_DIR}" "${SCRIPT_DIR}/deploy"; do
  if [[ -f "${candidate}/xiaomi-community-store.service" ]]; then
    DEPLOY_DIR="${candidate}"
    break
  fi
done
if [[ -z "${DEPLOY_DIR}" ]]; then
  printf '找不到 deploy/ 目录（需要 xiaomi-community-store.service 等文件）。\n' >&2
  exit 2
fi

for f in "${REQUIRED[@]}"; do
  if [[ ! -f "${PROJECT_DIR}/${f}" ]]; then
    printf '发布包缺少文件：%s\n' "${f}" >&2
    exit 2
  fi
done
for f in xiaomi-community-store.service xiaomi-community-store.nginx.conf register_plugin.py; do
  if [[ ! -f "${DEPLOY_DIR}/${f}" ]]; then
    printf 'deploy 目录缺少文件：%s\n' "${f}" >&2
    exit 2
  fi
done

# ---------- 前置检查 ----------
find_cmd() {
  local name="$1"
  if command -v "${name}" >/dev/null 2>&1; then
    command -v "${name}"
    return 0
  fi
  local candidate
  for candidate in \
    "/usr/sbin/${name}" "/usr/bin/${name}" "/sbin/${name}" "/bin/${name}" \
    "/usr/local/sbin/${name}" "/usr/local/bin/${name}"
  do
    if [[ -x "${candidate}" ]]; then
      printf '%s' "${candidate}"
      return 0
    fi
  done
  return 1
}

for cmd in python3 openssl systemctl; do
  if ! find_cmd "${cmd}" >/dev/null; then
    printf '缺少命令：%s\n' "${cmd}" >&2
    exit 1
  fi
done
NGINX_BIN="$(find_cmd nginx || true)"
if [[ -z "${NGINX_BIN}" ]]; then
  printf '找不到 nginx。请确认 NAS 上已安装 nginx，或将其加入 PATH。\n' >&2
  printf '常见路径：/usr/sbin/nginx  /usr/bin/nginx\n' >&2
  exit 1
fi
printf 'nginx 路径：%s\n' "${NGINX_BIN}"

if [[ "$(id -u)" -ne 0 ]]; then
  printf '请以 root 运行此脚本。\n' >&2
  exit 1
fi

# ---------- 检测小米用户 ID ----------
if [[ -z "${NAS_USER_ID:-}" ]]; then
  REGISTRY_USERS=""
  for path in /data/plugin/u*.list; do
    [[ -f "${path}" ]] || continue
    name="${path##*/}"
    REGISTRY_USERS="${REGISTRY_USERS}${name%.list}"$'\n'
  done
  REGISTRY_USERS="$(printf '%s' "${REGISTRY_USERS}" | sed '/^$/d')"
  REGISTRY_COUNT="$(printf '%s\n' "${REGISTRY_USERS}" | sed '/^$/d' | wc -l | tr -d ' ')"
  if [[ "${REGISTRY_COUNT}" == "1" ]]; then
    NAS_USER_ID="${REGISTRY_USERS}"
  elif [[ "${REGISTRY_COUNT}" -gt 1 ]]; then
    printf '检测到多个小米账号：\n%s\n' "${REGISTRY_USERS}" >&2
    printf '请输入当前账号对应的用户 ID：' >&2
    read -r NAS_USER_ID
  else
    printf '未找到小米用户注册表（/data/plugin/u*.list）。\n' >&2
    printf '请设置 NAS_USER_ID 后重试，例如：NAS_USER_ID=u123456 bash %s\n' "$0" >&2
    exit 2
  fi
fi
if [[ ! "${NAS_USER_ID}" =~ ^[A-Za-z0-9_-]+$ ]]; then
  printf '小米用户 ID 无效：%s\n' "${NAS_USER_ID}" >&2
  exit 2
fi
URL_USER_ID="${NAS_USER_ID#u}"
printf '使用小米用户：%s\n' "${NAS_USER_ID}"

# ---------- 1. 创建目录 ----------
printf '[1/8] 创建目录 …\n'
mkdir -p \
  "${RELEASE_DIR}" \
  "${STORE_ROOT}/state" \
  "${STORE_ROOT}/staging" \
  "${STORE_ROOT}/backups" \
  /data/plugin/www/icon

# ---------- 2. 复制应用文件 ----------
printf '[2/8] 复制应用文件 …\n'
cp "${PROJECT_DIR}/server.py"   "${RELEASE_DIR}/"
cp "${PROJECT_DIR}/storelib.py" "${RELEASE_DIR}/"
cp -a "${PROJECT_DIR}/web"      "${RELEASE_DIR}/web"
cp -a "${PROJECT_DIR}/catalog"  "${RELEASE_DIR}/catalog"

# ---------- 3. 校验 catalog 签名 ----------
printf '[3/8] 校验 catalog 签名 …\n'
openssl dgst -sha256 -verify \
  "${RELEASE_DIR}/catalog/repository-public.pem" \
  -signature "${RELEASE_DIR}/catalog/catalog.json.sig" \
  "${RELEASE_DIR}/catalog/catalog.json" >/dev/null

# ---------- 4. 安装图标 ----------
printf '[4/8] 安装图标 …\n'
cp "${PROJECT_DIR}/web/assets/community-store-v4.png" \
   /data/plugin/www/icon/community-store-v4.icon

# ---------- 5. 生成并安装 systemd service ----------
printf '[5/8] 安装 systemd service …\n'
SERVICE_PATH="/etc/systemd/system/xiaomi-community-store.service"
STAMP="$(date +%s)"
if [[ -f "${SERVICE_PATH}" ]]; then
  cp -p "${SERVICE_PATH}" "${SERVICE_PATH}.before-${STAMP}.bak"
fi
sed \
  -e "s|__NAS_USER_ID__|${NAS_USER_ID}|g" \
  -e "s|__URL_USER_ID__|${URL_USER_ID}|g" \
  "${DEPLOY_DIR}/xiaomi-community-store.service" > "${SERVICE_PATH}"
chmod 0644 "${SERVICE_PATH}"

# ---------- 6. 安装 nginx 配置 ----------
printf '[6/8] 安装 nginx 配置 …\n'
NGINX_PATH="/etc/nginx/conf.d/luci/xiaomi-community-store.conf"
if [[ -f "${NGINX_PATH}" ]]; then
  cp -p "${NGINX_PATH}" "${NGINX_PATH}.before-${STAMP}.bak"
fi
cp "${DEPLOY_DIR}/xiaomi-community-store.nginx.conf" "${NGINX_PATH}"
chmod 0644 "${NGINX_PATH}"

# ---------- 7. 生成会话密钥 + 注册插件 ----------
printf '[7/8] 注册插件 …\n'
TOKEN_FILE="${STORE_ROOT}/admin-token"
if [[ ! -s "${TOKEN_FILE}" ]]; then
  umask 077
  openssl rand -hex 16 > "${TOKEN_FILE}"
fi
chmod 600 "${TOKEN_FILE}"

# 切换 current 符号链接
ln -sfn "${RELEASE_DIR}" "${CURRENT_LINK}"

python3 "${DEPLOY_DIR}/register_plugin.py" \
  --user-id "${NAS_USER_ID}" \
  --plugin-id "${PLUGIN_ID}"

# ---------- 8. 启动并验证 ----------
printf '[8/8] 启动服务 …\n'
"${NGINX_BIN}" -t
systemctl daemon-reload
systemctl enable xiaomi-community-store.service
systemctl restart xiaomi-community-store.service
systemctl reload nginx

# 等待健康检查
for i in $(seq 1 15); do
  if python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:18119/healthz', timeout=3)" 2>/dev/null; then
    printf '\n安装完成！健康检查通过。\n'
    printf '请完全退出并重新打开小米智能存储客户端，在「全部应用」中打开「应用商店」。\n'
    exit 0
  fi
  sleep 1
done

printf '\n警告：服务已启动但健康检查超时。请检查日志：\n' >&2
printf '  journalctl -u xiaomi-community-store.service -n 50\n' >&2
exit 1
