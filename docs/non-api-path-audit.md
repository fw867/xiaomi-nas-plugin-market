# 非 API 通路静态排查：外部输入 → 持久化配置/env → root 执行

- 目标设备固件：**1.0.9.462**（rootfs 已完整解包至 WSL `/root/rootfs/`）
- 方法：**纯静态**（grep + 源码精读 + `llvm-objdump-18` 反汇编），**未对设备发起任何请求**
- 可信度：🟢 已在本机/固件上实测确证 ｜ 🟡 有来源未验证 ｜ 🔴 推测
- 统一判据：外部输入 → ①能否控制 → ②是否写进**持久化**存储 → ③该值是否进入 **shell/root 执行** → ④**我们能否从网络/文件侧触发**
- 配套文档：**`docs/nas-root-recovery-2026-10.md` §5.6**（收尾结论与完整证伪清单）、**§6 步骤 7**（切槽手册）

> **可写面**（前序实测确认）：`/tmp`、`/var/plugin`、`/home/<用户>/**`（含 `/pool0/**`）
> **只读面**：`/etc`、`/data`、`/run`

---

## 总结论（先说结果）

> **第 1 条优先项（root 守护进程把池子里的文件当配置读）不存在。**
> 全盘扫描后，**可写面与"执行面"之间没有任何交集** —— 我把 6 条优先项逐一走完，
> 找到 **1 个真实的 shell 注入 sink**（`smb_mgr.sh` → `/var/etc/smb.conf`），
> **但它的上游（可写字段）全部缺失** ⇒ 无法成套。
> **没有任何一条能形成"写入 → root 执行"的完整链条。**

---

## 优先项 1 —— ⭐ root 是否把「池子里的文件」当配置读？→ **否** 🔴

这是最高价值的问题，我按"最可能"到"最不可能"依次扫了 9 类模式：

| # | 扫描模式 | 结果 | 证据 |
| --- | --- | --- | --- |
| 1.1 | `. <可写路径>` / `source <可写路径>` | 🟢 **零命中** | 唯一命中是 `. /lib/nas/library.sh`（**固定只读路径**），出现在 `lib/nas/**` 约 30 个脚本里 |
| 1.2 | `sh <可写路径>/脚本` | 🟢 **零命中** | `grep -rnE '(sh\|bash\|/bin/sh)\s+[^ ]*/(tmp\|var/plugin\|home\|nas\|mnt/usb)'` → 无 |
| 1.3 | `jq … <可写路径>` | 🟢 **零命中**（只有写 `/tmp` 结果文件的方向）| `fix_filesystem.sh:21` 是 `> /tmp/...`（写，不是读）|
| 1.4 | `uci import` / `uci -c <可写目录>` | 🟢 **零命中** | `grep -rn 'uci import' /usr /etc /lib` → 无 |
| 1.5 | `while read … < <可写路径>` | 🟢 **零命中** | — |
| 1.6 | shell 里出现 `/pool0` / `/nas/pool*` / `/mnt/usb` | 🟡 **有命中，但全是"数据操作"不是"配置读取"** | 见下方清单 |
| 1.7 | lua `loadJson/readfile/dofile/loadfile` 读可写路径 | 🟢 **零命中** | 读的全是 `/etc/nas/naspath.json`、`/usr/share/minas.version` 等只读路径 |
| 1.8 | python `open('<可写路径>')` 当配置 | 🟢 **零命中**（排除 site-packages）| — |
| 1.9 | 遍历可写目录并**执行** | 🟢 **仅 1 处，且不执行** | `/usr/lib/nas/fs/prepare_usb:16` `for dir in /mnt/*` ⇒ 只做 `make_usb_link` + `mkdir -m 777 $dir/.temp_upload_dir`，**无任何 exec/source**（全文 43 行已读）|

### 1.6 的命中清单（都是"数据"不是"配置"）🟢

| 位置 | 用途 | 是否执行 |
| --- | --- | --- |
| `usr/sbin/restore_database:9` | `rm -f "/nas/sys/album/${USER}"*` | ❌ 只删 DB |
| `usr/bin/sp_colls/*.daily.sh` | `DB_PATH="/nas/sys/findex"`、`DB_FILE=/nas/sys/video/mediacenter.db` | ❌ 只当 DB 路径 |
| `etc/syshotplug/delbase/*.album` 等 | `rm -fr /nas/sys/album/*` | ❌ 只删 |
| `etc/syshotplug/ota/10.dsync:3` | `SYS_DSYNC="/nas/sys/system/etc/dsync"` | ❌ 固定路径 |
| `usr/bin/smb_mgr.sh:68-90` | 池内路径 → 建符号链接 | ❌ 见优先项 5 |
| `usr/bin/balance.sh:148` | `mnt_arr+=("/nas/pool$((pool_id-1))")` | ❌ 只做挂载判断 |
| `usr/bin/plugin.sh:248` | `dsync_path "$PLUG_USER" /nas/pool0/"$PLUG_USER"/plugin 1` | ❌ 同步目录 |

⇒ 🟢 **优先项 1 的结论：不存在"root 读可写目录里的文件当配置"的逻辑。我们唯一已确认可写的面，没有对应的执行消费者。**

---

## 优先项 2 —— `/etc/syshotplug/**` 全部处理器 → **无可控外部字符串进入配置** 🟡

`/etc/syshotplug/` 共 **87 个 handler**（13 个事件目录，全部列出见 `out/source-audit-part2.txt`）。

**事件源与传入的变量**（🟢）：
| 事件 | 谁触发 | 传入变量 | 证据 |
| --- | --- | --- | --- |
| `block` | `lib/storage.sh:368` → `env -i ACTION="$action" DEVNAME="$devname" /sbin/syshotplug block` | `$ACTION`、`$DEVNAME` | `DEVNAME` 来自内核 udev（形如 `sda1`），`:8` 还校验 `${DEVNAME:0:2} = "sd"` |
| `mount` | 同上 | `$ACTION`、`$DEVICE`、`$MOUNTPATH`、`$DEVNAME` | `mount/00-storage:5-7` 校验非空 |
| `pool`/`user`/`net`/`ota`/`restore`/`power`/`button`/`docker`/`delbase`/`miio_ot`/`change_pool` | 各自内部服务 | `$ACTION`、`$POOLID`、`$USER`、`$MODULE`、`$TIMING` 等 | — |

