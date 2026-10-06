# Unifi（rtrcenter）

把局域网里的 **UniFi SoftCenter**（跑在路由器上的插件中心，`http://192.168.1.1:9958/`）
搬进小米智能存储：**手机 App 里直接打开它、装插件、看日志、改定时任务**。

关键点：**不需要给路由器做端口映射**。插件在 NAS 上做反向代理，软件中心的页面与接口都经
NAS 的客户端入口（客户端证书 + 小米云通道）转发到路由器，路由器始终待在局域网里。

```
手机 小米智能存储 App（外网）
   │  小米云通道 + 客户端证书
   ▼
NAS nginx 443  /plugin/<用户>/rtrcenter/
   ├── /            软件中心页面本身（插件服务取回后本地化再发出，不套 iframe）
   ├── /api/…       软件中心的接口 → 路由器（Authorization 透传）
   ├── /ctl/…       插件自己的接口（状态、设置、GitHub 转发）
   └── /assets/…    本地资源（Vue / Tailwind / Lucide / 关闭按钮 / 设置按钮）
```

## 功能

- **插件根路径就是软件中心**（不套 iframe）：页面由插件服务取回，把三个外部 CDN 换成本地副本、
  把直连 GitHub 的地址改走 NAS，因此在**只信任 NAS 域名的 App 内 webview** 里也能完整显示；
- **自动带令牌**：在插件里存一次 AdminToken（存在 NAS 上，权限 0600），打开页面时自动写进同源的
  `localStorage`，软件中心一加载就通过鉴权 —— 手机上不用每次重输（令牌校验那层"配置门"由软件中心
  自己负责，电脑端与移动端同一套）；
- **入口不挑、令牌不丢**：令牌注入对小米 App、控制台电脑端、浏览器直连、`/view/` 一视同仁；
  转发到路由器的 `Authorization` 按三条规则取值 —— 客户端自己带的真实令牌原样透传；控制台那一跳
  塞的占位值 `Bearer console-loopback`（`xiaomi-nas-console-lan.nginx.conf` 里写死的）换成插件保存的
  令牌；完全没带凭据时也用保存的令牌顶上（都没保存才不带，让软件中心照实报 401）；
- **关闭按钮**：左上角中间一个图形按钮，客户端里调宿主方法 `normal_goback` 关闭当前插件页
  （与官方「应用商店」插件同款），非客户端环境退回 `history.back()` / `window.close()`；
- **设置页 `panel.html`**（右上角 ⚙ 进入）：改目标地址、看路由器可达性、延迟与自检状态；
- **目标地址可改**：默认 `http://192.168.1.1:9958/`，改地址会重新渲染插件自己的那**一个**
  nginx 入口文件，且必须 `nginx -t` 通过才 `reload`，失败自动回滚；
- **两种用户名写法都认**：Android App 请求的是 `/plugin/3943892/…`（不带 `u`），注册表里是
  `u3943892` —— nginx 用 `[^/]+` 匹配，两种都能进；
- **只读采集**：状态探测只发两个 GET（首页 + `/api/system/info`），不会改动路由器上任何东西；
- 体积小：纯标准库 Python 服务 + 静态前端，服务常驻内存约 17 MB（本地资源约 900 KB）。

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
- 挂在 `https://<NAS>/plugin/<用户>/rtrcenter/` 下时，请求自动变成 `<前缀>/api/...`，
  由 nginx 转回路由器的 `/api/...`，后端不需要任何改动。

插件自己的接口在 `<前缀>/ctl/...`（走插件服务），本地资源在 `<前缀>/assets/...`（nginx 直接发）。

页面 URL 必须**以 `/` 结尾**，否则"页面目录"会算错 —— 插件的 nginx 入口里有一条
`location = /plugin/<用户>/rtrcenter { return 301 .../; }` 专门兜这件事。

## 接口

插件服务（经 nginx 暴露在 `<前缀>/ctl/…`）：

