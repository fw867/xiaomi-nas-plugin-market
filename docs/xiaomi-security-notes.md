# 小米智能存储（minas / RP05）安全观察说明

> **文档性质**：面向厂商的安全问题说明，**不含任何攻击步骤或可执行利用指令**。
> 每条只描述 **现象 / 影响 / 建议修复 / 证据**。
> **测试范围**：设备所有者本人的设备（RP05，固件 1.0.9.462），仅限本地局域网内的只读验证。
> 可信度：🟢 已在固件或设备上确证 ｜ 🟡 有来源未验证 ｜ 🔴 推测

---

## 目录

1. [🟡 中危：`/etc/webdav/delete_to_trash.sh` 对删除路径完全没有归一化（当前为"死代码"）](#1)
2. [🟡 中危：路径归一化补丁覆盖不完整 + 存在无白名单的 `internal` `/home/` 出口](#2)
3. [🟡 中危：`filemgr/get_file` 的 `X-Accel-Redirect` 通道**可读 `/home/**` 下文件**，并暴露路径存在性](#3)
4. [🟡 低危：`realpath -sm` 语义导致"只做字面归一化、不解析符号链接"](#4)
5. [🟡 低危：`delete_to_trash.sh` 把 `env` 全量写入 `/tmp/trash`](#5)
6. [🟡 中低危：`plugincenter/get_access_token` 可用裸客户端证书自助签发 root 上下文令牌](#6)
7. [🔴 中高：`/sysinfo` 鉴权盐值硬编码，导致可预测的弱鉴权](#7)
8. [汇总建议](#8)
9. [🟡 维护性建议：组件版本清单与安全更新流程](#9)（含中枢 `webcfg` 8086 认证策略待确认）

---

<a id="1"></a>
## 1. 🟡 中危：`/etc/webdav/delete_to_trash.sh` 对删除路径**完全没有归一化**（当前为"死代码"）

### 现象
`/etc/webdav/delete_to_trash.sh`（全 28 行）从 CGI 环境变量 `REQUEST_URI` 中直接切分用户与路径，
**未做任何 `..`/符号链接归一化**，随后**以 root 身份执行递归删除**：

```sh
# /etc/webdav/delete_to_trash.sh:7-9,21
user=$(echo "$REQUEST_URI" | cut -d'/' -f2)
path=$(echo "$REQUEST_URI" | cut -d'/' -f3-)
delete_path="/home/$user/$path"           # ★ $path 未做 realpath / .. 过滤
...
rm -rf "$delete_path"                     # ★ root 递归删除
```

### 影响
- 若该脚本可达，`REQUEST_URI` 中携带 `..` 序列即可让 `delete_path` **越出 `/home/<user>/`**，
  从而**以 root 删除任意文件/目录**（例如系统配置目录）。
- **当前不可达**（三重保护，均已确证 🟢）：
  1. `/etc/webdav/nginx/conf.d/webdav/webdav.conf:64-66` 中**唯一**指向它的 `rewrite` 被**注释掉**：
     ```nginx
     #if ($request_method = DELETE) {
     #    rewrite ^ /delete_to_trash;
     #}
     ```
  2. 5000 端口**实际加载**的配置是 `sites-available/webdav_server`，它**不含**
     `location = /delete_to_trash`；该 location 只存在于 `webdav_share_server.template`，
     而该模板**不在** `sites-enabled/`（🟢 目录实测为空）。
  3. 即便存在，`location = /delete_to_trash { internal; }`（🟢 模板 `:40-41`）也要求经**内部重定向**才能到达。

### 为什么仍然值得修
**这不是"没问题"，而是"靠配置巧合没出问题"**：
一旦有人**取消那段注释**，或**启用 share server**（`webdav_mgr.sh:47` 的 `ln -sf` 会把它链入 `sites-enabled`
并 `reload`），该脚本立刻变成 **root 任意路径递归删除**。

### 建议修复
1. **删除**该脚本或**改写为使用归一化后的路径**：
   对 `delete_path` 做一次规范化，并校验结果仍位于 `/home/<user>/` 之内（与 `transcode_404_handler.sh` 同款做法）；
2. 在 `delete_to_trash.sh` 内**加白名单**（只允许 `/home/u*/pool*`、`/home/u*/usb*` 前缀）；
3. 建议**不要保留**这段被注释的 rewrite；如需保留功能，应连同校验一起提交；
4. 该脚本以 root 运行却没有 `set -u`/参数校验，建议改为**降权到对应用户**执行。

### 证据
- `/etc/webdav/delete_to_trash.sh:7-9, 11-17, 21`
- `/etc/webdav/nginx/conf.d/webdav/webdav.conf:64-66`（注释）、`:84`（error_page）
- `/etc/webdav/nginx/sites-available/webdav_server`（无该 location）
- `/etc/webdav/webdav_share_server.template:40-47`
- 设备实测：`/etc/webdav/nginx/sites-enabled/` 为空

---

<a id="2"></a>
## 2. 🟡 中危：路径归一化补丁**覆盖不完整** + 存在**无白名单的 `internal` `/home/` 出口**

### 现象
`/usr/bin/transcode_404_handler.sh` 里有一套**较严格**的路径校验（🟢 `:47-62`）：

```sh
REQUEST_PATH=$(urldecode "$REQUEST_URI")
USER_NAME=${USER_RELATIVE%%/*}
case "$USER_NAME" in u*) ;; *) deny_path ;; esac          # ① 一级目录必须是 u*
LOGICAL_PATH=$(realpath -sm "$REQUEST_PATH") || deny_path
case "$LOGICAL_PATH" in "$USER_ROOT"/*) ;; *) deny_path ;; esac   # ② 必须落在 /home/u*/ 内
```

**但它只挂在一条入口上**：
- 它只由 `/etc/webdav/nginx/conf.d/webdav/webdav.conf:84` 的
  `error_page 404 = @transcode_404;` 触发（🟢），**即只有"产生 404 的请求"才会进入这套校验**；
- 同一配置的主 location 对**成功**的 `PUT / MKCOL / MOVE / COPY / PROPFIND / DELETE`
  **只有一级前缀白名单**（🟢 `:36 if ($uri !~ ^/((pool|usb)[^/]*)(/|$)) { return 451; }`）＋
  `:42` 的一级目录存在性检查，**没有做 `realpath` 归一化**。

### 另有一处**完全没有白名单**的 `/home/` 出口
`/usr/sbin/nas_cgi.init:19-36` 在设备**开机时生成**以下配置并**建立软链**（🟢）：

```sh
# nas_cgi.init:19-33 写入 /etc/nginx/conf.d/luci/file.conf
location /home/ {
    internal;
    alias /home/;            # ★ 整个 /home/ 目录，没有任何路径前缀白名单
    ...
}
# nas_cgi.init:35-36
ln -sf /etc/nginx/sites-available/luci.conf        /etc/nginx/sites-enabled/luci.conf
ln -sf /etc/nginx/sites-available/secure_link.conf /etc/nginx/sites-enabled/secure_link.conf
```

- 这是给 `X-Accel-Redirect` 用的**内部文件出口**，**放行了整个 `/home/`**；
- `/etc/nginx/sites-available/secure_link.conf` 又在 **`listen 8081`** 上暴露**同一份** `/home/` 出口（🟢 `:39-51`）。

### 影响
- **加固不一致**：同一设备上存在"有严格校验的路径"与"只有前缀白名单/完全无白名单的路径"两种风格；
  任何**能达到 `X-Accel-Redirect` 的组件**都会获得一个**未被路径白名单保护的读取出口**。
- 本次已确证：唯一使用 `X-Accel-Redirect` 的组件（`module/filemgr.lua:584`）
  **因自身逻辑问题未能真正读取内容**（见第 3 条）⇒ **当前没有造成越权读取**；
  但**这个出口本身缺少纵深防御**，一旦有新组件使用它、或修好现有组件，风险立即上升。

### 建议修复
1. 给 `location /home/`（以及 8081 的那一份）**加上与 `transcode_404_handler.sh` 一致的白名单**：
   仅允许 `/home/u*/pool*/`、`/home/u*/usb*/` 前缀，或改为 `internal` + `alias` 到**仅池内**的子目录；
2. 把 `transcode_404_handler.sh` 的校验**抽成公共函数**，供所有文件出口复用（消除风格不一致）；
3. 建议对 `X-Accel-Redirect` 的目标做**统一校验层**（例如在 nginx 层用 `secure_link` 或后端统一过滤）。

### 证据
- `/usr/bin/transcode_404_handler.sh:45-62`
- `/etc/webdav/nginx/conf.d/webdav/webdav.conf:35-46, 64-66, 84-108`
- `/usr/sbin/nas_cgi.init:19-36`
- `/etc/nginx/sites-available/secure_link.conf:30-51`
- `/etc/webdav/nginx/sites-available/webdav_server:42-53`

---

<a id="3"></a>
## 3. 🟡 中危：`filemgr/get_file` 的 `X-Accel-Redirect` 通道可读 **`/home/**` 下任意文件**，并暴露路径存在性

### 现象
`/usr/lib/lua/5.1/luci/module/filemgr.lua:540-591` 的 `get_file()`：

```lua
local path = jsonTable["path"]                 -- 完全来自请求
local file_stat = fs.lstat(path)
if not file_stat then return { code = 1105 } end          -- 不存在
if file_stat.type ~= "reg" then return { code = 1003 } end -- 类型不是 reg
http.header("X-Accel-Redirect", path)                     -- ★ 原样作为内部重定向目标
```
而 nginx 侧的内部出口是（由 `/usr/sbin/nas_cgi.init:19-33` 生成）：
```nginx
location /home/ { internal; alias /home/; }     # ★ 整个 /home/，无路径白名单
```

### 影响（三件事）

**(a) ★ 经 `X-Accel-Redirect` 可以**读到 `/home/` 下文件的真实内容** —— 🟢 **设备实测**：
```
POST /cgi-bin/luci/filemgr/get_file {"path":"/home/u3943892/pool0/data/Docker/.v.subcfg"}
   -> HTTP 200  Content-Type: application/octet-stream  Content-Length: 96
   -> 响应体为文件真实内容（96 字节）
```
⇒ 由于 `location /home/` 用的是 `alias /home/` 且**没有任何白名单**，
**凡是落在 `/home/` 之下的文件，其内容都可以被读取**（包括各账号家目录里的配置、密钥、令牌类文件）。
**这不是"读不到内容"，而是"读取范围被限定在 `/home/` 之内"。**

**(b) 但 `../` 路径穿越被 nginx 拦住 —— 🟢 设备实测**：
```
/home/../etc/hostname                       -> HTTP 404
/home/../../etc/hostname                    -> HTTP 404
/home/u3943892/../../etc/hostname           -> HTTP 404
/home/u3943892/../../../etc/hostname        -> HTTP 404
/etc/hostname                               -> HTTP 404（对照）
/home/../../../data/central/default_image.tar.xz -> HTTP 404
```
⇒ nginx 在 `alias /home/` 之前**已完成路径归一化**，`..` 无法越出 `/home/`
⇒ 🔴 **因此不是"任意文件读取"**（`/etc`、`/data`、`/root` 等都读不到）。

**(c) 同时它还是一个"路径存在性 + 类型"探测面** —— 🟢 三档返回可区分：
| 返回 | 含义 |
| --- | --- |
| `200` + `code=1105` | **路径不存在** |
| `200` + `code=1003` | **存在且是目录** |
| `HTTP 404`（无 JSON）| **存在但不是 `/home/` 下的普通文件**（`X-Accel-Redirect` 已触发但内部未命中） |
| `200` + 真实内容 | **存在且是 `/home/` 下的普通文件**（内容被返回） |

⇒ 仅凭客户端证书即可**枚举设备文件系统结构**（某配置/密钥/插件目录是否存在），
并且 **`/home/**` 内的文件内容会直接返回**。这属于**信息泄露**：
`/home/` 下通常存放各账号的家目录、插件数据与可能的凭据文件。

### 建议修复
1. ★ **给 `location /home/` 加白名单**：只允许 `/home/u*/pool*/`、`/home/u*/usb*/` 前缀
   （与 `transcode_404_handler.sh` 一致），或改为指向**仅池内**子目录的 `alias`；
   **8081 端口那份 `secure_link.conf` 里的同一个 `/home/` 出口也要一并收窄**；
2. **`X-Accel-Redirect` 的值不应由客户端直接决定**：应改为"服务端查表得到的内部 ID → 真实路径"，
   并在服务端做归一化 + 前缀白名单（不能只依赖 nginx 的 `..` 归一化）；
3. 修正 `get_file` 的类型判定（`type ~= "reg"`）时 **必须同时**把上述白名单补齐 ——
   否则该通道的范围仍是整个 `/home/`；
4. 统一返回码：不要把"不存在"与"类型不符/不可服务"用**不同的 JSON code**暴露给客户端；
5. 建议评估 `/home/` 下是否真的存在**任何需要被这个内部出口服务的**文件；
   若只用于池内下载，应把出口直接指向池目录。

### 证据
- `/usr/lib/lua/5.1/luci/module/filemgr.lua:540-591`（尤其 `:584`）
- `/usr/sbin/nas_cgi.init:19-33`（生成 `location /home/ { internal; alias /home/; }`）
- `/etc/nginx/sites-available/secure_link.conf:39-51`（8081 上的同一出口）
- 设备实测：上文 (a)(b)(c) 三组请求
  （**.本报告的验证过程只读了 1 个我们自己的 96 字节测试文件**；
  穿越测试只读到 nginx 的 404 页面，未取得任何 `/home/` 之外的内容）

---

<a id="4"></a>
## 4. 🟡 低危：`realpath -sm` 语义导致"只做**字面归一化**、不解析符号链接"

### 现象
`/usr/bin/transcode_404_handler.sh:58`：
```sh
LOGICAL_PATH=$(realpath -sm "$REQUEST_PATH" 2>/dev/null) || deny_path
```
- 🟢 **GNU coreutils 的 `realpath -s` = `--no-symlinks`**：**不展开符号链接**；
  `-m` = `--canonicalize-missing`：允许路径不存在。
- ⇒ 该调用**只做字面归一化**（处理 `..` / `.` / 重复斜杠），**不会解析路径中的符号链接**。

### 影响
- 紧随其后的 `[ -f "$REQUEST_PATH" ]`、`stat -c%s`、`tail`、`cat` **都会跟随符号链接**（内核行为）。
- ⇒ **原理上**：若目录树中存在指向 `/etc` 等位置的符号链接，`-sm` **不会发现**，
  而后续读取会**跟随**到真实目标。
- 🟢 **同族脚本的写法不一致**：`/usr/bin/plugin.sh:23,37,69,78` 用的是 **`readlink -f`（完全解析）**，
  这是**正确**的用法 ⇒ 说明项目内**没有统一的路径校验规范**。

### 建议修复
1. 目录逃逸校验应使用**能解析符号链接**的方式（`realpath -e`/`readlink -f`），并**校验解析后的结果**；
2. 若确实需要"字面归一化"，应在**归一化之后再做一次 `realpath` 并二次校验**；
3. 更稳妥：在 WebDAV 层启用 nginx 的 `disable_symlinks`（当前配置**未启用**，🟢 全盘无该指令）；
4. 建议把路径校验抽成**公共函数**，所有文件出口统一调用。

### 证据
- `/usr/bin/transcode_404_handler.sh:58-62`
- `/usr/bin/plugin.sh:23, 37, 69, 78`
- `grep -r disable_symlinks /etc` → 无命中

---

<a id="5"></a>
## 5. 🟡 低危：`delete_to_trash.sh` 把 `env` 全量写入 `/tmp/trash`

### 现象
`/etc/webdav/delete_to_trash.sh:3-4`：
```sh
echo "$(date +%Y%m%d%H%M%S)" > /tmp/trash
env  >> /tmp/trash
```
🟢 该脚本每次运行都会把**完整环境变量**（含 CGI 注入的 `REQUEST_URI`、`HTTP_*`、可能的
`HTTP_AUTHORIZATION` 等）**追加写入世界可写的 `/tmp/trash`**。

### 影响
- **调试残留**：`/tmp/trash` 会持续累积环境变量快照；
- 若其中包含**认证信息**（如 `HTTP_AUTHORIZATION`，见 `/etc/nginx/fastcgi_params:2` 确实会传递该头），
  则等于把凭据**明文写进可写目录**；
- `/tmp` 属于可写面（🟢 本项目已确证 `/tmp` 可写），因此**低权限进程/用户可能读到**。

### 建议修复
1. **删除这两行调试代码**；
2. 若需排查，应写入**仅 root 可读**的日志目录并对敏感字段打码；
3. 排查其它 CGI 脚本是否也有类似 `env` 落盘（本轮已全盘扫过，**仅此一处**）。

### 证据
- `/etc/webdav/delete_to_trash.sh:3-4`
- `/etc/nginx/fastcgi_params:1-2`（`SSL_CLIENT_CERT`、`HTTP_AUTHORIZATION` 会被传入 CGI）

---

<a id="6"></a>
## 6. 🟡 中低危：`plugincenter/get_access_token` 可用**裸客户端证书**自助签发 root 上下文令牌

### 现象
`/usr/lib/lua/5.1/luci/module/plugincenter.lua:33-49`：
```lua
path = {"plugincenter", "get_access_token"},
auth_method = {
    func = {"cert_auth"},     -- ★ 只允许证书认证
    setuser = false           -- ★ 认证后保持 root 执行
}
```
实现（`:172-200` → `NasAuth.lua:63-116`）用 `MIUID` + `DEV_TYPE` 标识会话，
生成 **32 位令牌**，落在 `/tmp/luci-token/`，**有效期 1800 秒且每次校验自动续期**。

🟢 **设备实测**（仅调用 1 次，只读语义、未改变任何状态）：
```
POST /cgi-bin/luci/plugincenter/get_access_token {}  -> 200
{"result":"成功","data":{"token":"<32 位>"},"code":0}
```

### 影响
`token_auth` 的设计意图是"比 `cert_auth` 更细粒度、更可撤销"的**会话级**凭据。
但既然**任何持有客户端证书的人都能自己签发**一个合法令牌，
`token_auth` 的**保护强度就被降级到与 `cert_auth` 等同** —— 它不再构成"第二道门槛"。

### 建议修复
1. `get_access_token` 应要求**额外的用户交互确认**，或绑定到一次**已完成的用户登录会话**，
   而不是仅凭设备证书即可签发；
2. 对签发做**速率/来源限制**；
3. 令牌应**绑定具体的 API 白名单**（例如只允许插件自身的前端调用）；
4. TTL 30 分钟且**可无限续期**，建议改为一次性或"短 TTL + 不可续期"。

### 证据
- `/usr/lib/lua/5.1/luci/module/plugincenter.lua:33-49, 172-200`
- `/usr/lib/lua/5.1/luci/function/NasAuth.lua:3-5, 30-56, 63-116`
- 设备实测响应（已存档，仅 1 次调用）

---

<a id="7"></a>
## 7. 🔴 中高：`/sysinfo` 鉴权**盐值硬编码**，导致可预测的弱鉴权

### 现象
`/usr/sbin/sysinfo`（由 `/etc/nginx/80.conf/sysinfo.nginx.conf` 以 `NAS_AUTH_METHOD=8` 暴露）：
```sh
# /usr/sbin/sysinfo:19（盐值硬编码在脚本里）
hash = sha256(SN + MAC + "5a4f121986a0676d75bb9bfb12ad8c16a3a88bcdb27db7b7f4f89797421f5f08")[0:8]
```
它校验的只是一个 **8 位十六进制** 摘要，且**参与计算的三个要素都可获得**：
- `SN` 与 `MAC` 印在**设备外包装/机身标签**上，App 的「设备信息」页也显示；
- 盐值是**源码里的常量**。

### 影响
- **任何能访问该端口的人**，只要拿到设备标签上的 SN/MAC，即可**自行算出该 hash** 并通过校验；
- 通过后返回**设备敏感信息**（🟢 脚本返回）：`SN`、`MAC`、`MODEL`、固件版本、
  `secboot` 状态、`rpmb` 状态、`minas_sys_mode`；
- 8 位摘要的**有效强度仅 32 bit**，即使不知道 SN/MAC 也存在暴力空间。

### 建议修复
1. **不要把盐值硬编码**在设备脚本里：改为**每设备唯一的密钥**，且存于安全存储（如 RPMB/TPM）；
2. 校验应包含**时间戳/nonce + HMAC**，防重放；不要用"常量拼接再截断"的做法；
3. 提高摘要长度（≥128 bit），并限制调用频率与来源 IP；
4. 只返回**必需**字段；`secboot`/`rpmb` 状态等内部信息建议移除或要求更强鉴权。

### 证据
- `/usr/sbin/sysinfo:19`（盐值常量）
- `/etc/nginx/80.conf/sysinfo.nginx.conf:1-6`（`location /sysinfo`、`NAS_AUTH_METHOD "8"`）
- `/etc/nginx/sites-available/default_server:2-7`（`listen 80` + `include /etc/nginx/80.conf/*.conf`）

---

<a id="8"></a>
## 8. 汇总建议（按优先级）

| 优先级 | 问题 | 建议动作 |
| --- | --- | --- |
| **P0** | §1 `delete_to_trash.sh` 无路径归一化 | **立即修**：加白名单 + 归一化校验；并清掉那段被注释的 rewrite |
| **P0** | §2 / §3 `location /home/` 无白名单（含 8081 那份），**导致 `/home/**` 文件内容可被读取** | 立刻给该出口加白名单（仅池目录）；`X-Accel-Redirect` 目标改为服务端映射 |
| **P1** | §3 `X-Accel-Redirect` 由客户端直接决定目标 + 错误码泄露路径存在性 | 改为服务端映射；补齐白名单后再修类型判定；统一错误码 |
| **P1** | §7 `/sysinfo` 硬编码盐值 | 换为每设备唯一密钥 + HMAC + 时间戳 |
| **P2** | §6 `get_access_token` 可自助签发 | 要求用户交互/登录态绑定；令牌绑定 API 白名单；短 TTL 不可续期 |
| **P2** | §4 `realpath -sm` 不解析符号链接 | 统一路径校验规范；启用 `disable_symlinks` |
| **P3** | §5 `env` 写入 `/tmp/trash` | 删除调试代码 |
| **P3** | §9 组件版本偏旧（nginx 1.24.0 / dropbear 2022.83 / Samba 4.19.x 等）| 建立 SBOM 与例行安全更新流程；定期复核监听面 |
| **P3** | §9 附：中枢 `webcfg` 8086 免认证可读 | **确认其后端配置接口的认证策略** |

### 贯穿性问题（建议作为一次专项）
**路径安全校验在项目内没有统一规范**：同一固件里同时存在
`. /lib/nas/library.sh` 式的固定路径（安全）、`readlink -f` 式完全解析（正确）、
`realpath -sm` 式只做字面归一化（不足），以及**完全没有校验**（`location /home/`）。
而 `location /home/ { internal; alias /home/; }` 这类**无白名单的内部出口**，
一旦被任何组件用作用户可控路径的重定向目标，就会**把整个 `/home/` 暴露为可读面**（本次已实测）。
建议：
1. 抽出**统一的路径校验函数**（解析符号链接 + 二次前缀校验 + 拒绝控制字符）；
2. 所有**文件出口**（WebDAV location、`X-Accel-Redirect` 目标、CGI 脚本）强制调用；
3. 所有**以 root 执行**的 shell 脚本，其外部入参**必须**先过白名单（本固件 1.0.9.462 中
   `storage/stop_raid` 的 `^/dev/md[0-9]+$` 校验**就是好的范例**，建议推广）；
4. **`X-Accel-Redirect` / `internal` 出口的可见范围应"最小化"**：
   只指向确实需要对外提供的目录，而不是 `alias` 一个顶层目录。

---

<a id="9"></a>
## 9. 🟡 维护性建议：组件版本清单与安全更新流程

> 本节**只列版本与建议**，不含任何利用步骤。目的是便于厂商侧建立**例行升级流程**。

### 现象
设备固件（`1.0.9.462`）中以下网络相关组件版本**偏旧**（🟢 均为静态读取的版本串）：

| 组件 | 版本 | 说明 |
| --- | --- | --- |
| **nginx** | `1.24.0` | 对外监听 80 / 443 / 8081 / 5000，**暴露面最大** |
| **dropbear** | `2022.83` | SSH 服务（22，publickey only）|
| **Samba**（smbd/nmbd）| `4.19.x` 区间 | 445 / 139 |
| **OpenSSL** | `3.2.2`（2024-06-04）| 全链路 TLS |
| **GStreamer** | `1.22.12` | 媒体解析/转码链 |
| **FFmpeg**（libavcodec/libavformat）| `6.0.x` | 媒体解析/转码链 |
| **mosquitto** | `2.0.18` | 中枢网关 MQTT（8883，已强制 mTLS）|

### 影响
单个组件版本偏旧**本身不构成漏洞**；但**网络暴露面组件（nginx / dropbear / Samba）长期不跟进安全更新**，
会使设备在未来某个已披露漏洞面前处于被动。**本次审计未发现这些组件上"预认证且局域网可达"的可利用漏洞**
（Samba 不在 `CVE-2017-7494` 影响区间且未配置 `wins hook`；nginx 未编译 mp4 模块；
dropbear 相关 CVE 属 MITM 类；mosquitto 强制 mTLS）—— 但这只说明**当下**，不代表后续。

### 建议修复
1. **建立组件版本清单（SBOM）与例行安全更新流程**，至少覆盖 nginx / dropbear / Samba；
2. 对外服务**按最小必要开放端口**，并定期复核监听面；
3. 媒体解析链（FFmpeg / GStreamer）建议**在受限身份下处理不可信媒体**（当前转码/缩略图链的触发面
   已被限制在用户自身家目录内，这一设计值得保持）；
4. 厂商自研守护进程（`miio_client` / `ipc_mgr` / `mediacenterd` / `albumd` / `filemgr` 等）
   **未内嵌版本串**，建议补充以便资产管理与更新追溯。

### 证据
- 版本串取自固件解包文件：`/usr/sbin/nginx`、`/usr/sbin/dropbear`（→`dropbearmulti`）、
  `/usr/sbin/smbd`+`nmbd`、`/usr/lib/libcrypto.so.3`、`/usr/lib/libgstreamer-1.0.so.0.2212.0`、
  `/usr/lib/libavcodec.so.60.31.102`、`/usr/sbin/mosquitto`
- 监听面：静态配置（`/etc/nginx/**`、`/etc/webdav/nginx/**`、`/etc/samba/smb.conf`）
  + **只读 TCP 连通性探测**（无任何载荷）

### ⚠️ 附：中枢网关 `webcfg` 端口（8086）建议确认认证策略

🟢 **只读实测**：`http://<设备>:8086/` 返回 `200`，`Server: nginx/1.24.0`，
页面标题「**米家自动化极客版**」（响应头含 `x-mijia-geek-version: 2.0.0`、`x-xiaomi-meta-acl: PUBLIC_READ`），
**静态首页无需任何凭据即可读取，且带任意凭据结果相同**。

🟡 **需厂商确认**：该端口由**中枢网关容器内**提供（宿主侧 nginx 未声明 `listen 8086`），
**建议确认其后端配置接口的认证策略**（若配置接口同样免认证且可写，应视为需修复项）。
🔴 **本报告未测试、也未尝试任何写操作**（见 §8 合规说明）。

---

*本文档由设备所有者整理，用于向小米官方反馈安全问题。所有验证均为**只读**，未读取任何用户数据、未修改设备状态。*
