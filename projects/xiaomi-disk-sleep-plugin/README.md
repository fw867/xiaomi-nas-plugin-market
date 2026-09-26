# 硬盘休眠

在小米智能存储上自定义硬盘休眠时间，并查看休眠与唤醒记录。

**非小米官方产品。**

## 为什么需要它

系统设置里的「硬盘休眠」只有一个开关，时长写死在系统 unit 里：

```ini
# /usr/lib/systemd/system/hdidle.service
ExecStart=/bin/sh -c 'if [ "$(uci get system.disk.hibernate 2>/dev/null)" = "1" ];
                      then exec /usr/bin/hdidle -n -i 1800; fi'   # 1800 秒 = 30 分钟
```

本插件不改动官方 unit，只在 `/etc/systemd/system/hdidle.service.d/` 放一个 drop-in
覆盖 `ExecStart`，把 `-i 1800` 换成你设置的值。

## 联动方式

| 项目 | 说明 |
| --- | --- |
| 开关 | 仍然是系统设置里的那个开关（uci `system.disk.hibernate`）。App 改、插件改，两边同步；官方 App 的启停逻辑不受影响 |
| 时长 | 插件设置，写入 drop-in，`systemctl restart hdidle.service` 生效 |
| 恢复 | 点「恢复官方 30 分钟」或卸载插件，drop-in 被删除，回到官方行为 |

由于开关仍由官方 unit 判断，插件的 drop-in 里保留了同样的判断：
**开关关闭时不休眠**，与系统设置保持一致。

## 日志

- **事件**：插件每 20 秒查一次 `hdparm -C`（该查询不会唤醒硬盘），记录
  `进入休眠` / `被唤醒` 的状态跃迁，带设备与时间。
- **hdidle 原始**：直接读 `journalctl -u hdidle.service`，可看到守护进程每次
  检测到的读写活动。

日志按时间倒序显示，可一键复制。

## 排查：到底是谁在写盘

插件页面里的「谁在写盘」折叠面板已经内置了这套排查，不用登 SSH；
仓库里另有一份等价的独立脚本，方便在没有插件页面的机器上跑。

hdidle 的日志只报整盘计数（`reads: / writes:`），看不出是哪个分区、哪个文件、
哪个插件的写入。系统里跑着索引、相册、媒体库等一堆会周期性写库的服务，加上
**`/nas/sys` 建在 md0 上，而 md0 是跨两块盘的 RAID1**——任何一次写 `/nas/sys`
都会被镜像到两块盘，所以「有东西写阵列」就等于「所有盘都醒着」。

### 页面面板

日志卡片里的「谁在写盘」标签页依次显示：结论提示、分区写入速率、这段时间被改动的
文件、系统索引记录到的改动。每条文件都能点一下复制路径。

**诊断要读硬盘，所以必须由你主动触发**：

- **块设备速率**是纯 `/proc/diskstats` 读取，一直免费，状态卡上也会显示
  「正在写盘：…」，不用打开这个标签页就知道有没有活动。
- **目录扫描和事件库读取只在打开标签页或点「重新扫描」时做一次**，之后页面每
  10 秒的自动刷新只读缓存，不产生任何磁盘 I/O。采样线程同样只记 `/proc`
  计数（`activity_tick` 不做扫描）。插件自己绝不能成为唤醒硬盘的那个程序。
- 打开标签页本身会读一次硬盘（扫目录 + 复制 34 MB 的事件库 WAL），**会把盘唤醒**。
  查完切回其它标签页即可，盘会在设定的空闲时间后重新休眠。

### 独立脚本

在 NAS 上以 root 运行（只读，不改配置、不重启服务）：

```bash
python3 tools/disk-activity-report.py                # 采样 60 秒
python3 tools/disk-activity-report.py --seconds 180  # 现象很稀疏时加大窗口
python3 tools/disk-activity-report.py --depth 8      # 加大目录扫描深度
```

报告分 6 节，逐层收敛：

| 节 | 回答 | 手段 |
| --- | --- | --- |
| 1 | 哪块盘、哪个分区在写 | `/proc/diskstats` 增量 + `/proc/mdstat`，并检测 RAID1 成员盘写入量是否对称 |
| 2 | 哪些文件被改动 | 挂载点遍历，对比文件大小与 mtime |
| 3 | 系统索引记录到的文件改动 | 读 `findexd` 的 fanotify 事件库 `/nas/sys/findex/*.db` |
| 4 | 谁持有可写句柄 | 扫 `/proc/*/fd` + `fdinfo` 的 O_WRONLY/O_RDWR |
| 5 | 这些文件属于哪个插件 | 用容器 bind 挂载反查（含 `/nas/pool0` → `/nas/mnt/pa*` 路径别名） |
| 6 | 结论 | 按字节数和事件次数排序的来源清单 |

### 为什么不用 iotop / pidstat

本机内核没有开 `CONFIG_TASK_IO_ACCOUNTING`，`/proc/<pid>/io` 不存在，
所以 `iotop`、`pidstat -d` 拿不到任何数据（`pidstat` 本身装了，但只会显示 0）。
`blktrace` 在这个受限 shell 里起不了线程。因此脚本改走「文件 mtime 增量 +
系统 fanotify 事件库」这两条不依赖进程 I/O 统计的路。

### 一个真实案例（本机实测）

`/nas/sys` 上 findexd 的 4 个 WAL 库每 30 秒原地重写一次，RAID1 成员 `sda1`
与 `sdb1` 的写入量逐秒完全相等；触发源是 **Jellyfin 容器自带的健康检查**
（镜像内置 `HEALTHCHECK_URL=http://localhost:8096/health`，`Interval=30s`）：
每打一次 `/health`，Jellyfin 就会重写一次 `jellyfin.db-shm`（32768 B），
fanotify 立刻把这个事件交给 findexd，findexd 再写 `/nas/sys`——一秒钟后两块盘
同时出现写入。手动验证：

