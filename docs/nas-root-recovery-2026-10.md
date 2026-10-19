# 小米智能存储（minas / RP05）root 恢复与固件分析技术报告

> ## ✅ 结论已更新（2026-10-19）：**已恢复**
>
> **root 通道已恢复；10 个社区插件全部恢复运行；自愈机制已安装。**
>
> | 项 | 状态 |
> | --- | --- |
> | **root 权限** | ✅ **已恢复**（2026-10-19）—— 参照社区针对 1.0.9.462 的方案（`lvmaple/minas_plugin` 仓库的 `xiaomi-nas-root-tool-1.0.9.462`）。**利用细节不写入本仓库** |
> | **设备 IP** | ⚠️ **已变更为 `192.168.1.9`**（原 `192.168.1.8`；`.8` 现为飞牛 fnOS，勿混用） |
> | **10 个社区插件** | ✅ **全部恢复**：目录 / `current` 软链 / systemd 服务 / 端口 18080·18100·18101·18119·18130·18140·18160·18170·18180·18190 全部 `HTTP 200`；注册表 10 条 `frontend`+`info` 完整 |
> | **自愈机制** | ✅ **已安装**：`keepalive.py`（server.list 条目注入 + 每插件备份 + 目录/`current`/服务自愈，服务内后台线程 + root cron 双通道）+ 开机钩子的 `restore-plugins.py`（注册表重建） |
> | **SSH** | ✅ **已修复（2026-10-19 晚）**：公钥**与密码**登录都能正常执行命令。根因不是 dropbear 的 bug，而是**厂商把 dropbear 写死 exec `/usr/sbin/mi-shell`**（一个 JSON 协议受限壳，忽略 `-c` 参数）；根分区是只读 erofs 无法改二进制，改用 **bind-mount 垫片转发 bash** 解决（原文件不动，`umount` 即还原） |
> | **数据** | ✅ 未受影响（照片/视频/下载/共享/相册/备份全程未触碰） |
>
> #### 本次恢复实操中确认的三条关键事实（供未来参考）
> 1. 🟢 **`plugincenter` 的对账破坏面比预期大**：打开 App「应用市场」（或跑 `/usr/bin/plugincenter update`）会
>    **把注册表 `<用户>.list` 整个替换成云端清单**、**刷新 `server.list`**、并**可能只删 `<插件>/current` 而保留目录**。
>    ⇒ 必须有多重兜底：**server.list 条目注入 + 注册表重建 + `current` 重链**。
> 2. 🟢 **`/home/<root的新home>`（本例 `/home/rootx`）是 0700 root 目录**，`filemgr/get_file` 的存在性预言机
>    对它**会假阴性**（返回"不存在"）—— 判断这类路径是否在位，**必须用 root 权限实际读取**，不能信预言机。
> 3. 🟢 **对账是"整文件替换"，两个方向都会丢东西**：云端正常时换成 **5 条厂商条目**（**我们的 10 条被削**），
>    云端空/失败时换成 **`{}`**（**厂商 5 条也没了** ⇒ App 里"应用少几个"，实测丢了百度网盘/迅雷）。
>    ⇒ 两侧都必须有**本地基线**：我们的 10 条靠插件目录/INFO 重建，**厂商 5 条必须另存快照**
>    （`/data/backup/plugin-registry/vendor-entries.json`），重建时**只增不删、已存在的一字不改**。
>
> **恢复后的运维要点**见 §6；**自愈机制的复现验证方法**见
> `projects/xiaomi-community-app-store/README.md` 的「在真机上验证自愈」一节。

- 报告日期：2026-10-10（**结论更新：2026-10-19**）
- 设备：小米智能存储 RP05（`xiaomi.station.rp05`），aarch64 / Realtek RTD1619B，内核 6.6.35-yocto
- 固件：**1.0.9.462**（升级前 1.0.8.425）
- 定位：本文档写给**未来拿到 root 之后的我**，以及**想复现整个调查的人**
- 可信度图例：🟢 已在本机/设备/固件上实测确证 ｜ 🟡 有来源未验证 ｜ 🔴 推测

> **全文不包含任何私钥内容**，只出现指纹与文件路径。

---

## 目录

