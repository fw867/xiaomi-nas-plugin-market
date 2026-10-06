"""路由器软件中心插件：把局域网里的 UniFi SoftCenter 反代到小米存储的插件页，实现远程管理。

设计要点（和「控制台」插件同一套经过验证的做法）：
  * 纯标准库，目标机没有 pip；
  * 浏览器入口与客户端入口共用同一份前端；
  * 唯一会「写系统」的动作是渲染自己的那一个 nginx 配置文件（换目标地址时），
    且必定 nginx -t 通过才 reload，失败回滚；
  * 路由器那个软件中心的 AdminToken 存在插件自己的目录（0600），只通过本机接口
    交给管理员，前端拿到后写进 localStorage 供被嵌入的页面使用。
"""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 常量与路径
# ---------------------------------------------------------------------------

PLUGIN_KEY = os.environ.get('PLUGIN_KEY', 'rtrcenter')
RELEASE_DIR = Path(__file__).resolve().parent
VERSION_FILE = RELEASE_DIR / 'VERSION'
RELEASE_DIR_PATTERN = re.compile(r'^v?(\d+\.\d+\.\d+(?:-[A-Za-z][0-9A-Za-z.]*)*)(?:-\d+)+$')

PLUGIN_ROOT = Path(os.environ.get('PLUGIN_ROOT', '/data/plugin/router-center'))
STATE_FILE = Path(os.environ.get('STATE_FILE', str(PLUGIN_ROOT / 'settings.json')))
TOKEN_FILE = Path(os.environ.get('TOKEN_FILE', str(PLUGIN_ROOT / 'router-token')))
HOME_ROOT = Path(os.environ.get('HOME_ROOT', '/home'))
# 插件注册表目录（/data/plugin/<用户>.list）：用来反推本插件装给哪个用户（见 plugin_owner）
REGISTRY_ROOT = Path(os.environ.get('REGISTRY_ROOT', '/data/plugin'))

NGINX_CONF = Path(os.environ.get('NGINX_CONF', '/etc/nginx/conf.d/luci/xiaomi-router-center.conf'))
NGINX_TEMPLATE = Path(os.environ.get('NGINX_TEMPLATE',
                                     str(RELEASE_DIR / 'deploy' / 'xiaomi-router-center.nginx.conf')))
DEFAULT_TARGET = os.environ.get('ROUTER_TARGET', 'http://192.168.1.1:9958/')
PROBE_TIMEOUT = float(os.environ.get('PROBE_TIMEOUT', '6'))
_toggle_lock = threading.Lock()


def log(message: str) -> None:
    stamp = time.strftime('%Y-%m-%d %H:%M:%S')
    print(f'[{stamp}] {message}', flush=True)


def read_text(path: str | Path) -> str:
    try:
        return Path(path).read_text(encoding='utf-8', errors='replace')
    except OSError:
        return ''


def run(command: list[str], timeout: float = 15) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)


# ---------------------------------------------------------------------------
# 版本号（与「控制台」同一套规则：安装目录名 > INFO > VERSION 文件）
# ---------------------------------------------------------------------------


def version_from_release_dir(path: Path) -> str:
    if path.parent.name != 'releases':
        return ''
    match = RELEASE_DIR_PATTERN.match(path.name)
    return match.group(1) if match else ''


def _read_version_file() -> str:
    try:
        return VERSION_FILE.read_text(encoding='utf-8').strip()
    except OSError:
        return ''


