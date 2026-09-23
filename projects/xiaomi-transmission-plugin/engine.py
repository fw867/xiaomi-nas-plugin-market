"""Bounded Transmission container lifecycle (Docker Engine API)."""
from __future__ import annotations

import http.client
import io
import json
import os
import re
import secrets
import shutil
import socket
import threading
import time
import urllib.request
import zipfile
import base64
from pathlib import Path
from urllib.parse import urlencode

VERSION = '0.2.0'
# LinuxServer Transmission 4.1.3-r0-ls362，多架构 manifest digest（含 arm64）。
IMAGE = 'lscr.io/linuxserver/transmission@sha256:1a12fef3c89eca48b7be9e7d36b17b4eb4e1bcf5e1ee7fbf372e3a38b562939d'
IMAGE_VERSION = '4.1.3 / LSIO ls362'
NAME = 'xiaomi-plugin-transmission'
LABEL = 'io.xiaomi-plugin.tr.owner'
PORT = 9091
BT_PORT = 51413
DOCKER_SOCKET = os.environ.get('DOCKER_SOCKET', '/var/run/docker.sock')


class Error(RuntimeError):
    pass


def installed_version():
    """插件包真实版本；源码树里解析不出来时回退 VERSION。"""
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


def webui_username(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 64:
        raise Error('WebUI 用户名须为 1 至 64 个字符')
    if any(ord(c) < 33 or ord(c) > 126 for c in value) or any(c in value for c in ':@/'):
        raise Error('WebUI 用户名含无效字符')
    return value


def webui_password(password):
    if not isinstance(password, str) or not 8 <= len(password) <= 200 or any(ord(c) < 32 for c in password):
        raise Error('WebUI 密码须为 8 至 200 个字符')
    if sum(bool(re.search(p, password)) for p in (r'[A-Z]', r'[a-z]', r'[0-9]', r'[^A-Za-z0-9\s]')) < 2:
        raise Error('WebUI 密码须至少包含大写、小写、数字、符号中的两种')
    return password


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


def settings_document():
    """写入 /config/transmission-daemon/settings.json 的非鉴权配置。

    WebUI 账号密码走 LinuxServer 的 USER/PASS 环境变量，不要写进
    settings.json，否则 s6 可能无法干净停掉 transmission-daemon。
    """
    return {
        'download-dir': '/downloads',
        'watch-dir': '/watch',
        'watch-dir-enabled': True,
        'rpc-enabled': True,
        'rpc-port': PORT,
        'rpc-authentication-required': True,
        'rpc-whitelist-enabled': False,
        'rpc-bind-address': '0.0.0.0',
        'peer-port': BT_PORT,
        'peer-port-random-on-start': False,
        'port-forwarding-enabled': False,
        'trash-original-torrent-files': True,
        'incomplete-dir-enabled': False,
        'utp-enabled': True,
        'dht-enabled': True,
        'lpd-enabled': True,
        'pex-enabled': True,
        'rename-partial-files': True,
        'start-added-torrents': True,
    }


def settings_path(config_folder):
    """LinuxServer 镜像以 `-g /config` 启动 daemon，读写的就是这个文件。

    写成 `<配置目录>/transmission-daemon/settings.json`（原生版的路径）不会被读取，
    daemon 会回落到镜像默认值——其中 `rpc-bind-address` 是 `[::]`，在没有 IPv6 的
    容器里绑不上 9091，Web 界面永远起不来。
    """
    return config_folder / 'settings.json'


def write_settings(config_folder, uid, gid):
    payload = json.dumps(settings_document(), ensure_ascii=False, indent=2) + '\n'
    path = settings_path(config_folder)
    tmp = path.with_suffix('.tmp')
    with tmp.open('w', encoding='utf-8') as stream:
        os.chmod(tmp, 0o600)
        stream.write(payload)
    os.chown(tmp, uid, gid)
    tmp.replace(path)
    return path


WEBUI_VERSION = 'v1.6.1-update1'
# 官方安装脚本就是把这个 tag 的 src/ 拷进 TRANSMISSION_WEB_HOME，这里照做。
WEBUI_ARCHIVE = ('https://github.com/ronggang/transmission-web-control/archive/'
                 + WEBUI_VERSION + '.zip')
WEBUI_SUBDIR = 'transmission-web-control-' + WEBUI_VERSION.lstrip('v') + '/src'
# 控制台固定装在 <配置目录>/webui/（容器内 /config/webui），TRANSMISSION_WEB_HOME 指到它。
# 想换成别的 WebUI，把文件放进这个目录即可，不需要改插件。
WEBUI_FOLDER = 'webui'
WEBUI_HOME = '/config/' + WEBUI_FOLDER


def webui_path(config_folder):
    return config_folder / WEBUI_FOLDER


def fetch_bytes(url, timeout=180, limit=64 * 1024 * 1024):
    request = urllib.request.Request(url, headers={'User-Agent': 'xiaomi-transmission-plugin'})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        payload = response.read(limit + 1)
    if len(payload) > limit:
        raise Error('控制台资源过大，已中止下载')
    return payload


def extract_zip(payload, destination, prefix=''):
    """解压 zip；prefix 指定只保留该前缀下的内容（去掉顶层目录）。"""
    destination = destination.resolve()
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        for member in archive.infolist():
            if prefix and not member.filename.startswith(prefix):
                continue
            relative = member.filename[len(prefix):] if prefix else member.filename
            if not relative:
                continue
            target = (destination / relative).resolve()
            if target != destination and not str(target).startswith(str(destination) + os.sep):
                raise Error('控制台压缩包包含非法路径')
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target.open('wb') as output:
                shutil.copyfileobj(source, output)


def install_webui(config_folder, uid, gid, force=False):
    """把默认控制台铺到 <配置目录>/webui/，已存在则跳过（用户自行替换过的不会被覆盖）。"""
    target = webui_path(config_folder)
    index = target / 'index.html'
    if index.is_file() and not force:
        return WEBUI_HOME
    if force and target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    extract_zip(fetch_bytes(WEBUI_ARCHIVE), target, prefix=WEBUI_SUBDIR + '/')
    if not index.is_file():
        raise Error('控制台资源不完整，缺少 index.html')
    for path in [target] + sorted(target.rglob('*')):
        os.chown(path, uid, gid)
        os.chmod(path, 0o755 if path.is_dir() else 0o644)
    return WEBUI_HOME


def container_config(config, data, password):
    username = config['username']
    return {
        'Image': IMAGE,
        'ExposedPorts': {
            str(PORT) + '/tcp': {},
            str(BT_PORT) + '/tcp': {},
            str(BT_PORT) + '/udp': {},
        },
        'Env': [
            'PUID=' + str(config['uid']),
            'PGID=' + str(config['gid']),
            'UMASK=077',
            'TZ=Asia/Shanghai',
            'USER=' + username,
            'PASS=' + password,
            'PEERPORT=' + str(BT_PORT),
            # 控制台固定在 <配置目录>/webui；换 UI 只需替换该目录里的文件
            'TRANSMISSION_WEB_HOME=' + WEBUI_HOME,
        ],
        'Labels': {LABEL: config['owner']},
        'HostConfig': {
            'RestartPolicy': {'Name': 'no'},
            'Memory': 512 * 1024 * 1024,
            'MemorySwap': 512 * 1024 * 1024,
            'NanoCpus': 1500000000,
            'PidsLimit': 128,
            'SecurityOpt': ['no-new-privileges:true'],
            'LogConfig': {'Type': 'json-file', 'Config': {'max-size': '5m', 'max-file': '2'}},
            'PortBindings': {
                str(PORT) + '/tcp': [{'HostIp': '0.0.0.0', 'HostPort': str(PORT)}],
                str(BT_PORT) + '/tcp': [{'HostIp': '0.0.0.0', 'HostPort': str(BT_PORT)}],
                str(BT_PORT) + '/udp': [{'HostIp': '0.0.0.0', 'HostPort': str(BT_PORT)}],
            },
            'Mounts': [
                {'Type': 'bind', 'Source': config['config'], 'Target': '/config'},
                {'Type': 'bind', 'Source': config['download'], 'Target': '/downloads'},
                {'Type': 'bind', 'Source': config['watch'], 'Target': '/watch'},
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
        self.port_state = {'testedAt': 0, 'open': None, 'error': ''}
        self.worker = None
        self.cfgfile = self.data / 'settings.json'
        self.credentialfile = self.data / 'credential.json'
        self.config = None
        if self.cfgfile.exists():
            try:
                loaded = json.loads(self.cfgfile.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                loaded = None
            # 旧版原生插件的 settings.json 没有 docker 字段，不能当成已初始化。
            if isinstance(loaded, dict) and loaded.get('owner') and loaded.get('download') and loaded.get('config'):
                self.config = loaded

    def _call(self, method, path, body=None, timeout=30, ok=(200, 201, 204)):
        if self.dev:
            raise Error('预览模式不会操作 Docker')
        status, data = docker_api(method, path, body, timeout)
        if status not in ok:
            raise Error('Docker 操作失败，请检查镜像网络、端口和可用资源；未修改其他容器')
        return data

    def inspect(self):
        if self.dev:
            raise Error('预览模式不会操作 Docker')
        status, data = docker_api('GET', '/containers/' + NAME + '/json')
        if status == 404:
            return None
        if status != 200:
            raise Error('Docker 操作失败，请检查镜像网络、端口和可用资源；未修改其他容器')
        item = json.loads(data)
        return item if isinstance(item, dict) else None

    def pull(self):
        image, _, digest = IMAGE.partition('@')
        repository, _, tag = image.partition(':')
        query = urlencode({'fromImage': repository + ('@' + digest if digest else ''),
                           'tag': tag or 'latest'})
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
                raise Error('Docker 操作失败，请检查镜像网络、端口和可用资源；未修改其他容器')

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
                       for p in folder.iterdir() if not p.name.startswith('.') and p.is_dir() and not p.is_symlink()],
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
                    tr_rpc_probe()
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
            'imageVersion': IMAGE_VERSION,
            'download': self.config.get('download_relative', '') if self.config else '',
            'config': self.config.get('config_relative', '') if self.config else '',
            'watch': self.config.get('watch_relative', '') if self.config else '',
            'username': self.config.get('username', '') if self.config else '',
            # BT 端口是否对公网开放：只有点了「测试端口」才有值（port-test 会访问外部检测服务）
            'port': {'peerPort': BT_PORT, **self.port_state},
        }

    def _claim_folder(self, relative, field):
        folder = confined(self.root, relative)
        if ',' in str(folder):
            raise Error(field + '路径不能包含逗号')
        return folder

    def setup(self, paths, username, password):
        if self.config:
            raise Error('已完成初始化；现有目录和账号不会被覆盖')
        username = webui_username(username)
        password = webui_password(password)
        download_rel = paths.get('download', '')
        config_rel = paths.get('config', '')
        watch_rel = paths.get('watch', '')
        if not download_rel or not config_rel or not watch_rel:
            raise Error('请分别选择下载目录、配置文件夹目录和监控目录')
        download = self._claim_folder(download_rel, '下载目录')
        config_folder = self._claim_folder(config_rel, '配置文件夹目录')
        watch = self._claim_folder(watch_rel, '监控目录')
        resolved = [str(p.resolve()) for p in (download, config_folder, watch)]
        if len(set(resolved)) != 3:
            raise Error('下载、配置与监控目录不能是同一个文件夹')
        owners = [(p.stat().st_uid, p.stat().st_gid) for p in (download, config_folder, watch)]
        if any(not uid or not gid for uid, gid in owners):
            raise Error('所选目录须由非 root 的 NAS 用户拥有')
        if len(set(owners)) != 1:
            raise Error('三个目录的属主用户必须相同')
        uid, gid = owners[0]
        self._call('GET', '/info')
        if self.inspect() is not None:
            raise Error('同名容器已存在，拒绝覆盖')
        write_settings(config_folder, uid, gid)
        install_webui(config_folder, uid, gid)
        stats = {key: path.stat() for key, path in
                 [('download', download), ('config', config_folder), ('watch', watch)]}
        self.config = {
            'owner': secrets.token_hex(24),
            'uid': uid,
            'gid': gid,
            'username': username,
            'download': str(download),
            'download_relative': download_rel,
            'download_device': stats['download'].st_dev,
            'download_inode': stats['download'].st_ino,
            'config': str(config_folder),
            'config_relative': config_rel,
            'config_device': stats['config'].st_dev,
            'config_inode': stats['config'].st_ino,
            'watch': str(watch),
            'watch_relative': watch_rel,
            'watch_device': stats['watch'].st_dev,
            'watch_inode': stats['watch'].st_ino,
            'enabled': True,
        }
        atomic_json(self.cfgfile, self.config)
        atomic_json(self.credentialfile, {'password': password, 'username': username})

    def saved_password(self):
        try:
            data = json.loads(self.credentialfile.read_text(encoding='utf-8'))
            password = data.get('password')
        except (OSError, ValueError, TypeError):
            return ''
        return password if isinstance(password, str) and password else ''

    def check_directories(self):
        if not self.config:
            return
        pairs = [
            ('download', 'download_relative', 'download_device', 'download_inode', '下载目录'),
            ('config', 'config_relative', 'config_device', 'config_inode', '配置文件夹目录'),
            ('watch', 'watch_relative', 'watch_device', 'watch_inode', '监控目录'),
        ]
        for path_key, rel_key, dev_key, ino_key, label in pairs:
            folder = confined(self.root, self.config[rel_key])
            stat = folder.stat()
            if str(folder) != self.config[path_key] or (stat.st_dev, stat.st_ino) != (
                    self.config[dev_key], self.config[ino_key]):
                raise Error(label + '身份已变化，拒绝启动；请先检查存储挂载')

    def ensure_settings(self):
        """把非鉴权配置写到 daemon 真正读取的位置，并修复旧版写错目录的安装。"""
        config_folder = Path(self.config['config'])
        try:
            current = json.loads(settings_path(config_folder).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            current = {}
        if not isinstance(current, dict):
            current = {}
        document = settings_document()
        stale = not current or any(current.get(key) != value for key, value in document.items())
        if stale:
            write_settings(config_folder, self.config['uid'], self.config['gid'])
        return stale

    def ensure_webui(self):
        """确保默认控制台已铺到 <配置目录>/webui；用户自己替换过的文件不动。"""
        return install_webui(Path(self.config['config']), self.config['uid'], self.config['gid'])

    def webui_env_stale(self, item):
        """容器是否还指向旧的控制台路径（旧版本装在 webui/<主题>/ 下）。"""
        env = item.get('Config', {}).get('Env') or []
        return ('TRANSMISSION_WEB_HOME=' + WEBUI_HOME) not in env

    def test_port(self):
        """调 Transmission 的 port-test，判断 BT 端口是否对公网开放。

        注意：容器里 UPnP 自动映射不生效（容器 IP 不在 LAN 网段），
        要开放需要在路由器上手动把 BT 端口转发到 NAS。
        """
        if not self.config:
            raise Error('请先初始化')
        password = self.saved_password()
        username = self.config.get('username', '')
        if not password:
            raise Error('缺少 WebUI 密码，无法测试端口')
        code, body, session = tr_rpc('port-test', username, password, timeout=30)
        if code == 409 and session:
            code, body, session = tr_rpc('port-test', username, password, session, timeout=30)
        if code != 200:
            raise Error('端口测试失败（HTTP %s）' % code)
        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise Error('端口测试返回异常') from exc
        if payload.get('result') != 'success':
            raise Error('端口测试失败：' + str(payload.get('result')))
        opened = bool(payload.get('arguments', {}).get('port-is-open'))
        self.port_state = {'testedAt': int(time.time()), 'open': opened, 'error': ''}
        return self.port_state

    def start(self):
        if not self.config:
            raise Error('请先初始化')
        self.check_directories()
        repaired = self.ensure_settings()
        self.ensure_webui()
        item = self.owned()
        if item and self.webui_env_stale(item):
            # 控制台路径变了：环境变量只在建容器时生效，必须重建
            if item.get('State', {}).get('Running'):
                self._call('POST', '/containers/' + NAME + '/stop?t=15', timeout=90)
            self._call('DELETE', '/containers/' + NAME + '?force=1&v=1', timeout=60)
            item = None
        if item:
            if not item.get('State', {}).get('Running'):
                self._call('POST', '/containers/' + NAME + '/start')
            elif repaired:
                # 旧版配置写错位置，daemon 用的是镜像默认的 rpc-bind-address=[::]；
                # 容器没有 IPv6 时绑不上 9091，必须重启才能读到修正后的配置。
                self._call('POST', '/containers/' + NAME + '/restart?t=15', timeout=120)
        else:
            password = self.saved_password()
            if not password:
                raise Error('缺少 WebUI 密码，请重新初始化或检查插件数据目录')
            self.pull()
            self.check_directories()
            self._call('POST', '/containers/create?name=' + NAME,
                       body=container_config(self.config, self.data, password), timeout=120)
            self._call('POST', '/containers/' + NAME + '/start')
        self.config['enabled'] = True
        atomic_json(self.cfgfile, self.config)
        for _ in range(60):
            try:
                tr_rpc_probe()
                return
            except Error:
                pass
            time.sleep(1)
        raise Error('容器已启动，但 Transmission Web 尚未就绪；可稍后刷新')

    def stop(self, remember=True):
        if not self.config:
            return
        item = self.owned()
        if item and item.get('State', {}).get('Running'):
            self._call('POST', '/containers/' + NAME + '/stop?t=15', timeout=90)
        if remember:
            self.config['enabled'] = False
            atomic_json(self.cfgfile, self.config)

    def launch(self, action, data):
        if self.dev:
            raise Error('预览模式不会启动下载或修改 NAS')
        if action not in ('setup', 'start', 'stop', 'port-test'):
            raise Error('未知服务操作')
        if not self.lock.acquire(False):
            raise Error('服务操作正在进行，请稍候')
        self.busy, self.error = True, ''

        def work():
            try:
                if action == 'setup':
                    self.setup(data, data.get('username', ''), data.get('password', ''))
                if action == 'port-test':
                    self.test_port()
                elif action in ('setup', 'start'):
                    self.start()
                else:
                    self.stop()
            except Error as exc:
                self.error = str(exc)
            except Exception:
                self.error = '操作失败；保留已有配置和文件，请检查目录权限与 Docker 状态'
            finally:
                self.busy = False
                self.lock.release()

        self.worker = threading.Thread(target=work, daemon=False)
        self.worker.start()


def tr_rpc(method, username=None, password=None, session_id='', timeout=15):
    """调一次 Transmission RPC；返回 (状态码, body, session-id 头)。"""
    connection = http.client.HTTPConnection('127.0.0.1', PORT, timeout=timeout)
    headers = {'Content-Type': 'application/json'}
    if username and password:
        token = base64.b64encode((username + ':' + password).encode('utf-8')).decode('ascii')
        headers['Authorization'] = 'Basic ' + token
    if session_id:
        headers['X-Transmission-Session-Id'] = session_id
    body = json.dumps({'method': method}).encode('utf-8')
    try:
        connection.request('POST', '/transmission/rpc', body, headers)
        response = connection.getresponse()
        data = response.read(2 * 1024 * 1024 + 1)
        return response.status, data, response.getheader('X-Transmission-Session-Id', '')
    except (OSError, http.client.HTTPException) as exc:
        raise Error('Transmission 未运行或尚未就绪') from exc
    finally:
        connection.close()


def tr_rpc_probe(username=None, password=None):
    """就绪探测：401/409 都说明 daemon 已在监听。"""
    code, _, session = tr_rpc('session-get', username=username, password=password)
    if code == 409 and session:
        code, _, _ = tr_rpc('session-get', username=username, password=password, session_id=session)
    if code in (200, 401, 409):
        return True
    raise Error('Transmission Web 尚未就绪')
