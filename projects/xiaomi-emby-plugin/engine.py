"""Bounded Emby container lifecycle for the Xiaomi plugin market.

只做固定几件事：按锁定镜像创建/启停一个容器，挂载私有配置目录和用户
选择的媒体目录。不提供通用 Docker 接口，也不转发 Emby 的 API。媒体目录
以读写方式挂载，Emby 才能把元数据、字幕写到媒体文件夹。
"""
from __future__ import annotations

import http.client
import json
import os
import re
import secrets
import shutil
import socket
import threading
import time
import urllib.parse
from pathlib import Path

# 开发环境（源码树里直接跑）的回退值。页面上显示的是插件包的真实版本，
# 由 installed_version() 从自身所在的发布目录名里取。
VERSION = '0.1.0'
# Emby 官方镜像的多架构 manifest（含 amd64 / arm64v8 / arm32v7），
# 与 `latest` 当前指向同一 digest。用 tag + digest 固定，避免被上游重新推送影响。
IMAGE = 'emby/embyserver:4.10.0.40@sha256:3aafff933d3f28d23ed0bc201022abe71c0aa80deb17177566c726b9bbc686c6'
NAME = 'xiaomi-plugin-emby'
LABEL = 'io.xiaomi-plugin.emby.owner'
# Emby 容器内外都用 8096（Emby 默认 Web/API 端口），并发布到 NAS 局域网，
# 否则电视、手机等 Emby 客户端无法连接。
PORT = 8096
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


def confined(root, relative):
    """把相对路径限制在用户存储根目录内，拒绝穿越、隐藏目录和符号链接。"""
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


def emby_info():
    """读取 Emby 无需认证的公开信息；任何 HTTP 响应都算服务已就绪。"""
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
        raise Error('Emby 未运行或尚未就绪') from exc
    finally:
        connection.close()