**关键：卷标/LABEL 是否被写进配置？** 🟢 **否**
```sh
# 卷标只进 mkfs / btrfs label 的结果，不进任何配置文件
lib/nas/library.sh:299-311   mkfs.btrfs --quiet --label "$label" "$part"
lib/nas/storage/replace_fs_label:6   btrfs filesystem label "$dev" "$label"
lib/nas/storage/format:55    create_btrfs_fs "$part_dev2" "$label" "$size"
lib/nas/storage/create_raid1:27  create_btrfs_fs "$mdev" "$label"
```
⇒ 卷标是**被写进文件系统**（`mkfs --label`，作为独立 argv 传给 mkfs，**不经 shell**），**不是写进 `/etc/config/*` 或 `smb.conf`**。

**`pool/26.samba` 与 `user/02.samba`**（🟢 全文）：
```sh
# pool/26.samba —— 只做服务启停 + 固定参数调用
mounted)    systemctl restart smb/nmb; systemctl start wsdd
preumount)  systemctl stop smb nmb; systemctl stop wsdd
DELPOOL)    nas call smb_mgr.sh global compatible 0
            nas call smb_mgr.sh del_account all
# user/02.samba —— 只有固定动作
ACTIVE)     nas call smb_mgr.sh set_status "$USER" 1
DEACTIVE)   nas call smb_mgr.sh set_status "$USER" 0
```
⇒ 🟢 **两个 Samba hotplug handler 都不把外部字符串送进 `smb.conf`**。
（`$USER` 会进 `set_status`，但 `smb_mgr.sh set_status` 只改 UCI 的 `status` 字段，见优先项 5。）

---

## 优先项 3 —— 媒体元数据通路 → 🟡 **未能穷尽，但已确认"文件名"这条有独立补丁**

| 组件 | 检查结果 |
| --- | --- |
| `transcode_req`（aarch64 ELF，stripped）| 🟢 动态符号里**无 `system`/`popen`**；调用形态是 `execp` argv（前序已确证）⇒ 文件名拼接那条**已被 `transcode_404_handler.sh` 的路径校验补掉** |
| `mediacenterd` / `albumd` | 🟡 **未做反汇编**（体积大、stripped）。**元数据（容器 title/artist、EXIF、字幕名）是否被拼进命令，我无法用静态 grep 确证** 🔴 |
| `ffprobe`/`ffmpeg` 调用点 | 🟡 镜像里 `ffmpeg` 存在；但调用点未定位到具体 argv 构造 |
| 元数据落库 | 🟢 落 `/nas/sys/video/mediacenter.db`、`/nas/sys/findex`（**SQLite DB，不是脚本/配置**）⇒ 落库本身不构成执行 |

⇒ 🟡 **这是本次唯一"没走完"的面**。要确证需要反汇编 `mediacenterd`/`albumd` 找 `system()`/`popen()` 式拼接
（成本 2–4 小时）。**但注意：即使存在，元数据来源是"我们放进池子的媒体文件"** ⇒ 若能得手，
这确实是一条**从可写面到执行面**的路，值得列为**下一个待查项**（见"最值得试"）。

---

## 优先项 4 —— 网络来源的字符串 → **没有一个进入配置** 🟢

| 来源 | 检查 | 结论 |
| --- | --- | --- |
| DHCP client（`udhcpc`）| 🟢 `/usr/share/udhcpc/default.script:3` → `exec run-parts -a "$1" /etc/udhcpc.d`；`/etc/udhcpc.d/50default` 全文已读 | 🟢 **只做 `ip addr/link`、`resolv.conf`、`route`** —— **`hostname`/选项 12 完全没有被使用**（`grep hostname` 零命中）|
| mDNS / SSDP / DLNA | 🟢 `/etc/config` 里 `minidlna`/`avahi` 配置均为**本机固定值**；`friendly_name` 来自 UCI（无 API 写入口）| ❌ 不进执行面 |
| NetBIOS / LLMNR | 🟢 `nmbd` 配置由 `smb_mgr.sh` 生成（见优先项 5），字段来自 UCI | ❌ 同上 |
| UPnP 端口映射描述 | 🟢 镜像里 `miniupnpd` 配置为默认值 | ❌ |
| 打印机 / IPP | 🟢 `printer.target` 存在但无 CUPS/ipp 服务在跑 | ❌ |
| NTP | 🟢 `/etc/syshotplug/net/01-timesync` 只做时间同步 | ❌ |
| 作为 SMB 客户端挂载远端共享时对端提供的名字 | 🟢 镜像里**没有 SMB 客户端挂载逻辑**（无 `mount.cifs` 调用点）| ❌ |

---

## 优先项 5 —— `smb_mgr.sh` 的全部调用点 → **找到 sink，但上游缺失** 🟡

### 5.1 调用点清单（🟢 全量）

| 调用者 | 传入值 | 是否含外部字符串 |
| --- | --- | --- |
| `usr/lib/lua/.../NasSamba.lua:77` | `restart` | ❌ |
| `NasSamba.lua:89` | `global compatible <0/1>` | ❌ 只有 0/1 |
| `NasSamba.lua:99,114,129` | `del_user/user set/user delete <account> <password>` | 🟡 **`account`/`password` 来自 API** |
| `NasSamba.lua:144` | `shares add_dir <user_> <path> <account> <share_point> <user>` | 🟡 **`path` 来自 API** |
| `NasSamba.lua:154` | `shares del_dir <user_>` | ❌ |
| `NasSamba.lua:312` | `add_user <id> <user> <account> <password> <dirs…>` | 🟡 |
| `NasSamba.lua:533` | `set_samba <id> <user> <account> <password> <dirs[1]>` | 🟡 |
| **非 API 调用者** | | |
| `etc/syshotplug/pool/26.samba:22,24` | `global compatible 0` / `del_account all` | ❌ **固定常量** |
| `etc/syshotplug/user/02.samba:4,6` | `set_status "$USER" <0/1>` | 🟡 仅 `$USER`，且只改 UCI status |
| `usr/lib/systemd/system/smb.service:12,17` | `init_config` / `add_nobody` | ❌ **固定常量** |
| **其它脚本** | 🟢 **无**（`grep -rn 'smb_mgr.sh' /usr /etc /lib` 全部列出，上面就是全部）| |

