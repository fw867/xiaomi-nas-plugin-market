# 网络邻居（netneighbor）

解决两个日常痛点：

1. **Windows 资源管理器「网络」里看不到小米存储**，每次只能手输 `\\192.168.1.8`；
2. **共享 app 里一个账号只能配一个共享目录**，想再共享一个文件夹没有入口。

两件事都不是硬件或内核的限制：第一件是系统自带的发现服务（`/usr/bin/wsdd`）发的报文
少了一个必需字段、元数据接口也只认 GET；第二件更简单——底层本来就支持一个账号挂多个
目录，只是 app 的界面不给。本插件把这两处补齐。

## 一、为什么自己实现 WSD 回应器

Windows 的「网络」靠 **WSD（WS-Discovery）** 找设备。系统里的 `/usr/bin/wsdd` 看起来在
正常工作：能回答 Probe、启动时发 Hello、停止时发 Bye，`ss` 也能看到它监听 3702。但它有
三处致命缺口，**任何一个都会让 Windows 完全无视这台主机**（2026-09-28 在 NAS 上与上游
[wsdd](https://github.com/christgau/wsdd) 做字节级对比实测得出）：

| # | 缺口 | 实测现象 |
| --- | --- | --- |
| 1 | 消息**没有 `wsd:AppSequence` 头** | WS-Discovery 规定 Hello / Bye / ProbeMatches / ResolveMatches 必须带它。缺了之后 Windows 收到 ProbeMatch 也**整条丢掉**，连设备描述都不来取（抓包可见：只有我们的 Probe 在发，NAS 侧收不到任何 HTTP 请求） |
| 2 | 元数据接口**不响应 `WS-Transfer Get`（SOAP POST）** | `curl -X POST http://192.168.1.8:35169/wsd/` → `(52) Empty reply from server`；而同一地址 `curl`（GET）返回 `200`、2175 字节。Windows 取描述用的正是那个 POST |
| 3 | 描述响应**没有 `wsa:RelatesTo`** | 补上 1 之后 Windows 会来取元数据（服务端能看到 `POST … action=Get`），但缺了这条关联 ID 它会丢弃整份描述——表现为“日志里明明 200 了，主机还是不出现” |

补齐三条之后，本机 Windows 的资源管理器「网络」里立刻出现 `SMARTSTORAGE`，双击可直接
进入 SMB 共享；发 `Bye` 时它会消失、再 `Hello` 又回来（对应插件的停止/启动路径）。

所以 `wsd.py` 自己实现了完整的服务端一侧：

- 组播监听 `3702`：`Probe` → `ProbeMatches`，`Resolve` → `ResolveMatches`；
- 主动通告：启动 `Hello`（发两遍，UDP 会丢）、退出 `Bye`、每 `WSD_HELLO_INTERVAL`
  秒（默认 900）重发一次；
- 元数据 HTTP 服务（默认 `5357`）：普通 GET 与 `WS-Transfer Get` 都返回 `wsx:Metadata`，
  并按请求回填 `wsa:RelatesTo`；
- 身份 `urn:uuid` 落在 `<DATA_DIR>/wsd.json`，重启不变——否则 Windows 里会多出幽灵设备；
- 细节：响应**从 3702 端口发出**；发送前用 `IP_MULTICAST_IF` 把组播出口钉在局域网网卡上
  （不钉的话可能从 `docker0` 出去，一台机器都看不到，NAS 上实测踩过）。

## 二、为什么接管官方 wsdd 用 drop-in 而不是 `systemctl mask`

两个发现服务会抢 3702，必须让官方那个停下来。但**不能用 `mask`**：

官方的 `/usr/bin/smb_mgr.sh`（共享 app 的后端）每次保存共享，最后一步都会
`systemctl restart wsdd`。`mask` 之后这一步必然失败，整条命令返回 1——用户在共享 app 里
就会看到「保存失败」，尽管 `smb.conf` 其实已经写好了。这是可见的功能回归。

改成写一个空操作的 drop-in（只动我们自己的那个文件，不碰 `/usr/lib` 下的原单元）：

```ini
# /etc/systemd/system/wsdd.service.d/netneighbor.conf
[Service]
ExecStart=
ExecStart=/bin/true
ExecStop=
ExecStop=/bin/true
ExecReload=
ExecReload=/bin/true
RemainAfterExit=yes
```

这样 `systemctl restart wsdd` 依旧返回 0，而 3702 被真正释放。接管顺序是
`stop wsdd` → `pkill -x wsdd` 兜底 → 写 drop-in → `daemon-reload` → 自检 3702 是否真的
空出来（占用时日志会打印持有进程，便于排查）。

**卸载/停用时还原**：删掉 drop-in → `daemon-reload` → `systemctl start wsdd`。
服务单元里还有一条 `ExecStopPost=… server.py --restore-wsdd`，作为进程被 `kill -9`
时的第二道保险。两步都是幂等的。

## 三、一个账号多个共享目录

小米共享 app 的数据源是 UCI：

```
/etc/config/sambauser    # 每个账号一段：option user 'u3943892'（NAS 用户）
                         #                 option name 'fw867'（账号）
                         #                 list dirs '<目录>'（本来就是列表）
/etc/config/sambashare   # 每个共享一段：option name / path / list users 'samba<账号>'
/etc/samba/users.map     # samba<账号> = <账号>
```

底层一直是「一个账号 → 多个 dirs → 多个 sambashare 段」，只是 app 只写一个。官方
`/usr/bin/smb_mgr.sh` 也留了命令：

```
smb_mgr.sh shares add_dir <share_name> <path> "<账号>" <共享名> [force_user]
smb_mgr.sh shares del_dir <share_name>
```

实测（同一账号 `fw867`，SMB 用户 `sambafw867`）：

```
$ smb_mgr.sh shares add_dir u3943892_nb_1 '/home/u3943892/pool0/data/我的照片' 'fw867' '照片-3943892' 'u3943892'
$ testparm -s | grep -A3 照片-3943892
[照片-3943892]  path = "/home/u3943892/pool0/data/我的照片"  valid users = sambafw867
$ net view \\192.168.1.8          # 同一账号下出现两个共享
  ���-3943892  Disk   u3943892_1 share directory
  ��Ƭ-3943892  Disk   u3943892_nb_1 share directory
```

两个容易踩的坑（都已实测，别再改回去）：

- `<user_list>` 要传**账号名**（`fw867`），`smb_mgr.sh` 内部自己加 `samba` 前缀；
  传 `sambafw867` 会在 `pdbedit` 那步报 `Username not found!` 并以 1 退出。
- **不要调用 `smb_mgr.sh reload`/`restart`**：它们最后一步是 `systemctl restart wsdd`，
  在接管之后（哪怕用 drop-in）也可能拿到非 0，让人误判失败。插件改成自己走
  `smb_mgr.sh init_config` + `systemctl reload smb nmb`，然后**回读 `/etc/config/sambashare`
  与 `/var/etc/smb.conf` 确认共享真的写进去了**，以此判定成功。

插件自建的共享用独立命名空间 `<账号>_nb_<序号>`（`SHARE_PREFIX = '_nb_'`），只允许增删
自己建的：app 生成的 `<账号>_<id>` 与 `public` 等受保护共享不会被插件碰。

### 外接设备：`/nas/mnt` 下的目录也能共享

`/nas/mnt` 下面是各种挂载点（`usb` 是外接设备 U 盘的稳定路径，NAS 上的服务把它 bind 到
厂商自己的 `/mnt/usb-<哈希>`；还有 `pa0`/`pa1` 之类）。插件默认把 `/nas/mnt` 作为
**追加**的允许根（环境变量 `EXTRA_ROOTS`，冒号分隔多个、支持单层通配如
`/mnt/usb-*`），所以 `/nas/mnt/usb` 及其下面的任意子目录都能通过校验并被共享：

```
最终允许根 = （ALLOWED_ROOTS 有则单独用它，否则 FALLBACK_ROOTS + 按配置派生）+ EXTRA_ROOTS
```

两点约定：

- `ALLOWED_ROOTS` 仍然是**显式覆盖**语义（给了就只用它），`EXTRA_ROOTS` 只是追加；
- 追加根**只列当前真实存在的**：拔盘后 `/nas/mnt/usb` 不存在，就不会出现在白名单里；
  `allowed_roots()` 的缓存把「当前的追加根列表」也算进有效期，插上/拔掉 U 盘下一次调用
  就能反映出来（不必等缓存过期）。已共享的 U 盘目录在拔盘后仍然保留在配置里（不报错），
  只是弹窗里那个位置显示「未接入」。

`EXTRA_ROOTS` 的三种取值：

| 取值 | 行为 |
| --- | --- |
| 未设置 | 默认 `/nas/mnt`（开箱即可共享外接设备里的目录） |
| **显式空串**（`EXTRA_ROOTS=`） | **一个都不追加**：`ALLOWED_ROOTS` 就是严格的最终白名单，运维可用它锁死可共享范围 |
| 设了值 | 冒号分隔追加，支持单层通配（如 `EXTRA_ROOTS=/nas/mnt:/mnt/usb-*`） |

「添加共享」弹窗里可以逐层浏览（位置：存储池 / 外接存储），也可以直接**手填绝对路径**
（如 `/nas/mnt/usb/下载`）；服务端照旧校验绝对路径、根白名单、`..` 与符号链接的真实路径。

## 四、接口与页面

插件服务监听 `127.0.0.1:18190`，页面经 nginx 走 `/plugin/<用户>/netneighbor/`。

页面只有三块：**网络发现开关 + 主机名**、**共享目录**、**目录浏览器弹窗**。
共享目录分上下两块：上部是「添加共享」按钮（打开目录浏览器选目录），下部是**已共享的目录**
列表（每行：显示名 + 共享名 `<账号>_nb_<序号>` + 完整路径 + 行尾 `-` 删除按钮，删前二次确认；
官方 app 建的共享照旧显示，但 `-` 置灰并说明原因）。

目录浏览器与 transmission 插件同形：顶部**位置切换**（存储池 / 外接存储）、当前**完整绝对路径**、
逐层进入子目录、「上一级」、「选择此目录」；也可以直接手填绝对路径（`/nas/mnt` 下的目录，
如 `/nas/mnt/usb`）。拔盘时该位置显示「未接入」并禁止进入。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/healthz` | `{"ok": true, "version": "0.1.0"}` |
| GET | `/api/status` | 设置（开关/主机名）、发现服务状态、账号与共享列表；工作组、XAddrs、官方 wsdd 状态、最近取元数据时间仍在返回里（便于排查），页面不显示 |
| GET | `/api/log` | 最近日志 |
| GET | `/api/browse?account=fw867&root=0&path=照片` | 目录浏览器：`root` 是位置序号（0 = 存储池 = 账号数据根，其余 = `EXTRA_ROOTS`，默认 `/nas/mnt` 显示为「外接存储」），`path` 是**相对该位置**的路径（空 = 位置根目录）。返回 `locations`（`{index,label,path,exists}`）、`path`/`absolute`（当前目录）与 `items`（一层子目录：`{name,path,absolute,shared}`，跳过隐藏目录与符号链接）。越界（`..`/绝对路径）、位置无效、目录不存在都返回 400 + 中文原因 |
| GET | `/api/dirs?account=fw867` | 老接口（保留行为与结构）：账号数据根目录下的子目录；每项带 `shared`、`shareName`/`display` 与 `deletable`，另有 `locations` 分组（`{label, root, available, dirs}`） |
| POST | `/api/discovery` | `{"enabled": true\|false}`：开 = 接管官方 wsdd + 起回应器；关 = 发 Bye + 还原官方 wsdd（幂等） |
| POST | `/api/hostname` | `{"hostname": "..."}`：校验 → 落盘 → 重建回应器并重发 Hello；`{"reset": true}` 为**恢复默认**（清掉落盘值，回到 `/etc/config/samba` 的 `option name`，页面上是「恢复默认」按钮） |
| POST | `/api/share/add` | `{"account","path","sharePoint"}`（单个，目录浏览器提交的就是**绝对路径**；兼容旧版）或 `{"account","paths":[...]}`（多选；所有 `add_dir` 之后只跑一次 `init_config` + reload） |
| POST | `/api/share/delete` | 删除插件自建的共享：`{"shareName": "<账号>_nb_<序号>"}`（单个，返回结构同旧版；页面上行尾 `-` 用的就是它）或 `{"shareNames": [...]}`（批量；所有 `del_dir` 之后只跑一次 `init_config` + reload，返回 `removed/verified/errors/initReturncode/reloadReturncode`）。**含受保护共享（`public` 等）时整批拒绝、一条命令都不跑** |
| POST | `/api/detect/restart` | 重启回应器并重发 Hello（排查用，页面上没有按钮） |

写操作需要会话令牌 + `X-CSRF-Token`（与仓库其它插件一致）。页面由**插件服务**发出
（nginx 里是 `proxy_pass`，不是静态 `alias`）：`index.html` 里的 `__SESSION_TOKEN__` /
`__CSRF_TOKEN__` / `__PLUGIN_VERSION__` 占位符只有服务端会替换，改成 alias 会让页面拿到
空令牌、所有接口 401。

前端不写浏览器自动化测试，但有两条静态兜底：`web/app.js` 里引用的每个 id 都要在
`web/index.html` 里存在（`scripts/check_web_ids.py`），关键元素/交互钩子由
`tests/test_web.py` 断言：

```bash
python3 scripts/check_web_ids.py
node --check web/app.js
python3 -m unittest tests.test_web -v
```

### 主机名：改的是 Windows「网络」里显示的名字

页面上的主机名保存后写进 `<DATA_DIR>/settings.json`（`settings['hostname']`），
优先级是 `settings['hostname']` > `/etc/config/samba` 的 `option name` > `SmartStorage`。
**这里改的只是 WSD 宣告的名字**（Windows 资源管理器「网络」里显示的那一个），
SMB 服务名仍然由官方共享 app 的配置决定，改这里不会动 `smb.conf` 里的 `netbios name`。

校验：长度 1–15，`[A-Za-z0-9][A-Za-z0-9._-]*`（NetBIOS 友好；对外宣告时按
`wsd.py` 现有实现大写化）；非法输入返回 400 + 中文原因。

「恢复默认」按钮 = `POST /api/hostname {"reset": true}`：把落盘的 `hostname` 清空，名字
回退到系统配置里的那个（`/etc/config/samba` 的 `option name`，读不到才是内置 `SmartStorage`），
同样会重建回应器并重发 Hello。注意 `/api/status` 的 `settings.hostname` 给的是**生效**的名字，
「是否被插件改过」看 `<DATA_DIR>/settings.json` 里的值（清空后是空串）。

「网络发现」开关的状态同样落盘在 `settings.json`：关闭后服务重启**不会**自动接管，
直到用户在页面上重新打开。

## 五、安装后的行为

- 服务：`xiaomi-netneighbor.service`（`/data/plugin/netneighbor/current/server.py`），
  数据目录 `/data/plugin/netneighbor/data`（设置落盘在 `data/settings.json`）；
- 「网络发现」开关默认打开（保持升级前的行为），关掉后停用/重启都不会再接管；
  停用/卸载时一定还原官方 wsdd；
- 需要 root：要执行 `systemctl`、写 UCI、调用 `smb_mgr.sh`。单元里用
  `ProtectSystem=full` + `ReadWritePaths=/etc/config /etc/systemd/system /data/plugin/netneighbor`
  把可写范围收到最小。

## 六、测试

```bash
cd projects/xiaomi-netneighbor-plugin
python3 -m unittest discover -s tests -v
```

不依赖第三方包，也不会真的去动系统：`subprocess`、`systemctl`、`smb_mgr.sh` 与
`WsdResponder` 全部可注入，测试用假 runner 断言命令序列。

## 七、真机实测记录（2026-09-28，RP05 / 192.168.1.8）

- **发现服务**：插件启动后官方 wsdd 被停掉（drop-in 生效），3702 由插件进程持有；
  Windows 资源管理器「网络」里出现 `SMARTSTORAGE`，插件日志能看到
  `收到 Probe` → `wsd http: POST … action=Get → 200`，`metadataPosts` 递增。
  发 `Bye` 后图标消失、再 `Hello` 又回来。
- **`systemctl restart` 的竞态**：停服时 `ExecStopPost --restore-wsdd` 会把官方 wsdd
  拉回来，新实例的接管会再把它停掉（停完自检 3702、必要时重试一次），全程无需人工干预
  —— 这条正是实测踩出来的：最初 `stop` 那步漏了 `systemctl` 前缀（日志里是
  「找不到命令 stop」），官方进程一直占着 3702，回应器永远起不来。
- **共享管理**：`POST /api/share/add` 给账号 `fw867` 加
  `/home/u3943892/pool0/data/我的照片` → 生成 `fw867_nb_1`，
  返回里 `verified / inConfig / smbActive` 均为 `true`；
  `net view \\192.168.1.8` 同一账号下出现两个共享，
  `\\192.168.1.8\照片-3943892` 能直接列出文件；
  `POST /api/share/delete` 删掉后配置恢复原样，而删官方那条会被拒绝
  （「由系统或小米 App 管理」）。
- 这台 NAS **没有 `pkill`**，只有 `killall`，所以兜底清理是按
  `killall`/`pkill` 的顺序探测实际存在的那个。

## 八、已知限制

- 只管理**插件自建**的共享；app 里那个 `<账号>_<id>` 仍由官方界面管。
- 插件新增的目录不会出现在共享 app 的界面上（它只认自己写的那一条），但 SMB 里正常可用。
- **刚新增的共享在 Windows 里可能要等一下才能打开**：`smb.conf` 是即时生效的
  （`testparm` 立刻能看到那一段），但 SMB 客户端会缓存共享列表与已建立的会话，刚点完
  「确定」就双击新共享可能报「找不到网络路径 / 无法访问」；重开一次资源管理器窗口、
  或断开重连（`net use \\<NAS-IP> /delete`）之后就正常了。2026-09-28 用户实测确认过这个现象，
  页面添加成功后也会提示一句「Windows 里可能要重开资源管理器才看得到」。
- **`list dirs` 与共享段不同步**：`sambauser` 的 `list dirs` 存的是目录路径，
  `sambashare` 才是共享段。若某目录只在 `list dirs` 里（例如共享段被删掉），页面会把它
  显示成「未共享」，此时可以用插件重新共享它；反过来，插件不会去动 `list dirs`，
  所以官方界面上看不到插件新增的目录。
- **符号链接只挡得住 `/nas` 下的**：`validate_share_path` 会把符号链接解析后的真实路径
  也拿去比白名单，但 `SYMLINK_ROOT` 是 `/home`，而白名单主力也是 `/home/<uXXXX>/pool0/data`
  ——`/home` 下的软链怎么解析都落在 `/home/` 开头，等于不设防。这是「插件只服务设备所有者
  自己的账号」这一前提下的取舍。
- `..` 的显式检查在 `posixpath.normpath` **之后**，绝对路径里的 `..` 会被先折叠，
  真正兜住越界的是白名单那一关（把 `/nas/pool0/../etc` 折叠成 `/nas/etc` 再比）。
- 允许根目录的推导（`derive_allowed_roots`）用 `Path(root)` 拼路径，只在 Linux 上正确
  ——插件本来就只跑在 NAS 上；单测里把两个「按平台拼路径」的小工具打桩成 POSIX 形态。
- 同一台 NAS 上同时装 dpanel 与 disksleep 会因为端口都写 18160 而冲突（与本插件无关，
  本插件用 18190）。