def container_config(config):
    """固定的容器配置（Engine API 的 create body）。

    固定参数，不接受调用方传入镜像、端口、挂载或命令。对应原来那串
    `docker run -d ...`，只是换成 JSON 形式：
    --memory/--memory-swap 用字节、--cpus 用纳核、--mount 用 Mounts。
    """
    return {
        'Image': IMAGE,
        # 必须显式声明 ExposedPorts：Engine API 不像 `docker run -p` 那样自动补，
        # 只给 HostConfig.PortBindings 时，镜像 EXPOSE 里没有的端口会被静默忽略。
        # Emby 镜像恰好自带 EXPOSE 8096 才没出问题，写明不依赖这个巧合。
        'ExposedPorts': {str(PORT) + '/tcp': {}},
        'Env': [
            'UID=' + str(config['uid']),
            'GID=' + str(config['gid']),
            'GIDLIST=' + str(config['gid']),
            'TZ=Asia/Shanghai',
        ],
        'Labels': {LABEL: config['owner']},
        'HostConfig': {
            'RestartPolicy': {'Name': 'no'},
            'Memory': 1024 * 1024 * 1024,
            'MemorySwap': 1024 * 1024 * 1024,
            'NanoCpus': 2 * 10 ** 9,
            'PidsLimit': 512,
            'SecurityOpt': ['no-new-privileges:true'],
            'LogConfig': {'Type': 'json-file', 'Config': {'max-size': '5m', 'max-file': '2'}},
            'PortBindings': {str(PORT) + '/tcp': [{'HostIp': '0.0.0.0', 'HostPort': str(PORT)}]},
            # 媒体目录以读写方式挂载：Emby 要把元数据、字幕写回媒体文件夹。
            # 配置目录可能是插件私有目录，也可能是用户指定的存储目录，由 config 记录。
            'Mounts': [
                {'Type': 'bind', 'Source': config['config'], 'Target': '/config'},
                {'Type': 'bind', 'Source': config['media'], 'Target': '/mnt/media'},
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
        self.config = json.loads(self.cfgfile.read_text(encoding='utf-8')) if self.cfgfile.exists() else None

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
                    info = emby_info()
                    ready = True
                    server_version = str(info.get('Version', ''))[:32]
                    if isinstance(info.get('StartupWizardCompleted'), bool):
                        wizard = info['StartupWizardCompleted']
                except Error:
                    ready = False
        return {'version': installed_version(), 'configured': bool(self.config), 'running': running, 'ready': ready,
                'busy': self.busy, 'error': error, 'preview': self.dev, 'port': PORT,
                'directory': self.config['relative'] if self.config else '',
                'configDirectory': self.config.get('config_relative', '') if self.config else '',
                'serverVersion': server_version, 'wizardCompleted': wizard,
                'imageVersion': '4.10.0.40'}

    def setup(self, relative, config_relative=''):
        if self.config:
            raise Error('已完成初始化；现有媒体目录和 Emby 配置不会被覆盖')
        if not isinstance(relative, str) or len(relative) > 1024:
            raise Error('媒体目录无效')
        folder = confined(self.root, relative)
        if ',' in str(folder):
            raise Error('Docker 挂载目录不能包含逗号')
        uid, gid = folder.stat().st_uid, folder.stat().st_gid
        if not uid or not gid:
            raise Error('所选目录须由非 root 的 NAS 用户拥有')
        # Emby 的配置目录。默认放在插件私有目录里（外部看不到，最干净）；
        # 也可以指定存储中的一个目录，方便备份、迁移或直接在文件管理器里查看。
        # 指定时不对该目录做 chown —— 与 qB 插件一致，绝不改动用户已有文件的所有权。
        if config_relative:
            if not isinstance(config_relative, str) or len(config_relative) > 1024:
                raise Error('配置目录无效')
            cfgdir = confined(self.root, config_relative)
            if ',' in str(cfgdir):
                raise Error('Docker 挂载目录不能包含逗号')
            if cfgdir == folder or cfgdir in folder.parents or folder in cfgdir.parents:
                raise Error('配置目录与媒体目录不能相同或互相包含')
            cfg_stat = cfgdir.stat()
            if not cfg_stat.st_uid or not cfg_stat.st_gid:
                raise Error('配置目录须由非 root 的 NAS 用户拥有')
        else:
            cfgdir = self.data / 'config'
            cfgdir.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chown(cfgdir, uid, gid)
            os.chmod(cfgdir, 0o700)
        self._call('GET', '/info')
        if self.inspect() is not None:
            raise Error('同名容器已存在，拒绝覆盖')
        stat = folder.stat()
        cfg_stat = cfgdir.stat()
        self.config = {'owner': secrets.token_hex(24), 'relative': relative, 'media': str(folder),
                       'uid': uid, 'gid': gid, 'device': stat.st_dev, 'inode': stat.st_ino,
                       'config': str(cfgdir), 'config_relative': config_relative or '',
                       'config_device': cfg_stat.st_dev, 'config_inode': cfg_stat.st_ino,
                       'enabled': True}
        atomic_json(self.cfgfile, self.config)

    def check_directory(self):
        folder = confined(self.root, self.config['relative'])
        stat = folder.stat()
        if str(folder) != self.config['media'] or (stat.st_dev, stat.st_ino) != (self.config['device'], self.config['inode']):
            raise Error('媒体目录身份已变化，拒绝启动；请先检查存储挂载')
        # 只有用户指定的配置目录（在存储里）需要校验身份；私有目录由插件自己管。
        cfg_relative = self.config.get('config_relative')
        if cfg_relative:
            cfgdir = confined(self.root, cfg_relative)
            cfg_stat = cfgdir.stat()
            if (cfg_stat.st_dev, cfg_stat.st_ino) != (self.config['config_device'], self.config['config_inode']):
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
        # Emby 首次启动要初始化数据库，可能明显慢于 qB；超时只影响提示，容器仍在运行。
        for _ in range(150):
            try:
                emby_info()
                return
            except Error:
                time.sleep(2)
        raise Error('容器已启动，但 Emby 尚未就绪；可稍后刷新页面，首次初始化可能需要数分钟')

    def stop(self, remember=True):
        if not self.config:
            return
        item = self.owned()
        if item and item.get('State', {}).get('Running'):
            # 给容器 30 秒优雅退出；stop 本身会阻塞到容器停下，所以超时要放宽。
            self._call('POST', '/containers/' + NAME + '/stop?t=30', timeout=90)
        if remember:
            self.config['enabled'] = False
            atomic_json(self.cfgfile, self.config)

    def remove(self):
        """删掉本插件自己的容器。

        挂载这类参数只在创建时确定，改了就必须重建，而 start() 遇到已存在的
        容器只会把它启动起来。只删带本插件所有权标签的容器。
        """
        if self.dev:
            raise Error('预览模式不会操作 Docker')
        if self.owned() is None:
            return
        self._call('DELETE', '/containers/' + NAME, ok=(200, 204))

    @staticmethod
    def _count_files(folder):
        return sum(1 for path in Path(folder).rglob('*') if path.is_file())

    def relocate_config(self, relative):
        """把配置目录迁到存储中的新位置（已初始化的实例换位置只能走这里）。

        顺序很关键：先停容器（Emby 的数据库不能带着运行状态直接拷），复制后
        核对文件数，一致才删旧目录。任何一步不满足都抛错并保留原配置，
        不会出现「两处各留一半」的状态。调用方负责在成功后重建容器。
        """
        if not self.config:
            raise Error('请先初始化')
        if not isinstance(relative, str) or not relative or len(relative) > 1024:
            raise Error('请选择新的配置目录')
        target = confined(self.root, relative)
        if ',' in str(target):
            raise Error('Docker 挂载目录不能包含逗号')
        media = Path(self.config['media'])
        if target == media or target in media.parents or media in target.parents:
            raise Error('配置目录与媒体目录不能相同或互相包含')
        if not target.stat().st_uid or not target.stat().st_gid:
            raise Error('配置目录须由非 root 的 NAS 用户拥有')
        current = Path(self.config['config'])
        if str(target) == str(current):
            raise Error('新目录与当前配置目录相同')
        if any(target.iterdir()):
            raise Error('新配置目录须为空，以免与已有文件混在一起')

        if current.is_dir():
            self.stop(remember=False)
            shutil.copytree(current, target, symlinks=True, dirs_exist_ok=True)
            if self._count_files(current) != self._count_files(target):
                raise Error('复制后文件数量不一致，已保留原配置目录，未改动任何设置')
        stat = target.stat()
        self.config['config'] = str(target)
        self.config['config_relative'] = relative
        self.config['config_device'] = stat.st_dev
        self.config['config_inode'] = stat.st_ino
        atomic_json(self.cfgfile, self.config)
        # 挂载变了，旧容器必须删掉，交给 start() 按新配置重建
        self.remove()
        if current.is_dir():
            shutil.rmtree(current, ignore_errors=True)

    def launch(self, action, data):
        if self.dev:
            raise Error('预览模式不会启动服务或修改 NAS')
        if action not in ('setup', 'start', 'stop', 'relocate'):
            raise Error('未知服务操作')
        if not self.lock.acquire(False):
            raise Error('服务操作正在进行，请稍候')
        self.busy, self.error = True, ''

        def work():
            try:
                if action == 'setup':
                    self.setup(data.get('path', ''), data.get('configPath', ''))
                elif action == 'relocate':
                    self.relocate_config(data.get('configPath', ''))
                if action in ('setup', 'start', 'relocate'):
                    self.start()
                elif action == 'stop':
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
