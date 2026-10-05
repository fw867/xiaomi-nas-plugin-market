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

# 显式关掉容器健康检查。当前 Emby 镜像（emby/embyserver:4.10.0.40）本身没带
# HEALTHCHECK，所以现在没有实际效果；写死它是为了防止上游镜像以后加上——
# 一旦有健康检查，它就会定时打 HTTP 接口，让 Emby 反复读写 SQLite 的
# -shm/-wal，而媒体库/配置目录在机械盘上，硬盘就再也进不了休眠。
# 同类问题的完整定位过程见
# projects/xiaomi-disk-sleep-plugin/tools/disk-activity-report.py。
HEALTHCHECK_OFF = {'Test': ['NONE']}


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
    items = []
    for part in (value or '').split(':'):
        item = part.strip()
        if item and Path(item).is_absolute():
            items.append(item)
    return [str(path) for path in unique_paths(items)]


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


def unique_backup(path):
    """备份文件名：settings.json.bak-<时间戳>，已被占用时再加序号。

    同一秒内重复操作不能覆盖上一份备份，否则回滚的退路就没了。
    """
    stamp = time.strftime('%Y%m%d%H%M%S')
    candidate = path.with_name(path.name + '.bak-' + stamp)
    serial = 1
    while candidate.exists():
        candidate = path.with_name(path.name + '.bak-' + stamp + '-' + str(serial))
        serial += 1
    return candidate


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
        # 不要继承镜像可能自带的健康检查（见 HEALTHCHECK_OFF 的说明）。
        'Healthcheck': dict(HEALTHCHECK_OFF),
        'HostConfig': {
            # 失败后自动重启，但限次：Emby 扫描媒体库时内存会明显上涨，撞到
            # 限额会被 cgroup 的 oom-killer 杀掉。限次可以避免在持续 OOM 时
            # 变成无限重启循环。
            'RestartPolicy': {'Name': 'on-failure', 'MaximumRetryCount': 3},
            # 原来卡在 1 GiB 太紧：实测 EmbyServer 的匿名内存会涨到 ~980 MB，
            # 一撞线就被杀，而容器日志只留一句 "Out of memory."、ExitCode 还是 0，
            # 表面上像「自己关了」。宿主机有 3.9 GiB，放宽到 2 GiB。
            'Memory': 2 * 1024 * 1024 * 1024,
            'MemorySwap': 2 * 1024 * 1024 * 1024,
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
                    info = emby_info()
                    ready = True
                    server_version = str(info.get('Version', ''))[:32]
                    if isinstance(info.get('StartupWizardCompleted'), bool):
                        wizard = info['StartupWizardCompleted']
                except Error:
                    ready = False
        return {'version': installed_version(), 'configured': bool(self.config), 'running': running, 'ready': ready,
                'busy': self.busy, 'error': error, 'preview': self.dev, 'port': PORT,
                'directory': self.config.get('media_relative', '') if self.config else '',
                'configDirectory': self.config.get('config_relative', '') if self.config else '',
                # 状态卡片显示完整绝对路径：相对路径在「存储位置」多于一个时看不出在哪
                'media_abs': self.config.get('media', '') if self.config else '',
                'config_abs': self.config.get('config', '') if self.config else '',
                # 配置目录留在插件私有目录（不属于任何存储位置）时为真，前端据此加一句说明
                'config_private': bool(self.config) and not (
                    self.config.get('config_root') or self.config.get('config_relative')),
                'roots': self.locations(),
                'serverVersion': server_version, 'wizardCompleted': wizard,
                'imageVersion': '4.10.0.40',
                'healthcheckOff': bool(self.config and self.config.get('healthcheck_off'))}

    def _describe_selection(self, relative, config_relative='', media_root=None, config_root=None,
                            uid=None, gid=None):
        """校验一对「媒体目录 + 配置目录」并算出要写进配置的字段。

        返回 {'uid','gid','media_root','media_relative','media','media_device','media_inode',
        'config_root','config_relative','config','config_device','config_inode'}。
        初始化与换目录共用这一套规则：目录必须存在、不是符号链接、落在允许的存储
        位置内、属主为非 root 的 NAS 用户、路径里不能有逗号、两个目录不能互相包含。
        config_relative 留空表示配置留在插件私有目录（不属于任何存储位置）。
        """
        if not isinstance(relative, str) or not relative or len(relative) > 1024:
            raise Error('请选择媒体目录')
        media_index, media_value = self.locate(relative, media_root)
        media_dir = confined(self.roots[media_index], media_value)
        if ',' in str(media_dir):
            raise Error('Docker 挂载目录不能包含逗号')
        media_stat = media_dir.stat()
        if not media_stat.st_uid or not media_stat.st_gid:
            raise Error('所选目录须由非 root 的 NAS 用户拥有')
        uid, gid = media_stat.st_uid, media_stat.st_gid

        # Emby 的配置目录。默认放在插件私有目录里（外部看不到，最干净）；
        # 也可以指定存储中的一个目录，方便备份、迁移或直接在文件管理器里查看。
        # 指定时不对该目录做 chown —— 与 qB 插件一致，绝不改动用户已有文件的所有权。
        if config_relative:
            if not isinstance(config_relative, str) or len(config_relative) > 1024:
                raise Error('配置目录无效')
            config_index, config_value = self.locate(config_relative, config_root)
            cfgdir = confined(self.roots[config_index], config_value)
            if ',' in str(cfgdir):
                raise Error('Docker 挂载目录不能包含逗号')
            if cfgdir == media_dir or cfgdir in media_dir.parents or media_dir in cfgdir.parents:
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
            cfg_stat = cfgdir.stat()
            chosen_config_root = ''
        return {'uid': uid, 'gid': gid,
                # *_root 记下这个选择落在哪个存储位置，启动校验时据此回到同一个根
                'media_root': str(self.roots[media_index]), 'media_relative': media_value,
                'media': str(media_dir),
                'media_device': media_stat.st_dev, 'media_inode': media_stat.st_ino,
                'config_root': chosen_config_root,
                'config': str(cfgdir), 'config_relative': config_value if config_relative else '',
                'config_device': cfg_stat.st_dev, 'config_inode': cfg_stat.st_ino}

    def setup(self, relative, config_relative='', media_root=None, config_root=None):
        """初始化：目录选择可以是绝对路径（前端提交）或根内相对路径（旧语义）。"""
        if self.config:
            raise Error('已完成初始化；现有媒体目录和 Emby 配置不会被覆盖')
        chosen = self._describe_selection(relative, config_relative, media_root, config_root)
        self._call('GET', '/info')
        if self.inspect() is not None:
            raise Error('同名容器已存在，拒绝覆盖')
        self.config = {'owner': secrets.token_hex(24), **chosen, 'enabled': True}
        atomic_json(self.cfgfile, self.config)

    def reconfigure(self, relative, config_relative='', media_root=None, config_root=None):
        """换目录但保留数据：校验新目录 → 备份配置 → 写新路径 → 按新挂载重建容器。

        挂载只在创建容器时决定，所以必须显式停掉旧容器再重建；只 restart 的话
        容器还会继续拿着旧宿主目录。重建失败就恢复备份的配置，并尽量把原来的
        容器建回来，绝不留下「配置指向新目录、容器还挂着旧目录」的半截状态。
        配置目录里的数据不迁移：新位置已有配置就用它，没有就以全新状态启动。
        """
        if not self.config:
            raise Error('请先初始化')
        old = dict(self.config)
        chosen = self._describe_selection(relative, config_relative, media_root, config_root,
                                          old.get('uid'), old.get('gid'))
        # 新旧挂载完全一致时不做无谓的容器重建，也不留备份文件
        if (chosen['media'], chosen['config']) == (old.get('media'), old.get('config')):
            raise Error('媒体目录和配置目录都没有变化，无需修改')
        self._call('GET', '/info')
        backup = unique_backup(self.cfgfile)
        if self.cfgfile.exists():
            shutil.copy2(self.cfgfile, backup)  # 备份保留原文件，回滚时再换回来
        self.config = {**old, **chosen}
        atomic_json(self.cfgfile, self.config)
        try:
            self._recreate_container()
        except Exception as exc:
            restored = self._fallback_to(old, backup)
            detail = restored or '（已恢复原配置，但旧容器未能恢复，请点「启动服务」重试）'
            print('emby: 换目录失败，%s：%s' % (detail, exc), flush=True)
            raise Error('新目录未能生效，已回滚到原目录：' + str(exc) + '；' + detail) from exc

    def _recreate_container(self):
        """停掉并删掉旧容器，再按当前配置重新创建并启动。

        挂载、端口这类参数只在创建时生效，换目录就必须重建；旧容器还活着的话
        Docker 会因为同名而拒绝创建。
        """
        item = self.owned()
        if item is not None:
            if item.get('State', {}).get('Running'):
                self._call('POST', '/containers/' + NAME + '/stop?t=30', timeout=90)
            self._call('DELETE', '/containers/' + NAME, ok=(200, 204, 404))
        self.pull()
        self.create_container()

    def _fallback_to(self, previous, backup):
        """回滚到换目录之前的配置；返回一句说明（回滚本身失败时说明里带上原因）。"""
        try:
            self.config = dict(previous)
            if backup.exists():
                shutil.copy2(backup, self.cfgfile)   # 文件也退回备份，两者保持一致
            else:
                atomic_json(self.cfgfile, self.config)
        except OSError as exc:
            return '原配置未能写回（%s），请手工用 %s 恢复' % (exc, backup)
        try:
            self._recreate_container()
        except Exception as exc:
            return '原配置已恢复，但原容器未重建（%s）' % exc
        return '已恢复原配置并重建原容器'

    def reset(self, confirm=False):
        """重新初始化：停掉并移除本插件容器、清空插件配置。

        只动插件自己的东西：容器与 settings.json（先备份成 .bak-<时间戳>）。
        **用户数据一个字节都不碰** —— 媒体目录与配置目录里的文件全部原样保留。
        """
        if not confirm:
            raise Error('请确认要重新初始化')
        self._remove_container()
        # 路由器端口映射之类的副作用：本插件不碰路由器（Emby 的端口靠 Docker
        # 自己的端口发布，容器删掉就随之消失），所以没有需要清理的外部状态；
        # transmission 那边的 remove_port_forward() 不适用于本插件。
        if self.cfgfile.exists():
            backup = unique_backup(self.cfgfile)
            os.replace(self.cfgfile, backup)
            print('emby: 重新初始化，原配置已备份到 %s' % backup, flush=True)
        self.config = None
        self.error = ''

    def _remove_container(self):
        """删掉本插件容器；容器本来就不存在或预览模式下不报错。"""
        if self.dev:
            return
        try:
            self.remove()
        except Error as exc:
            print('emby: 移除容器失败（%s），继续重新初始化' % exc, flush=True)

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
        print('emby: %s 所在的 %s（%s）每次挂载都会换设备号/inode 号，已更新记录'
              % (label, fstype, point), flush=True)
        return True

    def _config_root(self, absolute):
        """启动校验该用哪个根（存储位置）。

        配置里记下的 *_root 优先（即使 LOCAL_ROOTS 变了也回到同一个根）；
        旧配置没有这个字段，就按绝对路径前缀反查；再不行退化为第一个根。
        """
        key = 'media_root' if self.config.get('media') == absolute else 'config_root'
        recorded = self.config.get(key)
        if not isinstance(recorded, str) or not recorded:
            # 旧配置没有 *_root，按绝对路径前缀反查它属于哪个位置
            index = root_index_of(self.roots, absolute) if absolute else -1
            recorded = str(self.roots[index]) if index >= 0 else ''
        return Path(recorded) if recorded else self.root

    def check_directories(self):
        """启动前核对媒体目录（以及存储里的配置目录）的身份。"""
        targets = [('media', 'media_relative', 'media_device', 'media_inode', '媒体目录')]
        # 只有用户指定的配置目录（在存储里）需要校验身份；私有目录由插件自己管。
        # 旧配置没有 config_root，仍看 config_relative。
        if self.config.get('config_root') or self.config.get('config_relative'):
            targets.append(('config', 'config_relative', 'config_device', 'config_inode', '配置目录'))
        refreshed = False
        for path_key, relative_key, dev_key, ino_key, label in targets:
            folder = confined(self._config_root(self.config[path_key]), self.config[relative_key])
            try:
                stat = folder.stat()
            except OSError as exc:
                raise Error(label + '不可用（%s），拒绝启动；请先检查存储挂载' % exc.strerror) from exc
            if str(folder) != self.config[path_key]:
                raise Error(label + '身份已变化，拒绝启动；请先检查存储挂载')
            refreshed = self._identity_ok(folder, stat, dev_key, ino_key, label) or refreshed
        if refreshed:
            atomic_json(self.cfgfile, self.config)

    @staticmethod
    def inherited_healthcheck(item):
        """容器是否带着（镜像继承来的）健康检查。"""
        tests = ((item.get('Config') or {}).get('Healthcheck') or {}).get('Test') or []
        return bool(tests) and str(tests[0]).upper() != 'NONE'

    def drop_inherited_healthcheck(self, item):
        """旧容器带着健康检查时把它重建掉。

        健康检查只能在创建容器时决定：Docker 20.10 的
        `POST /containers/<id>/update` 虽然接受 Healthcheck 字段并返回 200，
        但实际不生效。所以停容器 → 删除 → 交给 start() 按新配置重建。
        /config 与 /mnt/media 都是 bind 挂载，配置和媒体库不受影响。
        """
        if not self.inherited_healthcheck(item):
            return False
        if item.get('State', {}).get('Running'):
            self._call('POST', '/containers/' + NAME + '/stop?t=30', timeout=90)
        self._call('DELETE', '/containers/' + NAME, ok=(200, 204, 404))
        return True

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
            # 去掉镜像自带的健康检查只能靠重建；升级插件后服务重启时自动完成。
            self.drop_inherited_healthcheck(item)
            item = None
            self.create_container()
        elif item is None:
            self.pull()
            self.create_container()
        elif not item.get('State', {}).get('Running'):
            self._call('POST', '/containers/' + NAME + '/start')
        self.config['healthcheck_off'] = True
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

    def relocate_config(self, relative, config_root=None):
        """把配置目录迁到存储中的新位置（已初始化的实例换位置只能走这里）。

        顺序很关键：先停容器（Emby 的数据库不能带着运行状态直接拷），复制后
        核对文件数，一致才删旧目录。任何一步不满足都抛错并保留原配置，
        不会出现「两处各留一半」的状态。调用方负责在成功后重建容器。
        """
        if not self.config:
            raise Error('请先初始化')
        if not isinstance(relative, str) or not relative or len(relative) > 1024:
            raise Error('请选择新的配置目录')
        config_index, config_value = self.locate(relative, config_root)
        target = confined(self.roots[config_index], config_value)
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
        self.config['config_relative'] = config_value
        self.config['config_root'] = str(self.roots[config_index])
        self.config['config_device'] = stat.st_dev
        self.config['config_inode'] = stat.st_ino
        atomic_json(self.cfgfile, self.config)
        # 挂载变了，旧容器必须删掉，交给 start() 按新配置重建
        self.remove()
        if current.is_dir():
            shutil.rmtree(current, ignore_errors=True)

    def launch(self, action, data):
        if action not in ('setup', 'start', 'stop', 'relocate', 'reconfigure', 'reset'):
            raise Error('未知服务操作')
        if action == 'reset' and not data.get('confirm'):
            # 破坏性操作必须先确认；这条纯输入校验放在预览模式判断之前，
            # 这样预览页也能明确提示「缺确认」，而不是含糊的预览模式提示。
            raise Error('请确认要重新初始化')
        if self.dev:
            raise Error('预览模式不会启动服务或修改 NAS')
        if self.busy or not self.lock.acquire(False):
            # 忙时明确拒绝（而不是排队），前端会看到这句话
            raise Error('当前有操作正在进行，请稍后再试')
        self.busy, self.error = True, ''

        def work():
            try:
                if action == 'setup':
                    self.setup(data.get('path', ''), data.get('configPath', ''),
                               data.get('pathRoot'), data.get('configRoot'))
                    self.start()
                elif action == 'relocate':
                    self.relocate_config(data.get('configPath', ''))
                    self.start()
                elif action == 'reconfigure':
                    self.reconfigure(data.get('path', ''), data.get('configPath', ''),
                                     data.get('pathRoot'), data.get('configRoot'))
                elif action == 'reset':
                    self.reset(True)
                elif action == 'start':
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