```bash
curl -s --noproxy localhost -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8097/health
stat -c '%y' "/nas/mnt/pa1/u3943892/data/下载/JellyfinConfig/data/jellyfin.db-shm"
```

连续 10 次请求，10 次都改写了 `jellyfin.db-shm`；换成静态路径 `/web/` 则不变。

停掉容器做 A/B（各阶段平均写入，KB/s）：

| 设备 | A 运行中 | B 已停止 | C 恢复后 |
| --- | --- | --- | --- |
| `md0`（`/nas/sys`，RAID1） | 22.2 | **5.6** | 33.9 |
| `sda1` / `sdb1` | 22.5 / 22.5 | **5.6 / 5.6** | 34.3 / 34.3 |
| `sdb2`（`/nas/mnt/pa1`） | 14.0 | 4.5 | 16.8 |
| 索引 WAL 被改次数 | 10 次/60 秒 | **0 次/180 秒** | 24 次/60 秒 |
| `jellyfin.db-shm` 被改次数 | 2 次/60 秒 | **0** | 2 次/60 秒 |

Jellyfin 一停，findexd 的 4 个 WAL 库从「每分钟被改 10 次」变成「3 分钟一次都没改」——
说明 findexd 是在**响应** Jellyfin 的文件事件，而不是自己在跑独立定时器。
Jellyfin 约占 `/nas/sys` 写入的 **75%**。

但 B 阶段的 5.6 KB/s 是**过渡态**，不是稳态：Jellyfin 停机/重启的收尾动作还会产生
一批文件事件，findexd 需要把它们排空。排空之后它的 60 秒维护循环就是空转——
没有新事件时 WAL checkpoint 无事可做，不产生任何磁盘 I/O。

实测（关闭健康检查并重建容器后约 20 分钟起）：

```text
hdparm -C /dev/sda → standby      hdparm -C /dev/sdb → standby
60 秒采样：sda / sda1 / sda2 / sdb / sdb1 / sdb2 / md0  写入全部为 0 扇区
findexd 40 分钟：run_maintenance 448 次、PASSIVE checkpoint 24 次、
                 raw_event_writer: committed 0 次
```

两块盘在 **12:27:29 同时进入 standby**，此后 60 多分钟 `hdidle` 一条 activity 都没有，
`hdparm -C` 复查仍是 `standby`。

结论：**硬盘不休眠不是休眠插件的问题**，而是被 Jellyfin 的 30 秒健康检查持续喂事件；
关掉它之后系统就能正常休眠，**不需要**停用索引/相册/媒体库服务。
单靠调大休眠时间没有用。

现在已经由 Jellyfin / Emby 插件在建容器时关掉健康检查（见各自 README），
上面这条 30 秒心跳不会再出现。

## 页面

移动端优先：按钮与输入框都不小于 44px，输入框 16px（iOS 聚焦不会放大页面），
顶栏与提示条避让刘海/Home 指示条（`viewport-fit=cover` + `env(safe-area-inset-*)`），
日志区最大高度跟着视口走，深色模式跟随系统。

## 安装

从应用商店安装，或在仓库根目录执行：

```bash
python3 scripts/build_apps.py --only disksleep
```

安装完成后在客户端「全部应用」中打开。

## 目录结构与接口

```text
deploy/plugin-meta.json      插件元数据（INFO 来源）
deploy/control               规范要求的控制脚本
deploy/xiaomi-disk-sleep.service / .nginx.conf
engine.py                    业务逻辑（uci、drop-in、采样、日志、写盘诊断）
server.py                    只监听 127.0.0.1:18160 的本地服务
web/                         插件页面（移动端优先）
tools/disk-activity-report.py  只读体检脚本（不随插件打包，仅在仓库里用）
```

`tools/` 不在 `scripts/build_apps.py` 的 `runtime` 清单里，所以不会进安装包。

| 接口 | 方法 | 说明 |
| --- | --- | --- |
| `/api/status` | GET | 开关、生效时长、硬盘状态、是否已接管、当前写入速率摘要 |
| `/api/events?limit=200` | GET | 休眠/唤醒事件（倒序） |
| `/api/hdidle-log?limit=200` | GET | hdidle 原始日志（倒序） |
| `/api/activity` | GET | 「谁在写盘」：分区速率、RAID 镜像提示、改动文件、事件库记录、归属 |
| `/api/switch` | POST | `{"enabled": true/false}` 写回系统开关 |
| `/api/timeout` | POST | `{"minutes": 45}` 设置休眠时长 |
| `/api/restore` | POST | 删除 drop-in，恢复官方 30 分钟 |

`/api/activity?force=1` 会跳过扫描节流（面板上的「重新扫描」用它）。

## 限制

- 时长范围 5–720 分钟，与官方一致按「无读写活动」计时，实际休眠还取决于是否有
  程序持续访问硬盘（SMB、Docker 容器、媒体库扫描等都会唤醒）。已经不休眠时，
  先用上面的[体检脚本](#排查到底是谁在写盘)定位写入来源，调长时间没有用。
- 事件采样间隔 20 秒，短于该间隔的休眠—唤醒不会单独记录。
- 需要 root 权限写 `/etc/config` 与 `/etc/systemd/system`（服务 unit 已用
  `ReadWritePaths` 限定范围）。

## 测试

```bash
cd projects/xiaomi-disk-sleep-plugin
python3 -m unittest discover -s tests
```
