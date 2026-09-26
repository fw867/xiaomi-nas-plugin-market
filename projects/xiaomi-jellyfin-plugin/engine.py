"""Bounded Jellyfin container lifecycle (Docker Engine API).

官方镜像 jellyfin/jellyfin：配置/缓存/媒体三路挂载，端口与 Emby 插件错开，
便于与 Emby 同时安装。插件只做固定容器的启停，不代理 Jellyfin API。
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
# 官方多架构 manifest（含 amd64 / arm64），与 Docker Hub `latest` 当前指向一致。
IMAGE = 'jellyfin/jellyfin:latest@sha256:78d3ea1207d1322471fcac39a614f004f2ccf7e878f95ab2977d752f07e4dd7e'
IMAGE_VERSION = 'latest / multi-arch 78d3ea12'
NAME = 'xiaomi-plugin-jellyfin'
LABEL = 'io.xiaomi-plugin.jellyfin.owner'
# 容器内 HTTP 端口是 Jellyfin 默认 8096；宿主机用 8097，避免与 Emby 插件的 8096 冲突。
PORT = 8097
CONTAINER_HTTP = 8096
CONTAINER_HTTPS = 8920
CONTAINER_DLNA = 7359
DOCKER_SOCKET = os.environ.get('DOCKER_SOCKET', '/var/run/docker.sock')

# 关掉容器健康检查。官方镜像自带一个 30 秒一次的健康检查
# （`curl --noproxy localhost -Lk -fsS ${HEALTHCHECK_URL}`，指向 /health），
# 而每打一次 /health，Jellyfin 都会重写 SQLite 的 -shm/-wal 文件。
# 文件一改，系统的 findex 索引服务（fanotify）立刻写 /nas/sys，而 /nas/sys 建在
# 跨两块盘的 RAID1（md0）上——两块机械盘因此每 30 秒被唤醒一次，永远进不了休眠，
# 每天多出约 1.4 GB/盘的无谓写入。关掉健康检查不影响 Jellyfin 自身功能，
# 只是不再主动"探活"；服务是否可用仍由插件页面的就绪状态反映。
# 定位过程见 projects/xiaomi-disk-sleep-plugin/tools/disk-activity-report.py。
HEALTHCHECK_OFF = {'Test': ['NONE']}


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


def jellyfin_info():
    """读取 Jellyfin 无需认证的公开信息；HTTP 200 即服务已就绪。"""
    connection = http.client.HTTPConnection('127.0.0.1', PORT, timeout=6)
    try:
        connection.request('GET', '/System/Info/Public')
        response = connection.getresponse()
        body = response.read(65536)
        if response.status == 200:
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                return {}
            return data if isinstance(data, dict) else {}
        return {}
    except (OSError, http.client.HTTPException) as exc:
        raise Error('Jellyfin 未运行或尚未就绪') from exc
    finally:
        connection.close()


def container_config(config):
    """固定的容器配置：端口映射、三路数据挂载与资源限制。"""
    user = ''
    if config.get('uid') and config.get('gid'):
        user = str(config['uid']) + ':' + str(config['gid'])
    return {
        'Image': IMAGE,
        'User': user,
        'ExposedPorts': {
            str(CONTAINER_HTTP) + '/tcp': {},
            str(CONTAINER_HTTPS) + '/tcp': {},
            str(CONTAINER_DLNA) + '/udp': {},
        },
        'Env': [
            'TZ=Asia/Shanghai',
            'JELLYFIN_PublishedServerUrl=http://__NAS_IP__:' + str(PORT),
        ],
        'Labels': {LABEL: config['owner']},
        # 不要继承官方镜像的 30 秒健康检查（见 HEALTHCHECK_OFF 的说明）。
        'Healthcheck': dict(HEALTHCHECK_OFF),
        'HostConfig': {
            'RestartPolicy': {'Name': 'no'},
            'Memory': 1024 * 1024 * 1024,
            'MemorySwap': 1024 * 1024 * 1024,
            'NanoCpus': 2 * 10 ** 9,
            'PidsLimit': 512,
            'SecurityOpt': ['no-new-privileges:true'],
            'LogConfig': {'Type': 'json-file', 'Config': {'max-size': '5m', 'max-file': '2'}},
            # 必要端口映射：HTTP 对局域网开放；HTTPS/DLNA 按官方默认保留。
            'PortBindings': {
                str(CONTAINER_HTTP) + '/tcp': [{'HostIp': '0.0.0.0', 'HostPort': str(PORT)}],
                str(CONTAINER_HTTPS) + '/tcp': [{'HostIp': '0.0.0.0', 'HostPort': str(CONTAINER_HTTPS)}],
                str(CONTAINER_DLNA) + '/udp': [{'HostIp': '0.0.0.0', 'HostPort': str(CONTAINER_DLNA)}],
            },
            # 数据持久化：配置、缓存、媒体
            'Mounts': [
                {'Type': 'bind', 'Source': config['config'], 'Target': '/config'},
                {'Type': 'bind', 'Source': config['cache'], 'Target': '/cache'},
                {'Type': 'bind', 'Source': config['media'], 'Target': '/media'},
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
            if isinstance(loaded, dict) and loaded.get('owner') and loaded.get('media'):
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
        stream = self._call('POST', '/images/create?' + query, timeout=1800)
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

    @staticmethod
    def inherited_healthcheck(item):
        """容器是否还带着（镜像继承来的）健康检查。"""
        tests = ((item.get('Config') or {}).get('Healthcheck') or {}).get('Test') or []
        return bool(tests) and str(tests[0]).upper() != 'NONE'

    def drop_inherited_healthcheck(self, item):
        """把旧版本插件建出来的容器重建掉，去掉镜像自带的健康检查。

        健康检查只能在创建容器时决定：Docker 20.10 的
        `POST /containers/<id>/update` 虽然接受 Healthcheck 字段并返回 200，
        但实际不生效（inspect 里仍是原值，State.Health 也照旧每 30 秒追加记录）。
        所以这里停容器 → 删除 → 交给调用方按新配置重建。
        /config、/cache、/media 都是 bind 挂载，配置与媒体库不受影响。
        返回 True 表示已经把它删掉了。
        """
        if not self.inherited_healthcheck(item):
            return False
        if item.get('State', {}).get('Running'):
            self._call('POST', '/containers/' + NAME + '/stop?t=30', timeout=90)
        self._call('DELETE', '/containers/' + NAME, ok=(200, 204, 404))
        return True

    def browse(self, relative):
        folder = confined(self.root, relative)
        return sorted([{'name': p.name, 'path': (relative + '/' if relative else '') + p.name}
                       for p in folder.iterdir()
                       if not p.name.startswith('.') and p.is_dir() and not p.is_symlink()],
                      key=lambda p: p['name'])[:1000]

    def snapshot(self):
        running, ready, error = False, False, self.error
        server_version, wizard = '', None
        if self.config and not self.busy:
            try:
                item = self.owned()
                running = bool(item and item.get('State', {}).get('Running'))
            except Error as exc:
                error = str(exc)
            if running:
                try:
                    info = jellyfin_info()
                    ready = True
                    server_version = str(info.get('Version', ''))[:32]
                    if isinstance(info.get('StartupWizardCompleted'), bool):
                        wizard = info['StartupWizardCompleted']
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
            'directory': self.config['media_relative'] if self.config else '',
            'configDirectory': self.config.get('config_relative', '') if self.config else '',
            'serverVersion': server_version,
            'wizardCompleted': wizard,
            'imageVersion': IMAGE_VERSION,
            'healthcheckOff': bool(self.config and self.config.get('healthcheck_off')),
        }

    def setup(self, media_relative, config_relative=''):
        if self.config:
            raise Error('已完成初始化；现有媒体目录和 Jellyfin 配置不会被覆盖')
        if not isinstance(media_relative, str) or not media_relative or len(media_relative) > 1024:
            raise Error('请选择媒体目录')
        media = confined(self.root, media_relative)
        if ',' in str(media):
            raise Error('Docker 挂载目录不能包含逗号')
        media_stat = media.stat()
        if not media_stat.st_uid or not media_stat.st_gid:
            raise Error('所选目录须由非 root 的 NAS 用户拥有')
        uid, gid = media_stat.st_uid, media_stat.st_gid

        if config_relative:
            if not isinstance(config_relative, str) or len(config_relative) > 1024:
                raise Error('配置目录无效')
            cfgdir = confined(self.root, config_relative)
            if ',' in str(cfgdir):
                raise Error('Docker 挂载目录不能包含逗号')
            if cfgdir == media or cfgdir in media.parents or media in cfgdir.parents:
                raise Error('配置目录与媒体目录不能相同或互相包含')
            cfg_stat = cfgdir.stat()
            if not cfg_stat.st_uid or not cfg_stat.st_gid:
                raise Error('配置目录须由非 root 的 NAS 用户拥有')
        else:
            cfgdir = self.data / 'config'
            cfgdir.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                os.chown(cfgdir, uid, gid)
            except OSError:
                pass
            os.chmod(cfgdir, 0o700)

        # 缓存始终落在插件私有目录，避免占用用户可见存储，也保证重建容器后可丢弃。
        cachedir = self.data / 'cache'
        cachedir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chown(cachedir, uid, gid)
        except OSError:
            pass
        os.chmod(cachedir, 0o700)

        self._call('GET', '/info')
        if self.inspect() is not None:
            raise Error('同名容器已存在，拒绝覆盖')

        mstat, cstat, kstat = media.stat(), cfgdir.stat(), cachedir.stat()
        self.config = {
            'owner': secrets.token_hex(24),
            'uid': uid,
            'gid': gid,
            'media_relative': media_relative,
            'media': str(media),
            'media_device': mstat.st_dev,
            'media_inode': mstat.st_ino,
            'config': str(cfgdir),
            'config_relative': config_relative or '',
            'config_device': cstat.st_dev,
            'config_inode': cstat.st_ino,
            'cache': str(cachedir),
            'cache_device': kstat.st_dev,
            'cache_inode': kstat.st_ino,
            'enabled': True,
        }
        atomic_json(self.cfgfile, self.config)

    def check_directories(self):
        media = confined(self.root, self.config['media_relative'])
        stat = media.stat()
        if str(media) != self.config['media'] or (stat.st_dev, stat.st_ino) != (
                self.config['media_device'], self.config['media_inode']):
            raise Error('媒体目录身份已变化，拒绝启动；请先检查存储挂载')
        cfg_relative = self.config.get('config_relative')
        if cfg_relative:
            cfgdir = confined(self.root, cfg_relative)
            cstat = cfgdir.stat()
            if str(cfgdir) != self.config['config'] or (cstat.st_dev, cstat.st_ino) != (
                    self.config['config_device'], self.config['config_inode']):
                raise Error('配置目录身份已变化，拒绝启动；请先检查存储挂载')

    def create_container(self):
        """按当前配置建容器并启动；调用方保证镜像已在本地。"""
        self.check_directories()
        self._call('POST', '/containers/create?name=' + NAME,
                   body=container_config(self.config), timeout=120)
        self._call('POST', '/containers/' + NAME + '/start')

    def start(self):
        if not self.config:
            raise Error('请先初始化')
        self.check_directories()
        item = self.owned()
        if item and self.inherited_healthcheck(item):
            # 旧容器带着镜像自带的 30 秒健康检查，必须重建才能去掉（见方法说明）。
            # 升级后插件服务重启、或用户点「启动服务」时都会走到这里。
            self.drop_inherited_healthcheck(item)
            item = None
            self.create_container()
        elif item is None:
            self.pull()
            self.create_container()
        elif not item.get('State', {}).get('Running'):
            self._call('POST', '/containers/' + NAME + '/start')
        # 到这里容器要么是新建的（配置里写死了健康检查 NONE），
        # 要么本来就带着 NONE，所以可以标记为已关闭。
        self.config['healthcheck_off'] = True
        self.config['enabled'] = True
        atomic_json(self.cfgfile, self.config)
        # 首次初始化数据库可能较慢，超时只影响提示。
        for _ in range(150):
            try:
                jellyfin_info()
                return
            except Error:
                time.sleep(2)
        raise Error('容器已启动，但 Jellyfin 尚未就绪；可稍后刷新，首次初始化可能需要数分钟')

    def stop(self, remember=True):
        if not self.config:
            return
        item = self.owned()
        if item and item.get('State', {}).get('Running'):
            self._call('POST', '/containers/' + NAME + '/stop?t=30', timeout=90)
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
                    self.setup(data.get('path', ''), data.get('configPath', ''))
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
