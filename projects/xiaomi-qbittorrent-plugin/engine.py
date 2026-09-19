"""Bounded qBittorrent container lifecycle and Web API adapter."""
from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import re
import secrets
import socket
import threading
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit, parse_qs

# 开发环境（源码树里直接跑）的回退值。页面上显示的是插件包的真实版本，
# 由 installed_version() 从自身所在的发布目录名里取。
VERSION = '0.1.2'
IMAGE = 'ghcr.io/linuxserver/qbittorrent@sha256:a00b6a597a3832a1814cde0ef60abc55c94644f3f80902c3432f6af6de8d4a96'
NAME = 'xiaomi-plugin-qbittorrent'
LABEL = 'io.xiaomi-plugin.qb.owner'
PORT = 18123
# BT 的入站监听端口（TCP + UDP）。必须固定下来并映射到宿主机，否则别人无法
# 主动连进来：PT 做种没有上传、分享率上不去，连接状态会一直是 firewalled。
# qB 默认会自己随机挑一个，这里写死，也方便在路由器上做端口转发。
BT_PORT = 36754
# qB 的 WebUI 会话有效期（秒）。插件把登录后的会话持久化到磁盘，下次打开
# 下载列表直接复用，所以放宽到 30 天，避免频繁要求重新登录。
SESSION_TIMEOUT = 30 * 24 * 3600
# 设备上只有 dockerd，没有 docker 命令行（/usr/bin/docker 不存在），
# 所以不能 subprocess 调 CLI，一律走 socket 上的 Engine API。
DOCKER_SOCKET = os.environ.get('DOCKER_SOCKET', '/var/run/docker.sock')


class Error(RuntimeError):
    pass


def installed_version():
    """插件包的真实版本号。

    商店安装器把包解压到 <releaseRoot>/releases/<版本>-<时间戳>-<pid>/ 下，
    current 是指向它的符号链接，所以本文件所在目录名里就带着版本。这样页面
    显示的版本跟着实际装上的包走，不会和代码里的常量各自漂移。

    源码树里跑（开发、预览）时解析不出来，回退到 VERSION。
    """
    parts = Path(__file__).resolve().parent.name.split('-')
    if len(parts) > 2 and parts[-1].isdigit() and parts[-2].isdigit():
        return '-'.join(parts[:-2])
    return VERSION


class _UnixHTTPConnection(http.client.HTTPConnection):
    """让 http.client 通过 Unix socket 说话，用来直连 Docker Engine API。"""

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(DOCKER_SOCKET)


def docker_api(method, path, body=None, timeout=30):
    """调一次 Engine API，返回 (状态码, 响应体)。

    只连本机 socket，不接受外部传入地址；路径由调用方用固定常量拼出。
    """
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


def password_hash(password):
    if not isinstance(password, str) or not 8 <= len(password) <= 200 or any(ord(c) < 32 for c in password):
        raise Error('密码须为 8 至 200 个字符')
    if sum(bool(re.search(p, password)) for p in (r'[A-Z]', r'[a-z]', r'[0-9]', r'[^A-Za-z0-9\s]')) < 2:
        raise Error('密码须至少包含大写、小写、数字、符号中的两种')
    salt = secrets.token_bytes(16)
    key = hashlib.pbkdf2_hmac('sha512', password.encode(), salt, 100000, 64)
    return base64.b64encode(salt).decode() + ':' + base64.b64encode(key).decode()


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


def torrent_hash(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-fA-F0-9]{40}|[a-fA-F0-9]{64}', value):
        raise Error('任务标识无效')
    return value


def mutation(action, data):
    """No arbitrary qB API, save path, shell command, or deleteFiles input."""
    if action in ('start', 'stop', 'remove'):
        params = {'hashes': torrent_hash(data.get('hash'))}
        if action == 'remove':
            params['deleteFiles'] = 'false'
        return 'torrents/' + {'start': 'start', 'stop': 'stop', 'remove': 'delete'}[action], params
    if action == 'magnet':
        value = data.get('url', '')
        if not isinstance(value, str) or len(value) > 16384 or '\n' in value or '\r' in value:
            raise Error('磁力链接无效')
        parsed = urlsplit(value)
        xt = parse_qs(parsed.query).get('xt', [])
        if parsed.scheme != 'magnet' or not any(re.fullmatch(r'urn:btih:(?:[a-fA-F0-9]{40}|[A-Z2-7a-z]{32})|urn:btmh:1220[a-fA-F0-9]{64}', v) for v in xt):
            raise Error('请填写有效的磁力链接')
        return 'torrents/add', {'urls': value, 'savepath': '/downloads', 'autoTMM': 'false', 'stopped': 'false'}
    if action == 'limits':
        prefs = {}
        for source, target, maximum in [('download', 'dl_limit', 1048576), ('upload', 'up_limit', 1048576), ('active', 'max_active_downloads', 10)]:
            value = data.get(source)
            if type(value) is not int or not (1 if source == 'active' else 0) <= value <= maximum:
                raise Error('限速或并发数无效')
            prefs[target] = value * 1024 if source != 'active' else value
        prefs['queueing_enabled'] = True
        return 'app/setPreferences', {'json': json.dumps(prefs)}
    raise Error('不支持此操作')


