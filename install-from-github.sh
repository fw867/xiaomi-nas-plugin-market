#!/usr/bin/env bash
# 一键从 GitHub Releases 下载最新应用商店安装包并部署到小米智能存储。
#
# 用法：
#   bash install-from-github.sh
#   NAS_IP=192.168.31.100 NAS_SSH_KEY=~/.ssh/nas-root-key bash install-from-github.sh
#
# 可选环境变量：
#   NAS_IP          NAS 局域网 IP（不设则交互询问）
#   NAS_SSH_KEY     root SSH 私钥路径（不设则自动查找常见位置）
#   NAS_USER_ID     小米用户 ID（不设则自动检测）
#   PLUGIN_ID       商店插件编号，默认 11002
#   GITHUB_REPO     仓库 owner/name，默认 fw867/xiaomi-nas-plugin-market
#   GITHUB_TOKEN    可选，私有仓库或提高 API 限速时使用

set -euo pipefail

REPO="${GITHUB_REPO:-fw867/xiaomi-nas-plugin-market}"
API="https://api.github.com/repos/${REPO}"
PLUGIN_ID="${PLUGIN_ID:-11002}"
WORK_DIR="$(mktemp -d -t xiaomi-store-install.XXXXXX)"
trap 'rm -rf "${WORK_DIR}"' EXIT

# ---------- 颜色输出 ----------
if [[ -t 1 ]]; then
  C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'; C_RED=$'\033[31m'; C_RESET=$'\033[0m'
else
  C_GREEN=""; C_YELLOW=""; C_RED=""; C_RESET=""
fi
info()  { printf '%s\n' "${C_GREEN}[信息]${C_RESET} $*"; }
warn()  { printf '%s\n' "${C_YELLOW}[警告]${C_RESET} $*" >&2; }
error() { printf '%s\n' "${C_RED}[错误]${C_RESET} $*" >&2; }

# ---------- 依赖检查 ----------
for cmd in curl tar ssh scp python3; do
  if ! command -v "${cmd}" >/dev/null 2>&1; then
    error "缺少命令：${cmd}，请先安装。"
    exit 1
  fi
done

# ---------- GitHub API 请求 ----------
github_get() {
  local url="$1"
  local args=(-fsSL --retry 3 --retry-delay 2 -H "Accept: application/vnd.github+json")
  if [[ -n "${GITHUB_TOKEN:-}" ]]; then
    args+=(-H "Authorization: Bearer ${GITHUB_TOKEN}")
  fi
  curl "${args[@]}" "${url}"
}

# ---------- 1. 获取最新 Release ----------
info "查询 ${REPO} 最新 Release …"
RELEASE_JSON="$(github_get "${API}/releases/latest")"

TAG="$(printf '%s' "${RELEASE_JSON}" | python3 -c "import sys,json; print(json.load(sys.stdin).get('tag_name',''))")"
if [[ -z "${TAG}" ]]; then
  error "无法获取最新 Release 标签。仓库可能没有 Release，或 API 限流。"
  exit 1
fi
VERSION="${TAG#v}"
info "最新版本：${VERSION}（${TAG}）"

# ---------- 2. 找到 ZIP 与 SHA256SUMS 资产 ----------
ZIP_NAME="xiaomi-plugin-market-${VERSION}.zip"
SHA_NAME="SHA256SUMS.txt"

ZIP_URL="$(printf '%s' "${RELEASE_JSON}" | python3 -c "
import sys, json
assets = json.load(sys.stdin).get('assets', [])
for a in assets:
    if a['name'] == '${ZIP_NAME}':
        print(a['browser_download_url']); break
")"
SHA_URL="$(printf '%s' "${RELEASE_JSON}" | python3 -c "
import sys, json
assets = json.load(sys.stdin).get('assets', [])
for a in assets:
    if a['name'] == '${SHA_NAME}':
        print(a['browser_download_url']); break
")"

if [[ -z "${ZIP_URL}" ]]; then
  error "Release 中未找到 ${ZIP_NAME}。可用资产："
  printf '%s' "${RELEASE_JSON}" | python3 -c "
import sys, json
for a in json.load(sys.stdin).get('assets', []):
    print(f\"  - {a['name']}  ({a.get('size',0)//1024} KiB)\")
" >&2
  exit 1
fi
info "下载地址：${ZIP_URL}"

# ---------- 3. 下载 ----------
info "下载安装包 …"
github_get "${ZIP_URL}" > "${WORK_DIR}/${ZIP_NAME}"
info "下载校验文件 …"
if [[ -n "${SHA_URL}" ]]; then
  github_get "${SHA_URL}" > "${WORK_DIR}/${SHA_NAME}"
else
  warn "Release 未附带 ${SHA_NAME}，将跳过 SHA-256 校验。"
fi

# ---------- 4. SHA-256 校验 ----------
if [[ -f "${WORK_DIR}/${SHA_NAME}" ]]; then
  info "校验 SHA-256 …"
  EXPECTED="$(grep -E "[[:space:]]${ZIP_NAME}\$" "${WORK_DIR}/${SHA_NAME}" | awk '{print $1}')"
  if [[ -z "${EXPECTED}" ]]; then
    error "${SHA_NAME} 中未找到 ${ZIP_NAME} 的哈希。"
    exit 1
  fi
  ACTUAL="$(python3 -c "import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],'rb').read()).hexdigest())" "${WORK_DIR}/${ZIP_NAME}")"
  if [[ "${EXPECTED}" != "${ACTUAL}" ]]; then
    error "SHA-256 校验失败！"
    error "  期望：${EXPECTED}"
    error "  实际：${ACTUAL}"
    exit 1
  fi
  info "SHA-256 校验通过：${ACTUAL:0:16}…"
