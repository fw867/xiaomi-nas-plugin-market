
## v0.2.2

- 修复 NAS 上 nginx 不在 PATH 时安装失败的问题（自动查找 /usr/sbin/nginx 等路径）。


## v0.2.3

- 新增关于页：显示本地/远程版本号，支持一键自更新。
- 修复 api.github.com 在更新检测时被域名校验拦截的问题。
- 商店只从 GitHub apps.json 拉取插件列表，图标使用 raw URL。


## v0.2.4

- 开机钩子新增 dockerd fd 软上限检测与修复：systemd 默认软上限只有 1024，而
  `iptables:false` 时发布的端口全靠用户态 docker-proxy（一条连接占 2 个 fd），
  BT 类插件把 peer 上限开大后代理会撞 EMFILE 后退出、宿主端口消失。修复动作排在
  「拉起插件服务」之前，并会先记下在跑的容器、重启后原样拉起。
- 市场服务启动时比对已安装的开机钩子与发行包里的副本，不一致就重装（升级后
  钩子里新增的修复能第一时间生效）。


## v0.2.5

- 开机钩子新增发布端口 DNAT 规则检测与修复（配合 UCI `mi_docker.globals.iptables=1`：
  外部流量走内核 DNAT，不再经过用户态 docker-proxy；回环仍走代理，`127.0.0.1:<端口>`
  不受影响）。判据是「正在运行且发布了端口的容器缺对应 DNAT 规则」，缺了就重启 docker 修。
- 修复动作更稳：拉起插件服务前先等 docker 就绪；开机窗口内忽略上一次开机留下的重试冷却
  （systemd 在 overlay 挂载前读完单元目录，开机时看不到 drop-in，dockerd 会带旧上限起来，
  必须立刻修）；重启 docker 后除了按清单拉回容器，还会重启对应插件服务让它重新同步
  （否则路由器端口映射这类状态不会自己回来）。
- 新增「上次在跑的容器」快照（每分钟刷新）并据此在开机后兜底恢复容器：容器
  `RestartPolicy` 是 `no`，重启后不会自己回来，插件服务也不一定去启动容器（例如 Jellyfin）。


## v0.2.29

- **更新成功后自动清理旧版本目录**：每个插件保留「当前 + 上一版」，只清理商店自己创建的
  `<版本>-<时间戳>-<pid>` 目录；`current` 指向的那个永远保留，名字不符的遗留目录只记日志不动，
  `releases` 之外（`data/`、`lib/`）完全不在作用域；单个目录失败不影响其余，清理失败也不影响更新结果。
  关于页新增「清理旧版本」按钮（对所有已安装插件执行并汇总清理数与释放空间）。
- **底部「来源」tab 换成「回滚」**：列出可回滚的插件，一键退回上一版本（复用安装时的原子切换与收尾步骤：
  铺 UI、补齐原生结构、切 `current`、重启服务、写注册表），全程不动用户数据；未安装 / 没有上一版 /
  商店自身 / 正在操作中都有明确中文报错。原「来源」信息（内置可信源、ECDSA P-256、自定义源说明）挪进「关于」。
- 关于页卡片重排：可信源置顶、更新卡片其次、「清理旧版本」紧随更新卡片；删除已禁用的「添加签名源」入口
  与其下说明。
- **顶部与底部所有图标改为内联 SVG（字体无关）**：此前用 Unicode 字形，Android WebView 缺字形时会走字体
  回退，导致图标忽大忽小（实测回滚图标在 App 里只有 12×10.3 CSS px）；现六个图标统一 20×20 网格、
  `stroke-width 1.8`、圆头圆角、`currentColor`，渲染描边 1.71–1.8px、光学大小一致。


## v0.2.31

- **新增自愈模块 keepalive.py**（三件事，互为兜底）：
  1. **server.list 条目注入**：把在册的 10 个社区插件条目补进 /data/plugin/server.list 的 .data
     里。背景：打开 App 的「应用市场」会让 plugincenter 拿云端 server.list 与本地插件对账，
     云端只有厂商那几条，社区插件不在其中就会被判成非法安装并 force uninstall（删目录、删注册表、
     停服务）。注入后对账时它们是「在册」的。**只增不改**：厂商 5 条与任何已存在的条目一律不动。
  2. **每插件备份**：把 /data/plugin/<插件>/ 打包到 /data/backup/plugins/<插件>.tar.gz，
     **显式排除 data/、__pycache__、.git、
