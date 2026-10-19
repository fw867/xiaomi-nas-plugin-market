# Docker 控制台 · 安装 / 卸载说明

> 插件 key：`dockerctl` ｜ 服务端口：`18210`（仅回环） ｜ 服务名：`xiaomi-dockerctl.service`
> 上游：`lvmaple/minas_plugin` 的 `xiaomi-nas-plugin-dockerctl`（未声明许可证，仅本地移植使用）

## 安装前确认

| 项目 | 说明 |
| --- | --- |
| Docker | 宿主机的 `dockerd` 必须在运行；容器管理通过 Docker CLI 调用引擎 |
| Docker CLI | 设备上的 CLI 不在 `PATH` 里，默认 `/data/docker/docker`。可在插件页「设置 → 运行配置」里修改 |
| 端口 | 服务只监听 `127.0.0.1:18210`；对外只经小米客户端证书入口，**不要**把 18210 映射到局域网/公网 |
| 权限 | 插件以 root 运行、能操作全部容器与镜像，权限等价于 Docker 级 root。只装在自有设备上 |
| 磁盘 | 运行数据在 `/data/plugin/dockerctl/data`：操作日志、拉取时间、`config.json`、Compose 项目 |

## 方式一：从应用商店安装（推荐）

在小米智能存储客户端打开「应用商店」，找到 **Docker 控制台** 安装即可。商店会：

1. 把插件包解压到 `/data/plugin/dockerctl/releases/<版本>-<时间戳>-<pid>/`，并切换
   `/data/plugin/dockerctl/current` 软链；
2. 把页面复制到 `/home/<用户>/plugin/dockerctl/src/ui`，并补齐小米插件规范要求的
   `INFO`、`scripts/control`、`etc/var/tmp` 等结构（`plugin.sh verify` 需要）；
3. 渲染 `deploy/xiaomi-dockerctl.service` 与 `deploy/xiaomi-dockerctl.nginx.conf` 里的
   `__NAS_USER_ID__` 占位符，装到 `/etc/systemd/system/` 与
   `/etc/nginx/conf.d/luci/xiaomi-dockerctl.conf`；
4. `systemctl restart` 服务并 reload nginx，然后做一次 `/api/health` 健康检查。

安装完成后**完全退出并重开**小米智能存储客户端，在「全部应用」里打开「Docker 控制台」。

## 方式二：本机手工安装（排障/开发用）

假设 NAS 用户为 `u123456`（下文用 `<用户>` 表示），插件目录放在
`/data/plugin/dockerctl`。商店安装的目录结构是：

```text
/data/plugin/dockerctl/
├── current -> releases/0.1.0-1789828016-8538   # 软链，单元里所有路径都走它
├── releases/…
├── data/                                        # 运行数据（用户数据，升级不动）
│   ├── config.json
│   ├── ops.log
│   ├── pull-times.json
│   └── compose/
└── ...
/home/<用户>/plugin/dockerctl/
├── INFO                     # 含 abstract，plugin.sh verify 用
├── scripts/control          # plugincenter boot 时执行 control enable
└── src/ui/                  # 页面副本（由商店从包里的 ui/ 铺过来）
```

手工装一份等价结构：

```sh
# 1) 放运行时
mkdir -p /data/plugin/dockerctl/releases/0.1.0-manual
cp server.py engine.py VERSION README.md /data/plugin/dockerctl/releases/0.1.0-manual/
cp -r web /data/plugin/dockerctl/releases/0.1.0-manual/
ln -sfn /data/plugin/dockerctl/releases/0.1.0-manual /data/plugin/dockerctl/current

# 2) 装页面（plugins/INFO/control 等原生结构由商店的 deploy/native_layout.py 生成；
#    手工安装时建议直接用商店安装，避免 abstract 校验不过）
mkdir -p /home/<用户>/plugin/dockerctl/src
cp -r web /home/<用户>/plugin/dockerctl/src/ui

# 3) 渲染并安装 systemd 单元（把 __NAS_USER_ID__ 换成真实用户）
sed 's/__NAS_USER_ID__/u123456/g' xiaomi-dockerctl.service \
    > /etc/systemd/system/xiaomi-dockerctl.service
systemctl daemon-reload
systemctl enable --now xiaomi-dockerctl.service

# 4) 渲染并安装 nginx 入口（目录按设备上 luci 的包含方式，通常是 conf.d/luci/）
sed 's/__NAS_USER_ID__/u123456/g' deploy/xiaomi-dockerctl.nginx.conf \
    > /etc/nginx/conf.d/luci/xiaomi-dockerctl.conf
nginx -t && nginx -s reload

# 5) 健康检查（健康检查不需要客户端证书）
curl -s http://127.0.0.1:18210/api/health
```

## 卸载

**从应用商店卸载**即可，商店会停服务、删单元/nginx 入口/页面副本/图标，并保留
`/data/plugin/dockerctl/data`（用户数据不会被动）。

手工卸载：

```sh
systemctl disable --now xiaomi-dockerctl.service
rm -f /etc/systemd/system/xiaomi-dockerctl.service
rm -f /etc/nginx/conf.d/luci/xiaomi-dockerctl.conf
rm -rf /home/<用户>/plugin/dockerctl/src/ui
rm -f /data/plugin/www/icon/dockerctl.icon
rm -rf /data/plugin/dockerctl            # 连 data/ 一起删；想保留日志/Compose 就只删 current 与 releases
nginx -t && nginx -s reload
systemctl daemon-reload
```

> 卸载**不会**自动删除由本插件创建的容器、镜像、存储卷或网络。它们是宿主机的
> Docker 资源，请在卸载前用插件页自己清理，或明确保留。

## 常见问题

| 现象 | 处理 |
| --- | --- |
| 页面能开，所有操作报 `docker binary not found` | 在「设置 → 运行配置」把 Docker CLI 路径改成设备上的真实路径（常见 `/data/docker/docker`），或用 `command -v docker` 确认 |
| `401 请从小米客户端打开本插件` | 说明请求没走客户端证书入口（例如直接拿浏览器访问 nginx 的 `/plugin/...`），属预期行为；排障时可在 NAS 上 `curl 127.0.0.1:18210/api/health`，或用 `server.py --dev` 本地预览 |
| 端口被占 | 18210 是本插件在 `PACKAGE_SPECS` 里登记的端口；若被别的进程占用，用 `ss -lntp` 查 18210 的监听者，不要和硬盘休眠（18160）、DPanel（18110）等现有插件撞号 |
| Compose 部署提示找不到 compose | 设备上的 Docker 可能没有 compose 插件；后端会再尝试 PATH 里的 `docker-compose`。两者都没有时请改用表单部署单容器 |
| 拉取镜像走代理失败 | 单次代理拉取需要 NAS 上装有 `skopeo`（或设置 `SKOPEO_BIN`）。不装 skopeo 时请直连或用镜像加速前缀 |
