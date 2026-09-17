#!/usr/bin/env bash
# 在小米存储 NAS 本机（已 SSH 登录 root）从 GitHub Releases 下载最新应用商店并安装。
#
# 用法（在 NAS 上执行）：
#   bash install-from-github.sh
#   NAS_USER_ID=u123456 bash install-from-github.sh
#
# 一行安装（NAS 上）：
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/fw867/xiaomi-nas-plugin-market/main/install-from-github.sh)"
#
# 可选环境变量：
#   NAS_USER_ID     小米用户 ID（如 u123456），不设则自动检测
#   PLUGIN_ID       商店插件编号，默认 11002
#   GITHUB_REPO     仓库 owner/name，默认 fw867/xiaomi-nas-plugin-market
#   GITHUB_TOKEN    可选，私有仓库或提高 API 限速时使用

set -euo pipefail

REPO="${GITHUB_REPO:-fw867/xiaomi-nas-plugin-market}"
API="https://api.github.com/repos/${REPO}"
PLUGIN_ID="${PLUGIN_ID:-11002}"
STORE_ROOT="/data/plugin/community-store"
WORK_DIR="$(mktemp -d -t xiaomi-store-install.XXXXXX)"
trap 'rm -rf "${WORK_DIR}"' EXIT

# ---------- 颜色 ----------
if [[ -t 1 ]]; then
  C_G=$'\033[32m'; C_Y=$'\033[33m'; C_R=$'\033[31m'; C_0=$'\033[0m'
else
  C_G=""; C_Y=""; C_R=""; C_0=""
fi
info()  { printf '%s\n' "${C_G}[信息]${C_0} $*"; }
warn()  { printf '%s\n' "${C_Y}[警告]${C_0} $*" >&2; }
error() { printf '%s\n' "${C_R}[错误]${C_0} $*" >&2; }

# ---------- 工具函数 ----------
find_cmd() {
  local name="$1" candidate
  if command -v "${name}" >/dev/null 2>&1; then
    command -v "${name}"; return 0
  fi
  for candidate in \
    "/usr/sbin/${name}" "/usr/bin/${name}" "/sbin/${name}" "/bin/${name}" \
    "/usr/local/sbin/${name}" "/usr/local/bin/${name}"
  do
    [[ -x "${candidate}" ]] && { printf '%s' "${candidate}"; return 0; }
  done
  return 1
}

github_get() {
  local args=(-fsSL --retry 3 --retry-delay 2 -H "Accept: application/vnd.github+json")
  [[ -n "${GITHUB_TOKEN:-}" ]] && args+=(-H "Authorization: Bearer ${GITHUB_TOKEN}")
  curl "${args[@]}" "$1"
}

# ==============================
# 1. 前置检查
# ==============================
if [[ "$(id -u)" -ne 0 ]]; then
  error "请以 root 运行此脚本（ssh root@<NAS_IP> 后执行）。"
  exit 1
fi

for cmd in curl python3 openssl systemctl; do
  if ! find_cmd "${cmd}" >/dev/null; then
    error "缺少命令：${cmd}"
    exit 1
  fi
done
NGINX_BIN="$(find_cmd nginx || true)"
if [[ -z "${NGINX_BIN}" ]]; then
  error "找不到 nginx。常见路径：/usr/sbin/nginx  /usr/bin/nginx"
  exit 1
fi
info "nginx：${NGINX_BIN}"

# ==============================
# 2. 查最新 Release
# ==============================
info "查询 ${REPO} 最新 Release …"
RELEASE_JSON="$(github_get "${API}/releases/latest")"

TAG="$(printf '%s' "${RELEASE_JSON}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tag_name',''))")"
[[ -z "${TAG}" ]] && { error "无法获取 Release（可能没有 Release 或 API 限流）。"; exit 1; }
VERSION="${TAG#v}"
info "最新版本：${VERSION}"

# ==============================
# 3. 定位并下载资产
# ==============================
ZIP_NAME="xiaomi-plugin-market-${VERSION}.zip"
SHA_NAME="SHA256SUMS.txt"

ZIP_URL="$(printf '%s' "${RELEASE_JSON}" | python3 -c "
import sys, json
for a in json.load(sys.stdin).get('assets', []):
    if a['name'] == '${ZIP_NAME}':
        print(a['browser_download_url']); break
")"
SHA_URL="$(printf '%s' "${RELEASE_JSON}" | python3 -c "
import sys, json
for a in json.load(sys.stdin).get('assets', []):
    if a['name'] == '${SHA_NAME}':
        print(a['browser_download_url']); break
")"

if [[ -z "${ZIP_URL}" ]]; then
  error "Release 中未找到 ${ZIP_NAME}。可用资产："
  printf '%s' "${RELEASE_JSON}" | python3 -c "