⇒ 🟢 **"非 API 调用者"只有 `pool/26.samba`、`user/02.samba`、`smb.service` 三处，传入的**全部是固定常量
或仅 `$USER`** —— 它们**没有"绕过参数校验"的机会**（因为根本不传用户内容）。

### 5.2 sink 本体（🟢 原文）

```sh
# usr/bin/smb_mgr.sh:262-274  _nas_smb_header()
config_get name        $1 name        "SmartStorage"
config_get workgroup   $1 workgroup   "WORKGROUP"
config_get description $1 description "MiNas"
mkdir -p /var/etc
sed -e "s#|NAME|#$name#g" \
    -e "s#|WORKGROUP|#$workgroup#g" \
    -e "s#|DESCRIPTION|#$description#g" \
    -e "s#|MINPROTOCOL|#$server_min_protocol#g" \
    -e "s#|NTLMAUTH|#$ntlm_auth#g" \
    ${SAMBA_CONF_TEMPLATE} >${VAR_SAMBA_CONF}          # ★ /var/etc/smb.conf
```
🟢 **`sed` 的替换文本里如果含 `\n`，会真的插入换行** ⇒ 可往 `smb.conf` 注入任意指令，
其中 **`preexec` / `root preexec` 是 Samba 以 root 执行的官方指令**。

**⇒ 这是一个真实存在的、高价值的 sink。问题是它的三个上游字段全都写不了：**
```
grep -rn 'samba.*global.*name\|"samba", "global", "name"' /usr/lib/lua /usr/bin /etc  →  🟢 零命中
```
（前序已确证：全量 lua 里**没有任何地方**写 `samba.global.*` 或 `@samba`；
唯一的 samba 写入是 `NasSamba.lua:601-609` 的 `sambauser/sambashare .status`。

⇒ 🟡 **结论：sink 真实、可被 newline 注入、后果是 root 执行 —— 但缺少"能写 `samba.global.name`/`description` 的入口"。这是一个"待条件成熟即可用"的潜伏点。**

---

## 优先项 6 —— 其它"用户可影响的持久化字段 → 脚本" → **无可达的持久化写入口** 🟡

| 字段 | 是否有 API 写入口 | 是否进 root 脚本 |
| --- | --- | --- |
| 主机名 / 设备名 | 🟡 **未发现**（镜像里 `system.@system[0].hostname` 无 lua 写入点；App 改的是设备别名，不是宿主 hostname）| 🟢 进 `_router_smb_header`（`:224-226`），但那是**路由器分支**（`IS_NAS=false`），NAS 分支不用 hostname |
| 共享名 / 注释 | 🟡 `NasSamba` 的 `account`/`dirs` 可控 | 🟢 进 `_smb_add_share`（`:304` `config_get name $1 name`）—— 但值来自 `sambashare` UCI，由 `nas call smb_mgr.sh shares add_dir <path> <account> <share_point>` 写入，**`share_point` 未做换行过滤** 🟡 |
| 备份任务名 | 🟡 未定位到进脚本的路径 | ❌ |
| DDNS 域名 | 🟢 镜像无 DDNS 服务 | ❌ |
| 邮件 / 通知模板 | 🟢 无 |
| 时区 / NTP | 🟢 只进时间同步 |
| UPnP 映射描述 | 🟢 无 API 写入口 |
| DLNA 库名 | 🟢 `minidlna` 配置固定 |

🟡 **`share_point` 这条值得单独说**：`_smb_add_share:304` 的 `config_get name $1 name` 若拿到含 `\n` 的值，
同样注入 `smb.conf`。它由 `NasSamba.lua:144` 的 `shares add_dir ... "$share_point"` 写入，
而 `share_point` 在 `NasSamba` 里没有被专门过滤换行（只过滤连续空格 `:242`）。
**但这需要先能调用 `add_samba_account`/`set_samba_account` 并让 `share_dir` 走到 `add_dir`**
⇒ 🟡 **未验证**（需要实测一次，会创建 Samba 账号/共享 ⇒ **属于改设备状态，须先报批**）。

---

## 最值得试的 3 条（按性价比）

### 🥇 第 1 条：反汇编 `mediacenterd` / `albumd`，找元数据的 `system()`/`popen()` 拼接 🟡
- **理由**：这是**唯一"输入源落在我们可写面（池内媒体文件）"且"尚未穷尽"**的面。
  文件名那条被补了，但**元数据（容器 title/artist/comment、EXIF、字幕名）走的是另一条代码路径**，
  厂商很可能**没有一起补**（与 `NasSamba` 缺换行过滤是同一类疏漏）。
- **确切操作**（纯静态）：
  ```bash
  objdump -d /root/rootfs/usr/bin/mediacenterd > /tmp/mc.asm
  readelf -sW /root/rootfs/usr/bin/mediacenterd | grep -iE 'system|popen|exec'
  strings -a /root/rootfs/usr/bin/mediacenterd | grep -iE 'ffprobe|ffmpeg|sh -c|%s'
  # 重点看有没有 "sh -c" + 元数据字段拼进同一个 argv
  ```
- **代价**：2–4 小时（stripped 二进制）
- **若命中**：观测手段 = 把带恶意元数据的媒体文件放进 `/pool0/video/`，等索引/转码流水线处理，
  然后**在池内写标记文件**（如 `/pool0/data/Docker/_tag`）作为证据（不依赖反连）
- ⚠️ **需要触发（放入媒体文件让流水线跑）⇒ 属于改设备状态，先报你**