| 路径 | 方法 | 说明 |
| --- | --- | --- |
| `/healthz` | GET | 健康检查（版本、目标地址、运行时长、本地资源是否齐全） |
| `/api/status` | GET | 目标可达性、延迟、软件中心标题、设备与版本、令牌状态、nginx 入口是否已渲染 |
| `/api/settings` | GET | 目标地址、`token_set`、`token_hint`、`token_visible`；**明文 `token` 只回给设备所有者的小米客户端**（见"安全"） |
| `/api/settings` | POST | `{target?, token?}` 保存；改目标走"渲染 → `nginx -t` → reload"，失败回滚 |
| `/root/` | GET | 软件中心页面（本地化 + 令牌注入后返回，App 打开的就是这里） |
| `/view/…` | GET | 兼容入口（页面 + 接口转发），老地址仍可用；`/view/api/…` 带令牌转发到路由器 |
| `/api/github/…` | GET | 页面里直连 GitHub 的地址经此转发（见下） |

### 转发到路由器时的 Authorization

`/view/api/…` 转发前会过一遍 `engine.resolve_authorization`：

| 收到的 Authorization | 转发用的值 |
| --- | --- |
| 客户端自己带的真令牌（含 App 注入的、页面里手输的） | 原样透传，绝不覆盖 |
| 控制台电脑端那一跳的占位值 `Bearer console-loopback` | 插件保存的令牌（`/data/plugin/router-center/router-token`） |
| 完全没带 | 插件保存的令牌；没保存就不带（软件中心照实报 401，日志里写清该去哪填） |

## 安全

- 插件注册为 `permission: admin`，只有管理员账号能看到；
- 这个界面能在路由器上装包、重启服务、升级系统，等于路由器的高权限入口 —— 请给 AdminToken 用强随机值，
  并且**不要**在路由器上给 9958 做端口映射（本方案的价值就是不需要）；
- AdminToken 存在 NAS 上（0600）。**明文只在"设备所有者的小米客户端"里可见**：
  `GET /api/settings` 会用 nginx 透传的 `X-Xiaomi-Client-Verify: SUCCESS` + `X-Xiaomi-Client-DN`
  里的 `CN=nas.<用户号>.…` 判断（判据与其余插件里的 `trusted()` 同一套，见 `engine.client_certificate_owner`）；
  其它入口 —— 回环直连、**控制台电脑端**（`LAN_PORT`，默认 5001）、浏览器/局域网 curl、验签失败的请求 ——
  只回 `token_hint`（例如 `a1b2…z9`），响应体里不会出现明文令牌。
  注意插件的 nginx 对所有请求都写死 `X-Console-Entry: xiaomi`，**不能**拿它当判别依据。
- 判断"是本机哪个用户"的顺序：环境变量 `NAS_USER_ID`（单元文件里由安装脚本渲染；万一没渲染，
  插件会从注册表 `/data/plugin/<用户>.list` 或 `/home/<用户>/plugin/rtrcenter/` 反推，见
  `engine.plugin_owner`）→ 拿不到用户号时一律只给提示、不给明文（失败关闭）。
- 要看/复制明文令牌，请用小米 App 打开本插件（或在 NAS 本地打开设置页）；电脑端只能"替换"令牌，看不到原值。
  页面注入不受影响：那是插件服务端自己读文件写进页面 `localStorage.sc_token` 的，不经过 `/api/settings`。
- 电脑端入口（控制台的 `LAN_PORT`，默认 5001）走的是"回环即信任"，虽然读不到明文令牌，但仍然
  能打开插件页、改目标地址、把令牌替换成自己的 —— 所以那个端口只放局域网、不要转发到公网。
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

59 个用例：目标地址规整、设置与令牌读写（含 0600 权限）、假路由器探测（可达/带令牌/错令牌/不可达）、
换地址的失败回滚（`nginx -t` 不过与 reload 失败两种）、转发到路由器时 Authorization 的取值
（控制台占位值换保存令牌、缺 Authorization、真令牌原样透传、没保存令牌时不乱送）、
明文令牌的可见性（小米客户端证书 → 明文；控制台/回环/浏览器/证书不符 → 只有提示且响应体里无明文；
无 `NAS_USER_ID` 时失败关闭、占位符没渲染时从注册表反推用户号；nginx 模板确实透传证书头）、
令牌注入对 App / 控制台 / 浏览器直连 / `/view/` 四个入口都生效、服务真起一遍打 `/healthz`、
`/api/status`、`/api/settings`、静态文件、未知接口 404、路径穿越拒绝、前端语法与 id 一致性、
前端不把 `token_hint` 当令牌显示、安装脚本占位符校验。