import sys, json
for a in json.load(sys.stdin).get('assets', []):
    print('  -', a['name'], a.get('size',0)//1024, 'KiB')
" >&2
  exit 1
fi

info "下载安装包 …"
github_get "${ZIP_URL}" > "${WORK_DIR}/${ZIP_NAME}"
if [[ -n "${SHA_URL}" ]]; then
  info "下载校验文件 …"
  github_get "${SHA_URL}" > "${WORK_DIR}/${SHA_NAME}"
else
  warn "Release 未附带 ${SHA_NAME}，跳过校验。"
fi

# ==============================
# 4. SHA-256 校验
# ==============================
if [[ -f "${WORK_DIR}/${SHA_NAME}" ]]; then
  info "校验 SHA-256 …"
  EXPECTED="$(grep -E "[[:space:]]${ZIP_NAME}\$" "${WORK_DIR}/${SHA_NAME}" | awk '{print $1}')"
  [[ -z "${EXPECTED}" ]] && { error "${SHA_NAME} 中未找到 ${ZIP_NAME} 的哈希。"; exit 1; }
  ACTUAL="$(python3 -c "import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "${WORK_DIR}/${ZIP_NAME}")"
  if [[ "${EXPECTED}" != "${ACTUAL}" ]]; then
    error "SHA-256 校验失败！期望 ${EXPECTED}，实际 ${ACTUAL}"
    exit 1
  fi
  info "校验通过：${ACTUAL:0:16}…"
fi

# ==============================
# 5. 解压
# ==============================
info "解压 …"
EXTRACT_DIR="${WORK_DIR}/extracted"
mkdir -p "${EXTRACT_DIR}"
python3 -c "
import zipfile, sys
zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])
" "${WORK_DIR}/${ZIP_NAME}" "${EXTRACT_DIR}"

# 定位项目目录（Release ZIP 顶层有一层版本目录）
PROJECT_DIR=""
for candidate in "${EXTRACT_DIR}" "${EXTRACT_DIR}"/*/; do
  [[ -f "${candidate}/server.py" ]] && { PROJECT_DIR="${candidate%/}"; break; }
done
[[ -z "${PROJECT_DIR}" ]] && { error "解压后未找到 server.py"; find "${EXTRACT_DIR}" -maxdepth 3 -type f | head 20 >&2; exit 1; }
info "项目目录：${PROJECT_DIR}"

for f in server.py storelib.py web/index.html catalog/catalog.json catalog/repository-public.pem; do
  [[ -f "${PROJECT_DIR}/${f}" ]] || { error "发布包缺少文件：${f}"; exit 1; }
done
# deploy 目录下的文件（兼容 Release 包内有或没有 deploy/）
DEPLOY_DIR=""
for d in "${PROJECT_DIR}/deploy" "${PROJECT_DIR}"; do
  [[ -f "${d}/xiaomi-community-store.service" ]] && { DEPLOY_DIR="${d}"; break; }
done
[[ -z "${DEPLOY_DIR}" ]] && { error "找不到 xiaomi-community-store.service"; exit 1; }
for f in xiaomi-community-store.service xiaomi-community-store.nginx.conf register_plugin.py; do
  [[ -f "${DEPLOY_DIR}/${f}" ]] || { error "deploy 缺少文件：${f}"; exit 1; }
done

# ==============================
# 6. 检测小米用户 ID
# ==============================
if [[ -z "${NAS_USER_ID:-}" ]]; then
  info "检测小米用户 ID …"
  REGISTRY_USERS=""
  for path in /data/plugin/u*.list; do
    [[ -f "${path}" ]] || continue
    name="${path##*/}"
    REGISTRY_USERS="${REGISTRY_USERS}${name%.list}"$'\n'
  done
  REGISTRY_USERS="$(printf '%s' "${REGISTRY_USERS}" | sed '/^$/d')"
  COUNT="$(printf '%s\n' "${REGISTRY_USERS}" | sed '/^$/d' | wc -l | tr -d ' ')"
  if [[ "${COUNT}" == "1" ]]; then
    NAS_USER_ID="${REGISTRY_USERS}"
  elif [[ "${COUNT}" -gt 1 ]]; then
    printf '检测到多个小米账号：\n%s\n用户 ID：' "${REGISTRY_USERS}" >&2
    read -r NAS_USER_ID
  else
    error "未找到小米用户注册表。请设置 NAS_USER_ID，例如："
    error "  NAS_USER_ID=u123456 bash $0"
    exit 2
  fi
fi
[[ ! "${NAS_USER_ID}" =~ ^[A-Za-z0-9_-]+$ ]] && { error "小米用户 ID 无效：${NAS_USER_ID}"; exit 2; }
URL_USER_ID="${NAS_USER_ID#u}"
info "使用小米用户：${NAS_USER_ID}"

# ==============================
# 7. 安装
# ==============================
info "创建目录 …"
RELEASE_ID="${VERSION}-$(date +%Y%m%d%H%M%S)"
RELEASE_DIR="${STORE_ROOT}/releases/${RELEASE_ID}"
mkdir -p "${RELEASE_DIR}" "${STORE_ROOT}/state" "${STORE_ROOT}/staging" "${STORE_ROOT}/backups" /data/plugin/www/icon