### 🥈 第 2 条：验证 `share_point`/`share_dir` 换行 → `smb.conf` 注入（**需实测一次**）🟡
- **理由**：sink 已确证（`sed` 注入 + `preexec` root 执行），上游只差"能不能把换行写进 `sambashare` UCI"
- **确切操作序列**（**每一步都要回读**）：
  1. 先只读确认现状：`netshare/get_samba_account`（现存账号/`share_dir`）
  2. 创建**一个我们自己的** Samba 账号，`share_dir` 里带 `\n` + `preexec` 载荷，指向一个**我们自己的**空目录
     （例如 `/home/u3943892/pool0/data/Docker`）——**不碰用户目录**
  3. 回读 `/var/etc/smb.conf` 是否出现注入行。**但 `/var/etc` 不在可写面内、我们也读不到** 🔴
     ⇒ 替代观测：注入 `preexec = touch /home/u3943892/pool0/data/Docker/_tag`，
       然后看 `_tag` 是否出现（Samba 连接时会执行 `preexec`）
  4. 清理：删除我们建的账号/共享
- **代价**：30–60 分钟
- ⚠️ **明确风险**：会创建/删除 Samba 账号与共享（**可回滚、不碰用户数据**），
  但 `preexec` 一旦生效会**以 root 执行我们的命令** —— 如果成功就是**终点**
- **需要你批准**（这是"改设备状态"，且涉及 root 执行）

### 🥈 第 2 条的实测结果（**已执行，结论：此路不通**）🔴

用户批准后做了实测。**结论：`smb.conf` 的注入 sink 是真的，但"把换行送进目录名"在全部可写通道上都被拒绝。**

**① 注入点精确定位**（🟢 静态确证）：

```lua
-- NasSamba.lua:138-144
local dirname = path:match("([^/\\]+)$")     -- ★ 取 share_dir 的最后一段（basename）
local mid = user:gsub("^u+", "")
local dirname_ = dirname:sub(1, 30)
local share_point = dirname_ .. "-" .. mid
NasCommon.waitExec("nas","call","smb_mgr.sh","shares","add_dir", user_, path, account, share_point, user)
```
```sh
# smb_mgr.sh:441-488  nas_add_smb_share_dir
uci -q batch <<-EOF
    set sambashare.$share_name.name="$share_point"      # ★ 换行落进 UCI
    set sambashare.$share_name.path="$path"
EOF
# smb_mgr.sh:289-323  _smb_add_share
config_get name $1 name
echo -e "\n[$name]\n\tpath = \"${path}\"" >>${VAR_SAMBA_CONF}   # ★ echo -e 解释换行 → /var/etc/smb.conf
```
⇒ 换行若能到达 `name`，就能在 `smb.conf` 里注入 **`preexec` / `root preexec`（Samba 以 root 执行）**。

**② 但换行必须存在于"目录名"里，而全部写入通道都拒绝控制字符：**

| # | 尝试 | 结果 | 原始响应 |
| --- | --- | --- | --- |
| A | WebDAV `PUT /pool0/data/Docker/dsh_nlA%0Apreexec...` | ❌ | **HTTP 500** `{"errno":2,"message":"No such file or directory"}`（服务端解码 `%0A` 后找不到该真实路径）|
| B | 直接送**原始 `\n` 字节** | ❌ | 客户端即拒：`InvalidURL: URL can't contain control characters ... (found at least '\n')` ⇒ **HTTP 层就不允许** |
| C | WebDAV `MKCOL .../dsh_nlC%0Apreexec...` | ❌ | **HTTP 409 Conflict** |
| D | `filemgr/create_dir` 名字含 `\n` | ❌ | `{"result":"无权限执行操作","code":1000}`（native `filemgrd` 经 D-Bus 拒绝）|
| E | `filemgr/move` 改名到含 `\n` 的名字 | ❌ | `{"result":"目的路径不存在","code":1106}`（**控制组同样报 1106**，该接口在本场景不可用）|
| F | `NasSamba` 的 `share_dir` 直接传含 `\n` | ❌ | `check_parms` 有 `fs.access(dirs[1])` 存在性校验（`:236-240`）⇒ 路径必须真实存在，而 A–E 都建不出这种路径 |

**③ 探测过程与安全**（🟢）：
- **没有创建任何 Samba 账号或共享**（这是本项目标的第一步，因前提未成立而未进入）
- 只在**我们自己的** `/pool0/data/Docker/` 下创建/删除探测文件，全部名字带 `dsh` 前缀
- 探测前/后快照**逐字节一致**：`Docker` 目录条目 `[]`；Samba 账号数 **1**，仍是出厂匿名账号
  （`username=""`、`id=1`、`status=1`、`share_dir=["/home/u3943892/pool0/data/我的照片"]`）；
  `Samba info: {name: "SmartStorage", SMBv1: false}`
- 证据：`artifacts/scratch/out/newline-direct.log`、`newline-direct-probe.json`、
  `create-dir-newline.log`、`newline-rename2.log`、`samba-snapshot-before.json`

⇒ 🔴 **第 2 条结论：`smb.conf` 注入 sink 真实存在（`echo -e` 不转义 + `preexec` root 执行），
但需要"目录名含换行"，而 WebDAV / filemgr / Samba API 三条写入通道全部拒绝 ⇒ 无法成套，此路封闭。**

### 🥇 第 1 条的实测结果（**已执行，结论：元数据 → shell 这条路封闭**）🔴

用户批准后做了反汇编。（🟡 坑：本机 `objdump` 报 `can't disassemble for architecture UNKNOWN`，
不支持 aarch64；改用 **`llvm-objdump-18`** 成功。）

**① 三个媒体守护进程的执行原语**（🟢 `readelf -sW`）：

| 二进制 | 体积 | `system` | `popen` | `execv*` | 结论 |
| --- | --- | --- | --- | --- | --- |
| `mediacenterd` | 329 880 | ❌ | ❌ | 🟢 `execvp` ×1 | 无 shell |
| `albumd` | 265 392 | ❌ | 🟢 **`popen`+`pclose`** | 🟢 `execvp` ×3 | **有 shell，逐一审** |
| `findexd` | 400 320 | ❌ | ❌ | ❌（只有 `fork`）| 无 shell |
| `libnas.so`（共享库，被上面几个调用）| — | 🟢 **`system`** | 🟢 **`popen`** | 🟢 `execv`/`execvp` | **重点** |

