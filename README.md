# 小米智能存储应用商店

小米智能存储的第三方应用商店与插件生态。**非小米官方产品，与小米、115、阿里云盘没有隶属或背书关系。**

在已开启密钥 SSH 的自有 NAS 上安装一次商店，之后从小米智能存储客户端浏览、安装和更新插件。页面随客户端连接当前设备，不要求 NAS 固定 IP。

## 架构

```text
GitHub 仓库
├── projects/            插件源码
├── apps/                构建产物（插件 ZIP + 图标）
├── apps.json            应用清单（版本、SHA-256、简介等）
├── scripts/build_apps.py  打包脚本
└── projects/xiaomi-community-app-store/   商店本体

GitHub Releases           只发布商店安装器 ZIP

NAS 上的商店运行时
  → 从 GitHub raw 拉取 apps.json 获取应用列表
  → 从 GitHub raw 下载插件 ZIP，校验 SHA-256 后安装
  → 可检测 GitHub Releases 上的商店新版本
```

## 安装商店

### 方式一：一键脚本（推荐）

**Mac / Linux：**

```bash
# 自动下载最新 Release、校验、安装
bash -c "$(curl -fsSL https://raw.githubusercontent.com/fw867/xiaomi-nas-plugin-market/main/install-from-github.sh)"

# 或指定参数
NAS_IP=192.168.31.100 bash install-from-github.sh
```

**Windows PowerShell：**

```powershell
# 从仓库获取脚本后运行
.\install-from-github.ps1

# 或指定参数
.\install-from-github.ps1 -NasIp 192.168.31.100
```

脚本会自动：查询最新 Release → 下载 ZIP → SHA-256 校验 → 检测小米账号 → 安装到 NAS。

### 方式二：手动下载安装

