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
engine.py                    业务逻辑（uci、drop-in、采样、日志）
server.py                    只监听 127.0.0.1:18160 的本地服务
web/                         插件页面
```

| 接口 | 方法 | 说明 |
| --- | --- | --- |
| `/api/status` | GET | 开关、生效时长、硬盘状态、是否已接管 |
| `/api/events?limit=200` | GET | 休眠/唤醒事件（倒序） |
| `/api/hdidle-log?limit=200` | GET | hdidle 原始日志（倒序） |
| `/api/switch` | POST | `{"enabled": true/false}` 写回系统开关 |
| `/api/timeout` | POST | `{"minutes": 45}` 设置休眠时长 |
| `/api/restore` | POST | 删除 drop-in，恢复官方 30 分钟 |

## 限制

- 时长范围 5–720 分钟，与官方一致按「无读写活动」计时，实际休眠还取决于是否有
  程序持续访问硬盘（SMB、Docker 容器、媒体库扫描等都会唤醒）。
- 事件采样间隔 20 秒，短于该间隔的休眠—唤醒不会单独记录。
- 需要 root 权限写 `/etc/config` 与 `/etc/systemd/system`（服务 unit 已用
  `ReadWritePaths` 限定范围）。

## 测试

```bash
cd projects/xiaomi-disk-sleep-plugin
python3 -m unittest discover -s tests
```