def container_config(config, data):
    """固定的容器配置（Engine API 的 create body）。

    固定参数，不接受调用方传入镜像、端口、挂载或命令。对应原来那串
    `docker run -d ...`，只是换成 JSON 形式：
    --memory/--memory-swap 用字节、--cpus 用纳核、--mount 用 Mounts。
    """
    return {
        'Image': IMAGE,
        # 必须同时声明 ExposedPorts：Engine API 不像 `docker run -p` 那样自动补，
        # 只给 HostConfig.PortBindings 的话，镜像 EXPOSE 里没有的端口会被静默忽略
        # ——容器只留下镜像自带的 6881/8080，WebUI 的 18123 根本映射不出去。
        'ExposedPorts': {str(PORT) + '/tcp': {}, str(BT_PORT) + '/tcp': {}, str(BT_PORT) + '/udp': {}},
        'Env': [
            'PUID=' + str(config['uid']),
            'PGID=' + str(config['gid']),
            'UMASK=077',
            'TZ=Asia/Shanghai',
            'WEBUI_PORT=' + str(PORT),
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
            # WebUI 对整个局域网开放，便于直接用 qBittorrent 官方客户端或网页连接。
            'PortBindings': {
                str(PORT) + '/tcp': [{'HostIp': '0.0.0.0', 'HostPort': str(PORT)}],
                # BT 入站端口要映射出去别人才能主动连进来；UDP 用于 uTP 打洞。
                str(BT_PORT) + '/tcp': [{'HostIp': '0.0.0.0', 'HostPort': str(BT_PORT)}],
                str(BT_PORT) + '/udp': [{'HostIp': '0.0.0.0', 'HostPort': str(BT_PORT)}],
            },
            'Mounts': [
                {'Type': 'bind', 'Source': str(data / 'config'), 'Target': '/config'},
                {'Type': 'bind', 'Source': config['download'], 'Target': '/downloads'},
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
        self.config = json.loads(self.cfgfile.read_text()) if self.cfgfile.exists() else None
        self.credentialfile = self.data / 'credential.json'

    def _call(self, method, path, body=None, timeout=30, ok=(200, 201, 204)):
        """调一次 Engine API；非预期状态码统一报错。"""
        if self.dev:
            raise Error('预览模式不会操作 Docker')
        status, data = docker_api(method, path, body, timeout)
        if status not in ok:
            raise Error('Docker 操作失败，请检查镜像网络、端口 ' + str(PORT) + ' 和可用资源；未修改其他容器')
        return data

    def inspect(self):
        """容器详情；不存在时返回 None（容器不在 Docker 里不算错误）。"""
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
        """拉取镜像。这个接口是流式的，要把整个流读完才知道成功与否。"""
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
                       for p in folder.iterdir() if not p.name.startswith('.') and p.is_dir() and not p.is_symlink()], key=lambda p: p['name'])[:1000]

    def snapshot(self):
        running, ready, error = False, False, self.error
        if self.config and not self.busy:
            try:
                item = self.owned()
                running = bool(item and item.get('State', {}).get('Running'))
            except Error as exc:
                error = str(exc)
            if running:
                # 容器起来后 WebUI 还要几秒才监听；连不上只说明未就绪，不算错误。
                try:
                    qb_request('app/version')
                    ready = True
                except Error:
                    ready = False
        return {'version': installed_version(), 'configured': bool(self.config), 'running': running, 'ready': ready,
                'busy': self.busy, 'error': error, 'preview': self.dev,
                'directory': self.config['relative'] if self.config else '',
                'imageVersion': '5.2.3 / LSIO ls474'}

    def setup(self, relative, password):
        if self.config:
            raise Error('已完成初始化；现有目录和密码不会被覆盖')
        hashed = password_hash(password)
        if not relative:
            raise Error('请选择存储根目录下的文件夹')
        folder = confined(self.root, relative)
        if ',' in str(folder):
            raise Error('Docker 挂载目录不能包含逗号')
        # 直接使用所选目录：PUID/PGID 取自它的属主，容器以该身份读写下载内容。
        # 不新建子目录，也不 chown 用户目录，因此不会改动已有文件的所有权。
        uid, gid = folder.stat().st_uid, folder.stat().st_gid
        if not uid or not gid:
            raise Error('所选目录须由非 root 的 NAS 用户拥有')
        self._call('GET', '/info')
        if self.inspect() is not None:
            raise Error('同名容器已存在，拒绝覆盖')
        cfgdir = self.data / 'config' / 'qBittorrent'
        cfgdir.mkdir(parents=True, mode=0o700)
        for path in (cfgdir.parent, cfgdir):
            os.chown(path, uid, gid)
            os.chmod(path, 0o700)
        conf = ('[LegalNotice]\nAccepted=true\n[Network]\nPortForwardingEnabled=false\n'
                '[BitTorrent]\nSession\\DefaultSavePath=/downloads\nSession\\QueueingSystemEnabled=true\n'
                'Session\\Port=' + str(BT_PORT) + '\n'
                'Session\\MaxActiveDownloads=2\nSession\\MaxActiveTorrents=4\nSession\\MaxConnections=150\n'
                '[Preferences]\nWebUI\\Address=*\nWebUI\\Port=18123\nWebUI\\Username=admin\n'
                'WebUI\\Password_PBKDF2="@ByteArray(' + hashed + ')"\n'
                'WebUI\\LocalHostAuth=true\nWebUI\\AuthSubnetWhitelistEnabled=false\n'
                'WebUI\\CSRFProtection=true\nWebUI\\HostHeaderValidation=true\nWebUI\\UseUPnP=false\n'
                'WebUI\\SessionTimeout=' + str(SESSION_TIMEOUT) + '\n')
        confpath = cfgdir / 'qBittorrent.conf'
        with confpath.open('x') as stream:
            os.chmod(confpath, 0o600)
            stream.write(conf)
        os.chown(confpath, uid, gid)
        stat = folder.stat()
        self.config = {'owner': secrets.token_hex(24), 'relative': relative,
                       'download': str(folder), 'uid': uid, 'gid': gid, 'device': stat.st_dev,
                       'inode': stat.st_ino, 'enabled': True}
        atomic_json(self.cfgfile, self.config)
        self.save_credential(password)

    def save_credential(self, password):
        """记下 qB 的 WebUI 密码，供会话失效时自动重新登录。

        它与 qB 自己的 QQBittorrent.conf 同处一个仅 root 可读的目录，后者也存着
        等价的密码哈希，属于同一信任域，并没有多开一个口子。目的是让「进下载
        列表」这一步不再要求用户重复输入密码。
        """
        atomic_json(self.credentialfile, {'password': password})

    def saved_credential(self):
        try:
            data = json.loads(self.credentialfile.read_text(encoding='utf-8'))
            password = data.get('password')
        except (OSError, ValueError, TypeError):
            return ''
        return password if isinstance(password, str) and password else ''

    def check_directory(self):
        folder = confined(self.root, self.config['relative'])
        stat = folder.stat()
        if str(folder) != self.config['download'] or (stat.st_dev, stat.st_ino) != (self.config['device'], self.config['inode']):
            raise Error('下载目录身份已变化，拒绝启动；请先检查存储挂载')

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
                       body=container_config(self.config, self.data), timeout=120)
            self._call('POST', '/containers/' + NAME + '/start')
        self.config['enabled'] = True
        atomic_json(self.cfgfile, self.config)
        for _ in range(60):
            try:
                response, _, _ = qb_request('app/version')
                if response in (200, 403):
                    return
            except Error:
                pass
            time.sleep(1)
        raise Error('容器已启动，但 Web API 尚未就绪；可稍后刷新')

    def stop(self, remember=True):
        if not self.config:
            return
        item = self.owned()
        if item and item.get('State', {}).get('Running'):
            # 给容器 15 秒优雅退出；stop 本身会阻塞到容器停下，所以超时要放宽。
            self._call('POST', '/containers/' + NAME + '/stop?t=15', timeout=90)
        if remember:
            self.config['enabled'] = False
            atomic_json(self.cfgfile, self.config)

    def launch(self, action, data):
        if self.dev:
            raise Error('预览模式不会启动下载或修改 NAS')
        if action not in ('setup', 'start', 'stop'):
            raise Error('未知服务操作')
        if not self.lock.acquire(False):
            raise Error('服务操作正在进行，请稍候')
        self.busy, self.error = True, ''
        def work():
            try:
                if action == 'setup':
                    self.setup(data.get('path', ''), data.get('password', ''))
                if action in ('setup', 'start'):
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


def qb_request(route, params=None, cookie='', raw=None, content_type=None):
    connection = http.client.HTTPConnection('127.0.0.1', PORT, timeout=15)
    body = raw if raw is not None else urlencode(params).encode() if params is not None else None
    headers = {'Referer': 'http://127.0.0.1:' + str(PORT) + '/', 'Cookie': cookie,
               'Content-Type': content_type or 'application/x-www-form-urlencoded'}
    try:
        connection.request('POST' if body is not None else 'GET', '/api/v2/' + route, body, headers)
        response = connection.getresponse()
        data = response.read(8 * 1024 * 1024 + 1)
        if len(data) > 8 * 1024 * 1024:
            raise Error('任务数据过多，请减少单次查询')
        return response.status, data, response.getheader('Set-Cookie', '')
    except (OSError, http.client.HTTPException) as exc:
        raise Error('qBittorrent 未运行或尚未就绪') from exc
    finally:
        connection.close()