1. 在 [Releases](https://github.com/fw867/xiaomi-nas-plugin-market/releases) 下载最新 `xiaomi-plugin-market-x.y.z.zip` 和 `SHA256SUMS.txt`。
2. 核对 SHA-256 后完整解压。
3. Windows 双击 `install-windows.cmd`；Mac 运行 `install-macos.command`；Mac/Linux 也可执行 `bash install.sh`。
4. 按提示填写 NAS IP、SSH 私钥和小米账号。

### 方式三：NAS 本机安装

已 SSH 登录 NAS 时，无需 `NAS_IP` / `NAS_SSH_KEY`：

```bash
# 1. 把安装包传到 NAS 并解压
scp xiaomi-plugin-market-x.y.z.zip root@<NAS_IP>:/tmp/
ssh root@<NAS_IP>
cd /tmp && unzip xiaomi-plugin-market-x.y.z.zip && cd xiaomi-plugin-market-x.y.z

# 2. 直接安装
bash deploy/install-local.sh

# 多账号时指定用户
NAS_USER_ID=u123456 bash deploy/install-local.sh
```

安装完成后，**完全退出并重新打开**小米智能存储客户端，在「全部应用」中打开 **应用商店**。

> Mac 如果拦截未签名的 `.command`，在终端运行 `bash install.sh`；不要关闭系统安全机制。Windows 需要安装系统可选功能 OpenSSH 客户端。
>
> **每人使用自己的私钥，绝不共用发布者的私钥、证书或令牌。** 不需要另行输入管理码。

## 插件列表

| 插件 | 版本 | 说明 |
| --- | --- | --- |
| [设备管家](projects/xiaomi-device-manager-prototype) | 0.4.1 | 只读查看 CPU、内存、温度、网络、磁盘 SMART 和 Docker 状态 |
| [115 云备份](projects/xiaomi-115-sync-plugin) | 0.1.0 | 需要自己获批的 115 App ID，再扫码授权 |
| [阿里云盘备份](projects/xiaomi-aliyundrive-sync-plugin) | 0.1.0 | 需要自己获批的阿里云盘应用，再扫码授权 |
| [WebDAV 文件桥](projects/xiaomi-webdav-plugin) | 0.2.0-rc5 | 测试版；多账号、多目录授权、远程单向备份 |
| [qB 下载](projects/xiaomi-qbittorrent-plugin) | 0.1.0-rc1 | 开发候选版；磁力/种子下载、限速；需要 Docker |
| 夸克网盘 | 未发布 | 目前没有可用实现 |

## 插件如何分发

插件包存放在仓库 `apps/` 目录，元数据在 `apps.json`。商店在 NAS 上运行时通过 GitHub raw 拉取：

```text
apps.json          → raw.githubusercontent.com/<repo>/<branch>/apps.json
插件 ZIP            → raw.githubusercontent.com/<repo>/<branch>/apps/<id>-<version>.zip
```

本地没有对应文件时自动从 GitHub 下载；已缓存的包不会重复下载。所有插件安装前都校验 `apps.json` 中声明的 SHA-256。

## 开发

### 目录结构

```text
projects/                          插件源码
  xiaomi-community-app-store/      商店本体
  xiaomi-device-manager-prototype/ 设备管家
  xiaomi-115-sync-plugin/          115 云备份
  xiaomi-aliyundrive-sync-plugin/  阿里云盘备份
  xiaomi-webdav-plugin/            WebDAV 文件桥
  xiaomi-qbittorrent-plugin/       qB 下载

apps/                              构建产物（提交到 git）
apps.json                          应用清单（自动生成）
scripts/build_apps.py              打包脚本
.github/workflows/                 CI/CD
```

### 构建插件

```bash
# 打包全部稳定版插件
python3 scripts/build_apps.py

# 只打一个
python3 scripts/build_apps.py --only devicemanager

# 包含 rc/beta 测试包
python3 scripts/build_apps.py --include-candidates
```

构建产物写入 `apps/`，清单写入 `apps.json`。提交后 CI 会自动构建（见下文）。

### CI/CD

| 工作流 | 触发 | 作用 |
| --- | --- | --- |
| **Build Apps** | `projects/` 下除商店外有改动推送 | 自动打包插件 → 更新 `apps/` 和 `apps.json` → 提交回仓库 |
| **Release Store** | 手动触发，或提交信息含 `Releases` | 自动递增版本号 → 打包商店 ZIP → 创建 GitHub Release |

手动触发：仓库 Actions 页面 → 选择工作流 → Run workflow。

发布商店时在提交信息中带上 `Releases` 即可自动发布：

```bash
git commit -m "feat: 商店支持远程 apps.json Releases"
git push
```

### 本地开发商店

```bash
cd projects/xiaomi-community-app-store
python3 server.py --dev    # 预览模式，不修改 NAS
# 打开 http://127.0.0.1:18119/
```

### 添加新插件

1. 在 `projects/` 下创建插件目录（参考现有插件结构）。
2. 在 `scripts/build_apps.py` 的 `PACKAGE_SPECS` 中添加条目。
3. 运行 `python3 scripts/build_apps.py --only <id>` 验证打包。
4. 提交推送，CI 自动更新 `apps/` 和 `apps.json`。

## 当前限制

- 测试发布，不是经过全机型验证的正式产品。已在 RP05 和 Mac 客户端验证；Windows 10/11 实体安装、其他固件仍需测试。
- 第三方云盘接口、授权审批和限流由服务方决定。
- WebDAV 与云盘备份不是传播删除的双向镜像。重要文件另做备份。
- 不承诺官方 OTA 后入口与服务始终存在。
- 插件安装依赖 GitHub raw 可达性；GitHub 不可达时使用已缓存的包。
- 不附带 root 获取工具、个人密钥、云盘令牌或 NAS 配置。

## 文档

- [构建与验证](BUILD.md)
- [发布记录](CHANGELOG.md)
- [安全说明](SECURITY.md)
- [第三方组件与图标](THIRD_PARTY_NOTICES.md)
- [Windows 安装说明](projects/xiaomi-community-app-store/WINDOWS-安装说明.txt)

报告问题时附设备型号、固件、客户端版本和脱敏错误信息。不要上传账号密码、SSH 私钥、证书私钥或完整配置文件。