fi

# ---------- 5. 解压 ----------
info "解压安装包 …"
EXTRACT_DIR="${WORK_DIR}/extracted"
mkdir -p "${EXTRACT_DIR}"
python3 -c "
import zipfile, sys
zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])
" "${WORK_DIR}/${ZIP_NAME}" "${EXTRACT_DIR}"

# 找到解压后的项目目录（Release ZIP 顶层有一层版本目录）
PROJECT_DIR=""
for candidate in \
  "${EXTRACT_DIR}" \
  "${EXTRACT_DIR}"/*/ \
; do
  if [[ -f "${candidate}/deploy/install-on-nas.sh" ]]; then
    PROJECT_DIR="${candidate%/}"
    break
  fi
done
if [[ -z "${PROJECT_DIR}" ]]; then
  error "解压后未找到 deploy/install-on-nas.sh，目录结构："
  find "${EXTRACT_DIR}" -maxdepth 3 -type f | head -20 >&2
  exit 1
fi
info "项目目录：${PROJECT_DIR}"

for required in server.py storelib.py web/index.html catalog/catalog.json catalog/repository-public.pem deploy/install-on-nas.sh; do
  if [[ ! -f "${PROJECT_DIR}/${required}" ]]; then
    error "发布包缺少文件：${required}"
    exit 1
  fi
done
info "发布包文件完整。"

# ---------- 6. 收集 NAS 连接信息 ----------
if [[ -z "${NAS_IP:-}" ]]; then
  if [[ -t 0 ]]; then
    printf '请输入小米 NAS 当前局域网 IP：' >&2
    read -r NAS_IP
  else
    error "非交互模式下必须设置 NAS_IP。"
    exit 2
  fi
fi
if [[ ! "${NAS_IP}" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
  error "NAS IP 格式不正确：${NAS_IP}"
  exit 2
fi

if [[ -z "${NAS_SSH_KEY:-}" ]]; then
  for candidate in \
    "${HOME}/.xiaomi-nas-root/nas-root-key" \
    "${HOME}/.ssh/xiaomi-nas-root" \
    "${HOME}/.ssh/nas-root-key"
  do
    if [[ -f "${candidate}" ]]; then
      NAS_SSH_KEY="${candidate}"
      break
    fi
  done
fi
if [[ -z "${NAS_SSH_KEY:-}" && -t 0 ]]; then
  printf '未自动找到 SSH 密钥，请输入 root 私钥路径：' >&2
  read -r NAS_SSH_KEY
  NAS_SSH_KEY="${NAS_SSH_KEY/#\~/${HOME}}"
fi
if [[ ! -f "${NAS_SSH_KEY:-/dev/null}" ]]; then
  error "SSH 私钥不存在：${NAS_SSH_KEY:-（未设置）}"
  exit 2
fi

SSH_OPTS=(
  -i "${NAS_SSH_KEY}"
  -o BatchMode=yes
  -o IdentitiesOnly=yes
  -o PreferredAuthentications=publickey
  -o PubkeyAuthentication=yes
  -o PasswordAuthentication=no
  -o KbdInteractiveAuthentication=no
  -o StrictHostKeyChecking=accept-new
)
REMOTE="root@${NAS_IP}"

# 测试 SSH 连接
info "测试 SSH 连接 ${REMOTE} …"
if ! ssh "${SSH_OPTS[@]}" "${REMOTE}" 'echo ok' >/dev/null 2>&1; then
  error "无法 SSH 到 ${REMOTE}。请检查 IP、密钥和 NAS 是否已开启密钥 SSH。"
  exit 1
fi
info "SSH 连接成功。"

# ---------- 7. 检测小米用户 ID ----------
if [[ -z "${NAS_USER_ID:-}" ]]; then
  info "检测小米用户 ID …"
  REGISTRY_USERS="$(ssh "${SSH_OPTS[@]}" "${REMOTE}" 'for path in /data/plugin/u*.list; do [ -f "$path" ] || continue; name=${path##*/}; printf "%s\n" "${name%.list}"; done')"
  REGISTRY_COUNT="$(printf '%s\n' "${REGISTRY_USERS}" | sed '/^$/d' | wc -l | tr -d ' ')"
  if [[ "${REGISTRY_COUNT}" == "1" ]]; then
    NAS_USER_ID="$(printf '%s\n' "${REGISTRY_USERS}" | sed '/^$/d')"
  elif [[ -t 0 && -n "${REGISTRY_USERS}" ]]; then
    printf '检测到多个小米账号：\n%s\n用户 ID：' "${REGISTRY_USERS}" >&2
    read -r NAS_USER_ID
  else
    error "无法自动确定小米账号。请设置 NAS_USER_ID 后重试。候选值：${REGISTRY_USERS}"
    exit 2
  fi
fi
if [[ ! "${NAS_USER_ID}" =~ ^[A-Za-z0-9_-]+$ ]]; then
  error "小米用户 ID 无效：${NAS_USER_ID}"
  exit 2
fi
info "使用小米用户：${NAS_USER_ID}"

# ---------- 8. 执行安装 ----------
info "开始安装到 NAS …"
export NAS_IP NAS_SSH_KEY NAS_USER_ID PLUGIN_ID
bash "${PROJECT_DIR}/deploy/install-on-nas.sh"

info "安装完成！请完全退出并重新打开小米智能存储客户端。"