1. [事件时间线](#1-事件时间线)
2. [已确证的事实清单](#2-已确证的事实清单)（含 **§2.10 后续补充证伪清单**）
3. [固件分析资产](#3-固件分析资产)（含 **A/B 双槽机制**与防回滚）
4. [三条可行退路的评估](#4-三条可行退路的评估)
5. [安全观察](#5-安全观察与本事件相关但独立)（含 **§5.6 收尾结论：完整证伪清单 + 仅剩三条退路**；
   以及 **§5.7 独立成果：中枢网关镜像已成功提取并完整分析**；
   以及 **§5.8 兜底保险：池文件触发的固件回退（无需 SSH）**；
   以及 **§5.9 SSH「认证成功但命令无输出」的根因与修复**）
6. [**拿到 root 后 10 分钟恢复**](#6-拿到-root-后-10-分钟恢复)
7. [附录：可复现命令与证据索引](#7-附录可复现命令与证据索引)

---

## 1. 事件时间线

| 时间 | 事件 | 证据 |
| --- | --- | --- |
| ~2026-08-28 | 固件 **1.0.8.425** 时期；社区注入点 `GET /pool0/video/__x";<cmd>;"__.ts` 可用（配套 mTLS 客户端证书） | `artifacts/scratch/injection-candidates.md` §B.2 B-8（🟡 社区工具在真机验证过）；`kid0114` / `scruel` 两个仓库 |
| 此后 | 用户通过该注入点取得 root，部署了 **10 个社区插件**（跑在 18080–18190 端口），并写了 SSH 持久化钩子 | 本仓库 `projects/xiaomi-community-app-store/deploy/`（当年的部署脚本仍在） |
| 2026-10-10 | 固件升级到 **1.0.9.462** | 设备 `get_system_version` → `version: "1.0.9.462.F24C26D24-REL"`（🟢 实测） |
| 升级后 | ① `/etc/dropbear/authorized_keys` 失效 → SSH 不可用<br>② 第三方插件被 `plugincenter boot --system` **force uninstall** → 客户端应用列表里全部消失<br>③ 插件端口 18080–18190 全部不可达 | ① `restore-plugins.py` docstring（🟢 源码）；② `plugincenter` 的 `openssl -verify` 逻辑（🟢 逆向）；③ 用户实测 |
| 调查结论 | **软件侧已无可用提权路径**（详见 §2）；只剩物理/降级/官方支持三条退路 | 本文 §2、§4 |

### 1.1 升级后到底变了什么（精确版）

**a) 授权密钥为什么没了** —— 🔴 最可能：**OTA 重建了 `/etc` 的 overlay 上层**。
`/etc` 是 overlay 挂载：`lowerdir=/etc`、`upperdir=/data/etc/upper`（`/usr/lib/minas/syspreinit.sh:200-208` 🟢）。
升级后 overlay upper 被重建 ⇒ 写在 `/etc/dropbear/authorized_keys` 的文件随之消失。
> ⚠️ **注意**：`rpmb ssh_en` 标志**没有**被清（见 §2.3）——所以"SSH 失效"不是这个开关被改，而是授权文件丢了 🟢。

**b) 插件为什么被卸** —— 🟢 确证：`plugincenter` 对每个已注册插件下载 `.mpk`
（`<server_list>/<user>/<name>/<version>/<name>.mpk`），解出 `plugin.tgz` + `plugin.sign`，
再执行
```
openssl dgst -sha256 -verify /usr/share/plugin/plugin.pem -signature <sign> <tgz>
```
第三方插件没有小米签名 ⇒ 校验必然失败 ⇒ 判定 `can't use forever, force uninstall`：
- 删除 `/home/<用户>/plugin/<key>/`
- 从 `/data/plugin/<用户>.list` 移除条目

（🟢 证据：`plugincenter` 二进制 strings 里的上述 openssl 命令行 + `restore-plugins.py:4-9` 的中文说明）

**c) 端口为什么不可达** —— 🟡 推断：**容器本身没了 / 没被拉起**，而不是单纯 DNAT 丢失。
依据来自 `community-plugins-boot.sh:14-24` 的自述：
> `/etc/iptables/iptables.rules` 是 0 字节空文件，而 `iptables.service` 在开机很早执行 `iptables-restore`
> ⇒ 默认清空所有链，把 Docker 自建的 `MASQUERADE`/`DOCKER` 链一并抹掉；`docker.service` 晚两秒启动时因
> `docker0` 已存在而不重建 ⇒ 规则永久缺失 ⇒ 所有容器出不了网。而**"入站端口映射因为另有 `docker-proxy`
> 兜着，看起来仍然正常"**。

⇒ 即在**当年**（有 boot hook 时）DNAT 丢失只影响**出网**，不影响**入站**。所以现在端口不通，
更可能是容器不存在了。**确认需要 root**：`docker ps -a`、`iptables -t nat -S`。

---

## 2. 已确证的事实清单

> 每条一句话 + 证据。这是本文最有价值的部分：**它把"哪些路已死"钉死了**，避免未来重复劳动。

### 2.1 可写的面（以 root 身份写入任意内容 ✅）

| 目录 | 可写？ | 证据 |
| --- | --- | --- |
| `/tmp` | ✅ | 实测（见 §2.5 的写入原语对照实验） |
| `/var/plugin` | ✅ | 同上（`plugin.boot` 会 `chmod 1777`） |
| `/home/<用户>/**`（含 `/pool0/plugin/**`） | ✅ | 同上 + PROPFIND/MKCOL/PUT 逐级 201/204 |
| `/etc` | ❌ **只读** | 实测 `{"result":"拷贝字幕文件失败"}` |
| `/data`（含 `/data/plugin/<用户>.list`） | ❌ **只读** | 同上 |
| `/run` | ❌ 只读 | 同上 |

### 2.2 我们拥有的写入原语（🟢 已实测）

**漏洞**：`/usr/lib/lua/5.1/luci/function/NasSubtitle.lua`
- `:24-29` `getSubtitleConfigPath(video_path)` —— 目标路径的**目录与部分文件名完全来自用户输入**
- `:76-79` `writeSubtitleConfig` → `fs.writefile(config_path, …)` —— **无路径白名单、无 `realpath` 校验**
- `:152-224` `subtitleAddLocal` —— `video_path` / `subtitle_path` 均用户可控；`video_path` 指向的文件**不需要存在**

**路由**：`POST /cgi-bin/luci/player/subtitle_add_local`（**裸客户端证书即可**）

**两条写入路径**：
| # | 目标 | 文件名 | 内容 |
| --- | --- | --- | --- |
| 1 | `<DIR(video_path)>/.<BASE>.<EXT>.subcfg` | 受限（必修 `.subcfg`）| JSON |
| 2 | `<DIR(video_path)>/<basename(subtitle_path)>` | **完全可控** | **完全可控**（二进制安全）|

> 路径 2 是重点：只要 `DIR(video_path) != DIR(subtitle_path)`，就会把 `subtitle_path` 的
> **basename 与完整字节**写到 `<DIR(video_path)>/` 下。配合 WebDAV 先 PUT 一个任意名文件进池子，
> 即得"任意名 + 任意内容"的写入。
> ⚠️ `subtitle_path` 必须是**真实文件系统路径**（WebDAV 的 `/pool0/...` 对应 `/home/<用户>/pool0/...`）。

**实测对照**（原始响应）：
```
video_path=/tmp//v.mp4        -> {"data":{"path":"/tmp//dshx_probe.sh"},"code":0}   ✅
video_path=/var/plugin//v.mp4 -> {"data":{"path":"/var/plugin//dshx_probe.sh"},"code":0} ✅
video_path=/home/u3943892/pool0/data/Docker/v.mp4 -> code:0 ✅（WebDAV 回读内容一致）
video_path=/etc//v.mp4        -> {"result":"拷贝字幕文件失败","code":2300}  ❌
video_path=/data//v.mp4       -> 同上 ❌
video_path=/run//v.mp4        -> 同上 ❌
```

**复现脚本**：`artifacts/scratch/verify-subtitle-write.py`、`artifacts/scratch/probe-subtitle-writable-dirs.py`

### 2.3 dropbear 与 SSH 开关

| 事实 | 证据 |
| --- | --- |
| dropbear **实际读 `/root/.ssh/authorized_keys`** | `strings /usr/sbin/dropbear` → `%s/.ssh/authorized_keys`；**没有**任何 `/etc/dropbear/authorized_keys` 字面量 |
| 启动参数未改授权路径 | `/usr/lib/systemd/system/dropbear@.service`: `ExecStart=-/usr/sbin/dropbear -i -r ${DROPBEAR_RSAKEY_DIR}/dropbear_rsa_host_key $DROPBEAR_EXTRA_ARGS`；`/etc/default/dropbear`: `DROPBEAR_EXTRA_ARGS=" -B"`（**无 `-s`/`-c`**） |
| ⛔ **`-B`（允许空密码）只是"镜像状态"，运行时不可用 —— 已实测排除，不要再列为待验证项** | 镜像里 `/etc/shadow` 的 `root` 是**空哈希**、`/etc/default/dropbear` 带 `-B`、PAM `common-auth` 是 `pam_unix.so nullok`；**但实机对 `192.168.1.8:22` 用 `root` + 空密码尝试时，服务端返回** `Bad authentication type; allowed types: ['publickey']` ⇒ **运行时的 dropbear 只提供 `publickey`**。原因：运行时 `/etc` 是 overlay（`/data/etc/upper`），其内容与固件镜像里的那份**不一致** 🟢 |
| `/etc/dropbear/` 出厂为空 | host key 运行时由 `dropbearkey.service` 生成 |
| `/root` 是真目录（非软链），权限 `drwx------ root:root`，**没有 `.ssh`** | 解包 rootfs 实测 |
| `/root` 不在 overlay 覆盖内（overlay 只覆盖 `/etc` 与 `/home`）| `syspreinit.sh:203,210` 🔴 因此 `/root/.ssh/` 可能不可写 |
| ⇒ **稳妥做法：两处都写**（`/etc/dropbear/authorized_keys` 在 overlay 上、可写；`/root/.ssh/authorized_keys` 尽力而为） | — |

**`boot_check.sh` 的 SSH 开关**（`/usr/lib/minas/boot_check.sh:82-112` 🟢 全文已读）：

```sh
ssh_check() {
    channel="$(jq -r '.channel' /usr/share/minas.version)"
    [ "factory" = "$SYSMODE" ] && open="1"                    # ① 工厂模式
    [ "0" = "$open" ] && [ "develop" = "$channel" ] && open="1"   # ② channel=develop
    if [ "0" = "$open" ]; then
        flag_rpmb="$(mitee_tool rpmb get ssh_en)"
        [ "true" = "$flag_rpmb" ] && open="1"                  # ③ RPMB ssh_en=true
    fi
    if [ "1" = "$open" ]; then systemctl start   dropbear.socket
    else                       systemctl stop    dropbear.socket
                               systemctl disable dropbear.socket
    fi
}
```

🟢 **三个条件任一为真即开 SSH**。
🟢 **`rpmb ssh_en` 仍为 `true`**（设备实测确认）⇒ **这个标志抗 OTA**，下次升级也不会被清。
⇒ 所以"升级后 SSH 失效"的原因是**授权文件丢了**（§1.1a），而不是开关被关。
🔴 **`/usr/share/minas.version` 的 `channel` 改成 `develop` 是一条纯软件的 SSH 保活路径**，
但它与 overlay 上那个副本（`/etc/minas.version`）不是同一个文件 —— `ssh_check` 读的是只读分区上的
`/usr/share/minas.version`，要持久化需写 `/data/etc/upper/usr/share/minas.version`（**需要 root**）。

⚠️ **绝对不要碰 `uart_en` / `bootdelay`**：`boot_check.sh:114-138` 的 `_env_check` 一旦发现
env 与 RPMB 不一致，会 `fw_setenv` 并 **`reboot -f` 强制重启**。

### 2.4 老注入点为什么失效（🟢 已定位补丁本体）

**调用链**：
```
WebDAV GET /pool0/video/<需转码的>.ts
 └─ /etc/webdav/nginx/conf.d/webdav/webdav.conf:84   error_page 404 = @transcode_404;
     └─ 同文件 :87-108  location @transcode_404
          :88   if ($uri !~ ^/[^/]+/video/) { return 404 }
          :103  fastcgi_param SCRIPT_FILENAME /usr/bin/transcode_404_handler.sh
          :104  fastcgi_param REQUEST_URI /home/$remote_user$uri
```

**补丁本体** `/usr/bin/transcode_404_handler.sh`：
- `:47` 先 URL 解码
- `:49-55` ★**补丁 1**：路径第一段必须是 `u*`（`/home/u<name>/...`），否则 `deny_path`（403）
- `:57-62` ★**补丁 2**：`realpath -sm` 归一化后必须仍在 `/home/u<name>/` 内（堵 `../` 穿越）
- `:64` 只有过关才 `transcode_req handle_request "$LOGICAL_PATH"`（`execp` argv，不经 shell）

🟢 **旁证（很有力）**：脚本 `:66 MAX_WAIT=10`、`:138` 打印 `segment not ready after 10s` —— 与早期实测的
**固定 10.2s** 完全吻合，证明当年的请求**确实走到了这个脚本**，只是被 `:49-62` 拦下。

### 2.5 LuCI 全族**没有**命令注入（🟢 根因）

所有 LuCI 路由最终都经：
```lua
-- /usr/lib/lua/5.1/luci/function/NasCommon.lua:171-213
Nixio.execp(executable, unpack({...}))   -- ★ execvp，argv 数组，不经 shell
```
⇒ 参数里的 `;`、`|`、`$()`、反引号、换行**全都只是普通字符**。
**⚠️ 因此"29/32 条写路由仅凭客户端证书可用"这一事实，不能转化成命令执行。**

**接口面审计结果**：
- 只读路由 33 条：🟢 31 可用 ／ 🟡 2 需会话（`system/get_ups_status`、`plugincenter/app/all`）
- 写路由 32 条：🟢 29 条仅凭客户端证书即通过鉴权 ／ 🟡 3 条 403（`plugincenter/app/all`、`filemgr/add_shared`、`filemgr/cancel_shared`）

### 2.6 插件机制必须小米签名（🟢）

| 事实 | 证据 |
| --- | --- |
| `plugin_control` 是 `cert_auth` + **`setuser=false`（root 执行）** | `module/plugincenter.lua:114-126` |
| 但线上调用**不带 `-f`**（本地安装参数被注释） | `NasPluginCenter.lua:213-214` 保留着 `-- waitExec(…, "install", "-f", "/etc/plugin/"..plugin..".mpk")` |
| 包只能来自小米服务器 | `/etc/config/plugin` → `server_list: https://device.eco.mi.com/ota-server/version/plugin/get` |
| 每个 mpk 都验签 | `openssl dgst -sha256 -verify /usr/share/plugin/plugin.pem -signature <sign> <tgz>` |
| `plugin.sh verify` 的 `abstract` **不是准入关口** | `plugin.sh:115-160`，只是自洽 sha256（`INFO.abstract`）|
| system 插件来源目录 | `plugin.boot:35,38` → `plugincenter install -d /etc/plugin --system`（`/etc` 只读 ⇒ 无法投毒）|
| 第三方插件 boot | `syshotplug pool/40.plugin:6` → `plugincenter boot --third --pool "$POOLID"` |

**池内插件目录：可写但注册不了**（🟢 已实测，这是最后一次尝试的结论）
- ✅ `/pool0/plugin/<用户>/plugin/{pluginsrc,plugintmp}/<name>/` **四级全部可 MKCOL**，`INFO`/`scripts/control` 可 PUT 并可回读
  （⚠️ 坑：WebDAV 服务端**不接受连发 MKCOL**，会 409；必须逐级 + 间隔 ~0.8s）
- ❌ `plugin_control {"control":"enable"}` 对**不存在的名字也返回 `code:0 成功`** ⇒ 它是"改既有条目"的空操作，**不写注册表**
- ❌ `plugin_control {"control":"install"}` / `uninstall` 对未注册插件返回 `code:2101`
- ❌ `plugincenter/plugin_list` 中**始终没有**我们的条目
- **根因**：注册表 `/data/plugin/<用户>.list` 是服务端下发 + root 侧写；`/data` 我们不可写

**对照实验（决定性）**：
| 调用 | 已注册的 `mediacenter` | 未注册的 `dshprobe`/`zzznope` |
| --- | --- | --- |
| `info` | `code:0` + 完整真实数据（`install/enable/online=true`、icon、frontend url）| `code:5000 "Failed to parse JSON: nil"` |
| `enable` | `code:0 成功` | **`code:0 成功`**（同一个响应）|

### 2.7 其它已证伪的路径（清单）

| 路径 | 结论 | 证据 |
| --- | --- | --- |
| `samba.global.name` 换行 → `smb.conf preexec` | ❌ 无写入口 | 全量 grep：**没有任何 lua 写 `samba.global.*` 或 `@samba`**；`uci:set(…,"name",…)` 零命中；唯一 samba 写入是 `NasSamba.lua:601-609` 的 `sambauser/sambashare.status` |
| `taskcenter` 计划任务 | ❌ | 客户端「任务中心」只是上传/下载进度（用户 UI 实测）|
| 客户端本地/离线安装插件 | ❌ | 客户端无此入口（用户 UI 实测）|
| `/etc/hotplug.d/**` + `hotplug-call` | ❌ 不存在 | 无该目录、无 `hotplug-call`；固件用自研 `syshotplug`（**只跑 +x**，`/usr/sbin/syshotplug:30,48`）|
| `/etc/uci-defaults/*` 被 `sh` 执行 | ❌ 无消费点 | 目录存在（仅 `30_luci-theme-bootstrap`）但**全盘 grep `uci-defaults` 零命中**；`/etc/init.d/` **空**；**无 `/etc/rc.d`** |
| `/lib/preinit/*` | ❌ 不存在 | `ls /lib/preinit` 无此目录 |
| `run-parts` 执行 `/etc/cron.{daily,hourly,…}` | ❌ 文件名不匹配 | `crontab_init.sh:3-9` 确实注册了 `run-parts`，但 busybox `run-parts` **拒绝含 `.` 的名字**，且我们的写入必带 `.subcfg` |
| nginx `include …/sites-enabled/*`（不带后缀）| ❌ 不可用 | 需要 `/etc` 可写，且内容是 nginx 指令不是代码 |
| systemd glob（drop-in / `EnvironmentFile` / tmpfiles）| ❌ | 目标文件名固定，且 `/etc` 只读 |
| `/usr/lib/minas/*`、`sysota`、`restore`、`plugin.sh` 里的目录遍历执行 | ❌ 零命中 | grep `for …*` / `run-parts` / `sh <dir>` 全空 |
| 内置插件带"自定义命令/脚本/文件"能力 | ❌ 没有 | 商店只有 5 个插件（见下），`central`/`ipc` 虽带 `ext.admin=true` 但能力固定 |

### 2.8 厂商插件商店目录（🟢 只读枚举）

| 插件 | id | 版本 | system | type | port | admin | 状态 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `central` 中枢功能 | 14 | 1.0.0.68 | ✅ Xiaomi | standard | — | `true` | 运行中 |
| `ipc` 监控存储 | 6 | 1.0.0.106 | ✅ Xiaomi | **root** | 9100 | `true` | 运行中 |
| `mediacenter` 影视 | 2 | 1.0.0.124 | ✅ Xiaomi | standard | — | — | 运行中 |
| `baidupan` 百度网盘 | 7 | 2.16.27.142 | ❌ minas | standard | 9000 | — | 未装 |
| `xunlei` 迅雷 | 8 | 3.23.5.67 | ❌ minas | standard | 9200 | — | 未装 |

### 2.9 10 个社区插件**全部必须 root**（🟢 已确证）

**判据（对每个都成立）**：进客户端列表 ⇒ 必须写 `/data/plugin/<用户>.list`（只读）；
开机自启 ⇒ 必须写 root crontab + `/data/plugin/community-plugins-boot.sh`；
写 unit ⇒ `/data/etc/upper/systemd/system`、`/etc/systemd/system/docker.service.d`（只读）；
发端口 ⇒ `docker`/`iptables`（root）；`sshcontrol`/`disksleep` 的**功能本身**就是 root 专属。

**三环证据（全部来自仓库自己的部署脚本）**：
| 环 | 证据 |
| --- | --- |
| 注册 | `register_plugin.py:33,82,84` → 写 `/data/plugin/<uid>.list`；`restore-plugins.py:280,281` 原子替换 + `chmod` |
| 保活 | `install-boot-hook.sh:16,17,27,31` → `mkdir -p /data/plugin`、`cp … /data/plugin/community-plugins-boot.sh`、`crontab -` 加 `* * * * *` 条目；`community-plugins-boot.sh:20` 自述"由 **root crontab** 每分钟触发" |
| 拉起 | `community-plugins-boot.sh` → `UNIT_DIR=/data/etc/upper/systemd/system`、`DOCKER_DROPIN=/etc/systemd/system/docker.service.d/override.conf`、`systemctl daemon-reload/restart/start`、`iptables -t nat -A POSTROUTING … MASQUERADE`、docker unix socket |

`xiaomi-community-store.service`（仓库里那个 unit）**没有 `User=`** ⇒ systemd 默认 **root**。

**插件端口（取自 `apps.json`，🟢）**：

| 插件 | 端口 | 插件 | 端口 |
| --- | --- | --- | --- |
| devicemanager 设备管家 | 18080 | emby | 18150 |
| nasconsole 控制台 | 18100 | dpanel 容器管理 | 18160 |
| rtrcenter Unifi | 18101 | disksleep 硬盘休眠 | 18160 ⚠️ **与 dpanel 冲突** |
| 115sync | 18115 | jellyfin | 18170 |
| aliyundrivesync | 18117 | fwclient 内网穿透 | 18180 |
| communitystore 插件市场 | 18119 | netneighbor 网络邻居 | 18190 |
| webdav 文件柜 | 18120 | transmission | 18140（+9091/51413）|
| qbittorrent | 18122 | sshcontrol SSH 开关 | 18130 |

> ⚠️ `disksleep` 与 `dpanel` 都写 18160 —— 恢复后要核对，避免互相抢端口。

### 2.10 后续补充证伪清单（🟢 均为实测/源码确证，避免重复劳动）

> 这些是"看起来像新入口、实际走不通"的路径，逐条钉死。

| # | 假设 | 结论 | 证据 |
| --- | --- | --- | --- |
| 1 | "拿到客户端会话令牌就能解锁那几条 403 写路由" | ❌ **不能** | 令牌**成功签出**（见 §5.5，32 位合法令牌），但用 `Authorization: <token>` 复测 `plugincenter/app/all`、`filemgr/add_shared`、`filemgr/cancel_shared` **仍全部 403**；对照组 `plugincenter/plugin_list`、`filemgr/get_shared_list` 证书直连即 200 ⇒ 令牌本身有效，**403 与鉴权无关** |
| 2 | "`filemgr/add_shared` / `cancel_shared` / `plugincenter/app/all` 存在，只是我们权限不够" | ❌ **这些路由在 1.0.9.462 里根本没注册** | `grep -rn '"add_shared"' /usr/lib/lua/` → 无；`"cancel_shared"` → 无；`plugincenter` 的 `{"app","all"}` → 无；nginx `conf.d/luci/*.conf` 里也**没有任何**指向它们的二进制 location。而 `dispatcher.lua:971` 与 `:975` **对"未注册"与"未授权"都返回 `http.status(403,"Forbidden")`** ⇒ **403 的含义是"没有这条路"，不是"权限不足"**。固件里真正存在的是 `filemgr/add_shared_user`、`cancel_shared_user`、`edit_shared_directory`、`edit_shared_user`、`get_shared_list`、`get_shared_user`、`quit_shared` 这一族 |
| 3 | "换个 WebDAV 账号（`nobody`/guest/其它用户）就能看到池子以外的路径，例如 `/etc`、`/data`" | ❌ **架构上不可能** | `/etc/webdav/nginx/conf.d/webdav/webdav.conf` 原文：`:36` `if ($uri !~ ^/((pool\|usb)[^/]*)(/\|$)) { return 451; }`（**URL 路径只允许 `/pool*` 与 `/usb*`**）；`:42` `if (!-d /home/$remote_user/$1) { return 451; }`；`:46` **`root /home/$remote_user/;`**（**根目录被钉死在用户家目录**）。⇒ 与凭据、账号、`alias_root` 全部无关 —— **`alias_root` 不是每账号可配项，每个账号的根都是它自己的家目录**，不存在"根 = `/`"的账号。我们实测 `/proc/cmdline`、`/proc/self/mountinfo`、`/etc/minas.version` 一律 **451**，正是这条规则的效果 |
| 4 | "scruel 的挂载脚本（`minas-mount-webdav_wsl.sh`）用了不同的账号/URL，可能提供新的路径面" | ❌ **无新路径面** | 已逐行读完该脚本（194 行）：凭据来自 **`get_pool_info` 返回的 `username`/`password`**（`:142-157`）—— **与我们完全一致**；挂载 URL 是 **`https://$NAS_IP:5000/pool0/data`**（`:176`）—— **仍是池内**；用 `rclone mount` + `--client-cert/--client-key`（`:181-187`）⇒ 结论与 #3 一致，**不需要额外安装 davfs2 复核** |
| 5 | "镜像里 `root` 是空哈希 + dropbear 带 `-B` + PAM `nullok` ⇒ 空密码可登录" | ⛔ **已实测排除，不要再列为待验证项** | 实机对 `192.168.1.8:22` 尝试 `root` + 空密码，服务端返回 **`Bad authentication type; allowed types: ['publickey']`** ⇒ **运行时只提供 `publickey`**。原因：运行时 `/etc` 是 overlay（`/data/etc/upper`），其内容与固件镜像不一致（详见 §2.3 对应行）|

---

## 3. 固件分析资产

### 3.1 1.0.9.462 官方固件包

| 项 | 值 |
| --- | --- |
| 下载 URL | `https://cdn.eco.mi.com/eco-common-ota/version/firmware/full/system_RP05_release_1.0.9.462_dananshan-ota8-rc.server.bin` |
| 大小 | **342 979 584** 字节（327.1 MiB） |
| **sha256** | **`a59a49695d4e89036393cfb7358a907fa5cf0f1cc3e351fc1da7e1c62dc017e3`** |
| 与设备声明比对 | 🟢 **完全一致**（设备 `get_upgrade_history` 返回同值）|
| 魔数 | `4d 49 20 20 4e 41 53 20` = `"MI  NAS "` |
| 尾部 | `twhereiswhich` + 240 字节签名块（🔴 推测为厂商完整性尾标记）|
| 落地路径 | `artifacts/scratch/firmware/system_RP05_release_1.0.9.462_dananshan-ota8-rc.server.bin` |

**URL 从哪来**（🟢 可复现）：设备只读接口
```
POST /cgi-bin/luci/system/get_upgrade_history   (mTLS 客户端证书)
  -> code:0, data.history[0].downloadUrl / checksum / fileSize
```
> ⚠️ 注意是 **`get_upgrade_history`**（`get_upgrade_check` 只返回"已是最新"）。

### 3.2 容器结构（🟢 已解析并逐分区校验）

```
"MI  NAS " + 4B + 4B 头长度(=728) + base64(JSON)
```
JSON：
```json
{"model":"RP05","version":"1.0.9.462","channel":"release","prefix":"system",
 "partition":[
   {"partition":"kernel","file":"dananshan-image-nas-*-mmcblk0.direct.p2-trim",
    "size":25165824,"sha256":"cd703f9c…8b52e","type":"fat","dual":true,"preupgrade":""},
   {"partition":"system","file":"dananshan-image-nas-*-mmcblk0.direct.p9",
    "size":317812736,"sha256":"a61af801…7c5a5","type":"erofs","dual":true,"preupgrade":""}]}
```

| 分区 | 头部声明大小 | 实测负载偏移 | 负载 sha256 | 校验 |
| --- | --- | --- | --- | --- |
| kernel | 25 165 824 | `0x2F0`（752）| `cd703f9c208cceaff76ed583e3691de734aabe8243799359fc42428807d8b52e` | 🟢 一致 |
| system | 317 812 736 | 25 166 584 | `a61af801de6921d9f0c5128b82c6da41531f2126920211764316a1e37bb7c5a5` | 🟢 一致 |

> ⚠️ 两个坑：① `MI  NAS` 头第 8..11 字节**不是**文件长度；② 每个分区负载前还有 **8 字节前缀**，
> 负载（从 +8 开始、长度 = 头部 size）才与 sha256 对得上。

**包类型**：🟢 **整包（full）**，不是差分包（URL 在 `…/firmware/full/`；含两个完整分区镜像；`preupgrade` 为空）。

### 3.3 解包方法（🟢 已验证可复现）

```powershell
# WSL 里以 root 身份（wsl -u root 不需要 sudo 密码）
wsl -d Ubuntu -u root -- bash -c "rm -rf /root/rootfs && mkdir -p /root/rootfs && \
  fsck.erofs --extract=/root/rootfs \
  '/mnt/d/软件开发/xiaomi-nas-plugin-market/artifacts/scratch/firmware/dananshan-image-nas-1.0.9.462-mmcblk0.direct.p9.erofs'"
```
- 结果：**`/root/rootfs/`，1.1 GB，约 33 秒**（erofs-utils 1.7.1，`fsck.erofs -V` 查版本，`--version` 不认）
- ⚠️ **不要解到 `/mnt/d/...`**：Windows 盘（9p）**不支持符号链接**，会报
  `erofs: failed to open: ... (File exists)` 并失败
- 切分区脚本：`artifacts/scratch/split-firmware.py`；容器头解析：`artifacts/scratch/parse-firmware-header.py`
- 零依赖交叉验证：`artifacts/scratch/erofs.py`（纯 Python EROFS 解析器，`meta_base=0x808`、32B inode 槽、
  `iloc = nid*32`；inode 起始 = `iloc + inode_isize + xattr_isize`）

**EROFS 参数（🟢）**：`magic 0xe0f5e1e2`、version 1.0、`blkszbits=12`（4 KiB）、
`feature_incompat=0x3`（ZLZ4 + LZ4_0PADDING）、`sb_extslots=0`、`blocks=77591`、`root_nid=37`。
元数据段（inode 表、目录项）**未压缩、可直接 grep** —— 这就是为什么很多结论无需完整解包即可得到。

### 3.4 A/B 双槽机制（🟢 已确证，★ 决定了"能否回到 1.0.8.425"）

> 这是本文最关键的新发现之一：**升级只是切到了另一套分区，旧固件很可能仍在设备里。**

**分区布局**（🟢 权威依据是 `/usr/sbin/bootinfo` 全文）：

```sh
dev_system_cmdline="$(awk -F'root=' '{print $2}' /proc/cmdline | cut -d 'p' -f 2)"
minas_sys_index="$(fw_printenv -n minas_sys_index)"     # ★ 槽位号（U-Boot env）
dev_kernel=2 ; dev_system_env=9                          # slot 0
[ "1" = "$minas_sys_index" ] && { dev_kernel=3 ; dev_system_env=10 ; }   # slot 1
# 一致性校验：cmdline 与 env 不一致则直接报错退出
[ "$dev_system_cmdline" != "$dev_system_env" ] && { echo "System Error! cmdline and env data not same!"; exit 1; }
```

| 槽 | `minas_sys_index` | kernel | system | cmdline |
| --- | --- | --- | --- | --- |
| slot 0 | `0` | `/dev/mmcblk0p2` | **`/dev/mmcblk0p9`** | `root=…p9` |
| slot 1 | `1` | `/dev/mmcblk0p3` | **`/dev/mmcblk0p10`** | `root=…p10` |

另有 eMMC **硬件 boot 分区** `/dev/mmcblk0boot0` / `boot1`（U-Boot 本体所在；
`sysota` 里有专门分支 `if partition_name == "boot0" or partition_name == "boot1"` 🟢），
且设备带 `libubootenv.so.0.3.5` / `libubootenv-bin` ⇒ **U-Boot env 可被 OTA 更新** 🟢。

**升级时如何切槽**（🟢 `sysota._upgrade()` 原文）：
```python
sys_index = int(fw_printenv -n minas_sys_index)
sys_index = 1 - sys_index                     # ★ 目标槽 = 另一槽（A/B 交替）
...
if partition.get('dual') == True:
    partition_name = partition['partition'] + str(sys_index)   # "kernel0/1"、"system0/1"
    is_need_switch_sys = True
...
with open(dev_name, 'wb') as dev:  ...        # 把新固件写进**非活动槽**（不擦源槽）
...
if is_need_switch_sys and not args.keep:
    runcmd("fw_setenv minas_sys_ota true")    # ★ 置位，下一次启动由 bootloader 真正切槽
```
⇒ 🟢 **源槽（升级前的 1.0.8.425）不会被擦除** —— 叠加 `/boot` 里两套内核都在、
以及 `boot_check.sh` 仍在从 `/upgrade/minas.bin` 做备份，**旧槽极可能完整保留**。

**槽位状态标志全在 U-Boot env**（🟢 已枚举）：

| 变量 | 含义 | 谁写 |
| --- | --- | --- |
| `minas_sys_index` | **当前槽号 0/1** | bootloader 读/写；`bootinfo` 只读 |
| `minas_sys_ota` | **切槽请求置位** | `sysota._upgrade():1051` 置 `true`；`systest:230` 置 `false` |
| `minas_sys0_failed` / `minas_sys1_failed` | 各槽失败计数 | `boot_check.sh:16,18` **只清零**；`bootinfo` 只显示 |
| `minas_sys_success` | 启动成功标记 | `boot_check.sh:48` 无条件置 `true` |
| `minas_ota_state` | OTA 状态（`boot`/`backup`）| `boot_check.sh:21-44` |
| `minas_sys_restore` / `minas_restore_pre` / `minas_restore_boot` | 恢复模式 | `systest` 等 |

🟢 **全盘 grep 的硬结论**：`minas_sys0_failed` / `minas_sys1_failed` 在 **Linux 侧只有"清零"和"显示"**，
**没有任何读取它来决定切槽的逻辑** ⇒ 自动回退（如果有）只能发生在 **U-Boot 侧**，
而 **U-Boot 不在 system 镜像里** ⇒ 其阈值与行为 🔴 **无法从固件侧验证**。
同理，**我们没有任何用户态写入面能碰到这些 env 变量**（✗ `/tmp`、✗ `/var/plugin`、✗ `/home/<用户>/**`）——
**这就是"软件侧无法切槽"的根本原因**。

### 3.5 防回滚机制（🟢 已确证存在）

`sysota:35` 错误码表里有 **`2011: "forbid to downgrade"`**，触发点 `:937-949`：
```python
preversion_code  = cal_version_code(self.version)               # 当前版本
postversion_code = cal_version_code(self.metadata['version'])   # 包内版本
if postversion_code < preversion_code:
    ret, downgrade_en = runcmd("mitee_tool rpmb get downgrade_en")   # ★ RPMB 硬件标志
    if ... downgrade_en.strip() == "true":
        self._info("permit to downgrade because of downgrade_en is true")
    else:
        self.code = 2011      # ★ 拒绝降级
```
`cal_version_code()`（`:89-99`）只取数字段：`1.0.9.462`→`0x10009`，`1.0.8.425`→`0x10008` ⇒ **判定为降级** 🔴

🟢 **注意适用范围**：这个检查拦的是 **`sysota` 的安装流程（用 OTA 装旧版）**；
**切槽不走 `sysota`**（直接把引导指向另一槽已有的旧系统）⇒ **切槽本身不触发它**。
但切过去之后，**旧系统自己的 `sysinit.sh`** 会跑 `mount_verify_ext4_partition` 校验
`/firmware`、`/data/central`、`/data/docker`（这些内容已被 1.0.9.462 升级过）⇒ 🟡 可能挂载失败（影响功能，未必无法启动）。

🟢 另有一条 "跳过验签" 的口子（`sysinit.sh:145-178`）：
```sh
build="$(jq -r '.build' /usr/share/minas.version)"
if [ "$build" = "local" ]; then log "local build, skip verify $dst partition"; return 0; fi
```
⚠️ 但 `/usr/share/minas.version` 在**只读 erofs** 上，且当前值是 `"build": "server"`（🟢 已 cat 原文）
⇒ **我们改不了 ⇒ 不可用** 🔴

### 3.6 CDN 上现存的 1.0.9.x 全量固件（🟢 枚举结果）

| 版本 | tag | ETag |
| --- | --- | --- |
| 1.0.9.450 | `dananshan` | `1377a247d785c06adbbaa0c4bf3a0a2d` |
| 1.0.9.451 | `dananshan` | `f14ff3934680bea5dd69816472f93e4c` |
| 1.0.9.452 | `dananshan-ota8-rc` | `12cd52275a0a1e0ca2e38faf0aec0609` |
| 1.0.9.453 | `dananshan-ota8-rc` | `5c6904025aa63a94bac5405d5233d33d` |
| 1.0.9.454 | `dananshan-ota8-rc` | `2c8e5ccac114ea57bbfb5ffb17c05370` |
| 1.0.9.455 | `dananshan-ota8-rc` | `be09758eb6170918441ddb166fac8ddd` |
| 1.0.9.456 | `dananshan-ota8-rc` | `9c56ede9439110cb0d86766d378c9e7e` |
| 1.0.9.457 | `dananshan-ota8-rc` | `359a3ab132e282a8e4242de3e3f760f7` |
| 1.0.9.458 | `dananshan-ota8-rc` | `f2d4af7e5aa48c15f1a5c80bcdf0e191` |
| 1.0.9.459 | `dananshan-ota8-rc` | `ab99cde9a7b1e0eb14bf49fe14c15bf6` |
| 1.0.9.460 | `dananshan-ota8-rc` | `378077c0e73ddff2c429d17cc3e1c536` |
| 1.0.9.461 | `dananshan-ota8-rc` | `33c21b31c4b0fee19a0d97d2d1e73d10` |
| **1.0.9.462** | `dananshan-ota8-rc` | `d923e6e18e9dbaec026403570089b781` |
| 1.0.9.463 | `dananshan-ota8-rc` | `c378e9e11530fed23edd22202c448d09` |

命名模板：`system_RP05_release_<version>_<tag>.server.bin`，
URL 前缀 `https://cdn.eco.mi.com/eco-common-ota/version/firmware/full/`。
🟢 ETag 各不相同 ⇒ 是**各自独立的真实对象**（不是通配路由）。
🟢 **tag 随 minor 版本变化**（1.0.9.450/451 用 `dananshan`，452+ 用 `dananshan-ota8-rc`）。

⚠️ **1.0.8.425 拿不到**：枚举 `1.0.8.0…1.0.8.999`（1000 次 HEAD，tag `dananshan-ota8-rc`）**0 命中**；
针对 `1.0.8.425` 试了 35 种命名形态（含用户提供的 `1.0.8.425.F24C26D24-REL`）**全 404**；
`incremental/diff/delta/patch` 兄弟目录全 404。⇒ **CDN 上已被清理**。

---

## 4. 三条可行退路的评估

### 退路 1：官方支持（风险最低）
- **做法**：以"插件机制异常 / 第三方插件全部消失 / 需要 SSH 做数据恢复"为由提交工单（模板见 `docs/xiaomi-support-ticket.md`）
- **诉求**：① 回退到 1.0.8.425 的方法（或降级包）；② 或协助恢复第三方插件与 SSH
- **代价**：无风险；成功率未知 🟡
- **注意**：工单里**不要**提任何注入/root 相关细节

### 退路 2：物理 / 串口（风险最高）
- **做法**：拆机接 UART
- ⚠️⚠️ **高危警告**：`boot_check.sh:114-138` 的 `_env_check` 会在 `uart_en`/`bootdelay` 的
  env 与 RPMB 不一致时执行 **`reboot -f`**。
  ⇒ **只能用 U-Boot 改这两个变量，绝不要通过软件改**（改了会立即强制重启）
- **代价**：拆机 + 🔴 变砖风险；不建议作为首选

### 退路 3：再收一台未升级（1.0.8.425）机器做差分（最有技术价值）
- **做法**：用当时可用的注入点拿 root → 导出 rootfs → 与 1.0.9.462 的 system 分区做二分差分
- **价值**：能精确看出厂商补了哪些地方，反过来可能找到"同族未修补的兄弟"或新的绕过
- **代价**：需要一台机器；🔴 且 1.0.8.425 的包已从 CDN 下线，只能靠物理机

#### ⚠️ 本机**不能**用"从 `/upgrade` 本地重装"来替代退路 3（🟢 实测）

`boot_check.sh:22-44` 里有一条"本地重装/备份"分支：
```sh
minas_ota_state="$(fw_printenv -n minas_ota_state)"
if [ "boot" = "$minas_ota_state" ] || [ "backup" = "$minas_ota_state" ]; then
    if [ -f "/upgrade/minas.bin" ]; then
        sysota local /upgrade/minas.bin -kd        # ★ 从 /upgrade 装本地包
```
理论上若能往 `/upgrade/minas.bin` 放一个包，就能**不走 CDN** 触发安装。
**但实测（存在性预言机，🟢）**：
| 路径 | 判定 |
| --- | --- |
| `/upgrade` | 📁 目录**存在**（升级分区已挂载）|
| **`/upgrade/minas.bin`** | ❌ **不存在** |
| `/upgrade` 下的其它内容 | 无（升级后已被清理）|

⇒ 🔴 **本机上没有留下任何旧固件包** ⇒ **不能靠 `/upgrade/minas.bin` 做本地重装或降级**；
且 `/upgrade` 的可写性未知（不属我们的可写面 `/tmp`、`/var/plugin`、`/home/<用户>/**`）。
⇒ **退路 3 仍然只能靠"再收一台未升级的机器"**，这一点没有变化。
（另：🟢 实测 `/data/plugin/u3943892.list` 与 `/data/plugin/community-plugins-boot.sh` **都是普通文件、都还在**，
说明升级**没有**清空 `/data/plugin` 下的插件数据 —— 这对 §6 步骤 3 的恢复是**好消息**。）

> **本次调查已把这台机器上所有软件侧路径穷尽**（§2 + §5.6），所以现在只能选上面三条之一。

---

## 5. 安全观察（与本事件相关但独立）

### 5.1 🔴 `/sysinfo` 弱鉴权 + 信息泄露

`/usr/sbin/sysinfo`（`NAS_AUTH_METHOD=8`，`/etc/nginx/80.conf/sysinfo.nginx.conf`）：
- 只校验一个 **8 位 hash**，而**盐值硬编码在脚本里**（`/usr/sbin/sysinfo:19`）：
  ```
  hash = sha256(SN + MAC + "5a4f121986a0676d75bb9bfb12ad8c16a3a88bcdb27db7b7f4f89797421f5f08")[0:8]
  ```
- **返回**：SN、MAC、MODEL、固件版本、`secboot` 状态、`rpmb` 状态、`minas_sys_mode`
⇒ 任何能访问该端口的人，只要能读出本机外包装上的 SN/MAC，即可**自行算出该 hash** 并拉取上述信息。
**建议**：把盐值改为服务端随机密钥 + 加时间戳/HMAC，而不是硬编码常量。

### 5.2 🟡 客户端证书可调的管理接口面极宽

- 只读 33 条中 **31 条**仅凭客户端证书即返回数据
- 写 32 条中 **29 条**仅凭客户端证书即**通过鉴权**（返回"参数错误"而非 403）
- 其中**明确以 root 执行**的有：`plugincenter/plugin_control`（`setuser=false`）、
  `player/transcode_switchable`（`setuser=false`）、`usermanager/set_nickname`（`setuser=false`）
- **结合 §2.2**：`NasSubtitle.lua` 的任意文件写原语，加上这个宽接口面，构成一条完整的
  "cert → 任意文件写（root 身份）→ 落在可写目录"链（虽然最终因 `/etc` 只读而未能提权，但原语本身是真实的）
⇒ **建议**：给写路由加"用户态 + 路径白名单"，尤其是所有接收路径参数的接口。

### 5.3 🟡 `NasSamba.check_parms` 不拒绝换行

`NasSamba.lua:242` 只检查"连续空格"（`dirs[1]:find("  +")`），**不过滤 `\n`**。
`smb_mgr.sh:230-235 / :269-274` 用 `sed` 把 `samba.global.name` 拼进 `/var/etc/smb.conf`，
而 **Samba 的 `preexec` / `root preexec` 是以 root 执行的指令**。
⇒ 🟡 目前**没有写入口**（全量 lua 里没有任何地方写 `samba.global.name`），但这是一个**潜在的高危注入面**：
一旦将来出现能设置该字段的路由，即可直接获得 root 执行。

### 5.4 🟢 `syshotplug` 会执行 `/etc/syshotplug/<event>/*` 下所有 +x 文件

`/usr/sbin/syshotplug:30,48`：`[ -x "$script" ] && "$script"`。
这是厂商自己的设计（也是仓库里已验证的持久化原语），但**需要先有 root 才能写 `/etc`**。
⇒ **它是持久化手段，不是提权手段。**

### 5.5 🟡 `plugincenter/get_access_token` 允许**裸客户端证书自助签发 32 位 root 上下文会话令牌**

**路由与鉴权**（🟢 `module/plugincenter.lua:33-49` 原文）：
```lua
path = {"plugincenter", "get_access_token"},
title = "get access token for auth",
auth_method = {
    func = {"cert_auth"},     -- ★ 只允许证书认证
    setuser = false           -- ★ 认证后保持 root 执行
}
```

**签发实现**（🟢 `NasAuth.lua:63-116`）：`getToken()` 校验 `nixio.getuid() == 0`，
用 `MIUID` + `DEV_TYPE` 标识会话，生成 `genRandomString(32, 1)` = **32 位令牌**，
落在 `/tmp/luci-token/`（tmpfs），**有效期 1800 秒**，每次校验自动续期。

**实测**（🟢 原始响应）：
```
POST /cgi-bin/luci/plugincenter/get_access_token   {}    -> HTTP 200
{"result":"成功","data":{"token":"9e5Bln7ixNku9cnpJ2Z1tlR49a721C69"},"code":0}
★ token 长度 = 32（NasAuth 要求恰好 32）
```

**问题**：`token_auth` 的设计意图是"比 `cert_auth` 更细粒度、更可撤销"的会话级凭据，
但既然**任何持有客户端证书的人都能自己签发**一个合法令牌，`token_auth` 的**保护强度就被降级到与 `cert_auth` 等同** ——
它不再构成"第二道门槛"，只是多一次往返。

**影响范围（🟡 需注意，但不是直接提权）**：本次已验证**它并不能解锁不存在的路由**（见 §2.7 与 §2.10），
所以**没有造成提权**；但这是一处**认证模型的结构性弱点**，值得厂商评估。

⇒ **建议**：
1. `get_access_token` 应要求**额外的用户交互确认**（或绑定到一次已完成的用户登录会话），而不是仅凭设备证书即可签发；
2. 或对签发做**速率/来源限制**，并让令牌**绑定具体的 API 白名单**（例如只允许插件自身的前端调用）；
3. 令牌 TTL 30 分钟且可无限续期，建议改为一次性或短 TTL + 不可续期。

> **本轮使用情况（如实记录）**：只调用过 **1 次** `get_access_token`（只读语义，**未改变设备任何状态**），
> 目的是验证"令牌能否解锁 403 路由"这一假设；结论是**不能**（原因见 §2.10）。
> 原始响应见 `artifacts/scratch/out/access-token.log`。

---

## 5.6 🏁 收尾结论：软件侧（API 面 + 非 API 面）已**逐条证伪**

> ## ⚠️ 本章已被 2026-10-19 的实操结果**部分推翻**（请先读这段）
>
> 本章当时（2026-10-10）的结论是「软件侧没有可用提权路径，仅剩三条退路」。
> **该结论对当时已知的路径是成立的，但当时漏掉了一条**：
> `smb_mgr.sh` 里 `nas_add_smb_share_dir()` 用 **heredoc 拼 uci 命令**，而 `share_name`/路径参数未经转义
> ⇒ **目录名里的换行可以注入 uci 配置**（社区在 1.0.9.462 上验证的方案走的就是这条）。
>
> **本轮（2026-10-19）已据此恢复 root，并在恢复后完成插件修复与自愈部署**（结论见文首）。
> 因此：
> - 本章的**逐条证伪清单仍有价值**（说明"哪些路确实不通"，避免重复劳动）；
> - 但**"仅剩三条退路"这一句不再成立** —— 已新增第五条路径（uci heredoc 注入）。
> - **利用细节不写入本仓库**（遵守"不落攻击步骤"的原则）；需要时参照社区对应版本的公开工具。

> **一句话结论（2026-10-10 时点）**：**软件侧已经没有可用的提权路径** —— API 面与非 API 面共 20 余条候选全部逐条走完并证伪；
> **仅剩三条退路：官方支持 / 物理串口 / 收一台未升级机做差分。**（详见 §4）
> **[2026-10-19 更新]** 上述"仅剩三条退路"已不成立：已通过 uci heredoc 注入恢复 root（见文首结论）。

### 5.6.1 完整证伪清单

#### A. API 面（LuCI 接口）

| # | 候选 | 结论 | 证据位置 |
| --- | --- | --- | --- |
| A1 | LuCI 路由命令注入 | ❌ `execp` argv，**全族无 shell** | §2.5 |
| A2 | 任意文件写原语（`NasSubtitle.lua`）| ⚠️ **原语真实**，但可达写面只有 `/tmp`、`/var/plugin`、`/home/<用户>/**`（`/etc`、`/data`、`/run` 只读）| §2.2 |
| A3 | 插件机制投毒 | ❌ 必须小米签名 + 注册表 `/data/plugin/<用户>.list`（不可写）| §2.6 |
| A4 | 池内插件目录 | ⚠️ 四级可写，但**注册不了**（`plugin_control enable` 对不存在的名字也返回成功 = 空操作）| §2.6 |
| A5 | `samba.global.name` 换行 → `preexec` | ❌ **零写入口**（全量 lua 无一处写 `samba.global.*`）| §2.7 / §5.3 |
| A6 | 会话令牌解锁 403 路由 | ❌ 令牌**能自助签发**，但那三条路由**未注册**；403 是"没有这条路" | §2.10 #1/#2 |
| A7 | 客户端证书可调面极宽 | ⚠️ 真实（29/32 写路由仅凭证书过鉴权），但**无可利用 sink** | §5.2 |
| A8 | `/sysinfo` 弱鉴权 | ⚠️ 真实（盐值硬编码），仅**信息泄露**，非提权 | §5.1 |

#### B. 非 API 面（外部字符串 → 持久化配置 → root 执行）

| # | 候选 | 结论 | 证据位置 |
| --- | --- | --- | --- |
| B1 | **root 守护进程把池内文件当配置读** | ❌ **不存在**（9 类模式全零命中；唯一目录遍历 `prepare_usb` 不执行）| `non-api-path-audit.md` 优先项 1 |
| B2 | `/etc/syshotplug/**`（87 个 handler）卷标 → 配置 | ❌ 卷标只进 `mkfs.btrfs --label`（argv，不经 shell）| 同 优先项 2 |
| B3 | 网络来源字符串（DHCP/mDNS/SSDP/DLNA/NetBIOS/NTP）| ❌ **没有一个进配置**（`udhcpc` 脚本完全不用 hostname）| 同 优先项 4 |
| B4 | `smb_mgr.sh` → `/var/etc/smb.conf` 注入 `preexec` | ❌ **sink 真实**（`echo -e` 不转义），但上游字段零写入口；非 API 调用者只传固定常量 | 同 优先项 5 |
| B5 | 「可写目录被当 `authorized_keys`」| ❌ 授权路径固定 `%s/.ssh/authorized_keys`，无任何 `-c`/`AuthorizedKeysFile` 配置项 | §2.3 / 上轮 SSH 审计 |
| B6 | **WebDAV 换账号越出池子** | ❌ 架构锁死：nginx `:36` `^/((pool\|usb)[^/]*)` → 451；`:46` `root /home/$remote_user/` | §2.10 #3 |
| B7 | scruel 挂载脚本提供新路径面 | ❌ 同一份 `get_pool_info` 凭据 + `/pool0/data` | §2.10 #4 |
| B8 | 空密码 / `-B` 登录 | ⛔ **已实测排除**（`allowed types: ['publickey']`）| §2.3 |
| B9 | **媒体元数据 → shell**（🥇 今天做）| ❌ `albumd` 与 `libnas.so` 的 `popen` **确实以 root 起 shell**，但输入被双向锁死 | 本文 §5.6.2 |
| B10 | `share_point`/`share_dir` 换行 → `smb.conf`（🥈 今天做）| ❌ **换行进不了目录名**（4 条通道全拒）| §5.6.3 |
| B11 | `/etc/hotplug.d` / `/etc/uci-defaults` / `/lib/preinit` / systemd glob | ❌ 目录/消费者不存在或不可达 | §2.7 |
| B12 | `run-parts` 执行 `/etc/cron.{daily,hourly,…}` | ❌ busybox `run-parts` 拒绝含 `.` 的文件名，而我们的写入必带 `.subcfg` | §2.7 |
| B13 | `samba.global.name` / `taskcenter` / 客户端本地安装 | ❌ 分别为零写入口 / 非计划任务 / 客户端无此入口 | §2.7 |

### 5.6.2 B9 详情：媒体元数据的 `popen` 为什么不可注入 🟢

**执行原语**（🟢 `readelf -sW`）：`mediacenterd` 只有 `execvp`（无 shell）；`findexd` 只有 `fork`；
**`albumd` 与 `libnas.so` 有 `popen`/`system`**，且 `album.service` 与 `mediacenter.service` 都是 **`User=root`**。

**`albumd` 的 `popen`**（🟢 `0xd5f8-0xd620` 逐条反汇编）：
`snprintf(buf, 128, "uci get user.%s.status" /* 0x31200 */, user+8)` → `popen(buf, "r")`。
- `%s` 是 **UCI section key**；`setNickname` 写的是**选项值 `nickname`**，不是 section key
- section key 只由 `add_user:51` 建立，`$user` = `"u" .. uid`（**云端下发纯数字**）
⇒ 🟢 **不可注入**

**`libnas.so` 的 `system`/`popen`**（6 处全审）：
- `system@0x169e4` = 硬编码 `gdbus call --system --dest com.minas.msgcenter …`
- `system@0x184f8 / 0x18604 / 0x18640 / 0x1bb2c` = 硬编码 `xqled disk_fail|disk_ok|disk_format > /dev/null 2>&1`
- `popen@0x16308`（在 `popen_exec_cmd` 内）= **先 `setreuid(0,0)`（保持 root）再 `popen(cmd,"r")`**

`popen_exec_cmd` 的 4 个调用点，模板为
`/usr/sbin/smartctl -A -j %s`、`-c -j %s`、`-c -j /dev/%s`、`lsblk -ndo SERIAL /dev/%s`，
**全部先 `nas_list_blks()`（解析 `lsblk` 的 JSON）再按名字 `strcmp` 查表**，
查不到即返回 NULL 报错 ⇒ 设备名必须等于真实设备名 ⇒ `;`、`` ` ``、`$()`、`|`、空格都不可能 ✅
LuCI 侧另有一层 `is_valid_block_device`: `^[a-zA-Z0-9_%-]+$`（Lua 中 `%-` 是**字面 `-`**，不含范围）✅

### 5.6.3 B10 详情：🥈 的确切卡点（换行进不了目录名）🟢

**注入链**（🟢 静态确证）：
```
share_dir=/home/u3943892/pool0/data/Docker/<NAME>
  → NasSamba.lua:139  dirname = path:match("([^/\\]+)$")     ← basename
  → NasSamba.lua:142  share_point = dirname(前30) .. "-" .. mid
  → smb_mgr.sh:483    uci set sambashare.<u>_<id>.name="$share_point"
  → smb_mgr.sh:323    echo -e "\n[$name]\n\tpath = \"${path}\"" >> /var/etc/smb.conf
⇒ name 含换行 ⇒ 可注入 preexec / root preexec（Samba 以 root 执行）
```
**但换行必须存在于"目录名"里，而全部写入通道都拒绝控制字符**：

| # | 通道 | 结果 | 原始响应 |
| --- | --- | --- | --- |
| 1 | WebDAV `PUT …%0Apreexec…` | ❌ | **HTTP 500** `{"errno":2,"message":"No such file or directory"}` |
| 2 | 直接送原始 `\n` 字节 | ❌ | 客户端即拒 `InvalidURL: URL can't contain control characters` |
| 3 | WebDAV `MKCOL …%0A…` | ❌ | **HTTP 409 Conflict** |
| 4 | `filemgr/create_dir` 含 `\n` | ❌ | `{"result":"无权限执行操作","code":1000}`（native `filemgrd` 经 **D-Bus** 拒绝）|
| 5 | `filemgr/move` 改名含 `\n` | ❌ | `{"result":"目的路径不存在","code":1106}`（控制组同样 1106）|
| 6 | `NasSamba.share_dir` 直传含 `\n` | ❌ | `check_parms` 有 **`fs.access(dirs[1])` 存在性校验**（`:236-240`）⇒ 路径必须真实存在，而 1–5 都造不出 |

⇒ 🔴 **此路封闭**（详见 §2.10 与本报告同级的 `docs/non-api-path-audit.md`）。

### 5.6.4 当时的"仅剩三条退路"（**已被 2026-10-19 的第五条路径取代**）

> **[2026-10-19 更新]** 下表是 2026-10-10 的判断。**已不再需要走这三条** ——
> 实际生效的是 **④ uci heredoc 注入**（见节首说明；利用细节不入库）。
> 三条退路**保留作备选**：万一哪天注入路径被新固件补掉，仍是这三条。

| 退路 | 做法 | 风险 | 详见 |
| --- | --- | --- | --- |
| **① 官方支持（推荐）** | 提交工单，用 **A/B 双槽**这条硬筹码要求：官方切槽/回退到非活动槽、或 1.0.8.425 降级包、或协助开启 `downgrade_en` | **最低** | §4 退路 1 ＋ `docs/xiaomi-support-ticket.md` |
| **② 物理 / 串口** | 拆机接 UART，在 U-Boot 阶段操作 ⚠️ **绝不能用软件改 `uart_en`/`bootdelay`**（`_env_check` 会 `reboot -f`）| **最高** | §4 退路 2 ＋ §6 步骤 7.4 |
| **③ 收一台未升级机做差分** | 用旧版可用的注入点取 root → 导出 rootfs → 与 1.0.9.462 二分差分 | 需一台机器 | §4 退路 3 |
| **⑤（实为第 4 条）uci heredoc 注入** | `nas_add_smb_share_dir` 用 heredoc 拼 uci，目录名带换行可注入 `sambashare.<名>.user_force=root` ⇒ 得到 root 身份的 SMB 共享 | 中（**必须先确认可回滚**）| ⚠️ **细节不写入本仓库** |

> ⚠️ **在拿到官方答复前**：**不要**调用 `nas_upgrade`、不要在 App 点"立即升级" —— 那会把 1.0.9.462
> 重新写进非活动槽、**永久覆盖掉那个可能还是 1.0.8.425 的槽**。**自动升级已关闭**（§8）。

---

---

## 5.7 📦 独立成果：**中枢网关（central）容器镜像已成功提取并完整分析**

> 这一项与"拿回 root"主线**无关**，是本轮探索的**独立收获**：
> 通过"给任意目录加 Samba 共享"的能力（见 §5.7.7），**从中枢网关的数据目录里完整取回了它的容器镜像并拆解**。

### 5.7.1 是怎么取到的（🟢）

1. **加共享指向 `/data/central`**（路径存在的普通目录 ⇒ 共享建成）
2. 用 SMB 列出并取回：

| 文件 | 大小 | 说明 |
| --- | --- | --- |
| **`default_image.tar.xz`** | **7 922 044 B（7.9 MB）** | 🟢 **XZ 压缩**（魔数 `fd 37 7a 58 5a 00`，CRC64）—— **已完整取回** |
| `central.version` | 173 B | `{"model":"RP05","version":"1.0.0.26","time":"2026-04-20 21:42:54","desc":"central firmware","channel":"release"}` |
| `central.sign` | 256 B | RSA-2048 签名（设备侧会验签 ⇒ 改包无效）|

3. 🟢 另发现设备上还留着**未压缩版**：`/data/other/central/default_image.tar` = **31 548 928 B**
   （升级流程 `7zz x "$package" -o/data/other/central/` 已把它解开了）
4. 产物：`artifacts/scratch/central-image/`

### 5.7.2 镜像身份（🟢）

| 项 | 值 |
| --- | --- |
| tag | **`micr.cloud.mioffice.cn/central_software_pack/central_software_pack:v0.10.8`** |
| 格式 | **OCI 归档**（`oci-layout` + `index.json` + `manifest.json` + `repositories` + `blobs/sha256/*`）|
| 架构 | **`architecture: arm` / `variant: v7`**（32 位 ARM ⇒ 宿主 aarch64 可兼容运行）|
| 构建 | `2026-01-19T21:09`，单层，`ADD ./rootfs / # buildkit` |
| 大小 | 层 tar 31 538 176 B ⇒ 解压后 **31 MB / 2472 个文件** |
| 基础 | busybox + **`busybox init`**（`inittab`）+ **`perp`** 服务管理（`perpboot -x /etc/perp`）|

### 5.7.3 它是什么（🟢 一个完整的米家中枢网关固件）

**业务服务**（`/usr/bin`，32 位 ARM ELF）：
`central_service`、`autoWebConfig_service`(460 KB)、`autoLocal_service`、**`lan_service`**、
**`bt_service`**(396 KB，蓝牙 Mesh 网关)、**`lightGroup_service`**(灯组)、**`bridge_service`**(桥接)、
`basegw_service`、`devinfo_service`、`property_service`、`automation_service`、`appMsg_service`、
`fds_backup_service`、`log_recorder`、`hotdatasync_{primary,secondary}`、`file_sync_{primary,secondary}`、
`miio_ota`、`miio_auto_ota`、`autolocal_upgrader`、`http_service`、`mqtt_server_service`、`centralCtl_service`
**第三方组件**：`mosquitto`(MQTT broker)、`nginx`、`stunnel`、`rsync`、`openssl`、`sqlite3`、`zstd`、`tcpdump`
**数据**：`/etc/appMsgDictionary.db`（1.4 MB 米家消息/属性字典）
**脚本**：`basegw_system_{dump,restore,restore_and_reboot,clear_net_data,clear_app_data_and_reboot}.sh`、
`diag_{arp,ping,route,tcpdump,info}.sh`、`upload_log.sh`、`get_autowebconfig_url.sh`、`miio_ota.sh`

⇒ 🟢 **本质是"米家网关固件被容器化"**：原本跑在网关硬件上的固件，被包成容器跑在这台 NAS 上。

### 5.7.4 它怎么连米家云（🟢 关键配置原文要点）

`/etc/mqtt_server_service/mosquitto.conf`（云端通道）：
```conf
listener 8883 0.0.0.0
allow_anonymous false
tls_version tlsv1.2
ciphers ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-ECDSA-AES128-GCM-SHA256
certfile /data/local/central_service/device.crt
keyfile  /data/local/central_service/device.key
cafile   /data/local/central_service/ca.crt
require_certificate true
plugin /usr/lib/libmosq_central_plugin.so
connection remote-broker
address localhost:8884
topic # both 0
```
`/etc/mips/mosquitto.conf`（与宿主 mips 通信）：
```conf
listener 0 /tmp/mosquitto.sock      ; allow_anonymous true
listener 8884 localhost             ; allow_anonymous true   ← 对应 platform.conf 的 ro.sys.mipsport=8884
```
🟢 **架构**：容器内起 **8883 mTLS broker**（强制客户端证书）+ **8884 本地桥**；
`8884` ↔ **宿主 `miiotcentralctld` / `miio_client`**（`platform.conf`：`ro.sys.mipsport=8884`）。
🟢 **唯一真实云端域名**（配置目录内）：**`cnbj1.fds.api.xiaomi.com`**
（与桌面客户端 `response.json` 里的 `https://cnbj1-inner-fds.api.xiaomi.net/signature/…` 同族）
🟢 **OTA 钩子**（`/etc/othook_config.json`）：`miIO.ota_central`→`miio_ota.sh`、`csp_diag_*`、
`miIO.set_autowebconfig_env`、`miIO.get_autowebconfig_url`；`online_hooks`: `miio_ota_on_online.sh`

### 5.7.5 能否脱离 NAS 独立运行（🟡 结论）

🟢 **镜像自身可跑**（`busybox init` + perp + 自带 mosquitto/nginx），
但**它被设计为依赖"宿主中介"**（`docker run` 的挂载）：

| 容器内 | 宿主 | 作用 |
| --- | --- | --- |
| `/tmp/external` | `/tmp/miot_central` | 与宿主 `miotcentralctld` 的 UDS 通信（`central_proxy.sock`）|
| `/etc/platform.conf` | `/etc/central_software_pack/platform.conf` | 设备身份/端口（`rsync 873`、`mips 8884`、`webcfg 8086`、`mqtt 8883`）|
| `/var/run/mdnsd` | 宿主同名 | mDNS 服务注册 |
| `/data` | `/data/other/central` | 运行数据 + 证书 |
| `/tmp` | `/tmp/shared_container_tmp` | 共享临时目录 |

⇒ 🟡 **理论上可在 PC 上做"仿真运行"**（宿主侧组件我们已从固件全部取得：
`miotcentralctld`、`miotcentralctl`、`central_proxy_cli`、`libcentral_proxy.so`、`platform.conf`），
但**没有米家云账号绑定与云端服务 ⇒ 业务上无法真正工作**，属研究性质。

### 5.7.6 设备 mTLS 凭据（🔴 只记路径与元数据，**不写正文/私钥**）

🟢 设备上确实存在（路径为实测确认）：
```
/data/other/central/local/central_service/device.crt   729 B
/data/other/central/local/central_service/device.key   241 B
/data/other/central/local/central_service/ca.crt      1157 B
```

| 项 | 值（仅元数据） |
| --- | --- |
| `device.crt` subject | `C=CN, O=Mijia Device, CN=mips.3943892.786f7468cfb0e7390dc4a7085a35e7814d4f471a.14` |
| `device.crt` issuer | **`C=CN, O=MIOT CENTRAL GATEWAY`** |
| 有效期 | `2026-09-19` → **`2056-09-11`**（30 年）|
| `ca.crt` subject / issuer | `O=MIOT CENTRAL GATEWAY, C=CN` / **`O=Mijia Root, C=CN`** |
| sha256 指纹 | `F9:A7:94:8A:AF:F9:0A:31:D0:57:66:B3:3F:C9:45:CC:34:F0:9B:AA:06:45:92:40:82:CB:A7:D2:1F:F9:E5:82` |
| `device.key` | PKCS#8 **EC 私钥**（P-256，241 B）|
| 用途 | 米家云 **MQTT 8883 双向 TLS 客户端凭据**（broker 要求 `require_certificate true`）|

> 🔴 **本报告与所有 `docs/` 文档均不含证书/私钥正文**，只记录**路径、subject/issuer、有效期、指纹、密钥类型**。

---

### 5.7.7 追加封闭项（本轮补充实测）

#### （A）"给任意目录加共享"可以越出用户家目录，但**只读** 🔴

🟢 **实测结论**：Samba 共享的路径**没有"必须在家目录内"的限制**（`check_parms` 只要求路径存在 + 无连续空格），
而且**可以指向系统目录**（已实测可建：`/data/central`、`/data/etc`、`/data/plugin`、**`/data/etc/upper`**、`/data`）。
⇒ **因此获得了对活体 `/etc`、`/data/plugin/*` 的 root 级只读能力**（`/data/etc/upper` 就是当前 `/etc` 的 overlay 上层）。

**但写入一律失败**（🟢 逐一实测 `NT_STATUS_ACCESS_DENIED`）：
| 目标 | 结果 |
| --- | --- |
| `/data/etc/upper/dsh_marker` | ❌ ACCESS_DENIED |
| `/data/etc/upper/dropbear/dsh_write_test` | ❌ ACCESS_DENIED |
| `/data/plugin/dsh_marker` | ❌ ACCESS_DENIED |
| `/home/u3943892/plugin/dsh_write_marker` | ❌ ACCESS_DENIED |

🟢 **根因**：`force user = $5 = user`（= **池主 `u3943892`**，**不由请求里的 `username` 决定**）
⇒ 会话以 `u3943892` 身份操作；而这些目录**是 root 所有**
⇒ `_prepare_smb_dir` 只 `chown` 了 `<上两级>/samba/<user>`，**不改共享目标属主**。
（🟢 实测确认：`username="root"` **被接口接受**、共享也能建成，但**写入仍然 ACCESS_DENIED**。）

⇒ 🔴 **无法借此写 `authorized_keys`** ⇒ **SSH 未打通**。

#### （B）"厂商插件运行器替我们以 root 执行改过的 control 脚本"—— 封闭 🔴

这条路需要两个前提，**两条实测都不成立**：
1. 🔴 **注册表里没有我们的条目**：`/data/plugin/u3943892.list`（8617 B）解析后**只剩 5 条厂商条目**
   （`mediacenter`/`central`/`ipc`/`baidupan`/`xunlei`）⇒ 我方 9 条**已被移除**
   ⇒ `plugin_control {"control":"enable"}` 是空操作（前序已实测：对不存在的名字也返回 `code:0` 但**不执行任何脚本**）
2. 🔴 **插件目录只读**：`/data/plugin/*` 与 `/home/u3943892/plugin/*` **都写不进去**（见 (A)）
   ⇒ 连"改已注册插件的控制脚本"也做不到

#### （C）⚠️ 操作坑记录（避免未来重复踩）

| 坑 | 说明 |
| --- | --- |
| **Samba 账号 id 槽位只有 1/2** | 若 `id=2` 已被占用，**后续所有 `add_samba_account` 都会返回 `2505 添加samba共享点失败`**（**假故障**，看起来像"路径不被允许"）⇒ 必须**先 `del_samba_account` 释放槽位**再建 |
| 共享路径判据 | 需 `_prepare_smb_dir` 的 `mkdir -p <上两级>/samba/<user>` + `chown nassys:<路径第3段>` 成功 |
| `nas` 侧还有一层池校验 | `nas` 二进制里有 `error: path '%s' dismatch any pool or 1st level under data_path` ⇒ 路径还需匹配"某个池"或"其 data_path 下一级目录" |

#### （D）"影视/照片/文件 API 能不能按我们指定的路径写文件" —— 封闭 🔴

> 这是本轮最后一条被验证的面：**能不能借 native 服务（以 root 运行）把公钥文件"搬"进 `/etc/dropbear/`**。
> 结论：**不能**。下面这条**对照实验**是本轮最干净的证据链。

##### D.1 静态：限制不在 Lua，而在 native

🟢 **Lua 层完全不校验路径** —— `NasFileOps.lua` 把请求里的 `path` / `dst_path`
**原样写进临时文件并直接送 D-Bus**（`packFileList2TmpFile` → `b:call('com.minas.filemgr', …, 'copy'|'move'|'mkdir'|'compress'|'decompress')`），
**没有 `..` 过滤、没有前缀白名单、没有 `realpath`**。

🟢 **限制在 native `filemgr`**（`/usr/bin/filemgr`，aarch64、stripped、133 168 B；
`/etc/systemd/system/filemgr.service` 里 **`User=root`**）。它的字符串里明确含：
- **`__realpath_chk`**、**`check_origin_path failed!`**
- **`get_path_pool_id failed for '%s'`**
- **`Warning: invalid path of '%s'`**、**`permission denied!`**
- 路径模板 **`/home/%s/pool%d/%s`**（⇒ native 按"存储池"归属校验）

🟢 而 **D-Bus 策略不是瓶颈**：`/etc/dbus-1/system.d/com.minas.filemgr.conf` 的
`<policy context="default">` 明确注释 **"Allow any user to call methods on this service"**
⇒ **限制完全来自 native 的池校验**。

##### D.2 ★ 对照实测（决定性）

| 操作 | 目标 | 实测结果 |
| --- | --- | --- |
| **`filemgr/copy` → 同池目录**（`…/pool0/data/Docker`）| **池内** | ✅ **`{"result":"成功","data":{"task_id":"24"},"code":0}`**，且**真的落地** `.v_subcfg(1)`（WebDAV 已确认）|
| `filemgr/copy` → `/data/etc/upper` | 池外 | ❌ `{"result":"无权限执行操作","code":1000}` |
| `filemgr/copy` → `/data/etc/upper/etc/dropbear` | 池外 | ❌ `{"result":"目的路径不存在","code":1106}` |
| `filemgr/copy` → `/etc/dropbear` | 池外 | ❌ `code:1000` |
| `filemgr/copy` → `/tmp` | 池外 | ❌ `code:1000` |
| `filemgr/move` → `/data/etc/upper/etc/dropbear` | 池外 | ❌ `code:1106` |
| `filemgr/move` → 同池（`dst=BASE`）| 池内 | ⚠️ `code:1115 目的路径不能在源路径下`（另一条校验）|
| `filemgr/decompress` → `/data/etc/upper/etc/dropbear` | 池外 | ❌ `code:1106` |
| `filemgr/decompress` → `/etc/dropbear`、`/tmp` | 池外 | ❌ `code:1000` |
| **`filemgr/create_dir` → `/data/etc/upper/etc`（并显式传 `"sysmod": true`）** | 池外 | ❌ `code:1000` ⇒ 🟢 **`sysmod` 参数无效** |
| `filemgr/create_dir` → `/tmp`、`/etc/dropbear` | 池外 | ❌ `code:1000` |
| `filemgr/create_dir` → 同池 | 池内 | ✅ `code:0`（并落地，已清理）|

🟢 **池外落点事后逐项复查**（存在性预言机）：
`/data/etc/upper/.v_subcfg`、`/data/etc/upper/etc/dropbear/authorized_keys`、
`/etc/dropbear/authorized_keys` **全部为 `1105 不存在`** ⇒ **池外确实一个字节都没写进去**。

⇒ 🔴 **结论：文件位置类 API（`copy`/`move`/`create_dir`/`compress`/`decompress`/回收站系列）
一律限定在存储池内，写不到 `/etc`（含 `/data/etc/upper`）。**

##### D.3 影视 / 照片侧：**没有写路径类接口**

🟢 `mediacenter` 模块**全部 29 条路由**逐条核对，只有三类：
- **读**：`media_list`、`media_search`、`media_details`、`media_online`、`media_recently_added`、`media_recently_watched`、`media_recommend`、`file_list`、`file_path_list`、`collection_get_info`
- **状态标记**：`media_seen`、`file_seen`、`media_favourite`、`file_favourite`、`media_update_last_seen`、`file_update_last_seen`、`file_update_episode`、`media_rematch`、`file_rematch`、`media_clear_watched`
- **删除 / 媒体库管理**：`media_delete`、`file_delete`、`lib_create`、`lib_edit`、`lib_delete`、`lib_scan`、`lib_del_share`、`lib_exit_share`

🟢 **没有** `dst_path` / `output_path` / `out_dir` / `target_path` / `save_as` / `export` / 转码输出路径
这类**指定目标路径的写接口**（在影视侧 grep 以上关键词 **0 命中**）。

🟡 相册/照片**没有独立 Lua module**（`module/` 下无 `album.lua`/`photo.lua`）—— 相册业务由 native **`albumd`** 承担，
其 `popen` 调用**前序已封闭**（服务端生成的 `"u"..uid` section key 锁死）。

🟡 唯一"按调用方指定目录写文件"的 Lua 实现是 `NasSubtitle.lua` 的 `generateUniqueFilename(target_dir, filename)`；
**前序已实测**：写 `/tmp` ✅、写 `/var/plugin` ✅、**写 `/etc`、`/data` ❌ 失败**。

##### D.4 ⚠️ 重要操作坑（值得记住，否则会重犯）

**`copy` / `move` 的 `src_path` 必须是 `[{file, policy}]` 形式的"对象数组"，不是字符串数组。**

- `zip`（`compress`）的 `src_path` 要**平铺字符串数组**：`["/path/a"]`
- `unzip`（`decompress`）的 `src_path` 要**单个字符串**：`"/path/a.zip"`
- 而 `copy` / `move` 的 `src_path` 要 **`[{"file": "/path/a", "policy": "rename"}]`**

🟢 若给 `copy`/`move` 传**字符串数组**，Lua 会在 `splitRenameReplaceJson` 里报错
（`bad argument #1 to 'ipairs'` / `bad argument #2 to 'format'`），接口返回 **`5000 系统内部错误`**，
**表面看非常像"路径被限制了"** —— 极易误判（本次就一度被它误导）。
⇒ **以后调这几个接口，先按上表核对参数形状，再下结论。**

##### D.5 结论

⇒ 🔴 **"影视/照片/文件 API 面"已封闭**（与 §5.6 的结论一致，**未改变**）。
软件侧五个面 —— **API 面 / 非 API 面 / 共享面 / 插件面 / 文件位置面** —— **全部封闭**。

> 🟢 **纪律说明**：本节所有实测**只在我们自己的池目录内创建了测试文件**（`dsh_baseline_dir/`、
> `dsh_probe.zip`、`dsh_probe/`、`.v_subcfg(1)`、`dsh_dir_probe/`），**事后已逐一删除并经
> WebDAV 与 filemgr 双通道回读确认**；池外目标**一个都没有创建成功**；
> **未覆盖任何已存在文件、未改任何现有配置、未重启、未动用户数据。**

---

## 5.8 🛡️ 兜底保险：池文件触发的固件回退（无需 SSH）

> **为什么需要这一层**：万一将来某次 OTA 把 SSH 与插件自愈**同时**打掉，那时手上一分钱远程通道都没有。
> 但"**往池里扔一个文件**"这件事不需要 shell —— App 的文件管理、WebDAV、SMB 都能做。
> 所以留两个**写死的触发文件名**，由 root 侧的钩子去发现并执行。

### 触发文件名与含义（硬编码，脚本**不接受任何参数**）

| 池里的文件名 | 含义 | 会发生什么 |
| --- | --- | --- |
| `.minas-rollback-test` | **干跑** | 只在日志里写一行 `would stage …`，**什么都不改** |
| `.minas-rollback` | **真触发** | ① 校验包 sha256 → ② 置 `downgrade_en` → ③ `sysota local <包>` **暂存**；**不自动重启** |
| `.minas-reboot` | **重启** | 只有在"已暂存"状态下才 `reboot`；否则只记一行 `IGNORE` |

`<池>` = `/home/<用户>/pool0/data`（当前用户 `u3943892`）。日志在 `/data/plugin/safety/rollback.log`。

### 组件

| 路径 | 作用 |
| --- | --- |
| `/data/plugin/safety/rollback.sh` | 全部逻辑（0755，root，只认三个固定文件名，自己加锁、自己幂等） |
| `/etc/syshotplug/pool/99.rollback` | 每次**池挂载**（开机必经）转调上面的脚本 |
| `/data/plugin/community-plugins-boot.sh` | 每分钟的 re-arm 也会 `rollback_watch`（双通道） |
| `/data/backup/firmware/<包>` + `README.txt` | stash 的官方整机包 + 校验和 |

### 怎么测干跑（**安全**）

```bash
# 1) 在池里扔一个 dry-run 触发文件（App 文件管理 / WebDAV / SMB 都行）
#    路径：/pool0/data/.minas-rollback-test   内容随意
# 2) 等 ≤2 分钟，看日志
tail -5 /data/plugin/safety/rollback.log
#    应看到：DRY-RUN: would stage … (… bytes); no changes made
#            DRY-RUN: consumed …/.minas-rollback-test
# 3) 确认什么都没变
jq -r .version /etc/minas.version
fw_printenv -n minas_sys_ota      # 必须仍是 false
```

### 真触发后的流程（**分两步，故意不自动重启**）

1. 放 `.minas-rollback` → 脚本校验包 → 置 `downgrade_en=true` → `sysota local <包>` **只暂存** → 写 `.staged` 标记；
2. 确认日志里出现 `STAGED: … NOT rebooting` 后，再放 `.minas-reboot` → 才真正重启并进入更新。

**故意拆成两步**：暂存和重启之间留出人工复核的机会，避免"文件一扔就自动重启"。

### 风险与前提（**如实记录**）

| 项 | 说明 |
| --- | --- |
| 包的性质 | 是**整机系统更新包**（`system_RP05_release_1.0.9.462…server.bin`，342 979 584 B，sha256 `a59a4969…017e3`） |
| `/data` 与池 | **不动**（系统包只写系统分区） |
| 前提 | 需要 `downgrade_en` 可用（RPMB 里是 `true` 才允许降级/重装到同版本或更低） |
| 会不会丢数据 | 整机更新不清 `/data`；但**任何固件操作都有风险**，真触发前应先备份重要数据 |
| 失败会怎样 | 脚本每步都写日志并 `ABORT`/`FAIL`；sha256 不符直接中止，不会带着坏包去刷 |
| 触发文件残留 | 脚本消费后会删除，避免每分钟重复执行 |

> 🔒 **本节不包含任何提权/利用细节**：只描述"用户自己的设备上，用一个文件触发官方 OTA 命令"这一运维动作。

---

## 5.9 🔑 SSH「认证成功但命令无输出」的根因与修复（2026-10-19）

> **现象**：公钥/密码认证都成功（`exit=0`、能看到登录 Banner），但 `ssh root@nas 'id'` **什么都不输出**，
> 连 `id > /tmp/x` 这种副作用都不产生 —— 说明**命令压根没被执行**。
> 曾误判为 "dropbear exec bug"。

### 真正的根因（🟢 已实证）

| 证据 | 结论 |
| --- | --- |
| `strings /usr/sbin/dropbearmulti \| grep mi-shell` → **`/usr/sbin/mi-shell`** | 厂商把 child 程序**硬编码**改成了 `mi-shell` |
| `/usr/sbin/mi-shell` 用 `cJSON_Parse` + `fgets(stdin)`，含 `illegal cmd [%s]`、`run cmd: [%s]` | 它是个**JSON 协议受限壳**，只认 stdin 上的 JSON，**忽略 argv 里的 `-c 命令`** |
| `/bin/sh -c 'echo OK'` 在本机正常 | 问题不在 shell |
| 独立模式（`-F -E -p 2222`）同样无输出 | 与 `-i`/socket 激活无关，是**二进制层面**的行为 |
| `mount \| grep ' / '` → `erofs (ro)` | 根分区**只读**，不能直接改二进制 |

⇒ **不是 bug，是厂商的设计**：SSH 只给你一个受限的 JSON 壳。

### 修复（🟢 可逆、不改原文件）

```bash
# 垫片：把参数原样交给真正的 shell（落在 /data，抗 OTA）
cat > /data/plugin/safety/mi-shell-shim <<'EOS'
#!/bin/sh
exec /bin/bash "$@"
EOS
chmod 755 /data/plugin/safety/mi-shell-shim

# 运行时覆盖（内存操作，重启即消失，原文件不受影响）
mount --bind /data/plugin/safety/mi-shell-shim /usr/sbin/mi-shell

# 还原（立刻恢复厂商原行为）
umount /usr/sbin/mi-shell
```

**持久化**：`/etc/syshotplug/pool/98.ssh-restore` 在每次池挂载/开机时检查，未挂载就自动挂上，
并写日志 `mi-shell shim mounted (ssh exec enabled)`。

### 验证结果

| 检查 | 结果 |
| --- | --- |
| `ssh root@nas 'id; hostname; jq -r .version /etc/minas.version'` | ✅ `uid=0(root)` / `minasc71ab1` / `1.0.9.462.F24C26D24-REL` |
| 公钥登录 | ✅ |
| 密码登录 | ✅ |
| `umount` 后 `md5sum /usr/sbin/mi-shell` | ✅ 与原版一致（`c1544ec8…`），原文件从未被修改 |
| 钩子幂等 | ✅ 重复执行只有 1 条挂载 |

> ⚠️ **副作用**：`mi-shell` 被顶替期间，任何依赖它的厂商内部功能（若有）会拿到 bash 而不是 JSON 壳。
> 由于该路径来自 dropbear 的硬编码，**已知唯一使用者就是 SSH 会话本身**；如需完全恢复原状，
> 执行 `umount /usr/sbin/mi-shell` 即可。

---

## 6. 拿到 root 后 10 分钟恢复

> 本节是**可直接复制执行**的命令序列。所有涉及私钥的地方只写路径，**不要在聊天/日志里打印私钥**。
> 目标指纹：`SHA256:suO2arZhv2vhds8bucOMOPb85zoJuFs2EP6DvBnR1xk`

### 前置

| 变量 | 值 |
| --- | --- |
| `NAS_IP` | `192.168.1.8` |
| 私钥路径 | `F:\TEMP\dsh-nas-key\minas`（Windows）／在 NAS 侧只用**公钥** |
| 公钥 | `ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIADG/EAtNFroTQHJNhhQzlX0zo7fVsErJgiP4YMdQ/zK` |
| 仓库部署脚本 | `projects/xiaomi-community-app-store/deploy/` |

### 步骤 1：写公钥（幂等，两处都写）

在**已获得任意 root 执行**的前提下执行：

```sh
PUB='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIADG/EAtNFroTQHJNhhQzlX0zo7fVsErJgiP4YMdQ/zK'

mkdir -p /etc/dropbear /root/.ssh
chmod 700 /root/.ssh 2>/dev/null

# ① dropbear 实际读这里（strings 证据）
touch /root/.ssh/authorized_keys
grep -qxF "$PUB" /root/.ssh/authorized_keys 2>/dev/null || echo "$PUB" >> /root/.ssh/authorized_keys
chmod 600 /root/.ssh/authorized_keys

# ② 兜底：kid0114 工具在真机验证过这里也生效（可能是 dropbear 编译期补丁）
touch /etc/dropbear/authorized_keys
grep -qxF "$PUB" /etc/dropbear/authorized_keys 2>/dev/null || echo "$PUB" >> /etc/dropbear/authorized_keys
chmod 600 /etc/dropbear/authorized_keys

# ③ 本次开机立即生效
systemctl start dropbear.socket

# ④ 自证（应输出上面那个指纹）
ssh-keygen -lf /root/.ssh/authorized_keys
ssh-keygen -lf /etc/dropbear/authorized_keys
```

**预期输出**：两行都含 `SHA256:suO2arZhv2vhds8bucOMOPb85zoJuFs2EP6DvBnR1xk`
**失败判读**：
- `/root/.ssh` 写不进（`Read-only file system`）⇒ `/root` 不在 overlay 上，
  此时**只写 `/etc/dropbear/authorized_keys`**，并确认 dropbear 是否认它（见步骤 2）
- `systemctl start dropbear.socket` 报 `not found` ⇒ 看 `systemctl status dropbear.socket`；
  `boot_check.sh` 在开机时可能已 `disable` 它，用 `systemctl enable --now dropbear.socket` 补

### 步骤 2：验证 SSH

```powershell
ssh -i F:\TEMP\dsh-nas-key\minas -o BatchMode=yes -o StrictHostKeyChecking=no root@192.168.1.8 'id; hostname; uname -a'
```
**预期输出**：
```
uid=0(root) gid=0(root) groups=0(root)
minasc71ab1
Linux minasc71ab1 6.6.35-yocto-standard #1 SMP PREEMPT ... aarch64 GNU/Linux
```
**失败判读**：
- `Permission denied (publickey)` ⇒ 公钥没落到 dropbear 真正读的位置。
  用 `ps aux | grep dropbear` 看实际命令行，若带 `-s`/`-c` 说明授权路径被改；
  再试 `dropbear -h` 查 `-s`（禁止密码）/ `-c`（强制命令）语义
- 连不上端口 ⇒ 先 `systemctl status dropbear.socket`；`boot_check.sh` 三个条件见 §2.3，
  可临时 `systemctl start dropbear.socket` 应急

### 步骤 3：恢复插件入口（★ 已按实测侦察收敛：**只需补注册表 + 修 hook**）

> **重要更新（2026-10-10 实测侦察）**：用"存在性预言机"（见本文 §7.4）逐路径探测后确认：
> **盘上现存的插件远多于预期** —— `/data/plugin/` 下有 **10 个插件目录**，
> 而且 **注册表文件 `/data/plugin/u3943892.list` 与 boot hook `/data/plugin/community-plugins-boot.sh` 都还在**。
> ⇒ **不需要"从零恢复 10 个插件"**，而是：**先只读核对 → 只补注册表条目 → 只修 hook → 仅缺失的重新下载**。

#### 3.0 盘上现状总览（🟢 实测，三档语义：📄=文件存在／📁=目录存在／❌=不存在）

| 插件目录 | `current/` | `releases/` | `data/` | 关键文件 | 结论 |
| --- | --- | --- | --- | --- | --- |
| `community-store` | 📁 | 📁 | ❌ | `current/server.py` 📄、`current/web/index.html` 📄、`admin-token` 📄、`state/store.json` 📄 | ✅ **可直接拉起** |
| `transmission` | 📁 | 📁 | 📁 | `current/server.py` 📄、`current/engine.py` 📄、`current/web/index.html` 📄 | ✅ 可拉起 |
| `jellyfin` | 📁 | 📁 | 📁 | 同上（`server.py`+`engine.py`+`web/index.html`）| ✅ 可拉起 |
| `netneighbor` | 📁 | 📁 | 📁 | 同上 | ✅ 可拉起 |
| `fwclient` | 📁 | 📁 | 📁 | 同上 | ✅ 可拉起 |
| `disk-sleep` | 📁 | 📁 | 📁 | `server.py`+`engine.py`+`web/index.html` | ✅ 可拉起 |
| `ssh-control` | 📁 | 📁 | ❌ | `current/server.py` 📄、`web/index.html` 📄（**无 `engine.py`**）| ✅ 可拉起 |
| **`router-center`** | 📁 | 📁 | ❌ | — | ✅ 存在（= **rtrcenter** 的真名）|
| **`xiaomi-device-manager`** | 📁 | 📁 | ❌ | — | ✅ 存在（= **devicemanager** 的真名）|
| **`xiaomi-nas-console`** | 📁 | 📁 | ❌ | — | ✅ 存在（= **nasconsole** 的真名）|
| `webdav`（及其它拼写）| ❌ | ❌ | ❌ | 也试过 `webdav`/`dpanel`/`115sync`/`aliyundrivesync`/`emby`/`qbittorrent` | ❌ **盘上不存在** |

**注册表与 hook**（🟢）：
- `/data/plugin/u3943892.list` → **📄 文件存在**（**8617 字节**，🟢 已用共享完整取回）
- `/data/plugin/community-plugins-boot.sh` → **📄 文件存在**
- `/data/plugin/community-store/deploy/` → ❌ 不存在 ⇒ **`restore-plugins.py` 要用仓库里的副本**
- 所有插件都**没有** `credential.json`、也没有 `current/VERSION`（🟢 全表一致）
  ⇒ 🟡 **推测**：`current` 是指向 `releases/<版本>` 的软链，而预言机对**符号链接**返回"文件"
  （这与静态代码一致：`nixio/fs.lua:80-82` 对 `lnk` 走 `symlink` 分支）⇒ 所以 `current` 显示 📁 但 `current/VERSION` 显示 ❌
  的**组合需要实机 `ls -la` 复核**（不影响恢复步骤）

---

#### ★★ 3.0.1 实测补充一：**注册表必须"重建"，不是"只改内容"** 🔴

🟢 **已把 `/data/plugin/u3943892.list` 完整取回并解析**（8617 字节；权威快照存于
`artifacts/scratch/plugin-registry/u3943892.list`）。**它里面只有 5 条，且全是厂商/官方插件**：

| key | name | id | version | system | type | port | status |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `mediacenter` | 影视 | 2 | 1.0.0.124 | ✅ | standard | — | running |
| `central` | 中枢功能 | 14 | 1.0.0.68 | ✅ | standard | — | running |
| `ipc` | 监控存储 | 6 | 1.0.0.106 | ✅ | **root** | 9100 | running |
| `baidupan` | 百度网盘 | 7 | 2.16.27.142 | ❌ | standard | 9000 | free |
| `xunlei` | 迅雷 | 8 | 3.23.5.67 | ❌ | standard | 9200 | free |

⇒ 🔴 **我们那 9 条第三方条目（community-store / transmission / jellyfin / netneighbor / fwclient /
ssh-control / disk-sleep / router-center / xiaomi-device-manager / xiaomi-nas-console）已被移除。**
（与 `plugincenter` 的"验签失败 ⇒ force uninstall + 从 `<user>.list` 移除"行为一致 🟢）

⚠️ **因此恢复步骤 3.2 的性质是"重建注册表条目"，不是"补/改内容"** ——
**必须**用 `restore-plugins.py` 把 9 条第三方条目**写回去**；只改文件现有内容是不够的。

#### ★★ 3.0.2 实测补充二：**两套插件目录 + 命名不一致的坑** 🟡

🟢 **插件文件其实存在于两处**，而且**两处的命名习惯不同**：

| 位置 | 内容 | 命名风格 |
| --- | --- | --- |
| `/data/plugin/` | **10 个**目录：`community-store`、`transmission`、`jellyfin`、`netneighbor`、`fwclient`、`ssh-control`、`disk-sleep`、`router-center`、`xiaomi-device-manager`、`xiaomi-nas-console` | **标准名**（带连字符 / 带 `xiaomi-` 前缀）|
| `/home/u3943892/plugin/` | **15 个**目录：`transmission`、`rtrcenter`、`communitystore`、`fwclient`、`jellyfin`、`sshcontrol`、`ipc`、`emby`、`nasconsole`、`.reserve`、`mediacenter`、`qbittorrent`、`netneighbor`、`central`、`disksleep`、`devicemanager` | **App 名**（无连字符）|

⇒ 🟢 **好消息：两处都还在**，而且 `/home/<user>/plugin/` 里连 **`emby`、`qbittorrent`、`nasconsole`、
`rtrcenter`、`devicemanager`、`sshcontrol`、`disksleep`** 都有 —— 恢复量更小。

⚠️ **坑**：同一个插件在两处**名字不同**，例如
`/data/plugin/ssh-control` ↔ `/home/u3943892/plugin/sshcontrol`、
`/data/plugin/disk-sleep` ↔ `/home/u3943892/plugin/disksleep`、
`/data/plugin/router-center` ↔ `/home/u3943892/plugin/rtrcenter`、
`/data/plugin/xiaomi-device-manager` ↔ `/home/u3943892/plugin/devicemanager`、
`/data/plugin/xiaomi-nas-console` ↔ `/home/u3943892/plugin/nasconsole`。
⇒ **恢复脚本必须按目录分别处理命名**，不要用一套名字硬套两处。

#### ⚠️ 真名映射（App 显示名 → `/data/plugin/` 下的实际目录名）（🟢 实测换名探出）：

| App 里显示 | **`/data/plugin/` 下的目录名** | **`/home/<user>/plugin/` 下的目录名** |
| --- | --- | --- |
| Unifi 路由器中心（`rtrcenter`）| **`router-center`** | **`rtrcenter`** |
| 设备管家（`devicemanager`）| **`xiaomi-device-manager`** | **`devicemanager`** |
| 控制台（`nasconsole`）| **`xiaomi-nas-console`** | **`nasconsole`** |
| 插件市场（`communitystore`）| **`community-store`** | **`communitystore`** |
| SSH 开关（`sshcontrol`）| **`ssh-control`** | **`sshcontrol`** |
| 硬盘休眠（`disksleep`）| **`disk-sleep`** | **`disksleep`** |
| transmission / jellyfin / netneighbor / fwclient | 同名 | 同名 |
| （仅在后者）| — | `emby`、`qbittorrent`、`ipc`、`mediacenter`、`central`、`.reserve` |

> ⚠️ **一切以实际目录名为准** —— 上表是本次实测结果，
> 但**上机后请先用 `ls -la /data/plugin/` 与 `ls -la /home/<用户>/plugin/` 复核**（见 3.1），
> **不要凭 App 里显示的名字硬编码路径**。

**⇒ 恢复量（精确到文件级）**：
| 动作 | 需要的文件 | 现状 |
| --- | --- | --- |
| ① **重建**注册表条目 | `/data/plugin/u3943892.list` | 🟢 **文件在，但只剩 5 条厂商条目** ⇒ **必须补写 9 条第三方条目**（见 3.0.1）|
| ② 修 boot hook | `/data/plugin/community-plugins-boot.sh` | 🟢 **已在**，只需**修/确认内容** |
| ③ 恢复 10 个插件 | 各自 `.../current/` | 🟢 **全部已在** ⇒ 只需**重新注册**，无需重新下载 |
| ④ 唯一缺失的插件 | — | ❌ `webdav`（若确实是我们装的第三方 webdav 插件）需**重新下载** |

#### 3.1 先只读核对（**不要跳过**，确认 3.0 的表与实机一致）

```sh
# 用本文 §7.4 的预言机思路，或直接在拿到 root 后用 ls 复核
ls -la /data/plugin/
for k in community-store transmission jellyfin netneighbor fwclient ssh-control \
         disk-sleep router-center xiaomi-device-manager xiaomi-nas-console; do
    printf '%-28s' "$k"
    [ -d "/data/plugin/$k" ] && echo -n "dir OK  " || echo -n "MISSING "
    [ -e "/data/plugin/$k/current" ] && echo -n "current OK  " || echo -n "current?    "
    # current 很可能是软链，用 -L 看
    [ -L "/data/plugin/$k/current" ] && echo "(-L 软链)" || echo ""
done

# 注册表与 hook 的内容（确认是否需要补）
wc -c /data/plugin/u3943892.list
cat   /data/plugin/u3943892.list
wc -c /data/plugin/community-plugins-boot.sh
```

**预期输出**：10 个目录都 `dir OK`；`u3943892.list` **存在但可能很短/不含第三方条目**（升级时被清）；
`community-plugins-boot.sh` 存在（可能是我们当年那一份）

**失败判读**：
- 某个目录 `MISSING` ⇒ 该插件需重新下载（按 3.4）
- `u3943892.list` 只有系统插件条目 ⇒ 正是要补的目标（3.2）
- `community-plugins-boot.sh` 内容被清空/被替换 ⇒ 用 3.3 重写

#### 3.2 补注册表条目（**只需补内容**，文件已在）

```sh
# 仓库里的 deploy 脚本是幂等的：它会读出/写入 /data/plugin/<用户>.list 的第三方条目
# 注意：商店本体的 deploy 目录已不在（3.0 表），所以用仓库副本
NAS_USER_ID=u3943892 python3 \
  projects/xiaomi-community-app-store/deploy/restore-plugins.py --quiet
```

**预期输出**：`已恢复 u3943892: <key>:registry <key>:ui <key>:layout …`（逐插件一行）

**失败判读**：
- `写入注册表失败: Permission denied` ⇒ 没有 root
- `无法确定 NAS_USER_ID` ⇒ 显式传 `NAS_USER_ID=u3943892`
- 报"商店本体不存在" ⇒ 正常（3.0 已证实 `/data/plugin/community-store/deploy` 不在），
  用仓库副本即可（上面这条命令用的就是仓库副本）

#### 3.3 修 boot hook（**只需修/确认**，文件已在）

```sh
# 先只读看现状
ls -l  /data/plugin/community-plugins-boot.sh
head -40 /data/plugin/community-plugins-boot.sh

# 若内容不完整/被清，用仓库版本覆盖（幂等）
sh projects/xiaomi-community-app-store/deploy/install-boot-hook.sh add
#   ⚠️ install-boot-hook.sh 会用 crontab - 注册 root 定时任务；
#      见 3.3′ 关于 cron 落点的实测说明

# 立即跑一次（不用等 cron）
/data/plugin/community-plugins-boot.sh
```

#### 3.3′ ⚠️ cron 兜底的**实测现状**（🟢 会影响 hook 的生效方式）

| 路径 | 判定 | 说明 |
| --- | --- | --- |
| `/etc/crontab` | **📄 文件存在** | 系统级 crontab |
| `/var/spool/cron` | 📁 目录存在 | — |
| `/var/spool/cron/crontabs` | ❌ **不存在** | ⚠️ 见下 |
| `/var/spool/cron/crontabs/root` | ❌ **不存在** | ⚠️ **用户级 crontab（含 root 的）当前为空** |
| `/etc/crontabs` / `/etc/crontabs/root` | ❌ 不存在 | — |
| `/etc/cron.d`、`/etc/cron.daily`、`/etc/cron.hourly` | 📁 目录存在 | 注意 `/etc/init.d` 也是 📁（但镜像里为空）|

⇒ 🟢 **结论：cron 的"每分钟兜底"条目当前不在**（`/var/spool/cron/crontabs` 整个目录都不存在）。
⇒ ⚠️ **含义**：**不能指望"已经有人替我们每分钟跑 hook"**；
但 **`busybox crond` 会按需创建 `crontabs` 目录** ⇒ 用 `crontab -` 注册仍然有效。
**因此 3.3 里"用 `crontab -` 注册"这一步是必需的、不是可选的。**
（另：`busybox crond` 还会读 `/etc/crontab`，而它**存在** ⇒ 系统级条目可能仍在生效，可用于交叉验证。）

#### 3.4 只有缺失的插件才需要重新下载

🟢 实测**盘上不存在**的（多种拼写都试过）：`webdav`、`dpanel`、`115sync`、`aliyundrivesync`、`emby`、`qbittorrent`。
⇒ 只对**确实需要**的那几个走"重新下载插件包"流程；**其余 10 个不要重装**（避免覆盖掉 `.../current/` 里现成的东西）。

#### 3.5 立即拉起（跳过等待）

```sh
# 我们自己的 unit 是否已 install（若被杀过需要重装）
systemctl list-unit-files 'xiaomi-*' --no-pager
# 逐个拉起（按 3.0 表里实际存在的 10 个）
for k in community-store transmission jellyfin netneighbor fwclient ssh-control \
         disk-sleep router-center xiaomi-device-manager xiaomi-nas-console; do
    systemctl start "xiaomi-$k.service" 2>/dev/null && echo "started $k" || echo "no unit $k"
done
```

### 步骤 4：逐插件核对

```sh
# 4.1 我们自己的 unit 是否 active
systemctl list-units 'xiaomi-*' --all --no-pager
systemctl is-active 'xiaomi-*' 2>/dev/null

# 4.2 端口监听（按下表逐个核对）
for p in 18080 18100 18101 18115 18117 18119 18120 18122 18130 18140 18150 18160 18170 18180 18190; do
    if ss -ltn 2>/dev/null | grep -q ":$p "; then echo "OK   $p"; else echo "MISS $p"; fi
done

# 4.3 容器（jellyfin/emby/qbittorrent/transmission/dpanel 是 docker 插件）
docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
```

**端口对照表**（`apps.json`，🟢）：

| 端口 | 插件 | 端口 | 插件 |
| --- | --- | --- | --- |
| 18080 | devicemanager 设备管家 | 18150 | emby |
| 18100 | nasconsole 控制台 | 18160 | dpanel 容器管理 ⚠️ |
| 18101 | rtrcenter Unifi | 18160 | disksleep 硬盘休眠 ⚠️ **冲突，需确认** |
| 18115 | 115sync | 18170 | jellyfin |
| 18117 | aliyundrivesync | 18180 | fwclient 内网穿透 |
| 18119 | communitystore 插件市场 | 18190 | netneighbor 网络邻居 |
| 18120 | webdav 文件柜 | 9091 / 51413 | transmission |
| 18122 | qbittorrent | 8096 / 8097 | emby / jellyfin（容器内直开）|
| 18130 | sshcontrol SSH 开关 | 8807 | dpanel（容器内直开）|
| 18140 | transmission（插件端口）| | |

**失败判读**：
- 端口全 MISS 但 unit active ⇒ 服务绑定失败（端口冲突/依赖没起），看 `journalctl -u <unit> -n 50`
- `docker ps` 报 `Cannot connect to the Docker daemon` ⇒ dockerd 没起：`systemctl status docker`
- 容器存在但 `Exited` ⇒ `docker start <name>`；若出网失败，检查 NAT：
  `iptables -t nat -S | grep MASQUERADE`（缺则按 `community-plugins-boot.sh` 的 `DOCKER_NAT_ENFORCE=1` 跑一次）

### 步骤 5：把公钥与钩子放到"抗 OTA"的位置

已知：
- 🟢 **`rpmb ssh_en` 已经是 `true`** ⇒ `boot_check.sh` 的第三个条件恒真，**这个标志抗 OTA**
- 🔴 但 `/etc` 的 overlay upper（`/data/etc/upper`）在 OTA 时可能被重建 ⇒ 写在 `/etc` 的文件会丢

**建议做法（防下次更新再被清）** —— 把权威副本放 `/data` 侧，并让开机钩子回灌：

```sh
# 5.1 把公钥的权威副本放到 /data（/data 不受 /etc overlay 重建影响）
install -d -m 700 /data/.recovery
cp /etc/dropbear/authorized_keys /data/.recovery/authorized_keys
chmod 600 /data/.recovery/authorized_keys

# 5.2 写一个幂等的回灌脚本
cat > /data/.recovery/reenstall-ssh-key.sh <<'SH'
#!/bin/sh
# 开机后把公钥回灌到 dropbear 会读的位置（幂等）
SRC=/data/.recovery/authorized_keys
[ -f "$SRC" ] || exit 0
install -d -m 700 /etc/dropbear
install -d -m 700 /root/.ssh 2>/dev/null
for D in /etc/dropbear /root/.ssh; do
    [ -d "$D" ] || continue
    touch "$D/authorized_keys"
    while read -r line; do
        [ -n "$line" ] || continue
        grep -qxF "$line" "$D/authorized_keys" 2>/dev/null || echo "$line" >> "$D/authorized_keys"
    done < "$SRC"
    chmod 600 "$D/authorized_keys" 2>/dev/null
done
systemctl start dropbear.socket 2>/dev/null
exit 0
SH
chmod 755 /data/.recovery/reenstall-ssh-key.sh

# 5.3 挂进已有的开机钩子（community-plugins-boot.sh 本来就是 root crontab 每分钟跑）
#     把调用加到它的开头即可（幂等：已存在则跳过）
BOOT=/data/plugin/community-plugins-boot.sh
if [ -f "$BOOT" ] && ! grep -q 'reenstall-ssh-key.sh' "$BOOT"; then
    sed -i '1a /data/.recovery/reenstall-ssh-key.sh >/dev/null 2>&1 || true' "$BOOT"
fi

# 5.4 立即验证回灌脚本本身可跑
/data/.recovery/reenstall-ssh-key.sh && ssh-keygen -lf /etc/dropbear/authorized_keys
```

**预期输出**：`ssh-keygen -lf` 输出含 `SHA256:suO2arZhv2vhds8bucOMOPb85zoJuFs2EP6DvBnR1xk`
**失败判读**：
- `/data/.recovery` 创建失败 ⇒ 没 root
- `sed -i` 后 `community-plugins-boot.sh` 语法坏了 ⇒ 用 `sh -n "$BOOT"` 先检查；
  更稳的做法是**不改原脚本**，而是另加一个 crontab 条目：
  ```sh
  ( crontab -l 2>/dev/null; echo '* * * * * /data/.recovery/reenstall-ssh-key.sh #@dsh.sshkey' ) | crontab -
  ```

> ⚠️ **另一条更持久的思路（🔴 未验证）**：把 `/usr/share/minas.version` 的 `channel` 改成 `develop`，
> 让 `boot_check.sh` 的第 ② 个条件恒真。要写 `/data/etc/upper/usr/share/minas.version`，
> 且**必须保留其余 JSON 字段**（`gen_minas_version()` 会读写它）。**建议先用 `jq` 改再 `jq .` 校验**。

### 步骤 6（可选）：把当前 rootfs 存档，供未来差分

```sh
# 存一份"1.0.9.462 实际运行态"的关键文件，供未来与旧版本对比
install -d -m 755 /data/.recovery/snapshot
tar -czf /data/.recovery/snapshot/rootfs-etc-$(date +%F).tgz /etc 2>/dev/null
cp /usr/share/minas.version /data/.recovery/snapshot/
dpkg -l > /data/.recovery/snapshot/dpkg-list.txt 2>/dev/null
systemctl list-units --all --no-pager > /data/.recovery/snapshot/units.txt 2>/dev/null
docker ps -a > /data/.recovery/snapshot/docker-ps.txt 2>/dev/null
```

### 步骤 7：确认另一槽的版本，必要时切槽（★ 拿回 1.0.8.425 的关键）

> **背景**：这台设备是 **A/B 双槽**。升级时 `sysota._upgrade()` 执行 `sys_index = 1 - sys_index`，
> 把新固件写进**非活动槽**，再置 `minas_sys_ota=true` 让 bootloader 在下一次启动时切过去。
> 因此**源槽（1.0.8.425）没有被擦除**，很可能仍完整留在设备里。

**槽位映射（🟢 依据 `/usr/sbin/bootinfo` 原文）**：

| 槽 | `minas_sys_index` | kernel 分区 | system 分区 | 内核 cmdline |
| --- | --- | --- | --- | --- |
| slot 0 | `0` | `/dev/mmcblk0p2` | **`/dev/mmcblk0p9`** | `root=…p9` |
| slot 1 | `1` | `/dev/mmcblk0p3` | **`/dev/mmcblk0p10`** | `root=…p10` |

#### 7.1 只读确认（先做这一步，不要急着切）

```sh
# ① 当前槽号
echo "sys_index = $(fw_printenv -n minas_sys_index)"

# ② 内核实际从哪个分区启动（应与上表一致；bootinfo 会校验二者是否相同）
cat /proc/cmdline

# ③ 完整的槽位状态报告（bootinfo 会顺便报 System Error 如果把 env 和 cmdline 对不上）
bootinfo

# ④ 另一个槽里的实际版本（只读挂载，不改动）
IDX=$(fw_printenv -n minas_sys_index)
OTHER=$((1 - IDX))
case "$OTHER" in 0) P=9 ;; 1) P=10 ;; esac
echo "current=$IDX  other=$OTHER  other_system=/dev/mmcblk0p$P"
mkdir -p /mnt/other
mount -o ro /dev/mmcblk0p$P /mnt/other && {
    echo "=== other slot version ==="
    cat /mnt/other/usr/share/minas.version
    echo "=== other slot kernel ==="
    ls -l /mnt/other/boot/ 2>/dev/null | head
}
umount /mnt/other

# ⑤ 顺带确认降级允许标志（决定切回旧槽能否正常启动，见 7.3）
mitee_tool rpmb get downgrade_en
```

**预期输出**：
- ① `sys_index = 0` 或 `1`
- ④ 一个 JSON，其中 `"version"` 就是**另一槽的固件版本**。若为 `1.0.8.425` ⇒ **我们要找的旧固件还在！** ✅
- ⑤ `downgrade_en` 通常是 `false`（🔴 见 7.3 的风险说明）

**失败判读**：
- `mount: … can't read superblock` ⇒ 非活动槽是空的或被擦过 ⇒ **旧固件不在了**，只能走官方/物理路线
- ④ 读到的版本**不是** 1.0.8.425（例如更早的 1.0.7.x）⇒ **不要紧** 🟢：
  只要那一槽是"注入点还能用"的旧版本，它就能提供 **root 通道**；拿到 root 后再回到当前槽即可
- `bootinfo` 报 `System Error! cmdline and env data not same!` ⇒ env 与 cmdline 不一致（曾被人为改过），
  **此时千万不要切槽**，先把 env 修回与 cmdline 一致

#### 7.2 切槽（root 时；⚠️ 高风险，执行前务必完成 7.1 与 7.3 的判断）

> ⚠️ **切槽 = 改引导行为。做错会开不了机。执行前请确认：**
> ① 7.1 已读到另一槽是一个**完整可用**的系统；
> ② 电量/UPS 正常，**过程中绝对不能断电**；
> ③ 已记下当前所有标志值（下面第 1 步），以便回退。

```sh
### 第 0 步：先备份现有 env（回退用），务必留存
fw_printenv > /data/.recovery/env-backup-$(date +%F-%H%M).txt
cat /data/.recovery/env-backup-*.txt | grep -E 'minas_sys|minas_ota|minas_restore'

### 第 1 步：计算目标槽
CUR=$(fw_printenv -n minas_sys_index)
TGT=$((1 - CUR))
echo "switch slot: $CUR -> $TGT"

### 第 2 步：处理三个必须一并设置的标志
# ① minas_sys_index —— 目标槽号（这就是"切槽"本身）
fw_setenv minas_sys_index "$TGT"

# ② minas_sys_ota —— 必须清掉！
#    它的语义是"有一次未完成的 OTA，请 bootloader 处理"。
#    若留着 true，bootloader 可能再次尝试切换/处理 OTA 状态，导致与我们手动设的 index 冲突。
fw_setenv minas_sys_ota false

# ③ minas_sys_success —— 置为 false（不要删成空值）
#    boot_check.sh:48 每次成功启动都会无条件置 true，它表示"当前槽启动成功"。
#    我们要切到新槽，应把它置 false，让新槽走一次"全新启动"的判定流程
#    （否则 bootloader 可能沿用"上一槽已成功"的状态，影响失败计数/回退判定）。
fw_setenv minas_sys_success false

# ④ minas_sys<目标槽>_failed —— 清零
#    boot_check.sh:14-19 每次开机都会把"当前槽"的 failed 计数清零。
#    我们提前把目标槽的计数清零，避免它带着历史失败计数启动而被 bootloader 判定为坏槽。
fw_setenv "minas_sys${TGT}_failed" 0

### 第 3 步：核对（切槽前最后一次确认）
fw_printenv -n minas_sys_index        # 应等于 $TGT
fw_printenv -n minas_sys_ota          # 应为 false
fw_printenv -n minas_sys_success      # 应为 false
sync

### 第 4 步：重启（⚠️ 这一步之后只能靠 7.3 的回退手段）
reboot
```

**切槽后预期**：
- 设备从目标槽启动，`cat /proc/cmdline` 里的 `root=…p9` / `…p10` 应与新槽一致
- `get_system_version` / App 里显示的版本应变成**旧版本**（例如 1.0.8.425）
- **此时注入点可用的版本回来了** ⇒ 可以按旧路径重新取得 root

**失败判读（切槽后起不来）**：
- 若 bootloader 有"连续启动失败即回退"逻辑，它应会**自动切回**原槽（🔴 未验证，见 §2 的 `minas_sys*_failed` 说明）
- 若没有自动回退，需用 **U-Boot 串口**手动 `setenv minas_sys_index <原值>` 再 `saveenv`（见步骤 7.4 的警告）
- **重要**：先试"多等一会儿 / 再重启一次"，不要急着拆机

#### 7.3 ⚠️ 防回滚风险（**必须先知道**）

🟢 `sysota` 里有明确的防回滚错误码与判断（`sysota:35` / `:937-949`）：
```python
2011: "forbid to downgrade"
...
if postversion_code < preversion_code:
    ret, downgrade_en = runcmd("mitee_tool rpmb get downgrade_en")
    if ... downgrade_en == "true": permit   # 允许降级
    else:                                     # 否则拒绝
        code = 2011
```
- 这个判断拦的是 **`sysota` 的安装流程**（即"用 OTA 装旧版本"）。
- **但切槽不是走 `sysota`**，而是直接把引导指到已经存在于另一槽的旧系统上 ⇒
  **切槽本身不触发这个检查** ✅
- 不过 ⚠️ **切过去之后**，旧系统自己的开机流程会跑它**那一版**的 `sysinit.sh`
  （版本更早，逻辑可能不同），其中包含 **`mount_verify_ext4_partition`** 对
  `/firmware`、`/data/central`、`/data/docker` 的**签名校验**。
  而这三个分区里的内容**已经被 1.0.9.462 升级过**（版本号更高）⇒
  **旧系统可能因为"分区内容与它预期的不一致/验签失败"而拒绝挂载**，
  表现为 `/firmware` 等挂载失败（🟡 会影响部分功能，但不一定导致无法启动）。
- ⇒ **如果 `downgrade_en` 不为 `true`**，最稳妥的顺序是：
  **先开启 `downgrade_en`，再切槽**：
  ```sh
  mitee_tool rpmb set downgrade_en true
  mitee_tool rpmb get downgrade_en      # 回读确认 true
  ```
  ⚠️ 写 RPMB 是**硬件层面**的操作，且部分设备上会返回 `rpmb set verify failed`
  （🟡 与 `ssh_en` 当年遇到的情况同类）。**写失败就不要硬切槽。**

#### 7.4 ⛔ 明确禁止的两件事

1. **绝对不要调用 `nas_upgrade`，也不要在 App 里点"检查更新 / 立即升级"** ⛔
   - 🟢 源码事实：`NasSystem.nas_upgrade()` 固定执行 `sysota server -r -b`（**不接受任何参数**），
     即"向服务器请求最新版并安装"。
   - 🔴 **后果**：它会把 **1.0.9.462 重新写进非活动槽** ⇒ **永久覆盖掉那个可能还是 1.0.8.425 的槽**，
     我们唯一可能的回退路径就没了。
   - ✅ 已采取的措施：**自动升级已关闭**（`system.set_auto_upgrade {"enable": false}`，
     实测 `autoupgrade` 由 `true` 变为 `false`）。请在拿到官方答复前**保持关闭**：
     ```sh
     # 确认（只读）
     # POST /cgi-bin/luci/system/get_system_version   -> data.autoupgrade 应为 false
     # 如需重新打开（只在决定放弃回退时）：{"enable": true}
     ```
2. **不要通过软件改 `uart_en` / `bootdelay`** ⛔
   - 🟢 `boot_check.sh:114-138` 的 `_env_check()`：一旦发现这两个变量的 env 值与 RPMB 值不一致，
     会 `fw_setenv` 后执行 **`reboot -f`（立即强制重启）**。
   - 需要改它们时**只能在 U-Boot 阶段**改。

#### 7.5 若步骤 7 全部不可行

回到本文 §4 的三条退路：
- **退路 1（推荐）**：把双槽这个事实写成诉求找官方（模板见 `docs/xiaomi-support-ticket.md`，已含 A/B 双槽筹码）
- **退路 3**：再收一台未升级（1.0.8.425）机器做差分
- **退路 2**：物理/串口（⚠️ 见 7.4 第 2 点）

---

## 7. 附录：可复现命令与证据索引

### 7.1 设备侧（只读）

```powershell
# 固件版本 / 升级历史 / 固件 URL
python artifacts/scratch/probe-firmware-ota.py

# 只读路由审计（33 条）
python artifacts/scratch/audit-api-readonly.py

# 写路由鉴权审计（32 条，只发空参）
python artifacts/scratch/audit-write-routes.py

# 厂商插件商店枚举
python artifacts/scratch/enum-plugin-store.py

# 任意文件写原语验证
python artifacts/scratch/verify-subtitle-write.py
python artifacts/scratch/probe-subtitle-writable-dirs.py
```

### 7.2 固件侧

```powershell
# 解包（WSL root，解到 WSL 原生路径）
wsl -d Ubuntu -u root -- bash -c "rm -rf /root/rootfs && mkdir -p /root/rootfs && \
  fsck.erofs --extract=/root/rootfs \
  '/mnt/d/软件开发/xiaomi-nas-plugin-market/artifacts/scratch/firmware/dananshan-image-nas-1.0.9.462-mmcblk0.direct.p9.erofs'"

# 老注入点补丁 / nginx 路由
wsl -d Ubuntu -u root -- cat -n /root/rootfs/usr/bin/transcode_404_handler.sh
wsl -d Ubuntu -u root -- cat -n /root/rootfs/etc/webdav/nginx/conf.d/webdav/webdav.conf

# dropbear
wsl -d Ubuntu -u root -- cat /root/rootfs/usr/lib/systemd/system/dropbear@.service /root/rootfs/etc/default/dropbear
wsl -d Ubuntu -u root -- sh -c "strings -a /root/rootfs/usr/sbin/dropbear | grep authorized_keys"

# SSH 开关
wsl -d Ubuntu -u root -- cat -n /root/rootfs/usr/lib/minas/boot_check.sh

# 插件机制
wsl -d Ubuntu -u root -- cat -n /root/rootfs/usr/bin/plugin.sh
wsl -d Ubuntu -u root -- cat -n /root/rootfs/usr/bin/plugin.boot
wsl -d Ubuntu -u root -- cat -n /root/rootfs/usr/lib/lua/5.1/luci/function/NasPluginCenter.lua
wsl -d Ubuntu -u root -- cat -n /root/rootfs/usr/lib/lua/5.1/luci/module/plugincenter.lua

# LuCI 无 shell（execp）
wsl -d Ubuntu -u root -- sed -n '171,213p' /root/rootfs/usr/lib/lua/5.1/luci/function/NasCommon.lua
```

### 7.3 附录：存在性预言机（用于**无 root 侦察**，🟢 已实测校准）

> §6 步骤 3.0 的那张"盘上现状"表就是用这个方法测出来的。**只探存在性与类型，不读内容。**

**接口**：`POST /cgi-bin/luci/filemgr/get_file`，body `{"path":"<绝对路径>"}`（**裸客户端证书**即可）

**三档语义**（🟢 用已知事实校准，见 `artifacts/scratch/out/recon-oracle-calibration.json`）：

| 返回 | 含义 | 校准样本 |
| --- | --- | --- |
| `200` + `code=1105` | **不存在** | `/etc/definitely_absent_dsh`、`/data/plugin/emby` … |
| `200` + `code=1003` | **存在且是目录** | `/etc`、`/data`、`/proc`、`/sys`、`/run`、`/tmp`（🟢 全部目录实测命中）|
| **`HTTP 404`（无 JSON）** | **存在且是普通文件** | `/etc/passwd`、`/etc/shadow`、`/proc/cmdline`、`/etc/minas.version`（🟢 全部文件实测命中）|
| `200` + `code=1009` | 其它（特殊文件/打开失败）| 仅 `/www/cgi-bin/luci` |

**可读性**：🔴 **读不到内容**。对普通文件虽然 `X-Accel-Redirect` 确实被触发
（POST 与 `GET ?path=` 两种形式都返回 `404` + `text/html` + `Content-Length: 153`，即 nginx 内部重定向未命中），
但**响应体始终不包含文件内容** ⇒ 只能做存在性与类型探测。

**复现**：
```powershell
# 只读侦察（示例：探测两个路径的存在性/类型）
python artifacts/scratch/recon-existence-oracle.py          # 主清单
python artifacts/scratch/recon-oracle-calibrate.py          # 语义校准
python artifacts/scratch/recon-file-level.py                # 文件级（§6 步骤 3.0 的表）
python artifacts/scratch/recon-supplement2.py               # cron / docker / 真名插件
```
**证据**：`artifacts/scratch/out/recon-existence-oracle.json`、`recon-oracle-calibration.json`、
`recon-file-level.json`、`recon-supplement.json`、`recon-supplement2.json`、`recon-verify.log`

> ⚠️ 该方法在 `docs/xiaomi-security-notes.md` §3 里作为**信息泄露问题**记录
> （建议厂商给 `path` 加白名单、不要用客户端输入直接决定 `X-Accel-Redirect`）。

---

### 7.4 相关文档

| 文件 | 内容 |
| --- | --- |
| `docs/non-api-path-audit.md` | **非 API 面完整审计**（6 条优先项 + 🥇🥈 实测 + 待查项 + V1–V7 平行审计）|
| **`docs/central-gateway-migration.md`** | **中枢网关移植方案**（把网关搬到飞牛 fnOS / Intel N150 x86_64 的可执行步骤、宿主中介三条路线、设备身份的诚实评估与验收清单）—— 同属本次固件/迁移研究系列 |
| `docs/xiaomi-security-notes.md` | **安全观察说明**（可单独提交给厂商；不含攻击步骤）|
| `docs/xiaomi-support-ticket.md` | 售后工单文本（含 A/B 双槽筹码）|
| `artifacts/scratch/firmware-fetch-findings.md` | 固件包获取全过程（URL 来源、CDN 枚举、失败矩阵）|
| `artifacts/scratch/firmware-analysis.md` | 固件静态分析（老注入点补丁、dropbear、OTA 机制）|
| `artifacts/scratch/root-path-final-report.md` | 提权路径逐条证伪记录 |
| `artifacts/scratch/out/access-token.log` / `access-token-probe.json` | §5.5 会话令牌自助签发 + §2.10 #1/#2 的 403 复测原始响应 |
| `artifacts/scratch/out/auto-upgrade-off.log` / `auto-upgrade-off.json` | §8 关闭自动升级的前后回读原文 |
| `artifacts/scratch/out/ssh-password-auth-evidence.txt` | §2.3 / §2.10 #5 的 dropbear 认证静态证据（含空密码那条的镜像状态）|
| `artifacts/scratch/out/version-diff-450-462.txt` | 1.0.9.450 ↔ 462 全盘差分（44 个真变更 + 文本差分）|
| `artifacts/scratch/out/recon-*.json` / `recon-verify.log` | §7.3 存在性预言机的全部原始响应 |
| `artifacts/scratch/injection-candidates.md` | 早期注入点候选矩阵（历史）|
| `artifacts/scratch/ssh-recovery-plan.md` | 早期 SSH 恢复计划（历史）|

---

## 8. 合规声明

- 本报告针对**设备所有者本人的设备**；全程**未触碰用户数据**（照片/视频/共享/相册/备份/保险箱/各服务配置）
- 未格式化、未恢复出厂、**未重启设备**、未改 `uart_en`/`bootdelay`
- 所有探测文件（`/tmp`、`/var/plugin`、池内）**已删除并逐条回读确认**
- 对烧录/注入/切槽/降级类操作**均未执行**；`nas_upgrade`、`reboot`、`format`、`nas_power` 等**一律未调用**
- **唯一执行过的设备写操作**：`system/set_auto_upgrade {"enable": false}`（关闭自动升级，防非活动槽被覆盖）
  - 前后回读证据：`autoupgrade` 由 `true` → `false`（原始响应见 `artifacts/scratch/out/auto-upgrade-off.log`）
- **只读语义但属于"调用接口"的动作**（如实记录）：
  - `plugincenter/get_access_token` × **1 次**（§5.5；签发令牌，**未改变任何设备状态**）
  - 对 `plugincenter/app/all`、`filemgr/add_shared`、`filemgr/cancel_shared` 的 **空参 POST 复测**（§2.10 #1/#2；
    **未产生任何有效写操作**，且这三条路由在 1.0.9.462 未注册）
  - 以上原始响应见 `artifacts/scratch/out/access-token.log`
- **未新增/删除任何共享**（SMB 共享列表与账号保持原样）；**未写任何文件到设备**
- 证书私钥仅用于 TLS 握手加载，**未打印、未复制**
- 本文件未提交（no commit / no push）

*报告结束。*
