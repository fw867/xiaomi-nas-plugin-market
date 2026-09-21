"""Bounded DPanel container lifecycle (Docker Engine API).

DPanel 本身是 Docker 管理面板，必须挂载 /var/run/docker.sock 才能工作。
插件只负责创建/启停这一个带所有权标签的容器，不提供通用 Docker 接口。
"""
from __future__ import annotations

import http.client
import json
import os
import re
import secrets
import socket
import threading
import time
import urllib.parse
from pathlib import Path

VERSION = '0.1.0'
# DPanel Lite：不需要标准版的 80/443 域名转发端口，适合 NAS。
IMAGE = 'dpanel/dpanel:lite@sha256:befa4221aeebbeac9148cceca06f8b474f412b2de68b5f3968a68e04a0e632c1'
IMAGE_VERSION = 'lite / multi-arch befa4221'
NAME = 'xiaomi-plugin-dpanel'
LABEL = 'io.xiaomi-plugin.dpanel.owner'
# 宿主机端口：DPanel 官方示例常用 8807 → 容器 8080
PORT = 8807
CONTAINER_PORT = 8080
DOCKER_SOCKET = os.environ.get('DOCKER_SOCKET', '/var/run/docker.sock')


class Error(RuntimeError):
    pass


def installed_version():
    parts = Path(__file__).resolve().parent.name.split('-')
    if len(parts) > 2 and parts[-1].isdigit() and parts[-2].isdigit():
        return '-'.join(parts[:-2])
    return VERSION


class _UnixHTTPConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(DOCKER_SOCKET)


def docker_api(method, path, body=None, timeout=30):
    payload = json.dumps(body).encode('utf-8') if body is not None else None
    headers = {'Host': 'localhost'}
    if payload is not None:
        headers['Content-Type'] = 'application/json'
        headers['Content-Length'] = str(len(payload))
    connection = _UnixHTTPConnection('localhost', timeout=timeout)
    try:
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        return response.status, response.read()
    except (OSError, http.client.HTTPException) as exc:
        raise Error('Docker 不可用或操作超时') from exc
    finally:
        connection.close()


def confined(root, relative):
    if not isinstance(relative, str) or relative.startswith('/') or '\\' in relative:
        raise Error('目录路径无效')
    current = Path(root)
    if current.is_symlink() or not current.is_dir():
        raise Error('用户存储不可用')
    for part in relative.split('/') if relative else []:
        if part in ('', '.', '..') or part.startswith('.') or any(ord(c) < 32 for c in part):
            raise Error('目录路径无效')
        current /= part
        if current.is_symlink() or not current.is_dir():
            raise Error('目录不存在或为符号链接')
    current.resolve().relative_to(Path(root).resolve())
    return current


def atomic_json(path, value):
    tmp = path.with_suffix('.tmp')
    with tmp.open('w', encoding='utf-8') as stream:
        os.chmod(tmp, 0o600)
        json.dump(value, stream)
    tmp.replace(path)


def dpanel_ready():
    """面板 HTTP 任意响应都算就绪（首次会跳向初始化向导）。"""
    connection = http.client.HTTPConnection('127.0.0.1', PORT, timeout=6)
    try:
        connection.request('GET', '/dpanel/ui')
        response = connection.getresponse()
        response.read(4096)
        return response.status
    except (OSError, http.client.HTTPException) as exc:
        raise Error('DPanel 未运行或尚未就绪') from exc
    finally:
        connection.close()