ode_modules**（绝不复制用户数据）。
  3. **自愈循环**：每 2 分钟检查 —— server.list 缺条目就注入、插件目录缺失就从备份还原、
     服务没跑就 systemctl start、端口没听就记问题；每轮写一行结构化日志到
     /data/plugin/community-store/state/keepalive.log。
- **双通道驱动**：市场服务进程内的后台线程（daemon，随服务启动）+ root cron 里每分钟的
  keepalive.py --once 作双保险；两处都幂等，重复执行无副作用。
- CLI：--once / --backup / --restore <key> / --status / --install-cron / --remove-cron / --loop，
  便于手工排障。
- **开机钩子 PATH 加固**：community-plugins-boot.sh 顶部显式
  export PATH（含 /usr/sbin:/sbin）。iptables 在本机是 /sbin/iptables 软链，某些 cron
  实现的默认 PATH 不含 /sbin，会让 command -v iptables 判否、NAT 修复被静默跳过。
- 新增 	ests/test_keepalive.py（12 个用例）：条目生成、注入幂等、**厂商条目不被改动**、
  备份排除用户数据、备份→删除→还原、heal_once 的容错与报表。
- **新增 current 软链自愈**：plugincenter 对账有时**只删 `<插件>/current`、保留整个目录**
  （实测 ssh-control 在 20:05 还在、20:32 就没了，而 eleases/ 完好）。这种状态下服务
  ExecStart 指向 current/... 重启就起不来，current/keepalive.sh 类 cron 变成每分钟空转，
  而只看「目录是否存在」发现不了。ensure_current() 会在 current 缺失或断链时，
  重新指向 eleases/ 里最新的版本目录，并重启对应服务。

## v0.2.32

### 修复：重建注册表/server.list 时**丢掉厂商条目**，导致 App 里"应用少了几个"

**现象（用户实测反馈）**：应用市场里 **百度网盘、迅雷** 以及另一个系统应用不见了。

**根因**：
`plugincenter update`（打开 App 应用市场也会触发）会**把 `/data/plugin/<用户>.list` 整个替换成云端清单**：
- 云端返回正常时 → 文件被换成 **5 条厂商条目**，**我们的 10 条被削掉**；
- 云端返回空/失败时 → 文件被清成 `{}`，**厂商 5 条也没了**。

`restore-plugins.py` 本来只"补充我们自己的 10 条"（merge），所以第二种情况下厂商条目**没有来源可恢复**，
App 里就少了几个应用。`server.list` 同理。

**修法：给厂商条目一份基线，缺失时只增补回**

| 改动 | 说明 |
| --- | --- |
| 新增 `/data/backup/plugin-registry/vendor-entries.json` | 厂商 5 条（影视 / 中枢功能 / 监控存储 / 百度网盘 / 迅雷）的**原样快照**，0600，落 `/data` 抗 OTA |
| `deploy/restore-plugins.py` | 新增 `vendor_baseline()` / `merge_vendor_entries()`；写注册表前把基线里**缺失**的厂商条目补回 |
| `keepalive.py` | 新增 `vendor_baseline_path()` / `vendor_baseline()` / `restore_vendor_entries()`；`heal_server_list` 同步补 `server.list`；`HealReport` 增加 `vendor_restored`，日志打印 `vendor+...` |
| 单测 | 新增 `tests/test_restore_plugins.py`（7 例）；`tests/test_keepalive.py` 增加 `VendorEntryTests`（6 例） |

**铁律（两条路径一致）**：
- 已存在的厂商条目**一字不改**（哪怕云端版本看起来更旧）；
- **绝不删除**任何非我方条目；
- 只增"基线里有、文件里没有"的；
- 幂等：重复执行不产生任何变化。

**验证**：对账前 15 条 → `plugincenter update` 后变 5 条（云端清单）→ **≤1 分钟自愈回 15 条**，
厂商 5 条字段（`system/type/port/status/install/enable/id/version`）与快照**逐字一致**；我们 10 个端口全 200。
