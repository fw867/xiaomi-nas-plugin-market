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

### 方式一：NAS 本机一键安装（推荐）

SSH 登录 NAS 后直接执行：

```bash
ssh root@<NAS_IP>

# 一行安装（自动下载最新 Release、校验、安装）
bash -c "$(curl -fsSL https://raw.githubusercontent.com/fw867/xiaomi-nas-plugin-market/main/install-from-github.sh)"

# 多账号时指定用户
NAS_USER_ID=u123456 bash -c "$(curl -fsSL https://raw.githubusercontent.com/fw867/xiaomi-nas-plugin-market/main/install-from-github.sh)"
```

### 方式二：手动下载安装

1. 在 [Releases](https://github.com/fw867/xiaomi-nas-plugin-market/releases) 下载最新 `xiaomi-plugin-market-x.y.z.zip` 和 `SHA256SUMS.txt`。
2. 传到 NAS 并解压：
   ```bash
   scp xiaomi-plugin-market-x.y.z.zip root@<NAS_IP>:/tmp/
   ssh root@<NAS_IP>
   cd /tmp && unzip xiaomi-plugin-market-x.y.z.zip && cd xiaomi-plugin-market-x.y.z
   ```
3. 执行安装：
   ```bash
   bash deploy/install-local.sh
   ```

### 方式三：从电脑安装（需 SSH 密钥）

下载 Release ZIP 解压后，Windows 双击 `install-windows.cmd`；Mac 运行 `install-macos.command`；Mac/Linux 执行 `bash install.sh`。按提示填写 NAS IP、SSH 私钥和小米账号。

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
| [Transmission 下载](projects/xiaomi-transmission-plugin) | 0.1.0 | BT 下载、限速与并发设置；随包携带 transmission-daemon，不需要 Docker |
| 夸克网盘 | 未发布 | 目前没有可用实现 |

## 插件如何分发

插件包存放在仓库 `apps/` 目录，元数据在 `apps.json`。商店在 NAS 上运行时通过 GitHub raw 拉取：

```text
apps.json          → raw.githubusercontent.com/<repo>/<branch>/apps.json
插件 ZIP            → raw.githubusercontent.com/<repo>/<branch>/apps/<id>-<version>.zip
```

本地没有对应文件时自动从 GitHub 下载；已缓存的包不会重复下载。所有插件安装前都校验 `apps.json` 中声明的 SHA-256。

### apps.json 的缓存与 CDN

商店优先从 GitHub 拉取清单，但有两点容易踩：

- **`raw.githubusercontent.com` 的分支路径会被 CDN 缓存。** `<repo>/main/apps.json`
  在仓库更新后仍可能长时间返回旧内容（加 `Cache-Control: no-cache` 或 `?t=` 都无效）。
  因此商店先用 GitHub API 解析 HEAD 的 commit SHA，再按
  `<repo>/<sha>/apps.json` 拉取；失败才退回分支路径和本地缓存。
- **本地缓存只作加速与断网兜底。** 命中缓存有 180 秒 TTL，过期即重新拉取，
  不会再出现「装了新插件但列表不更新」。

## 插件如何被系统认可

小米智能存储有一套插件规范。**每次开机** `plugin.boot` 会执行
`plugincenter boot --system`，对它认定的每个「已安装」插件运行
`/usr/bin/plugin.sh verify`；校验不通过的插件会被判定为
`can't use forever, force uninstall` —— 删除插件目录并从注册表移除条目，
表现为「服务还在跑，但客户端应用列表里插件消失」。

### verify 到底校验什么

它**不是厂商签名校验**，而是**文件摘要自校验**：

```sh
plugin_verify() {
    [ ! -d "$PLUG_SRC_DIR" ] && return 1        # 缺 src 目录 → 失败
    [ ! -f "$PLUG_HOME_DIR/INFO" ] && return 1  # 缺 INFO 文件 → 失败

    abstract=$(find src/ -type f | LC_COLLATE=C sort \
               | xargs sha256sum | sha256sum)   # 逐文件摘要拼接后再取摘要

    if [ "installing" = "$PLUG_STATUS" ]; then
        jq --arg v "$abstract" '.["abstract"] = $v' "$PLUG_HOME_DIR/INFO"
    elif [ "unverified" = "$PLUG_STATUS" ]; then
        [ "$abstract" = "$(jq -r .abstract "$PLUG_HOME_DIR/INFO")" ] || return 1
    fi
}
```

即「对比当前文件与上次记录的摘要」，没有公钥、证书链或厂商密钥。

### 每个插件需要提供的要素

```text
/home/<小米用户>/plugin/<插件key>/
├── etc/  var/  tmp/          # 规范要求的目录，缺一不可
├── scripts/
│   ├── control               # plugincenter boot 会执行 `<control> enable`
│   └── hotplug
├── src/ui/                   # 前端页面
└── INFO                      # 元数据，含 "abstract" 摘要字段
```

因此**每个插件在仓库里都自带两个要素文件**：

| 文件 | 作用 |
|------|------|
| `deploy/plugin-meta.json` | `INFO` 的元数据来源（名称、描述、标签、服务名等） |
| `deploy/control` | 规范要求的控制脚本，转发到对应 systemd 服务 |

打包时会自动纳入 bundle（`runtime/plugin-meta.json`、`runtime/control`），
安装时由 `deploy/native_layout.py` 复制就位、创建目录、并按上面的算法
**重算 `abstract` 写入 `INFO`**。

> `abstract` 覆盖 `src/` 下**全部**文件，因此必须在 UI 文件全部就位后最后计算；
> 之后任何文件改动都会让校验失败——这是设计上的防篡改，重装即可恢复。
>
> 复刻算法时注意换行符：`plugin.sh` 是逐行写入文件后再整体取摘要，
> 拼接时少了 `\n` 摘要就对不上。

### 相关组件

| 脚本 | 职责 |
|------|------|
| `deploy/native_layout.py` | 补齐目录结构、`scripts/control`、`INFO`（含 abstract） |
| `deploy/restore-plugins.py` | 兜底：注册表或目录被清时重建条目、UI 与结构 |
| `deploy/community-plugins-boot.sh` | 开机钩子：恢复插件状态并启动服务 |
| `deploy/install-boot-hook.sh` | 安装上述钩子的 crontab 条目 |

### 另一个坑：/etc 是 overlay

小米 NAS 的根文件系统是只读 erofs，`/etc` 是 overlay（upperdir 在 `/data/etc/upper`）。
systemd 在 overlay 挂载**之前**就已读取单元目录，所以安装时新增到
`/etc/systemd/system/` 的 unit 虽然 `enabled`、符号链接也正确，开机时却不会
被拉起。`community-plugins-boot.sh` 由 root crontab 每分钟触发，在开机窗口内
`daemon-reload` 并补启动这些服务。

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
  xiaomi-ssh-control-plugin/       SSH 开关
  xiaomi-transmission-plugin/      Transmission 下载

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
2. **提供规范要素文件**（否则开机后会被系统强制卸载）：
   - `deploy/plugin-meta.json`：名称、描述、标签、服务名等元数据
   - `deploy/control`：转发到该插件 systemd 服务的控制脚本
3. 在 `scripts/build_apps.py` 的 `PACKAGE_SPECS` 中添加条目。
4. 运行 `python3 scripts/build_apps.py --only <id>` 验证打包，
   确认 bundle 里包含 `runtime/plugin-meta.json` 与 `runtime/control`。
5. 提交推送，CI 自动更新 `apps/` 和 `apps.json`。

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
