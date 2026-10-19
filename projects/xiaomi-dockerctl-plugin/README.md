# Docker 控制台（小米智能存储社区插件）

> **来源与致谢**：本插件移植自 [`lvmaple/minas_plugin`](https://github.com/lvmaple/minas_plugin)
> 的 [`xiaomi-nas-plugin-dockerctl`](https://github.com/lvmaple/minas_plugin/tree/main/xiaomi-nas-plugin-dockerctl)
> 子目录（作者 minas），保留其容器/镜像/Compose/存储卷/网络管理功能与界面。
> **上游仓库与插件子目录均未提供 LICENSE 文件**，README 只写了「License：仅供学习参考」，
> 因此上游**未声明许可证，本目录仅作本地移植使用**；若计划对外分发或上架，请先联系原作者取得许可。

在小米智能存储客户端里管理宿主机上的 Docker：容器、镜像、部署、存储卷、网络。
**不是小米或 Docker 的官方插件。**

## 功能

| 模块 | 功能 |
| --- | --- |
| 📊 概览 | 运行/停止/镜像/存储卷/网络统计、状态分布、快捷入口 |
| 📦 容器 | 列表、搜索、启动/停止/重启/暂停/恢复、删除、日志、详情（端口与挂载） |
| 💿 镜像 | 列表、按拉取时间排序、搜索、拉取（带进度与取消）、删除、仓库登录/退出 |
| 🚀 部署 | 表单部署（端口/挂载/环境变量/重启策略/网络/额外参数）+ Compose YAML 部署与项目停止/删除 |
| 📁 存储 | 存储卷列表、创建、删除 |
| 🌐 网络 | 网络列表、创建（bridge/host/overlay/macvlan/none，可设子网/网关/父接口）、删除 |
| 📜 日志 | 操作记录（含失败原因，落盘，重启后仍在），可清空 |
| ⚙️ 设置 | 修改 Docker CLI 路径与 Compose 目录（换设备/CLI 路径不同时不用改 unit） |

## 端口与路径

| 项 | 值 |
| --- | --- |
| 插件服务端口 | `18210`（只监听 `127.0.0.1`） |
| 对外入口 | 小米客户端证书入口 `/plugin/<用户>/dockerctl/` |
| 服务名 | `xiaomi-dockerctl.service` |
| 发布目录 | `/data/plugin/dockerctl`（`current` 软链到 `releases/<版本>-<时间戳>-<pid>`） |
| 页面副本 | `/home/<用户>/plugin/dockerctl/src/ui` |
| 运行数据 | `/data/plugin/dockerctl/data`：`config.json`、`ops.log`、`pull-times.json`、`compose/` |
| 日志 | systemd journal：`journalctl -u xiaomi-dockerctl -f` |
| Docker CLI | 默认 `/data/docker/docker`（设备上不在 `PATH` 里），可在插件页修改 |

## 权限与安全（重要）

本插件**本身就是 Docker 控制台**，能操作宿主机上的全部容器、镜像、存储卷与网络，
权限等价于 Docker 级 root。为降低风险，这次移植加了三层约束：

1. 服务只绑定 `127.0.0.1`，局域网无法直连；
2. 除 `/api/health` 外的请求，服务端会再校验一次小米客户端证书头
   （`X-Xiaomi-Client-Verify: SUCCESS` 且 DN 里的 `CN=nas.<用户>` 匹配），或来自回环的
   `X-Real-IP`；不信任 nginx 单层放行，也不返回 CORS 头；
3. 写操作必须 `Content-Type: application/json`，挡掉跨站表单伪造的简单请求。

系统层还有 systemd 加固（`NoNewPrivileges`、`ProtectHome`、`ProtectSystem=full`、
`ReadWritePaths` 只到插件目录、`MemoryMax=512M`）。

**不要**给 18210 做端口映射，也不要把本插件的 nginx 入口挪到不需要客户端证书的站点。
表单部署支持「额外参数」，等于允许把任意 Docker 参数交给 `docker run`，请只在自己信任的
设备上使用。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查（唯一不需要客户端证书的接口） |
| GET | `/api/info` | Docker 版本与引擎信息 |
| GET | `/api/overview` | 统计概览 |
| GET | `/api/containers?all=1` | 容器列表 |
| GET | `/api/containers/:id` | 容器详情 |
| GET | `/api/containers/:id/logs?tail=200` | 容器日志 |
| GET | `/api/containers/:id/stats` | 资源占用 |
| POST | `/api/containers/:id/{start,stop,restart,pause,unpause,kill,remove}` | 容器操作 |
| GET | `/api/images` | 镜像列表（含本地拉取时间） |
| GET | `/api/images/:id` | 镜像详情 |
| POST | `/api/images/pull` | 拉取镜像（异步任务，支持代理） |
| GET | `/api/images/pull/status` | 拉取进度 |
| POST | `/api/images/pull/cancel` | 取消拉取 |
| POST | `/api/images/:id/remove` | 删除镜像 |
| POST | `/api/deploy/run` | 表单部署容器 |
| POST | `/api/deploy/compose` | 部署 Compose 项目 |
| GET | `/api/compose` | 已部署项目列表 |
| POST | `/api/compose/:name/{down,remove}` | 停止/删除 Compose 项目 |
| GET/POST | `/api/volumes` | 存储卷列表 / 创建 |
| POST | `/api/volumes/:name/remove` | 删除存储卷 |
| GET/POST | `/api/networks` | 网络列表 / 创建 |
| POST | `/api/networks/:id/remove` | 删除网络 |
| GET | `/api/registry/auth` | 已登录仓库（只返回服务器名） |
| POST | `/api/registry/{login,logout}` | 仓库登录 / 退出 |
| GET | `/api/logs?tail=200` | 操作日志 |
| POST | `/api/logs/clear` | 清空操作日志 |
| GET/POST | `/api/settings` | 读取 / 保存运行配置（Docker CLI、Compose 目录） |

## 目录结构

```text
projects/xiaomi-dockerctl-plugin/
├── VERSION                     # 0.1.0
├── server.py                   # HTTP 服务、静态页面、鉴权、路由
├── engine.py                   # Docker CLI 封装、配置读写、拉取任务、操作日志
├── web/                        # 页面（原生 HTML/CSS/JS，无构建步骤）
│   ├── index.html
│   ├── app.js
│   ├── style.css
│   └── assets/dockerctl.png    # 256×256 图标（由 scripts/make_plugin_icons.py 生成）
├── deploy/
│   ├── xiaomi-dockerctl.service
│   ├── xiaomi-dockerctl.nginx.conf
│   ├── control
│   ├── plugin-meta.json
│   └── README.md               # 安装 / 卸载 / 排障说明
└── tests/                      # stdlib unittest
```

## 本地预览与测试

```bash
cd projects/xiaomi-dockerctl-plugin
python -m unittest discover -s tests -v
python server.py --dev            # http://127.0.0.1:18210/
```

`--dev` 使用临时数据目录、跳过客户端证书校验，方便在电脑上看页面；它**仍会调用本机的
Docker CLI**（如果装了），不会连接 NAS。没有 Docker 时页面里的列表会显示后端返回的错误。

## 与上游的差异（移植取舍）

| 项 | 上游 | 本移植 |
| --- | --- | --- |
| 页面分发 | nginx alias + `dockerctl.cgi` | Python 直接提供静态文件，nginx 全量反代 |
| 端口 | 9302 | 18210（按本仓库 `PACKAGE_SPECS` 分配，9302 未登记） |
| 鉴权 | 只靠 nginx/客户端入口 | 服务端二次校验客户端证书头 + 回环；POST 强制 JSON |
| 数据/日志 | `/tmp/dockerctl`、`/data/backup/plugins/dockerctl` | `/data/plugin/dockerctl/data`；服务日志进 journal |
| 生命周期 | `scripts/control` 自己写 systemd unit | 走本仓库约定：`deploy/*.service` + `deploy/control` 转发 |
| 平台占位 | `/home/u<NAS_UID>` | `__NAS_USER_ID__` 占位，由商店安装器渲染 |
| Docker CLI | 固定 `/data/docker/docker`（可用 `DOCKER_BIN` 覆盖） | 同上，另加「设置」页可持久化修改（`config.json`） |
| skopeo 容器包装 | 自带 `scripts/skopeo-container`（需预拉取固定镜像） | **未打包**：代理拉取仍支持，但要求 NAS 上自备 `skopeo`，否则提示安装 |
| 开机自愈 | 自带 `dockerctl-restore.service` + `/data/backup` 备份 | 由商店/仓库的恢复机制负责，不额外写 unit |

## 许可证

上游未声明许可证（仓库与插件子目录都没有 LICENSE/COPYING，README 仅写「仅供学习参考」）。
本目录仅作**本地移植使用**；若计划对外分发或上架，请先联系原作者取得许可，上游后续补充许可证时以其为准。
页面图标已改为本仓库自绘：由 `scripts/make_plugin_icons.py` 按市场统一规范
（256×256 圆角渐变底 + 白色鲸鱼/货箱图形）程序化生成，不再取自上游 `src/ui/icon.png`。