**② `albumd` 的 `popen` —— 本次唯一"看起来致命"的发现，但被输入源挡住**（🟢 逐条反汇编）：

```asm
; albumd @ 0xd5f8 附近，循环源是 nas_get_all_user() 返回的用户链表
d5e8:  add  x0, x23, #0x200     ; x23 = 0x31000 ⇒ 0x31200
d5ec:  str  x0, [sp]            ; 第 3 个参数 = 格式串
d5f8:  ldr  x4, [sp]
d5fc:  add  x20, x26, #0x8      ; x20 = user + 8  ← 用户名字段
d600:  mov  x5, x20             ; 第 4 个参数 = 用户名
d604:  mov  x3, #0x80           ; n = 128
d614:  bl   __snprintf_chk      ; snprintf(buf, 128, fmt, username)
d618:  add  x1, x25, #0x340     ; x25 = 0x32000 ⇒ 0x32340 = "r"
d620:  bl   popen@plt           ; popen(buf, "r")   ← 这里有 shell
```
- 模板 `0x31200` = **`uci get user.%s.status`**（🟢 逐字节读出）
- 运行身份：`album.service` → **`User=root`**（🟢）；`albumd` 未导入任何 `setuid/setgid`
⇒ **若 `%s` 可控，就是 root 命令注入。**

**但被输入源挡住了**（🟢）：
- `NasUserManager.lua:200-214` `setNickname` 写的是 `uci:set("user", userName, "nickname", nickname)`
  ⇒ **`nickname` 是 UCI 的 *选项值*，不是 section key**；命令里的 `%s` 是 **section key**
- section key 只由 `lib/nas/user/add_user:51` `uci -q set user."$user"=user` 建立，
  而 `$user` 来自 `miio_fcgi.lua`：`local user_name = "u" .. uid`（**云端下发的纯数字 uid**）
- 全盘枚举 `uci set user.<动态名>` 的全部写入点后，**没有任何一处把用户可控文本写进 section key**
⇒ 🔴 **`albumd` 的 `popen` 不可注入。**

**③ `libnas.so` 的 `system`/`popen`（6 处调用点，全部审完）**（🟢）：

| 调用点 | 命令 | 结论 |
| --- | --- | --- |
| `system@0x169e4` | **硬编码** `gdbus call --system --dest com.minas.msgcenter …` | 🟢 安全 |
| `system@0x184f8` / `0x18604` / `0x18640` / `0x1bb2c` | **硬编码** `xqled disk_fail > /dev/null 2>&1`、`xqled disk_ok …`、`xqled disk_format …` | 🟢 安全（纯常量）|
| `popen@0x16308`（在 `popen_exec_cmd` 内）| 见下 | 见下 |

**`popen_exec_cmd` 本体**（🟢 `0x16290`，关键三行）：
```asm
162f4:  bl  setreuid@plt        ; 参数 (0,0) —— 明确"保持 root"
16300/16304:  x1 = 0x23990 的 "r"
16308:  bl  popen@plt           ; popen(cmd, "r")
```
⇒ 🟢 **它有意以 root 执行命令**，故 cmd 的构造点必须逐个看。

**④ `popen_exec_cmd` 的 4 个调用点全部有"设备名白名单"保护**（🟢）：

命令模板（🟢 逐字节读出）：
```
0x24408  /usr/sbin/smartctl -A -j %s
0x244d0  /usr/sbin/smartctl -c -j %s
0x24620  /usr/sbin/smartctl -c -j /dev/%s
0x27a58  lsblk -ndo SERIAL /dev/%s
```
调用点：`0x10e00`、`0x112c0`、`0x11324`、`0x11658`（均在 `get_smart_health_by_dev` 一带）。

**保护机制**（🟢 反汇编确认）：
```asm
; nas_storage_smart_test @ 0x11b74
11b78:  bl  nas_list_blks@plt          ; ★ 先取真实块设备列表（解析 `lsblk` 的 JSON）
; nas_storage_cancel_smart_test @ 0x11d04  同
; nas_storage_disk_smart_info @ 0x1b848   同
; 然后按名字 strcmp 查表（查表函数 = 0x1a200 / nas_save_state+0x19e0）：
1a22c:  ldr  x0, [x19, #0x30]          ; 设备名
1a234:  bl   strcmp@plt
1a238:  cbnz w0, 0x1a224               ; 不匹配 → 继续遍历
1a250:  ... 返回 NULL ⇒ 调用方直接报错返回
```
🟢 字符串旁证：`libnas.so` 里有 `lsblk`、`parse lsblk json failed`、`parse lsblk error: no blockdevices`
⇒ 🟢 **命令里的设备名必须与 `lsblk` 报告的真实设备逐一 `strcmp` 相等** ⇒
`;`、`` ` ``、`$()`、`|`、空格 都不可能出现在真实设备名里 ⇒ **注入不了**。

**⑤ LuCI 侧还有一层与之一致的白名单**（🟢 `NasStorage.lua:983-985`）：
```lua
local function is_valid_block_device(dev)
    return type(dev) == "string" and dev:match("^[a-zA-Z0-9_%-]+$") ~= nil
