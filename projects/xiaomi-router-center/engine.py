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
# 渲染自己的 nginx 入口并热加载（失败回滚）
# ---------------------------------------------------------------------------


def render_conf(target: str, user: str, port: int) -> str:
    template = NGINX_TEMPLATE.read_text(encoding='utf-8')
    return (template
            .replace('__ROUTER_TARGET__', target)
            .replace('__NAS_USER_ID__', user)
            .replace('__PLUGIN_PORT__', str(port)))


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


def apply_target(target: str, user: str, port: int) -> dict[str, Any]:
    """渲染 nginx 入口并热加载；只有全部成功才把新目标落盘，失败一定回滚。"""
    normalized = normalize_target(target)
    with _toggle_lock:
        backup = NGINX_CONF.read_text(encoding='utf-8') if NGINX_CONF.is_file() else ''
        try:
            atomic_write(NGINX_CONF, render_conf(normalized, user, port), mode=0o644)
            ok, output = nginx_test()
            if not ok:
                raise RuntimeError(f'nginx 配置校验失败，已回滚：{output}')
            if not nginx_reload():
                raise RuntimeError('nginx 重载失败，已回滚')
        except RuntimeError:
            if backup:
                atomic_write(NGINX_CONF, backup, mode=0o644)
            else:
                NGINX_CONF.unlink(missing_ok=True)
            nginx_reload()
            raise
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
        'token_hint': (token[:4] + '…' + token[-2:]) if len(token) >= 6 else ('已设置' if token else ''),
        'router': probe_router(settings['target']),
        'nginx_conf': str(NGINX_CONF),
        'nginx_conf_ready': NGINX_CONF.is_file(),
        'hostname': platform.node(),
        'at': int(time.time()),
    }


if __name__ == '__main__':                                          # 手工自检用
    print(json.dumps(status_payload(int(os.environ.get('PORT', '18101'))), ensure_ascii=False, indent=2))
    sys.exit(0)
