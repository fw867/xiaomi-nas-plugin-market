"""Bounded Jellyfin container lifecycle (Docker Engine API).

官方镜像 jellyfin/jellyfin：配置/缓存/媒体三路挂载，端口与 Emby 插件错开，
便于与 Emby 同时安装。插件只做固定容器的启停，不代理 Jellyfin API。
"""
from __future__ import annotations

import contextlib
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

# 插件给容器设的环境变量键。`JELLYFIN_PublishedServerUrl` 是**已经删掉**的那个：
# 历史上写死成 `http://__NAS_IP__:8097`，占位符从来没有被替换过，于是 Jellyfin
# 把 `http://__NAS_IP__:8097` 当成自己的对外地址（`GET /System/Info/Public` 的
# LocalAddress 就是它），客户端拿到这个解析不了的域名就会连接失败、会话中断——
# 远程访问时表现为"用着用着就退出了"。环境变量只在创建容器时生效，所以旧容器
# 里残留这个键时必须在 start() 里重建一次。
CONTAINER_ENV_KEYS = ('TZ',)
REMOVED_ENV_KEYS = ('JELLYFIN_PublishedServerUrl',)


def container_env():
    """要往容器里塞的环境变量（只放必需的）。

    不再设置 JELLYFIN_PublishedServerUrl：设成局域网地址对远程访问是错的，
    而地址由客户端用哪个地址连上来决定才是对的，所以交给 Jellyfin 自己判断。
    """
    return ['TZ=Asia/Shanghai']


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


def covering_mount(path):
    """返回覆盖该路径的挂载点与文件系统类型（最长前缀匹配）；没有则 (None, '')。

    用途见 check_directory()：厂商的存储池 /nas/pool0 是 FUSE（fuse.cfs），
    **每次挂载都会换一套匿名设备号并重新合成 inode 号**，所以 st_dev/st_ino
    天然对不上；"这个目录到底在不在一个挂载点下面"才是稳定且有意义的判据。
    """
    best_point, best_type = None, ''
    try:
        with open('/proc/self/mountinfo', encoding='utf-8') as handle:
            for line in handle:
                left, separator, right = line.partition(' - ')
                fields = left.split()
                if not separator or len(fields) < 5:
                    continue
                point = (fields[4].replace('\\040', ' ')
                                  .replace('\\011', '\t')
                                  .replace('\\134', '\\'))
                if path != point and not path.startswith(point.rstrip('/') + '/'):
                    continue
                if best_point is None or len(point) > len(best_point):
                    best_point, best_type = point, right.split()[0]
    except OSError:
        return None, ''
    return best_point, best_type


def volatile_identity(fstype):
    """该文件系统的设备号/inode 号是否"每次挂载都变"——FUSE 都是。"""
    return fstype.startswith('fuse')


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


def unique_paths(values):
    """去重保序地转成 Path 列表（跳过空值）。

    去重按规范化后的字符串比较，顺序即「存储位置」在前端出现的顺序。
    """
    paths, seen = [], set()
    for value in values or ():
        if not value:
            continue
        path = Path(value)
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        paths.append(path)
    return paths


def parse_roots(value):
    """解析 LOCAL_ROOTS（冒号分隔的绝对路径，顺序即位置顺序）。

    只保留绝对路径：相对路径的含义随工作目录变化，不能当根用。
    调用方（server.py）在未设置该变量时退化成单个 LOCAL_ROOT。
    """
    parts = []
    for part in (value or '').split(':'):
        # Windows 盘符（C:\...）里的冒号不是分隔符；插件跑在 NAS（Linux）上，
        # 这里只是让同一份代码在开发机上也能按原样解析。
        if parts and os.name == 'nt' and len(parts[-1]) == 1 and parts[-1].isalpha():
            parts[-1] += ':' + part
        else:
            parts.append(part)
    items = [part.strip() for part in parts]
    return [str(path) for path in unique_paths(item for item in items
                                               if item and Path(item).is_absolute())]