end
```
（Lua 里 `%-` 是**字面 `-`**，不是字符范围，故字符集 = 字母+数字+`_`+`-`，**不含分号/空格/反引号/`$`** 🟢）
`formatDisk:995` 与 `getFormatStatus:1036` 都调用了它。

**⑥ 顺带发现一处"漏校验"，危险度中低**（🟡）：
`NasStorage.lua` 里 `jsonTable["dev"]` 共 6 个取值点，**只有 2 处紧跟 `is_valid_block_device`**：

| 行 | 函数 | 是否校验 | 去往 |
| --- | --- | --- | --- |
| 970 | `removeUsb` | ❌ | `nas storage removeusb <dev>` |
| 991 | `formatDisk` | ✅ | — |
| 1034 | `getFormatStatus` | ✅ | — |
| 1347 | `checkDisk` | ❌ | `/tmp/<dev>_smart_progress`（**文件路径拼接**）+ `nas storage smart <dev> …` |
| 1380 | `cancelDiskCheck` | ❌ | `nas storage cancel_smart <dev>` |
| 1404 | `getCheckStatus` | ❌ | `nas storage get_smart_progress <dev>` |

🟡 **但底层挡住了 shell**：`nas_storage_smart_test` / `cancel_smart_test` / `disk_smart_info` 都先
`nas_list_blks()` 查表，查不到即报错；`smart_test_progress` 走 `snprintf(buf,32,…)` + **`fopen`**（不是 shell）。
⇒ 🟡 **残余面**：`dev` 含 `/` 时能影响 `/tmp/<dev>_smart_progress` 这个**文件路径**（`fopen` / `io.open("w")`），
属**路径穿越 / 任意文件读的候选**，**但不是 shell 执行** ⇒ 列为下一个待查项，风险"中低"。

⇒ 🔴 **第 1 条结论：三个媒体守护进程里，`albumd` 的 `popen` 与 `libnas.so` 的 `popen_exec_cmd`
都以 root 运行 shell，但两处输入均被"服务端生成的用户名 / `lsblk` 真实设备白名单"锁住
⇒ 元数据（容器 title/artist/EXIF/字幕名）没有进入 shell 的路径 ⇒ 此路封闭。**

### 🥉 第 3 条：接受现状，回到 §4 的三条退路 🟢
- **理由**：第 1、2 条都是**赌"厂商漏了一处"**；而 §4 的官方支持（含 A/B 双槽筹码）是**最可能真正解决问题**的
- **确切操作**：把本文档 §2.10 与 §3.4 的结论补进 `docs/xiaomi-support-ticket.md`，然后提交工单

---

## 🟡 待查项（未追完，供未来接手）

> **状态**：已定位、已定性、**未实测**。**不是命令执行**，风险等级 **中低**。
> 触发需要构造含 `/` 的 `dev` 参数、并依赖 symlink / native filemgr 能力；成本较高、收益不确定。

### 待查项 1：`dev` 未校验 → `/tmp/<dev>_smart_progress` 的**文件路径拼接**

**① 取值点全清单**（🟢 `NasStorage.lua`，`jsonTable["dev"]` 共 6 处，**只有 2 处紧跟校验**）：

| 行 | 函数 | 有 `is_valid_block_device`？ | `dev` 的去向 |
| --- | --- | --- | --- |
| 970 | `removeUsb` | ❌ **无** | `nas storage removeusb <dev>` |
| 991 | `formatDisk` | ✅ 有 | （被挡）|
| 1034 | `getFormatStatus` | ✅ 有 | （被挡）|
| **1347** | **`checkDisk`** | ❌ **无** | ① `progress_file = "/tmp/" .. dev .. "_smart_progress"` → `io.open(progress_file,"w")`（**只建空文件**）<br>② `nas storage smart <dev> <mode> 0` |
| 1380 | `cancelDiskCheck` | ❌ **无** | `nas storage cancel_smart <dev>` |
| **1404** | **`getCheckStatus`** | ❌ **无** | `nas storage get_smart_progress <dev>` |

**② 为什么"命令执行"那层被挡住**（🟢 —— 这就是它风险只有"中低"的原因）：

- `nas_storage_smart_test`（`0x11b74`）、`nas_storage_cancel_smart_test`（`0x11d04`）、
  `nas_storage_disk_smart_info`（`0x1b848`）**都先调 `nas_list_blks()`**（解析 `lsblk` 的 JSON 得到真实块设备列表），
  再用 `strcmp` 逐个查表（查表函数 `0x1a200`，遍历 `[x19+0x30]` 的 devname），
  **查不到即返回 NULL、调用方直接报错** ⇒ 含 shell 元字符的伪设备名**进不到 `popen`**。
- 🟢 字符串旁证：`lsblk`、`parse lsblk json failed`、`parse lsblk error: no blockdevices`。
- `nas_storage_smart_test_progress`（`0x11930`）走 `snprintf(buf, 32, …%s…, dev)` + **`fopen`**
  —— **不是 shell** ⇒ 没有命令注入。

**③ 残余面到底是什么**（🟡 推测，需实测）：

`checkDisk` / `getCheckStatus` 都不校验 `dev`，而 `dev` 会被**字符串拼接**成文件路径：
```
"/tmp/"  +  <dev>  +  "_smart_progress"
```
若 `dev` 含 `/` 与 `..`，该路径可**离开 `/tmp`**，例：
```
dev = "../../etc/shadow"   →   /tmp/../../etc/shadow_smart_progress
```
⇒ 两个候选方向：
- **任意文件读（候选）**：`getCheckStatus` → `nas storage get_smart_progress <dev>`
  → `nas_storage_smart_test_progress` 用 `fopen(path,"r")` 打开并 `fgets` 一行返回。
- **任意文件写（候选，容量有限）**：`checkDisk` 先 `os.remove(progress_file)`，再 `io.open(progress_file,"w")` 建**空文件**；
  之后 `nas storage smart` 在自检过程中会把进度写进去。
  ⚠️ **`_smart_progress` 这 15 字符后缀无法去掉** ⇒ 只能命中"本身以 `_smart_progress` 结尾"的路径，
  或依赖 **symlink** 跳过中间层。

**④ 静态可验证的下一步**（无需动设备）：

1. **确认 `nas_list_blks` 的过滤范围**：读 `libnas.so` 里 `nas_list_blks` 的实现，
   看它是否**只接受 `/dev/` 开头的对象**、是否对 `devname` 做字符过滤；
   若它只挡 popen 那条路、不挡 `fopen` 那条路 ⇒ "任意文件读"候选成立。
   ```bash
   readelf -sW /root/rootfs/usr/lib/libnas.so | grep nas_list_blks     # 取地址
   llvm-objdump-18 -d --no-show-raw-insn /root/rootfs/usr/lib/libnas.so > /tmp/libnas.asm
   # 在该函数体内找 strncmp / strchr 之类对 "/dev/" 的判断
   ```
2. **确认 native 层是否补了格式校验**：lua 层没有，`module/storage.lua` 的 `get_check_status` 入参是否在 C++ 侧被校验。
3. **确认能否造出含 `/` 的 `<dev>` 或 symlink**：
   - `NasFileOps.lua` 全文**没有文件名字符合法性检查函数**（已 grep 确认）；
     但 `filemgr/create_dir` / `filemgr/move` 走 **D-Bus → native `filemgrd`**，真实校验在 C++ 侧 ⇒ **需实测**
     （方法论同 🥈：只用"我们自己的目录"、名字带 `dsh` 前缀、前后快照对比、用完即删）。
   - ⚠️ **任何实测都必须先报批**（属改设备状态）。
4. **若 1–3 全部成立** ⇒ 得到的是"**以 root 读/建一个受后缀约束的路径**"的原语，
   价值**远低于命令执行** ⇒ 建议**仅在其它路径都无望时再投入**。

### 待查项 2（更次要）：`removeUsb` 的 `dev` 数组无校验

`NasStorage.lua:970` 的 `devs` 未校验即逐个传给 `nas storage removeusb <dev>`（`0x211e0`）。
🟡 未反汇编该函数确认它是否也走 `nas_list_blks` 查表；若不走，则是与待查项 1 同类的候选。
**优先级低于待查项 1。**

---

## 平行审计（variant analysis）：补丁自身的边界 🟡

> 目标：检查 `transcode_404_handler.sh` 那套"路径逃逸防护"是否**只守一条路**，以及是否存在**同范式的兄弟**。

### V1. 补丁本体的边界：`realpath -sm` 是**字面归一化** 🟢

`/usr/bin/transcode_404_handler.sh:58`：
```sh
LOGICAL_PATH=$(realpath -sm "$REQUEST_PATH" 2>/dev/null) || deny_path
case "$LOGICAL_PATH" in
    "$USER_ROOT"/*) ;;          # USER_ROOT="/home/$USER_NAME"
    *) deny_path ;;
esac
```
- 🟢 **GNU realpath 的 `-s` = `--no-symlinks`（不展开符号链接）**，`-m` = `--canonicalize-missing`（允许路径不存在）
  ⇒ **它是纯字面归一化**（只处理 `..`/`.`/重复斜杠），**不解析 symlink**
- ⇒ 🟢 **若池内存在指向 `/etc` 之类的 symlink，`-sm` 不会发现**；
  而随后的 `[ -f "$REQUEST_PATH" ]`、`stat -c%s`、`readlink`、**`tail`/`cat` 都会跟随 symlink**（内核行为）
  ⇒ 🟡 **原理上是一条绕过** —— 但**前提是能把 symlink 写进池子**（见 V4，目前造不出来）

### V2. 这道校验**只挂在一条路上** 🟢（典型补漏）

| 出口 | 是否有"路径必须落在 `/home/u*/` 内"的校验 |
| --- | --- |
| `@transcode_404`（**只由主 location 的 `error_page 404` 触发**，`webdav.conf:84`）| ✅ **有**（`:49-62`）|
| WebDAV 主 location（PUT/MKCOL/MOVE/COPY/PROPFIND/DELETE）| ⚠️ **只有前缀白名单**（`:36` `^/((pool\|usb)[^/]*)`）＋ `:42` 一级目录存在性，**没有 `realpath` 归一化** |
| **`location /home/ { internal; alias /home/; }`** | ❌ **完全没有**（见 V3）|

⇒ 🟢 **`error_page 404` 只覆盖"产生了 404 的请求"**，而**成功的 PUT/MKCOL/PROPFIND 根本不会进 404 分支**
⇒ 🟢 **补丁只加固了"转码兜底"这一条，其余出口没有同等校验。**

### V3. ★ 发现一个**没有白名单的 `/home/` 出口** 🟡

`usr/sbin/nas_cgi.init:19-36` 在开机时**写文件 + 建软链**：
```sh
cat <<'EOF' >/etc/nginx/conf.d/luci/file.conf
location /home/ { internal; alias /home/; ... }
EOF
ln -sf /etc/nginx/sites-available/luci.conf        /etc/nginx/sites-enabled/luci.conf
ln -sf /etc/nginx/sites-available/secure_link.conf /etc/nginx/sites-enabled/secure_link.conf
```
- ⇒ **`/home/` 整个目录通过 `internal` 出口对外可达**（经 `X-Accel-Redirect`），**没有任何路径白名单**
- `secure_link.conf` 另在 **`listen 8081`** 上再暴露同一个 `/home/` 出口
- 🟢 全盘只有**一个** `X-Accel-Redirect` 使用者：`module/filemgr.lua:584`
  ```lua
  local file_stat = fs.lstat(path)
  if not file_stat then return { code = 1105 } end
  if file_stat.type ~= "reg" then return { code = 1003 } end
  http.header("X-Accel-Redirect", path)          -- ★ path 完全来自用户输入
  ```
  ⇒ **`filemgr/get_file` 看起来正是那条"任意文件读"的路**

**🔴 但实测否定了它**（🟢 只读验证，见下）：
```
POST /cgi-bin/luci/filemgr/get_file  {"path":"/etc/os-release"}   -> 200 {"result":"参数错误","code":1003}
POST /cgi-bin/luci/filemgr/get_file  {"path":"/etc"}               -> 200 {"result":"参数错误","code":1003}
POST /cgi-bin/luci/filemgr/get_file  {"path":"/etc/definitely_absent"} -> 200 {"code":1105}（源文件不存在）
```
⇒ 🟢 **对普通文件也返回 `1003`（`file_stat.type ~= "reg"` 判定失败）** ⇒ **永远到不了 `X-Accel-Redirect`**
⇒ 🔴 **`filemgr/get_file` 是一条"写坏了/失效"的代码路径，不构成任意文件读**（原因需在实机上进一步确认 lstat 的 type 取值）

**🟡 但意外得到一个信息泄露原语**：`1105`（不存在）与 `1003`（存在）**可区分**
⇒ **任意路径的存在性预言机**（可探测设备上有哪些文件/目录，例如确认某个配置、某个插件目录、某个密钥文件是否存在）。

### V4. 全盘"用户/池内可控字符串 → shell"的范式扫描结论 🟢

| 类别 | 结论 | 证据 |
| --- | --- | --- |
| `/etc/syshotplug/**`（87 handler）| 🟢 传入的 `$ACTION`/`$DEVNAME`/`$DEVICE`/`$MOUNTPATH` 来自 udev 与内部调用，**无用户可控文本**；卷标只进 `mkfs --label`（argv）| `non-api-path-audit.md` 优先项 2 |
| `*_handler.sh` / CGI 脚本 | 🟢 仅 2 个：`transcode_404_handler.sh`（已加固）、`/etc/webdav/delete_to_trash.sh`（**未加固但有 `internal` + 无 rewrite ⇒ 不可达**，见 V5）| 见 V5 |
| `tar`/`unzip`/`ffmpeg`/`rsync` argv 拼接 | 🟢 `zip`/`unzip`/`move`/`copy`/`remove` 全部经 **D-Bus → native `filemgrd`**，argv 由 C++ 侧构造 | `NasFileOps.lua:272-450` |
| `source`/`.` | 🟢 只有 `. /lib/nas/library.sh`（固定只读路径）| 优先项 1.1 |
| 未加引号变量 / 命令替换 / `eval` | 🟢 未发现把外部值送进 shell 的位置 | 优先项 1 的 9 类扫描 |

### V5. ★ `delete_to_trash.sh`：**危险的死路径** 🟡

`/etc/webdav/delete_to_trash.sh`（🟢 全文 28 行）：
```sh
user=$(echo "$REQUEST_URI" | cut -d'/' -f2)
path=$(echo "$REQUEST_URI" | cut -d'/' -f3-)
delete_path="/home/$user/$path"
[ ! -e "$delete_path" ] && { ...exit 1; }
rm -rf "$delete_path"          # ★ 以 root 删除，path 未做任何归一化
```
- 🟢 **`path` 未经 `realpath`/`..` 过滤** ⇒ 若可达，`REQUEST_URI=/u3943892/../../etc/config` **→ `rm -rf` 越出用户目录**
- 🟢 **但当前不可达**：
  - `webdav.conf:64-66`：唯一的 rewrite 被**注释掉**（`#if ($request_method = DELETE) { # rewrite ^ /delete_to_trash; #}`）
  - `webdav_server`（5000 端口实际加载的配置）**根本不含** `location = /delete_to_trash`（它只在 `webdav_share_server.template` 里，而该文件**不在 sites-enabled**）
  - `location = /delete_to_trash { internal; }` ⇒ 即使存在，也只能经内部重定向到达
- ⇒ 🟡 **结论：目前是一条"危险的死代码"**。价值在于：**若厂商某天取消那段注释、或启用 share server，它立刻变成"root 任意路径删除"** ⇒ 值得记为**潜在高危**。
- 🟡 顺带：脚本 `:3-4` 把 `date` 输出与 `env` 全量写进 `/tmp/trash`（**调试残留**），会泄露环境变量到可写目录。

### V6. 用 `realpath`/`readlink` 做逃逸防护的**其它**位置 🟢

| 位置 | 用法 | 是否解析 symlink | 评价 |
| --- | --- | --- | --- |
| `transcode_404_handler.sh:58` | `realpath -sm` | ❌ **不解析** | 🟡 见 V1（但需要先能造 symlink）|
| `plugin.sh:23,37,69,78` | `readlink -f` | ✅ **完全解析** | 🟢 正确用法（用于比较"可写目录是否在插件目录内"）|
| `lib/nas/library.sh:158` `make_symlink` | 建软链 | — | 🟢 内部固定目标 |
| `smb_mgr.sh:112` | `ln -sf` | — | 🟢 目标由 `share_name` 派生（见下）|

⇒ 🟢 **只有 `transcode_404_handler.sh` 用了"不解析 symlink"的变体**；
`plugin.sh` 用的是正确的 `readlink -f`。⇒ 🟡 **不一致的加固风格**（与 V2 一致：补丁是局部热修，未统一）。

### V7. `smb_mgr.sh:112` 的 `ln -sf`（🟡 需留意）

```sh
# smb_mgr.sh:88-90  src_path 由调用方给出，取两级父目录 + /samba
samba_dir="$(dirname "$(dirname "$src_path")")/samba"
symlink_path="$samba_dir/$user/$share_name"
target_path="/nas/$poolname/$username/data/$share_name"
ln -sf "$target_path" "$symlink_path"
```
🟡 `share_name` 由 `NasSamba` 的 `dirname` 派生（同 🥈 的可控面）。
但 `ln -sf` 的**目标**是内部构造的 `/nas/...`，且 NFS/Samba 侧无 shell ⇒ **风险低**；
只存在"**往非预期位置写一个符号链接**"的可能（属于文件系统操作，非执行）。

---

## 🟢 附带实证：卸载/升级**未做完整清理**

🟢 **只读实证**（存在性探测，未读取内容）：本机存在多处**实例早已不存在、但文件仍残留**的情况，例如
**`/etc/config/ipc@42661`** 与 **`/etc/nginx/modules-enabled/rtsp/rtsp_42661.conf`** ——
对应的 `ipc@42661` 实例已不存在（其 `uid` 已无家目录），但这两份配置仍在设备上；
另有 `/home/u3943892/pool0/ipc`（**空目录**，创建于 2026-10-04 后再未被写入）等同类残留。

⇒ 中性结论：**应用卸载/固件升级流程没有把"实例已消失"的遗留配置一并清掉**；
这些残留**不影响数据安全**，但说明"清理"这一步不完整（可作为厂商侧改进项）。
🔴 **这些文件的内容（含其中的 token 字段）本轮未读取**；本文也不对其归属作任何推测。

---

## 本轮约束遵守

- **纯静态分析**：只读 `/root/rootfs/` 里的文件，**未对设备发起任何请求**
- 未改设备、未 commit、未动用户数据
- 证据文件：
  - `artifacts/scratch/out/source-audit-writable-config.txt`（优先项 1 的 9 类扫描）
  - `artifacts/scratch/out/source-audit-part2.txt`（优先项 2/3/4/5/6 的 728 行原始输出）
