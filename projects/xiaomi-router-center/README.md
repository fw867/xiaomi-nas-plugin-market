# 路由器软件中心（rtrcenter）

把局域网里的 **UniFi SoftCenter**（跑在路由器上的插件中心，`http://192.168.1.1:9958/`）
搬进小米智能存储：**手机 App 里直接打开它、装插件、看日志、改定时任务**。

关键点：**不需要给路由器做端口映射**。插件在 NAS 上做反向代理，软件中心的页面与接口都经
NAS 的客户端入口（客户端证书 + 小米云通道）转发到路由器，路由器始终待在局域网里。

```
手机 小米智能存储 App（外网）
   │  小米云通道 + 客户端证书
   ▼
NAS nginx 443  /plugin/<用户>/rtrcenter/
   ├── /           插件外壳页（细顶栏 + iframe）
   ├── /site/      → 反代到路由器 http://192.168.1.1:9958/
   └── /api/       → 本机插件服务（状态、设置、令牌）
```

## 功能

- **外壳页**：顶部细栏显示目标地址、延迟、软件中心标题与令牌状态；下面用同源 iframe 承载真实界面；
- **自动带令牌**：在插件里存一次 AdminToken（存在 NAS 上，权限 0600），打开页面时自动写进同源的
  `localStorage`，软件中心一加载就通过鉴权 —— 手机上不用每次重输；
- **目标地址可改**：默认 `http://192.168.1.1:9958/`，可在插件里改成别的路由器/端口。改地址会重新渲染
  插件自己的那**一个** nginx 入口文件，且必须 `nginx -t` 通过才 `reload`，失败自动回滚；
- **只读采集**：状态探测只发两个 GET（首页 + `/api/system/info`），不会改动路由器上任何东西；
- 体积小：纯标准库 Python 服务 + 静态前端，服务常驻内存约 17 MB。

## 安装

```bash
NAS_IP=192.168.1.8 NAS_SSH_KEY=~/.ssh/id_rsa NAS_USER_ID=u3943892 \
  bash deploy/install-on-nas.sh
```

可选环境变量：`PLUGIN_ID`（默认 11021）、`PLUGIN_PORT`（默认 18101）、`ROUTER_TARGET`（默认
`http://192.168.1.1:9958/`；不传就沿用已装设置，再退回默认值）。

安装内容：

| 路径 | 说明 |
| --- | --- |
| `/data/plugin/router-center/releases/<版本-时间>/` | 程序与前端（`current` 软链指向它） |
| `/data/plugin/router-center/settings.json` | 目标地址 |
| `/data/plugin/router-center/router-token` | 路由器 AdminToken（0600） |
| `/home/<用户>/plugin/rtrcenter/` | 小米客户端插件结构（INFO / src/ui / scripts） |
| `/etc/systemd/system/xiaomi-router-center.service` | 插件服务 |
| `/etc/nginx/conf.d/luci/xiaomi-router-center.conf` | 客户端入口（**由服务渲染，改请改模板**） |
| `/data/plugin/<用户>.list` | 注册 `rtrcenter` 条目（改前自动备份） |

卸载：`NAS_IP=... bash deploy/uninstall-on-nas.sh`（`PURGE=1` 时连设置与令牌一起删）。

## 前端为什么能挂在子路径下

被反代的软件中心页面里，所有接口都通过 `apiUrl()`（基于 `document.baseURI` 的相对基址）发出：

```js
const PAGE_DIR = new URL('.', document.baseURI).href;
const apiUrl = (path) => new URL(String(path).replace(/^\/+/, ''), PAGE_DIR).href;
```

所以：

- 直连 `http://192.168.1.1:9958/` 时与原绝对路径完全等价，**原有用法零变化**；
- 挂在 `https://<NAS>/plugin/<用户>/rtrcenter/site/` 下时，请求自动变成 `<前缀>/api/...`，
  由 nginx 转回路由器的 `/api/...`，后端不需要任何改动。

页面 URL 必须**以 `/` 结尾**，否则"页面目录"会算错 —— 插件的 nginx 入口里有一条
`location = /plugin/<用户>/rtrcenter { return 301 .../; }` 专门兜这件事。

## 接口

| 路径 | 方法 | 说明 |
| --- | --- | --- |
| `/healthz` | GET | 健康检查（版本、目标地址、运行时长） |
| `/api/status` | GET | 目标可达性、延迟、软件中心标题、设备与版本、令牌状态 |
| `/api/settings` | GET | 目标地址、是否已存令牌（含明文，供同源页面注入） |
| `/api/settings` | POST | `{target?, token?}` 保存；改目标走"渲染 → `nginx -t` → reload"，失败回滚 |

## 安全

- 插件注册为 `permission: admin`，只有管理员账号能看到；
- 这个界面能在路由器上装包、重启服务、升级系统，等于路由器的高权限入口 —— 请给 AdminToken 用强随机值，
  并且**不要**在路由器上给 9958 做端口映射（本方案的价值就是不需要）；
- AdminToken 存在 NAS 上（0600），只有能打开插件页（需通过客户端证书 + 管理员）的人能取到；
- 软件中心自身的令牌比较已改为 `CryptographicOperations.FixedTimeEquals` 固定时间比较。

## 已知限制

- 手机 App 的远程通道只覆盖 NAS 的客户端入口，因此**软件中心主界面**可以远程用；
  但路由器上各应用自己的 Web 面板（xwall、webssh 等，各自监听不同端口）默认打不开，
  需要的话可以再加"通用端口反代"；
- 软件中心前端依赖 CDN 上的 Vue/Tailwind/Lucide，浏览器需要能访问外网；
- WebSocket 未纳入反代（软件中心本身是 REST + 长轮询，长轮询已设 `proxy_buffering off`）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

24 个用例：目标地址规整、设置与令牌读写（含 0600 权限）、假路由器探测（可达/带令牌/错令牌/不可达）、
换地址的失败回滚（`nginx -t` 不过与 reload 失败两种）、服务真起一遍打 `/healthz`、`/api/status`、
`/api/settings`、静态文件、未知接口 404、路径穿越拒绝、前端语法与 id 一致性、安装脚本占位符校验。