def root_label(path):
    """存储位置的显示名。

    小米 NAS 上有两类位置：内置存储池（/nas/pool0 之下，是 FUSE）和外接设备
    （U 盘：稳定 bind 路径 /nas/mnt/usb，或内核挂载点 /mnt/usb-xxxx）；
    其它位置就用最后一段目录名。
    """
    text = str(path).replace('\\', '/').rstrip('/') or '/'
    if text == '/nas/pool0' or text.startswith('/nas/pool0/'):
        return '存储池'
    if text.startswith('/nas/mnt/usb') or text.startswith('/mnt/usb-'):
        return '外接设备'
    return text.rsplit('/', 1)[-1] or text


def root_index_of(roots, path):
    """绝对路径反查它属于哪个根（最长前缀匹配）；不属于任何根返回 -1。

    按路径分量比较，所以 /nas/pool0x 不会被当成 /nas/pool0 之下；
    嵌套挂载点（如 /nas/mnt/usb 挂在某个池目录里）取最长的那个根。
    """
    target = Path(path)
    best, best_length = -1, -1
    for index, root in enumerate(roots):
        try:
            target.relative_to(root)
        except ValueError:
            continue
        if len(str(root)) > best_length:
            best, best_length = index, len(str(root))
    return best


def relative_in_root(root, path):
    """根内相对路径（正斜杠分隔，browse 接口与 confined 都用这种形式）；根自身返回空串。"""
    return '/'.join(Path(path).relative_to(Path(root)).parts)


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
        'Env': container_env(),
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
    def __init__(self, data, root, dev=False, roots=None):
        self.data, self.dev = Path(data), dev
        # 多个「存储位置」：内置存储池 / 外接设备。roots 顺序即前端位置顺序，
        # 未给出时退化为单个 root；self.root 始终是第一个（旧调用与旧配置照旧）。
        self.roots = unique_paths(roots or [root]) or [Path(root)]
        self.root = self.roots[0]
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

    @staticmethod
    def stale_env(item):
        """容器里的环境变量和现在要求的不一致（只能在创建时生效，得重建）。

        两个方向都要看：插件现在要设的键值对不上，以及插件**已经不再设置**的键
        （例如写过假地址的 JELLYFIN_PublishedServerUrl）还残留在旧容器里。
        """
        current = {}
        for entry in (item.get('Config') or {}).get('Env') or []:
            key, _, value = str(entry).partition('=')
            current[key] = value
        for entry in container_env():
            key, _, value = entry.partition('=')
            if current.get(key) != value:
                return True
        return any(key in current for key in REMOVED_ENV_KEYS)

    def recreate_if_stale(self, item):
        """旧配置只能靠重建生效：继承的健康检查、或已删除的环境变量。

        健康检查只能在创建容器时决定（Docker 20.10 的
        `POST /containers/<id>/update` 接受 Healthcheck 并返回 200，但不生效）；
        环境变量同理。这里停容器 → 删除 → 交给调用方按新配置重建。
        /config、/cache、/media 都是 bind 挂载，配置与媒体库不受影响。
        返回 True 表示已经把它删掉了。
        """
        reasons = []
        if self.inherited_healthcheck(item):
            reasons.append('镜像自带的健康检查')
        if self.stale_env(item):
            reasons.append('过期的环境变量')
        if not reasons:
            return False
        if item.get('State', {}).get('Running'):
            self._call('POST', '/containers/' + NAME + '/stop?t=30', timeout=90)
        self._call('DELETE', '/containers/' + NAME, ok=(200, 204, 404))
        print('jellyfin: 重建容器（%s）' % '、'.join(reasons), flush=True)
        return True

    def _root_index(self, index):
        """规范化请求里的位置序号；越界或非法即报错。"""
        try:
            index = int(index)
        except (TypeError, ValueError):
            raise Error('存储位置无效') from None
        if not 0 <= index < len(self.roots):
            raise Error('存储位置无效')
        return index

    def locations(self):
        """给前端的「存储位置」列表（切换浏览根用）。"""
        # exists 与 confined() 的判据一致：符号链接的根不算可用
        return [{'index': index, 'label': root_label(root), 'path': str(root),
                 'exists': root.is_dir() and not root.is_symlink()}
                for index, root in enumerate(self.roots)]

    def locate(self, choice, root_index=None):
        """把用户提交的目录选择解析成（位置序号，根内相对路径）。

        绝对路径一律按最长前缀反查它属于哪个存储位置（前端提交的就是绝对路径）；
        相对路径保持旧语义，用调用方指定的根（root_index 缺省为第 0 个）。
        """
        if Path(choice).is_absolute():
            index = root_index_of(self.roots, choice)
            if index < 0:
                raise Error('所选目录必须位于已挂载的存储位置内')
            return index, relative_in_root(self.roots[index], choice)
        return self._root_index(0 if root_index is None else root_index), choice

    def browse(self, relative, root_index=0):
        folder = confined(self.roots[self._root_index(root_index)], relative)
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
            # 状态卡片显示完整绝对路径：相对路径在「存储位置」多于一个时看不出在哪
            'media_abs': self.config.get('media', '') if self.config else '',
            'config_abs': self.config.get('config', '') if self.config else '',
            # 配置目录留在插件私有目录（不属于任何存储位置）时为真，前端据此加一句说明
            'config_private': bool(self.config) and not (
                self.config.get('config_root') or self.config.get('config_relative')),
            'roots': self.locations(),
            'serverVersion': server_version,
            'wizardCompleted': wizard,
            'imageVersion': IMAGE_VERSION,
            'healthcheckOff': bool(self.config and self.config.get('healthcheck_off')),
        }

    def _resolve_dirs(self, media, config_relative, media_root=None, config_root=None):
        """按插件既有规则校验并解析媒体/配置目录（setup 与 reconfigure 共用）。

        返回 {media_index, media_relative, media, uid, gid, config, config_relative,
        config_root}：目录选择可以是绝对路径（前端提交）或根内相对路径（旧语义），
        配置目录留空时用插件私有目录（此时 config_root 为空串）。
        """
        if not isinstance(media, str) or not media or len(media) > 1024:
            raise Error('请选择媒体目录')
        media_index, media_relative = self.locate(media, media_root)
        folder = confined(self.roots[media_index], media_relative)
        if ',' in str(folder):
            raise Error('Docker 挂载目录不能包含逗号')
        media_stat = folder.stat()
        if not media_stat.st_uid or not media_stat.st_gid:
            raise Error('所选目录须由非 root 的 NAS 用户拥有')
        uid, gid = media_stat.st_uid, media_stat.st_gid

        # 选了外部配置目录才记录它的根；留空时配置在插件私有目录（不属于任何根）。
        chosen_config_root = ''
        if config_relative:
            if not isinstance(config_relative, str) or len(config_relative) > 1024:
                raise Error('配置目录无效')
            config_index, config_relative = self.locate(config_relative, config_root)
            cfgdir = confined(self.roots[config_index], config_relative)
            if ',' in str(cfgdir):
                raise Error('Docker 挂载目录不能包含逗号')
            if cfgdir == folder or cfgdir in folder.parents or folder in cfgdir.parents:
                raise Error('配置目录与媒体目录不能相同或互相包含')
            cfg_stat = cfgdir.stat()
            if not cfg_stat.st_uid or not cfg_stat.st_gid:
                raise Error('配置目录须由非 root 的 NAS 用户拥有')
            chosen_config_root = str(self.roots[config_index])
        else:
            cfgdir = self.data / 'config'
            cfgdir.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                os.chown(cfgdir, uid, gid)
            except OSError:
                pass
            os.chmod(cfgdir, 0o700)
        return {
            'media_index': media_index,
            'media_relative': media_relative,
            'media': folder,
            'uid': uid,
            'gid': gid,
            'config': cfgdir,
            'config_relative': config_relative or '',
            'config_root': chosen_config_root,
        }

    def _ensure_cache(self, uid, gid):
        """缓存始终落在插件私有目录（挂载为 /cache），不跟着媒体目录走。"""
        cachedir = self.data / 'cache'
        cachedir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chown(cachedir, uid, gid)
        except OSError:
            pass
        os.chmod(cachedir, 0o700)
        return cachedir

    def setup(self, media, config_relative='', media_root=None, config_root=None):
        """初始化：目录选择可以是绝对路径（前端提交）或根内相对路径（旧语义）。"""
        if self.config:
            raise Error('已完成初始化；现有媒体目录和 Jellyfin 配置不会被覆盖')
        chosen = self._resolve_dirs(media, config_relative, media_root, config_root)
        folder, cfgdir = chosen['media'], chosen['config']
        cachedir = self._ensure_cache(chosen['uid'], chosen['gid'])

        self._call('GET', '/info')
        if self.inspect() is not None:
            raise Error('同名容器已存在，拒绝覆盖')

        mstat, cstat, kstat = folder.stat(), cfgdir.stat(), cachedir.stat()
        self.config = {
            'owner': secrets.token_hex(24),
            'uid': chosen['uid'],
            'gid': chosen['gid'],
            # *_root 记下这个选择落在哪个存储位置，启动校验时据此回到同一个根
            'media_root': str(self.roots[chosen['media_index']]),
            'media_relative': chosen['media_relative'],
            'media': str(folder),
            'media_device': mstat.st_dev,
            'media_inode': mstat.st_ino,
            'config_root': chosen['config_root'],
            'config': str(cfgdir),
            'config_relative': chosen['config_relative'],
            'config_device': cstat.st_dev,
            'config_inode': cstat.st_ino,
            'cache': str(cachedir),
            'cache_device': kstat.st_dev,
            'cache_inode': kstat.st_ino,
            'enabled': True,
        }
        atomic_json(self.cfgfile, self.config)

    @contextlib.contextmanager
    def _operation(self):
        """同步入口（修改目录 / 重新初始化）的操作位：已有操作在进行时直接拒绝。"""
        if self.busy or not self.lock.acquire(False):
            raise Error('当前有操作正在进行，请稍后再试')
        self.busy, self.error = True, ''
        try:
            yield
        finally:
            self.busy = False
            self.lock.release()

    def reconfigure(self, media, config_relative=None, media_root=None, config_root=None):
        """修改媒体/配置目录：保留数据，按新宿主路径重建容器。

        config_relative 传 None 表示「配置目录不动」（只改媒体目录的请求）；
        传空串表示改回插件私有目录。成功返回最新状态，失败抛出原因（已回滚）。
        """
        with self._operation():
            self._do_reconfigure(media, config_relative, media_root, config_root)
        return self.snapshot()

    def _current_config_choice(self):
        """当前配置目录对应的「选择值」：插件私有目录返回空串，用户目录返回绝对路径。"""
        absolute = (self.config or {}).get('config', '')
        if not absolute:
            return ''
        try:
            Path(absolute).relative_to(self.data)
        except ValueError:
            return absolute              # 用户可见目录：按绝对路径重新解析（含所属根）
        return ''                        # 插件私有目录：继续留空

    def reset(self, confirm=False):
        """重新初始化：移除容器并把配置挪成带时间戳的备份；用户目录里的文件一个都不动。"""
        with self._operation():
            self._do_reset(confirm)
        return self.snapshot()

    def _backup_path(self):
        """带时间戳的备份文件名（同一秒内连续操作也不覆盖已有的）。"""
        stamp = time.strftime('%Y%m%d-%H%M%S')
        target = self.data / ('settings.json.bak-' + stamp)
        index = 1
        while target.exists():
            index += 1
            target = self.data / ('settings.json.bak-%s-%d' % (stamp, index))
        return target

    def _backup_config(self):
        """把当前 settings.json 另存为备份，返回备份路径（原文件不动）。"""
        target = self._backup_path()
        tmp = self.data / (target.name + '.tmp')
        tmp.write_bytes(self.cfgfile.read_bytes())
        os.chmod(tmp, 0o600)
        tmp.replace(target)
        return target

    def _do_reconfigure(self, media, config_relative=None, media_root=None, config_root=None):
        """换目录但保留数据：备份配置 → 按新 bind 重建容器 → 失败回滚配置与原容器。"""
        if not self.config:
            raise Error('请先初始化')
        if config_relative is None:
            # 只提交了媒体目录：配置目录沿用当前设置，别默默换成插件私有目录
            config_relative = self._current_config_choice()
        chosen = self._resolve_dirs(media, config_relative, media_root, config_root)
        self._ensure_cache(chosen['uid'], chosen['gid'])          # 缓存挂在 /data 上，位置不动
        old_config = dict(self.config)
        mstat, cstat = chosen['media'].stat(), chosen['config'].stat()
        new_config = dict(old_config)
        new_config.update({
            'uid': chosen['uid'],
            'gid': chosen['gid'],
            'media_root': str(self.roots[chosen['media_index']]),
            'media_relative': chosen['media_relative'],
            'media': str(chosen['media']),
            'media_device': mstat.st_dev,
            'media_inode': mstat.st_ino,
            'config_root': chosen['config_root'],
            'config': str(chosen['config']),
            'config_relative': chosen['config_relative'],
            'config_device': cstat.st_dev,
            'config_inode': cstat.st_ino,
            'healthcheck_off': True,                              # 新建的容器一律不带健康检查
            'enabled': True,
        })
        backup = self._backup_config()
        self.config = new_config
        try:
            atomic_json(self.cfgfile, self.config)
        except OSError as exc:
            # 配置还没写下去，容器也没动过：把内存改回去就够了
            self.config = old_config
            raise Error('配置写入失败（%s），未改动容器' % exc) from exc
        try:
            self._rebuild_container()
        except Exception as exc:
            problems = self._rollback_reconfigure(old_config)
            message = str(exc) if isinstance(exc, Error) else '更换目录失败（%s）' % exc
            if problems:
                message += '；回滚时也遇到问题：' + '、'.join(problems)
            raise Error(message) from exc
        print('jellyfin: 已更换目录（备份 %s），容器已按新挂载重建' % backup.name, flush=True)

    def _rebuild_container(self):
        """按当前 self.config 重建容器：停 → 删 → 建 → 启。

        只 restart 不行：bind 挂载在创建容器时固定，旧容器会一直指向旧目录。
        """
        item = self.owned()
        if item is not None:
            if item.get('State', {}).get('Running'):
                self._call('POST', '/containers/' + NAME + '/stop?t=30', timeout=90)
            self._call('DELETE', '/containers/' + NAME, ok=(200, 204, 404))
        self.create_container()

    def _restore_old_container(self, old_config):
        """尽力按旧配置把容器恢复回来；问题收集起来一起上报，不盖掉最初的原因。"""
        problems = []
        try:
            item = self.inspect()
            if item is not None:
                # 可能是刚建到一半的新容器：先清掉，再按旧配置重建，保证挂载回到旧目录
                if item.get('State', {}).get('Running'):
                    self._call('POST', '/containers/' + NAME + '/stop?t=30', timeout=90)
                self._call('DELETE', '/containers/' + NAME, ok=(200, 204, 404))
            self.config = old_config
            self.create_container()
        except (Error, OSError) as exc:
            problems.append('按原目录重建容器失败（%s）' % exc)
        return problems

    def _rollback_reconfigure(self, old_config):
        """把配置与容器尽量恢复到改目录之前；返回恢复过程中的问题。"""
        self.config = old_config
        problems = []
        try:
            atomic_json(self.cfgfile, old_config)
        except OSError as exc:
            problems.append('配置写回失败（%s）' % exc)
        problems.extend(self._restore_old_container(old_config))
        return problems

    def _do_reset(self, confirm=False):
        """重新初始化：停容器 → 删容器 → 配置挪成备份。

        用户数据（媒体目录、配置目录）里的文件一个都不删、不移、不改；
        本插件没有路由器端口映射之类的副作用，容器就是唯一的系统改动。
        """
        if confirm is not True:
            raise Error('请确认重新初始化')
        item = self.owned()                     # 归属校验：同名容器不属于本插件就拒绝
        if item is not None:
            if item.get('State', {}).get('Running'):
                self._call('POST', '/containers/' + NAME + '/stop?t=30', timeout=90)
            self._call('DELETE', '/containers/' + NAME, ok=(200, 204, 404))
        backup = ''
        if self.config:
            backup = self._backup_config().name
            try:
                self.cfgfile.unlink()
            except FileNotFoundError:
                pass
            except OSError as exc:
                raise Error('配置已备份为 %s，但删除失败（%s）' % (backup, exc)) from exc
            self.config = None                  # 文件真的删掉了才清内存，两者保持一致
        print('jellyfin: 已重新初始化（容器与插件配置已清理，备份 %s；用户目录未改动）'
              % (backup or '无'), flush=True)

    def _identity_ok(self, folder, stat, dev_key, ino_key, label):
        """目录身份是否可信。

        FUSE（厂商存储池 /nas/pool0 是 fuse.cfs）每次挂载都会换匿名设备号、重新合成
        inode 号，dev/ino 对不上是正常的；这时只要目录确实落在某个挂载点下面
        （没挂盘会落到 /nas 的空目录上）就算通过，并把新值记下来。
        """
        if (stat.st_dev, stat.st_ino) == (self.config[dev_key], self.config[ino_key]):
            return False
        point, fstype = covering_mount(str(folder))
        if not point or not volatile_identity(fstype):
            raise Error(label + '身份已变化，拒绝启动；请先检查存储挂载')
        self.config[dev_key], self.config[ino_key] = stat.st_dev, stat.st_ino
        print('jellyfin: %s 所在的 %s（%s）每次挂载都会换设备号/inode 号，已更新记录'
              % (label, fstype, point), flush=True)
        return True

    def _config_root(self, path_key):
        """启动校验该用哪个根（存储位置）。

        配置里记下的 *_root 优先（即使 LOCAL_ROOTS 变了也回到同一个根）；
        旧配置没有这个字段，就按绝对路径前缀反查；再不行退化为第一个根。
        """
        recorded = self.config.get(path_key + '_root')
        if isinstance(recorded, str) and recorded:
            return Path(recorded)
        absolute = self.config.get(path_key)
        if isinstance(absolute, str) and absolute:
            index = root_index_of(self.roots, absolute)
            if index >= 0:
                return self.roots[index]
        return self.root

    def check_directories(self):
        targets = [('media', 'media_device', 'media_inode', '媒体目录')]
        # 选了外部配置目录才有目录身份要校验；旧配置没有 config_root，仍看 config_relative。
        if self.config.get('config_root') or self.config.get('config_relative'):
            targets.append(('config', 'config_device', 'config_inode', '配置目录'))
        refreshed = False
        for path_key, dev_key, ino_key, label in targets:
            root = self._config_root(path_key)
            folder = confined(root, self.config[path_key + '_relative'])
            try:
                stat = folder.stat()
            except OSError as exc:
                raise Error(label + '不可用（%s），拒绝启动；请先检查存储挂载' % exc.strerror) from exc
            if str(folder) != self.config[path_key]:
                raise Error(label + '身份已变化，拒绝启动；请先检查存储挂载')
            refreshed = self._identity_ok(folder, stat, dev_key, ino_key, label) or refreshed
        if refreshed:
            atomic_json(self.cfgfile, self.config)

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
        if item and self.recreate_if_stale(item):
            # 旧容器带着镜像自带的 30 秒健康检查、或残留着写死假地址的环境变量，
            # 都只能靠重建去掉（见方法说明）。升级后插件服务重启、或用户点
            # 「启动服务」时都会走到这里。
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

    def launch(self, action, data, wait=0):
        if self.dev:
            raise Error('预览模式不会启动服务或修改 NAS')
        if action not in ('setup', 'start', 'stop', 'reconfigure', 'reset'):
            raise Error('未知服务操作')
        if self.busy or not self.lock.acquire(False):
            raise Error('当前有操作正在进行，请稍后再试')
        self.busy, self.error = True, ''

        def work():
            try:
                if action == 'setup':
                    self.setup(data.get('path', ''), data.get('configPath', ''),
                               data.get('pathRoot'), data.get('configRoot'))
                    self.start()
                elif action == 'reconfigure':
                    # configPath 缺失（None）＝保持当前配置目录；传空串才是改成插件私有目录
                    self._do_reconfigure(data.get('path', ''), data.get('configPath'),
                                         data.get('pathRoot'), data.get('configRoot'))
                elif action == 'reset':
                    self._do_reset(data.get('confirm'))
                elif action == 'start':
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
        if wait:
            # 同步入口（修改目录 / 重新初始化）：跑得快就当场给结果，慢则让页面继续轮询
            self.worker.join(wait)