def container_config(config):
    return {
        'Image': IMAGE,
        'ExposedPorts': {str(CONTAINER_PORT) + '/tcp': {}},
        'Env': [
            'APP_NAME=' + NAME,
            'TZ=Asia/Shanghai',
        ],
        'Labels': {LABEL: config['owner']},
        'HostConfig': {
            'RestartPolicy': {'Name': 'no'},
            'Memory': 512 * 1024 * 1024,
            'MemorySwap': 512 * 1024 * 1024,
            'NanoCpus': 1500000000,
            'PidsLimit': 256,
            'SecurityOpt': ['no-new-privileges:true'],
            'LogConfig': {'Type': 'json-file', 'Config': {'max-size': '5m', 'max-file': '2'}},
            'PortBindings': {
                str(CONTAINER_PORT) + '/tcp': [{'HostIp': '0.0.0.0', 'HostPort': str(PORT)}],
            },
            'Mounts': [
                # DPanel 管理 Docker 必需；这是面板功能本身，不是误配。
                {'Type': 'bind', 'Source': '/var/run/docker.sock', 'Target': '/var/run/docker.sock'},
                {'Type': 'bind', 'Source': config['config'], 'Target': '/dpanel'},
            ],
        },
    }


class Engine:
    def __init__(self, data, root, dev=False):
        self.data, self.root, self.dev = Path(data), Path(root), dev
        self.data.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.data, 0o700)
        self.lock = threading.Lock()
        self.busy, self.error = False, ''
        self.worker = None
        self.cfgfile = self.data / 'settings.json'
        self.config = None
        if self.cfgfile.exists():
            try:
                loaded = json.loads(self.cfgfile.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                loaded = None
            if isinstance(loaded, dict) and loaded.get('owner') and loaded.get('config'):
                self.config = loaded

    def _call(self, method, path, body=None, timeout=30, ok=(200, 201, 204)):
        if self.dev:
            raise Error('预览模式不会操作 Docker')
        status, data = docker_api(method, path, body, timeout)
        if status not in ok:
            raise Error('Docker 操作失败，请检查镜像网络、端口 ' + str(PORT) + ' 和可用资源；未修改其他容器')
        return data

    def inspect(self):
        if self.dev:
            raise Error('预览模式不会操作 Docker')
        status, data = docker_api('GET', '/containers/' + NAME + '/json')
        if status == 404:
            return None
        if status != 200:
            raise Error('Docker 操作失败，请检查镜像网络、端口 ' + str(PORT) + ' 和可用资源；未修改其他容器')
        item = json.loads(data)
        return item if isinstance(item, dict) else None

    def pull(self):
        image, _, digest = IMAGE.partition('@')
        repository, _, tag = image.partition(':')
        query = urllib.parse.urlencode({
            'fromImage': repository + ('@' + digest if digest else ''),
            'tag': tag or 'latest',
        })
        if self.dev:
            raise Error('预览模式不会操作 Docker')
        stream = self._call('POST', '/images/create?' + query, timeout=900)
        for line in stream.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get('error'):
                raise Error('Docker 操作失败，请检查镜像网络、端口 ' + str(PORT) + ' 和可用资源；未修改其他容器')

    def owned(self):
        item = self.inspect()
        if item is None:
            return None
        if not self.config or item.get('Config', {}).get('Labels', {}).get(LABEL) != self.config['owner']:
            raise Error('同名容器不属于本插件，拒绝接管')
        return item

    def browse(self, relative):
        folder = confined(self.root, relative)
        return sorted([{'name': p.name, 'path': (relative + '/' if relative else '') + p.name}
                       for p in folder.iterdir()
                       if not p.name.startswith('.') and p.is_dir() and not p.is_symlink()],
                      key=lambda p: p['name'])[:1000]

    def snapshot(self):
        running, ready, error = False, False, self.error
        if self.config and not self.busy:
            try:
                item = self.owned()
                running = bool(item and item.get('State', {}).get('Running'))
            except Error as exc:
                error = str(exc)
            if running:
                try:
                    dpanel_ready()
                    ready = True
                except Error:
                    ready = False
        return {
            'version': installed_version(),
            'configured': bool(self.config),
            'running': running,
            'ready': ready,
            'busy': self.busy,
            'error': error,
            'preview': self.dev,
            'port': PORT,
            'configDirectory': self.config.get('config_relative', '') if self.config else '',
            'imageVersion': IMAGE_VERSION,
        }

    def setup(self, config_relative=''):
        if self.config:
            raise Error('已完成初始化；现有 DPanel 配置不会被覆盖')
        if config_relative:
            if not isinstance(config_relative, str) or len(config_relative) > 1024:
                raise Error('配置目录无效')
            cfgdir = confined(self.root, config_relative)
            if ',' in str(cfgdir):
                raise Error('Docker 挂载目录不能包含逗号')
            stat = cfgdir.stat()
            if not stat.st_uid or not stat.st_gid:
                raise Error('配置目录须由非 root 的 NAS 用户拥有')
            uid, gid = stat.st_uid, stat.st_gid
        else:
            uid = gid = 0
            try:
                # 插件私有配置目录：没有 NAS 用户属主时以 root 运行即可。
                uid = os.stat(self.data).st_uid
                gid = os.stat(self.data).st_gid
            except OSError:
                pass
            cfgdir = self.data / 'config'
            cfgdir.mkdir(parents=True, exist_ok=True, mode=0o700)
            if uid and gid:
                try:
                    os.chown(cfgdir, uid, gid)
                except OSError:
                    pass
            os.chmod(cfgdir, 0o700)
        self._call('GET', '/info')
        if self.inspect() is not None:
            raise Error('同名容器已存在，拒绝覆盖')
        cfg_stat = cfgdir.stat()
        self.config = {
            'owner': secrets.token_hex(24),
            'config': str(cfgdir),
            'config_relative': config_relative or '',
            'config_device': cfg_stat.st_dev,
            'config_inode': cfg_stat.st_ino,
            'uid': uid,
            'gid': gid,
            'enabled': True,
        }
        atomic_json(self.cfgfile, self.config)

    def check_directory(self):
        cfg_relative = self.config.get('config_relative')
        if not cfg_relative:
            return
        cfgdir = confined(self.root, cfg_relative)
        stat = cfgdir.stat()
        if str(cfgdir) != self.config['config'] or (stat.st_dev, stat.st_ino) != (
                self.config['config_device'], self.config['config_inode']):
            raise Error('配置目录身份已变化，拒绝启动；请先检查存储挂载')

    def start(self):
        if not self.config:
            raise Error('请先初始化')
        self.check_directory()
        item = self.owned()
        if item:
            if not item.get('State', {}).get('Running'):
                self._call('POST', '/containers/' + NAME + '/start')
        else:
            self.pull()
            self.check_directory()
            self._call('POST', '/containers/create?name=' + NAME,
                       body=container_config(self.config), timeout=120)
            self._call('POST', '/containers/' + NAME + '/start')
        self.config['enabled'] = True
        atomic_json(self.cfgfile, self.config)
        for _ in range(60):
            try:
                dpanel_ready()
                return
            except Error:
                time.sleep(1)
        raise Error('容器已启动，但 DPanel 尚未就绪；可稍后刷新')

    def stop(self, remember=True):
        if not self.config:
            return
        item = self.owned()
        if item and item.get('State', {}).get('Running'):
            self._call('POST', '/containers/' + NAME + '/stop?t=20', timeout=90)
        if remember:
            self.config['enabled'] = False
            atomic_json(self.cfgfile, self.config)

    def launch(self, action, data):
        if self.dev:
            raise Error('预览模式不会启动服务或修改 NAS')
        if action not in ('setup', 'start', 'stop'):
            raise Error('未知服务操作')
        if not self.lock.acquire(False):
            raise Error('服务操作正在进行，请稍候')
        self.busy, self.error = True, ''

        def work():
            try:
                if action == 'setup':
                    self.setup(data.get('configPath', ''))
                if action in ('setup', 'start'):
                    self.start()
                else:
                    self.stop()
            except Error as exc:
                self.error = str(exc)
            except Exception:
                self.error = '操作失败；保留已有配置，请检查目录权限与 Docker 状态'
            finally:
                self.busy = False
                self.lock.release()

        self.worker = threading.Thread(target=work, daemon=False)
        self.worker.start()