def _read_info_version() -> str:
    user = os.environ.get('NAS_USER_ID', '')
    if not user:
        return ''
    info_path = HOME_ROOT / user / 'plugin' / PLUGIN_KEY / 'INFO'
    try:
        info = json.loads(info_path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return ''
    version = info.get('version') if isinstance(info, dict) else None
    return version.strip() if isinstance(version, str) else ''


def load_version() -> str:
    for candidate in (version_from_release_dir(RELEASE_DIR), _read_info_version()):
        if candidate:
            return candidate
    return _read_version_file()


VERSION = load_version()

# ---------------------------------------------------------------------------
# 设置（目标地址 + 路由器令牌）
# ---------------------------------------------------------------------------


def atomic_write(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(text, encoding='utf-8')
    os.chmod(temporary, mode)
    temporary.replace(path)
    os.chmod(path, mode)


def load_settings() -> dict[str, Any]:
    try:
        payload = json.loads(STATE_FILE.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        payload = {}
    settings = payload if isinstance(payload, dict) else {}
    target = str(settings.get('target') or '').strip() or DEFAULT_TARGET
    return {'target': normalize_target(target)}


def save_settings(values: dict[str, Any]) -> dict[str, Any]:
    settings = load_settings()
    if 'target' in values:
        settings['target'] = normalize_target(str(values.get('target') or ''))
    atomic_write(STATE_FILE, json.dumps(settings, ensure_ascii=False, indent=2) + '\n')
    return settings


def normalize_target(raw: str) -> str:
    """把用户填的地址规整成 http://host:port/ 形式（必须以 / 结尾，反代才好拼路径）。"""
    value = (raw or '').strip()
    if not value:
        raise RuntimeError('目标地址不能为空')
    if not re.match(r'^https?://', value, re.IGNORECASE):
        value = 'http://' + value
    match = re.match(r'^(https?://)([^/]+)(/.*)?$', value, re.IGNORECASE)
    if not match:
        raise RuntimeError('目标地址格式不对，示例：http://192.168.1.1:9958/')
    scheme, authority, _path = match.groups()
    if not re.match(r'^[A-Za-z0-9.\-]+(:\d{1,5})?$', authority):
        raise RuntimeError('目标地址的主机名/端口不合法')
    return f'{scheme.lower()}{authority}/'


def router_token() -> str:
    try:
        return TOKEN_FILE.read_text(encoding='utf-8').strip()
    except OSError:
        return ''


def token_hint(token: str | None = None) -> str:
    """令牌的展示提示（绝不等于令牌本身）：够长就"前 4 位…后 2 位"，否则只说"已设置"。"""
    value = router_token() if token is None else (token or '')
    if not value:
        return ''
    return (value[:4] + '…' + value[-2:]) if len(value) >= 6 else '已设置'


def _user_id_digits(user: str) -> str:
    """把用户名规整成"用户号"：注册表里可能是 u3943892，客户端证书里是 nas.3943892.*。"""
    value = (user or '').strip()
    return value[1:] if value[:1].lower() == 'u' else value


def plugin_owner() -> str:
    """本插件是装给哪个小米用户的（拿它去核对客户端证书 CN 里的用户号）。

    优先环境变量 `NAS_USER_ID`；没有就反推（这个单元文件刻意保持无占位符，商店那套
    安装路径不一定会渲染它，所以不能只靠环境变量）：
      1. `/data/plugin/<用户>.list` 注册表里含本插件 key 的那个文件；
      2. `/home/<用户>/plugin/rtrcenter/` 这种插件目录（只有一个才认，多个不猜）。
    都拿不到就返回空串 —— 调用方按"不给明文"处理（失败关闭）。
    """
    from_env = os.environ.get('NAS_USER_ID', '').strip()
    if from_env and '__' not in from_env:                 # 占位符没被渲染时当没有，别拿去比对
        return from_env
    try:
        for path in sorted(REGISTRY_ROOT.glob('*.list')):
            name = path.name[:-len('.list')]
            if not name.startswith('u'):
                continue
            try:
                payload = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict) and PLUGIN_KEY in payload:
                return name
    except OSError:
        pass
    try:
        # path 形如 /home/<用户>/plugin/rtrcenter → 用户目录是再上一层
        owners = [path.parent.parent.name for path in HOME_ROOT.glob(f'*/plugin/{PLUGIN_KEY}')]
    except OSError:
        return ''
    return owners[0] if len(owners) == 1 else ''


def client_certificate_owner(verify: str, dn: str, user: str) -> bool:
    """请求是否来自"设备所有者的小米客户端"（判据与其余插件里的 trusted() 同一套）。

    nginx（443 的 server 块）把 `$ssl_client_verify` / `$ssl_client_s_dn` 透传成
    `X-Xiaomi-Client-Verify` / `X-Xiaomi-Client-DN`，所以判据是：

      * 验签结果必须是 SUCCESS；
      * DN 里的 CN 必须是 `nas.<用户号>.…`。

    控制台那一跳（本机 5001 → proxy_pass 到 127.0.0.1:443）**没有**客户端证书，
    `$ssl_client_verify` 是 NONE；插件的 nginx 又对所有请求都写死 `X-Console-Entry: xiaomi`，
    所以那个头区分不出来 —— 只有客户端证书能。拿不到证书就不给明文（失败关闭）。
    """
    if (verify or '').strip().upper() != 'SUCCESS':
        return False
    digits = _user_id_digits(user)
    if not digits:
        return False
    return re.search(r'CN=nas\.' + re.escape(digits) + r'\.', dn or '') is not None


# ---------------------------------------------------------------------------
# 转发时的 Authorization 决策
#
# 背景（实际报障）：从「控制台」电脑端的插件图标打开本插件时，页面由控制台的本机
# nginx（默认 5001，server 块见 xiaomi-nas-console-lan.nginx.conf）转到本机 443，
# 而那一跳会无条件写 `proxy_set_header Authorization "Bearer console-loopback"`
# 去过 443 的"至少一种凭据"门槛 —— 客户端原本的 Authorization 被这个占位值覆盖。
# 转发链再往下是插件自己的 nginx（proxy_set_header Authorization $http_authorization）
# 原样透传，于是路由器软件中心拿到 "Bearer console-loopback"，AdminToken 校验失败，
# 页面显示「令牌不正确」。
#
# 所以转发前要认得出这个占位值：认出来就用插件自己保存的令牌（TOKEN_FILE，0600）顶上；
# 认不出来（小米 App 注入的真实令牌、或客户端自己带的凭据）必须原样透传，不许覆盖。
# ---------------------------------------------------------------------------

# 与控制台 deploy/xiaomi-nas-console-lan.nginx.conf 里的字面量保持一致（有测试核对）
CONSOLE_LOOPBACK_TOKEN = 'console-loopback'
CONSOLE_PLACEHOLDER_AUTHORIZATION = f'Bearer {CONSOLE_LOOPBACK_TOKEN}'


def is_console_placeholder_authorization(value: str) -> bool:
    """判断 Authorization 是不是控制台那一跳塞进来的占位值。

    容错：授权方案大小写、Bearer 前后多写的空格、只剩裸值（没有方案）都认。
    """
    raw = (value or '').strip()
    if not raw:
        return False
    scheme, separator, credentials = raw.partition(' ')
    if separator and scheme.strip().lower() != 'bearer':
        return False
    return (credentials if separator else raw).strip() == CONSOLE_LOOPBACK_TOKEN


def resolve_authorization(value: str) -> str:
    """返回真正要转发给路由器的 Authorization；返回空串表示这一跳不带 Authorization。

    规则：
      * 客户端自己带了凭据（App 注入的真实令牌、浏览器/页面自己设的值）→ 原样返回，不动；
      * 没带凭据 → 用插件保存的令牌顶上（本地插件用自己的凭据，就不再 401）；
      * 带的是控制台那一跳的占位值 → 也当"没带"，换成保存的令牌；
      * 该顶上却没有保存令牌时返回空串（宁可不带，也别把占位值当令牌送去，否则软件中心
        只能报"令牌不正确"，看不出是哪一环的问题），并写日志说明下一步该做什么。
    """
    client_value = (value or '').strip()
    if client_value and not is_console_placeholder_authorization(client_value):
        return client_value
    saved = router_token()
    if saved:
        if client_value:
            log('收到控制台的占位 Authorization，改用插件保存的路由器令牌转发')
        else:
            log('请求里没有 Authorization，改用插件保存的路由器令牌转发')
        return saved
    if client_value:
        log(f'收到控制台的占位 Authorization（{CONSOLE_PLACEHOLDER_AUTHORIZATION}），'
            '但插件还没保存路由器令牌：本次转发不带 Authorization，'
            '路由器会回 401；请在 Unifi 设置页填入 AdminToken 后保存')
    else:
        log('请求里没有 Authorization，插件也还没保存路由器令牌：'
            '本次转发不带 Authorization，路由器会回 401；'
            '请在 Unifi 设置页填入 AdminToken 后保存')
    return ''


def save_router_token(token: str) -> None:
    token = (token or '').strip()
    if not token:
        TOKEN_FILE.unlink(missing_ok=True)
        return
    atomic_write(TOKEN_FILE, token + '\n', mode=0o600)


# ---------------------------------------------------------------------------
# 探测路由器软件中心
# ---------------------------------------------------------------------------


def probe_router(target: str | None = None, token: str | None = None) -> dict[str, Any]:
    """探测目标是否可用：先取首页（不需要令牌），再带令牌取一次 /api/system/info。

    只读，不会改动路由器上的任何东西。
    """
    base = normalize_target(target or load_settings()['target'])
    token = router_token() if token is None else token
    result: dict[str, Any] = {
        'target': base,
        'reachable': False,
        'status': 0,
        'latency_ms': None,
        'title': '',
        'server': '',
        'authenticated': False,
        'device': '',
        'version': '',
        'error': '',
        'at': int(time.time()),
    }
    started = time.time()
    body = ''
    try:
        request = urllib.request.Request(base, headers={'User-Agent': 'XiaomiNasRouterCenter/' + (VERSION or '?')})
        with urllib.request.urlopen(request, timeout=PROBE_TIMEOUT) as response:
            body = response.read(4096).decode('utf-8', 'replace')
            result['status'] = response.status
            result['reachable'] = response.status == 200
            result['server'] = (response.headers.get('Server') or '')
    except urllib.error.HTTPError as error:
        result['status'] = error.code
        result['reachable'] = error.code < 500
        result['error'] = f'HTTP {error.code}'
    except Exception as error:                                     # noqa: BLE001
        result['error'] = str(error) or error.__class__.__name__
    result['latency_ms'] = round((time.time() - started) * 1000, 1)

    match = re.search(r'<title>(.*?)</title>', body, re.IGNORECASE | re.DOTALL)
    if match:
        result['title'] = match.group(1).strip()[:80]

    if result['reachable'] and token:
        try:
            info_request = urllib.request.Request(
                base + 'api/system/info',
                headers={'Authorization': token, 'User-Agent': 'XiaomiNasRouterCenter/' + (VERSION or '?')})
            with urllib.request.urlopen(info_request, timeout=PROBE_TIMEOUT) as response:
                payload = json.loads(response.read() or b'{}')
            result['authenticated'] = True
            if isinstance(payload, dict):
                result['device'] = str(payload.get('Device') or payload.get('device') or '')
                result['version'] = str(payload.get('Version') or payload.get('version') or '')
        except urllib.error.HTTPError as error:
            result['authenticated'] = error.code != 401
            if error.code == 401:
                result['error'] = result['error'] or '令牌无效'
        except Exception as error:                                 # noqa: BLE001
            result['error'] = result['error'] or str(error)
    return result


# ---------------------------------------------------------------------------
# 页面重写：把软件中心依赖的外部 CDN 换成本地副本
#
# 为什么需要：小米客户端打开插件页用的是 App 内置 webview，这类 webview 常常只允许
# 访问 NAS 自己的域名（外部域名被拦），而软件中心的前端是从 jsdelivr / tailwindcss.com
# 加载 Vue、Tailwind、Lucide 的 —— 资源被拦，iframe 里就是一片空白。
# 所以这里把页面取回来、把三个 CDN 脚本换成本插件自带的副本（web/assets/），
# 再交给客户端，整条链路就只剩 NAS 一个域名。
# ---------------------------------------------------------------------------

ASSET_ROUTES: tuple[tuple[str, str], ...] = (
    ('cdn.tailwindcss.com', 'assets/tailwind.js'),
    ('cdn.jsdelivr.net/npm/vue@', 'assets/vue.js'),
    ('cdn.jsdelivr.net/npm/lucide@', 'assets/lucide.js'),
)
SCRIPT_SRC_PATTERN = re.compile(r'(<script[^>]*\bsrc=")(https?://[^"]+)(")', re.IGNORECASE)
LOCAL_ASSETS = ('assets/vue.js', 'assets/lucide.js', 'assets/tailwind.js')

# 软件中心前端还会在浏览器里直接 fetch GitHub（云端插件库、Release 检查、安装前的哈希校验）。
# App 内 webview 拦外网时这些都会失败，所以一并改走 NAS 转发（NAS 能直连 GitHub）。
GITHUB_ROUTES: tuple[tuple[str, str], ...] = (
    ('https://raw.githubusercontent.com/', 'github/raw/'),
    ('https://api.github.com/', 'github/api/'),
)


# 页面在插件里的路径段：GitHub 转发要经这个前缀（走插件服务），
# 而本地资源是 nginx 直接从 UI 目录发的，所以两者前缀不同
PAGE_SEGMENT = 'view'


def prepare_page(html: str, prefix: str, service_base: str, token: str = '') -> tuple[str, list[str]]:
    """把软件中心页面加工成"能直接在 App 里打开"的版本：

    1. 外部 CDN（Vue/Tailwind/Lucide）换成本地副本；
    2. 页面里直连 GitHub 的地址改走本插件的转发（App 内 webview 访问不了外网）；
    3. 已保存的 AdminToken 直接注入 localStorage（同源，软件中心一加载就通过鉴权）；
    4. 补一个本地 favicon（否则 App 会去请求站点根目录的 /favicon.ico 报 404）。

    注入的脚本（配置门或常驻设置按钮）由调用方按配置状态决定，见 server._serve_router_page。
    """
    rewritten, unknown = rewrite_page(html, prefix, service_base)
    head = f'<link rel="icon" href="{prefix}/icon.png">'
    rewritten = rewritten.replace('</head>', head + '</head>', 1) if '</head>' in rewritten else head + rewritten
    if token:
        payload = json.dumps(token)
        rewritten = rewritten.replace('</head>',
                                      f'<script>try{{localStorage.setItem("sc_token",{payload});}}catch(e){{}}</script></head>', 1)
    return rewritten, unknown


def rewrite_page(html: str, prefix: str, service_base: str = '') -> tuple[str, list[str]]:
    """把页面里的 CDN 脚本与 GitHub 直连地址换成本插件的路径。

    * 本地资源（Vue/Tailwind/Lucide）由 nginx 直接从插件 UI 目录发 → 用 {prefix}/assets/…
    * GitHub 转发要走插件服务 → 用 {service_base}/github/…（例如 /plugin/3943892/rtrcenter/ctl）
    """
    base = '/' + prefix.strip('/') if prefix.strip('/') else ''
    service = '/' + service_base.strip('/') if service_base.strip('/') else base
    unknown: list[str] = []

    def replace(match: re.Match[str]) -> str:
        head, url, tail = match.groups()
        for marker, local in ASSET_ROUTES:
            if marker in url:
                return f'{head}{base}/{local}{tail}'
        unknown.append(url)
        return match.group(0)

    rewritten = SCRIPT_SRC_PATTERN.sub(replace, html)
    for upstream, local in GITHUB_ROUTES:
        rewritten = rewritten.replace(upstream, f'{service}/{local}')
    return rewritten, unknown


def github_upstream(rest: str) -> str:
    """把 `github/raw/...`、`github/api/...` 映射回真正的 GitHub 地址（不认识就返回空串）。"""
    path = rest.lstrip('/')
    for upstream, local in GITHUB_ROUTES:
        if path.startswith(local):
            return upstream + path[len(local):]
    return ''


def fetch_router_page(target: str | None = None) -> str:
    """取软件中心首页 HTML（只读）。"""
    base = normalize_target(target or load_settings()['target'])
    request = urllib.request.Request(base, headers={
        'User-Agent': 'XiaomiNasRouterCenter/' + (VERSION or '?'),
        'Accept': 'text/html,application/xhtml+xml',
        'Accept-Language': 'zh-CN,zh;q=0.9',
    })
    with urllib.request.urlopen(request, timeout=PROBE_TIMEOUT * 2) as response:
        return response.read().decode('utf-8', 'replace')


def assets_present(static_dir: Path) -> dict[str, bool]:
    """本地 CDN 副本是否齐全（缺失时页面会退回直连模式，不至于白屏）。"""
    return {name: (Path(static_dir) / name).is_file() for name in LOCAL_ASSETS}


# ---------------------------------------------------------------------------
# 渲染自己的 nginx 入口并热加载（失败回滚）
# ---------------------------------------------------------------------------


def render_conf(target: str, user: str, port: int) -> str:
    """返回客户端入口配置。

    这个文件刻意不含占位符：商店安装会把包里的配置原样装到 /etc/nginx/conf.d/luci/，
    带 __XXX__ 占位符会让 nginx -t 直接失败（踩过）。所以端口写死 18101、路由器接口
    转给插件服务转发——参数留在这里只是为了保持调用方兼容。
    """
    return NGINX_TEMPLATE.read_text(encoding='utf-8')


def nginx_test() -> tuple[bool, str]:
    if os.name == 'nt':                                            # 开发机不碰 nginx
        return True, 'skip'
    try:
        done = run(['nginx', '-t'], timeout=20)
    except (OSError, subprocess.SubprocessError) as error:
        return False, str(error)
    return done.returncode == 0, (done.stderr or done.stdout).strip()


def nginx_reload() -> bool:
    if os.name == 'nt':
        return True
    try:
        return run(['systemctl', 'reload', 'nginx'], timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def ensure_conf(user: str = '', port: int = 18101) -> dict[str, Any]:
    """保证装到系统里的是"渲染后"的配置。

    正常路径（脚本安装）写的就是同一份内容；商店安装可能装进一份带占位符的旧模板，
    这里在服务启动时纠正过来：内容不同才写、写前先 nginx -t，失败回滚。
    """
    desired = render_conf('', user, port)
    try:
        current = NGINX_CONF.read_text(encoding='utf-8')
    except OSError:
        current = ''
    if current == desired:
        return {'changed': False, 'ok': True}
    if 'placeholder' in desired or re.search(r'__[A-Z_]+__', desired):
        log('模板里仍有占位符，拒绝写入')
        return {'changed': False, 'ok': False, 'error': '模板含占位符'}
    try:
        atomic_write(NGINX_CONF, desired, mode=0o644)
        ok, output = nginx_test()
        if not ok:
            raise RuntimeError(output)
        if not nginx_reload():
            raise RuntimeError('nginx 重载失败')
    except (RuntimeError, OSError) as error:
        if current:
            atomic_write(NGINX_CONF, current, mode=0o644)
        nginx_reload()
        log(f'入口配置自愈失败，已回滚：{error}')
        return {'changed': True, 'ok': False, 'error': str(error)}
    log('已把客户端入口配置纠正为最新版本')
    return {'changed': True, 'ok': True}


def apply_target(target: str, user: str, port: int) -> dict[str, Any]:
    """保存目标地址。

    地址只存在设置里（路由器接口由插件服务转发），所以改地址**不需要动 nginx**，
    也就没有"渲染失败要回滚"的问题了。
    """
    normalized = normalize_target(target)
    with _toggle_lock:
        settings = save_settings({'target': normalized})
        log(f'目标地址已更新为 {normalized}')
        return settings


def status_payload(port: int) -> dict[str, Any]:
    settings = load_settings()
    token = router_token()
    return {
        'version': VERSION,
        'plugin': PLUGIN_KEY,
        'target': settings['target'],
        'default_target': DEFAULT_TARGET,
        'token_set': bool(token),
        'token_hint': token_hint(token),
        'router': probe_router(settings['target']),
        'nginx_conf': str(NGINX_CONF),
        'nginx_conf_ready': NGINX_CONF.is_file(),
        'hostname': platform.node(),
        'at': int(time.time()),
    }


if __name__ == '__main__':                                          # 手工自检用
    print(json.dumps(status_payload(int(os.environ.get('PORT', '18101'))), ensure_ascii=False, indent=2))
    sys.exit(0)