info "复制应用文件 …"
cp "${PROJECT_DIR}/server.py" "${PROJECT_DIR}/storelib.py" "${RELEASE_DIR}/"
cp -a "${PROJECT_DIR}/web" "${RELEASE_DIR}/web"
[[ -d "${PROJECT_DIR}/catalog" ]] && cp -a "${PROJECT_DIR}/catalog" "${RELEASE_DIR}/catalog"
[[ -d "${PROJECT_DIR}/deploy" ]] && cp -a "${PROJECT_DIR}/deploy" "${RELEASE_DIR}/deploy"
[[ -d "${PROJECT_DIR}/scripts" ]] && cp -a "${PROJECT_DIR}/scripts" "${RELEASE_DIR}/scripts"
[[ -f "${PROJECT_DIR}/VERSION" ]] && cp "${PROJECT_DIR}/VERSION" "${RELEASE_DIR}/VERSION"
cp "${PROJECT_DIR}/web/assets/community-store-v4.png" /data/plugin/www/icon/community-store-v4.icon

info "校验 catalog 签名 …"
openssl dgst -sha256 -verify \
  "${RELEASE_DIR}/catalog/repository-public.pem" \
  -signature "${RELEASE_DIR}/catalog/catalog.json.sig" \
  "${RELEASE_DIR}/catalog/catalog.json" >/dev/null

info "安装 systemd service …"
SERVICE="/etc/systemd/system/xiaomi-community-store.service"
STAMP="$(date +%s)"
[[ -f "${SERVICE}" ]] && cp -p "${SERVICE}" "${SERVICE}.before-${STAMP}.bak"
sed -e "s|__NAS_USER_ID__|${NAS_USER_ID}|g" -e "s|__URL_USER_ID__|${URL_USER_ID}|g" \
  "${DEPLOY_DIR}/xiaomi-community-store.service" > "${SERVICE}"
chmod 0644 "${SERVICE}"

info "安装 nginx 配置 …"
NGINX_CONF="/etc/nginx/conf.d/luci/xiaomi-community-store.conf"
[[ -f "${NGINX_CONF}" ]] && cp -p "${NGINX_CONF}" "${NGINX_CONF}.before-${STAMP}.bak"
cp "${DEPLOY_DIR}/xiaomi-community-store.nginx.conf" "${NGINX_CONF}"
chmod 0644 "${NGINX_CONF}"

info "生成会话密钥 …"
TOKEN="${STORE_ROOT}/admin-token"
if [[ ! -s "${TOKEN}" ]]; then
  umask 077; openssl rand -hex 16 > "${TOKEN}"
fi
chmod 600 "${TOKEN}"

info "切换 current 链接 …"
ln -sfn "${RELEASE_DIR}" "${STORE_ROOT}/current"

info "注册插件 …"
python3 "${DEPLOY_DIR}/register_plugin.py" --user-id "${NAS_USER_ID}" --plugin-id "${PLUGIN_ID}"

info "部署客户端入口 …"
# plugin.sh 的 verify 要求插件目录下有 src/ 与 INFO，否则开机时会被
# plugincenter 判定为「force uninstall」并从注册表移除
STORE_HOME="/home/${NAS_USER_ID}/plugin/communitystore"
rm -rf "${STORE_HOME}/src/ui"
mkdir -p "${STORE_HOME}/src"
cp -a "${RELEASE_DIR}/web" "${STORE_HOME}/src/ui"

info "补齐小米插件规范结构 …"
python3 "${DEPLOY_DIR}/native_layout.py" \
  --user "${NAS_USER_ID}" \
  --name communitystore \
  --service xiaomi-community-store.service \
  --title "插件市场" \
  --plugin-id "${PLUGIN_ID}" \
  --version "${VERSION}" \
  --desc "安装、更新和管理社区插件" \
  --tags "store,community" \
  --quiet

info "安装开机自启钩子 …"
# 小米 NAS 的 /etc 是 overlay，systemd 开机时看不到安装时新增的 unit，
# 需要 cron 在开机窗口内补启动。
if [[ -f "${DEPLOY_DIR}/install-boot-hook.sh" ]]; then
  sh "${DEPLOY_DIR}/install-boot-hook.sh" add
else
  warn "未找到 install-boot-hook.sh，重启后插件可能不会自动启动。"
fi

info "启动服务 …"
"${NGINX_BIN}" -t
systemctl daemon-reload
systemctl enable xiaomi-community-store.service
systemctl restart xiaomi-community-store.service
systemctl reload nginx

info "健康检查 …"
for i in $(seq 1 15); do
  if python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:18119/healthz', timeout=3)" 2>/dev/null; then
    info "安装完成！请完全退出并重新打开小米智能存储客户端。"
    exit 0
  fi
  sleep 1
done

error "健康检查超时。请查看日志：journalctl -u xiaomi-community-store.service -n 50"
exit 1
