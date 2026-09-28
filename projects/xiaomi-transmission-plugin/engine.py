"""Bounded Transmission container lifecycle (Docker Engine API)."""
from __future__ import annotations

import base64
import datetime
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

import upnp

VERSION = '0.2.0'
# LinuxServer Transmission 4.1.3-r0-ls362，多架构 manifest digest（含 arm64）。
IMAGE = 'lscr.io/linuxserver/transmission@sha256:1a12fef3c89eca48b7be9e7d36b17b4eb4e1bcf5e1ee7fbf372e3a38b562939d'
IMAGE_VERSION = '4.1.3 / LSIO ls362'
NAME = 'xiaomi-plugin-transmission'
LABEL = 'io.xiaomi-plugin.tr.owner'
PORT = 9091
BT_PORT = 51413
DOCKER_SOCKET = os.environ.get('DOCKER_SOCKET', '/var/run/docker.sock')
PROC_NET = Path(os.environ.get('PROC_NET', '/proc/net'))

# 必须真正发布到宿主机的端口：WebUI 一个 TCP，BT 入站要 TCP + UDP。
PUBLISHED_PORTS = (('tcp', PORT), ('tcp', BT_PORT), ('udp', BT_PORT))
# 路由器映射失败后隔多久再试（路由器重启、UPnP 刚打开这类情况能自愈）
FORWARD_RETRY_SECONDS = 1800
# 定时「开启/关闭全部任务」：off=不做，'0'..'23'=每天到那个钟点执行一次（共 25 个选项）。
# 两个定时器各自独立，想错开就选不同钟点（例如 3 点开、23 点关）。
SCHEDULE_HOURS = tuple(str(hour) for hour in range(24))
SCHEDULE_VALUES = ('off',) + SCHEDULE_HOURS
SCHEDULE_KINDS = (('start', 'torrent-start'), ('stop', 'torrent-stop'))


def next_hour_epoch(hour, now=None):
    """下一个 HH:00 的时间戳：今天还没到就是今天，已经过了（或正好到点）就是明天。"""
    moment = datetime.datetime.fromtimestamp(now if now is not None else time.time())
    target = moment.replace(hour=int(hour), minute=0, second=0, microsecond=0)
    if target <= moment:
        target += datetime.timedelta(days=1)
    return int(target.timestamp())
# 容器资源上限。内存原来是 512 MiB，实测做种多的时候 transmission-daemon 的 RSS
# 会涨到 440 MiB 以上，撞上限就被内核 OOM 杀掉、由 s6 反复拉起（dmesg 里 5 分钟内
# 9 次 "Memory cgroup out of memory: Killed process ... (transmission-da)"），
# 所以放宽到 2 GiB。MemorySwap 与 Memory 相同＝不额外给 swap。
MEMORY_LIMIT = 2048 * 1024 * 1024
CPU_LIMIT = 1500000000


class Error(RuntimeError):
    pass


def listening_ports():
    """宿主机当前真正在监听的 {(协议, 端口)}。

    直接读 /proc/net，而不是问 Docker：`inspect` 里的 PortBindings /
    NetworkSettings.Ports 只是「声明过要发布」，docker-proxy 是用户态进程，
    它异常退出后宿主上其实没人监听，而 inspect 依然显示端口已绑定。
    BT 的 TCP 入站端口就是这样悄悄消失的（UDP 和 WebUI 还正常，很难发现）。
    """
    found = set()
    for name in ('tcp', 'tcp6', 'udp', 'udp6'):
        try:
            lines = (PROC_NET / name).read_text().splitlines()[1:]
        except OSError:
            continue
        proto = name.rstrip('6')
        for line in lines:
            fields = line.split()
            if len(fields) < 4:
                continue
            if proto == 'tcp' and fields[3] != '0A':        # 0A = LISTEN
                continue
            try:
                port = int(fields[1].split(':')[1], 16)
            except (IndexError, ValueError):
                continue
            found.add((proto, port))
    return found


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


def atomic_json(path, value):
    tmp = path.with_suffix('.tmp')
    with tmp.open('w', encoding='utf-8') as stream:
        os.chmod(tmp, 0o600)
        json.dump(value, stream)
    tmp.replace(path)


def settings_document():
    """首次初始化（或键缺失）时补进 <配置目录>/settings.json 的非鉴权配置。

    WebUI 账号密码走 LinuxServer 的 USER/PASS 环境变量，不要写进
    settings.json，否则 s6 可能无法干净停掉 transmission-daemon。
    这些值只在键不存在时补进已有配置，不会覆盖用户自己改过的值。
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


# 镜像每次启动都会重写这几个键（s6 的 init-transmission-config 用 jq 写回）：账号、
# 鉴权开关、白名单、BT 端口、umask 都由容器环境变量决定，直接改 settings.json 不生效。
IMAGE_MANAGED_KEYS = frozenset({
    'rpc-authentication-required', 'rpc-username', 'rpc-password',
    'rpc-whitelist', 'rpc-whitelist-enabled',
    'rpc-host-whitelist', 'rpc-host-whitelist-enabled',
    'peer-port', 'peer-port-random-on-start', 'umask',
})
# 和容器端口映射绑死（9091/tcp 固定、控制台也按 9091 转发）：值不对 WebUI 与插件
# 控制台都打不开，所以每次启动都纠正这三个键。其余键一律不覆盖。
REQUIRED_SETTINGS = {'rpc-enabled': True, 'rpc-port': PORT, 'rpc-bind-address': '0.0.0.0'}
# 旧版原生插件把 daemon 配置写在 <配置目录>/transmission-daemon/settings.json；
# Docker 版镜像写死 `transmission-daemon -g /config`，只读 <配置目录>/settings.json。
LEGACY_SETTINGS_FOLDER = 'transmission-daemon'
LEGACY_BACKUP_SUFFIX = '.legacy'
# 迁移旧配置时不动这些键：路径、端口、绑定、脚本文件名都和容器挂载/端口映射绑定，
# 照搬过来会让下载目录或 WebUI 直接失效。
MIGRATION_SKIP_KEYS = IMAGE_MANAGED_KEYS | frozenset({
    'download-dir', 'incomplete-dir', 'incomplete-dir-enabled',
    'watch-dir', 'watch-dir-enabled',
    'rpc-enabled', 'rpc-port', 'rpc-bind-address', 'rpc-url', 'rpc-socket-mode',
    'peer-port-random-high', 'peer-port-random-low',
    'bind-address-ipv4', 'bind-address-ipv6', 'pidfile', 'proxy_url',
    'script-torrent-done-filename', 'script-torrent-added-filename',
    'script-torrent-done-seeding-filename',
})


def settings_path(config_folder):
    """daemon 真正读写的配置文件：<配置目录>/settings.json（容器内 /config/settings.json）。

    镜像是 `transmission-daemon -g /config` 启动的（写死在镜像的 s6 run 脚本里，
    没有环境变量可改），写成 `<配置目录>/transmission-daemon/settings.json`（原生版
    的路径）不会被读取，daemon 会回落到镜像默认值——其中 `rpc-bind-address` 默认是
    `[::]`，在没有 IPv6 的容器里绑不上 9091，Web 界面永远起不来。
    """
    return config_folder / 'settings.json'


def legacy_settings_path(config_folder):
    """旧版原生插件留下的配置路径；Docker 版的 daemon 不会读它。"""
    return config_folder / LEGACY_SETTINGS_FOLDER / 'settings.json'


def read_settings(path):
    """读 settings.json；文件不存在或内容不是 JSON 对象时返回空字典。"""
    try:
        value = json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def write_settings_file(path, settings, uid, gid):
    """原子写入配置文件（0600，属主为所选目录的 NAS 用户）。"""
    tmp = path.with_suffix('.tmp')
    payload = json.dumps(settings, ensure_ascii=False, indent=2) + '\n'
    with tmp.open('w', encoding='utf-8') as stream:
        os.chmod(tmp, 0o600)
        stream.write(payload)
    os.chown(tmp, uid, gid)
    tmp.replace(path)
    return path


def merge_settings(config_folder, uid, gid, document=None):
    """把默认配置合并进现有 settings.json：只补缺失键 + 纠正 REQUIRED_SETTINGS。

    返回 (是否改动, 文件路径)。用户已经写下的值一律保留——旧实现只要发现任一托管键
    和默认值不同，就把整个文件重写成那 20 个默认键，用户调过的缓存、连接数、限速、
    DHT/PEX、队列等全部丢失，表现就是「每次启动配置都恢复成默认」。
    """
    document = settings_document() if document is None else document
    path = settings_path(config_folder)
    merged = read_settings(path)
    try:
        unreadable = not merged and path.stat().st_size > 0
    except OSError:
        unreadable = False
    if unreadable:
        # 文件存在但解析不出 JSON 对象：先留一份，再写默认值，避免内容被直接抹掉
        path.replace(path.with_name(path.name + '.invalid'))
        merged = {}
    changed = False
    for key, value in document.items():
        if key not in merged:
            merged[key] = value
            changed = True
    for key, value in REQUIRED_SETTINGS.items():
        if merged.get(key) != value:
            merged[key] = value
            changed = True
    if changed:
        write_settings_file(path, merged, uid, gid)
    return changed, path


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
            'Memory': MEMORY_LIMIT,
            'MemorySwap': MEMORY_LIMIT,
            'NanoCpus': CPU_LIMIT,
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
        # 路由器端口映射（UPnP/NAT-PMP）的结果，见 ensure_port_forward。
        # 必须落盘：服务停止时 systemd 的 ExecStopPost 是**另一个进程**，
        # 内存里那点状态它是看不到的，那样就删不掉路由器上留下的映射。
        self.forwardfile = self.data / 'forward.json'
        self.forward_state = self.load_forward()
        self.forward_lock = threading.Lock()
        # 入站端口的自检结果（见 ensure_published_ports）
        self.ports_state = {'missing': [], 'repaired': False, 'checkedAt': 0}
        # 上一次「因为缺端口而重启容器」的时刻，运行期巡检靠它限流
        self.last_port_repair = 0.0
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

    def missing_published_ports(self):
        """该发布、但宿主上其实没在监听的端口，形如 ['51413/tcp']。"""
        listening = listening_ports()
        return ['%d/%s' % (port, proto) for proto, port in PUBLISHED_PORTS
                if (proto, port) not in listening]

    def wait_published_ports(self, timeout=20):
        deadline = time.time() + timeout
        missing = self.missing_published_ports()
        while missing and time.time() < deadline:
            time.sleep(1)
            missing = self.missing_published_ports()
        return missing

    def ensure_published_ports(self):
        """入站端口自检：宿主机上没人监听就重启容器，把端口绑定重新下发。

        只在真的没监听时动手——Docker 的元数据区分不了「声明过」和「真的绑上了」，
        拿它当依据会漏掉 docker-proxy 掉线这种情况。重启比重建容器便宜得多
        （不用重拉镜像，也不动 /config），实测能把丢掉的 docker-proxy 补回来。
        """
        missing = self.missing_published_ports()
        repaired = False
        if missing and not self.dev:
            print('transmission: 入站端口未监听 %s，重启容器修复' % '、'.join(missing), flush=True)
            self._call('POST', '/containers/' + NAME + '/restart?t=15', timeout=120)
            repaired = True
            missing = self.wait_published_ports()
            if missing:
                print('transmission: 重启后仍未监听 %s' % '、'.join(missing), flush=True)
        self.ports_state = {'missing': missing, 'repaired': repaired, 'checkedAt': int(time.time())}
        return missing

    def load_forward(self):
        """读回上次的映射结果（新进程也要知道该删哪条映射）。"""
        empty = {'ok': False, 'method': '', 'detail': '尚未尝试', 'at': 0, 'removed': False}
        try:
            data = json.loads(self.forwardfile.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return empty
        return data if isinstance(data, dict) else empty

    def save_forward(self):
        try:
            atomic_json(self.forwardfile, self.forward_state)
        except OSError:
            pass

    def ensure_port_forward(self, force=False):
        """启动时（或用户点「映射端口」时）请路由器把 BT 端口转给本机。

        容器里的 Transmission 上报的 internal client 是 Docker 网桥地址，路由器
        路由不到，所以必须由宿主机发起。纯尽力而为：失败只记录原因，绝不阻断启动；
        已经在映射中时直接返回上次结果，避免并发打路由器。
        """
        if self.dev:
            return self.forward_state
        if not self.forward_lock.acquire(blocking=False):
            return self.forward_state
        try:
            address = upnp.lan_address()
            if not address:
                self.forward_state = {'ok': False, 'method': '', 'at': int(time.time()),
                                      'detail': '找不到局域网地址，跳过路由器映射'}
                self.save_forward()
                return self.forward_state
            state = upnp.forward_ports(BT_PORT, BT_PORT, address,
                                       'Xiaomi NAS Transmission')
            self.forward_state = state
            self.save_forward()
            print('transmission: 路由器端口映射（%s）%s'
                  % (state.get('method') or 'UPnP/NAT-PMP', state.get('detail') or ''), flush=True)
            return self.forward_state
        finally:
            self.forward_lock.release()

    def remove_port_forward(self):
        """停止服务时撤掉路由器上的映射（尽力而为）。

        留着没意义：容器都停了，那个端口没人应答。而且 UPnP 用的是永久映射，
        不删的话卸载插件后它会一直留在路由器上。
        """
        state = self.forward_state or {}
        if not state.get('at'):
            return {'ok': False, 'detail': '没有建过映射，跳过移除'}
        if self.dev:
            return {'ok': False, 'detail': '预览模式不碰路由器'}
        if not self.forward_lock.acquire(blocking=False):
            return {'ok': False, 'detail': '映射操作正在进行，跳过移除'}
        try:
            result = upnp.remove_forward(
                BT_PORT, BT_PORT,
                method=state.get('method', ''),
                gateway_address=state.get('gateway', ''),
                control=state.get('control', ''),
                service_type=state.get('service', ''),
                protocols=tuple(state.get('protocols') or ('TCP', 'UDP')))
            removed = bool(result.get('ok'))
            # forward_state 描述的是"路由器现在还在转发吗"，移除之后必然是 False；
            # 移除本身成没成功由返回值单独告诉调用方。
            self.forward_state = {'ok': False, 'method': '', 'removed': removed,
                                  'detail': result.get('detail', ''), 'at': int(time.time())}
            self.save_forward()
            print('transmission: 路由器端口映射清理：%s' % self.forward_state['detail'], flush=True)
            return {'ok': removed, 'removed': removed,
                    'detail': result.get('detail', '')}
        finally:
            self.forward_lock.release()

    def forward_snapshot(self):
        """页面用的映射状态；顺带带上本次要映射的端口，方便提示文案。"""
        state = dict(self.forward_state or {})
        state.setdefault('externalPort', BT_PORT)
        state.setdefault('protocols', ['TCP', 'UDP'])
        state.setdefault('at', 0)
        state.setdefault('lease', 0)
        state.setdefault('removed', False)
        state.setdefault('detail', '尚未尝试')
        state.setdefault('ok', False)
        state.setdefault('method', '')
        return state

    def forward_due(self, now=None):
        """现在该不该再碰一次路由器。

        - UPnP 用的是永久映射（lease=0），建成之后不用管；
        - NAT-PMP 的映射有租期，到期前一半就得重建，否则 BT 入站会静默失效；
        - 失败的话隔 30 分钟重试一次（路由器重启、UPnP 刚被打开这类情况能自愈）；
        - 刚被移除过就不动（那是停止服务时主动删的，别又加回去）。
        """
        state = self.forward_state or {}
        if state.get('removed'):
            return False
        stamp = int(state.get('at') or 0)
        if not stamp:
            return False
        elapsed = int(now if now is not None else time.time()) - stamp
        if state.get('ok'):
            lease = int(state.get('lease') or 0)
            return bool(lease) and elapsed >= max(60, lease // 2)
        return elapsed >= FORWARD_RETRY_SECONDS

    def keep_forward_alive(self):
        """给定时器调的：该续期/重试就再跑一次 ensure_port_forward。"""
        if self.dev or not self.forward_due():
            return False
        if not (self.config or {}).get('enabled'):
            # 服务是用户自己停的，别再往路由器上加映射
            return False
        self.ensure_port_forward(True)
        return True

    def keep_published_ports_alive(self, repair_interval=600):
        """给定时器调的：容器还在跑，docker-proxy 却掉线了，把端口补回来。

        `ensure_published_ports()` 只在「启动那一刻」自检。实测 proxy 会在容器继续
        运行时悄悄死掉（进程变僵尸，dmesg 里既没有 OOM 也没有 segfault），而 dockerd
        带 `iptables: false` 启动、没有 DNAT 兜底，端口就一直不通：局域网和公网同时
        连不上，页面上那条「入站端未发布」要等到用户下次手动启动才会消失。
        两次修复至少隔 repair_interval 秒，免得反复重启打断正在下的任务。
        """
        if self.dev or not (self.config or {}).get('enabled'):
            return ''            # 服务是用户自己停的，不能被定时器拉起来
        if not self.lock.acquire(False):
            return ''            # 正在启停，这一轮先不动
        try:
            missing = self.missing_published_ports()
            if not missing:
                return ''
            now = time.time()
            if now - self.last_port_repair < repair_interval:
                return '、'.join(missing)
            self.last_port_repair = now
            self.ensure_published_ports()
            return '、'.join(self.ports_state.get('missing') or [])
        finally:
            self.lock.release()

    def ports_snapshot(self, running):
        """页面用的端口状态；容器没跑时不报缺失（那是用户自己停的）。"""
        missing = self.missing_published_ports() if (running and not self.dev) else []
        return {
            'published': ['%d/%s' % (port, proto) for proto, port in PUBLISHED_PORTS],
            'missing': missing,
            'repaired': bool(self.ports_state.get('repaired')),
            'checkedAt': int(self.ports_state.get('checkedAt') or 0),
        }

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
            # daemon 真正读写的配置文件（页面用它告诉用户改哪里）
            'settingsFile': str(settings_path(Path(self.config['config']))) if self.config else '',
            'legacyFile': str(legacy_settings_path(Path(self.config['config']))) if self.config else '',
            'legacySettings': bool(self.config) and legacy_settings_path(
                Path(self.config['config'])).is_file(),
            # BT 端口是否对公网开放：只有点了「测试端口」才有值（port-test 会访问外部检测服务）
            'port': {'peerPort': BT_PORT, **self.port_state},
            # 入站端口是否真的发布到了宿主机（docker-proxy 掉线会让它悄悄消失）
            'ports': self.ports_snapshot(running),
            # 路由器上的端口映射（UPnP/NAT-PMP）
            'forward': self.forward_snapshot(),
            # 定时开启/关闭全部任务
            'schedule': self.schedule_snapshot(),
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
        merge_settings(config_folder, uid, gid)
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
        refreshed = []
        for path_key, rel_key, dev_key, ino_key, label in pairs:
            folder = confined(self.root, self.config[rel_key])
            try:
                stat = folder.stat()
            except OSError as exc:
                raise Error(label + '不可用（%s），拒绝启动；请先检查存储挂载' % exc.strerror) from exc
            if str(folder) != self.config[path_key]:
                raise Error(label + '身份已变化，拒绝启动；请先检查存储挂载')
            if (stat.st_dev, stat.st_ino) == (self.config[dev_key], self.config[ino_key]):
                continue
            # 设备号/inode 号对不上：先看这块盘是不是"每次挂载都会换号"的文件系统。
            # 厂商的存储池 /nas/pool0 就是 FUSE（fuse.cfs），每次挂载都换一套匿名
            # 设备号并重新合成 inode 号，所以重启后必然对不上——只要目录确实落在某个
            # 挂载点下面（没挂盘时会落在 /nas 的空目录上），就更新记录继续用。
            point, fstype = covering_mount(str(folder))
            if not point or not volatile_identity(fstype):
                raise Error(label + '身份已变化，拒绝启动；请先检查存储挂载')
            self.config[dev_key], self.config[ino_key] = stat.st_dev, stat.st_ino
            refreshed.append((label, fstype, point))
        if refreshed:
            atomic_json(self.cfgfile, self.config)
            for label, fstype, point in refreshed:
                print('transmission: %s 所在的 %s（%s）每次挂载都会换设备号/inode 号，已更新记录'
                      % (label, fstype, point), flush=True)

    def ensure_settings(self):
        """补上缺失的配置键，返回配置文件是否被改动。

        绝不覆盖用户已经写下的值：改过缓存、连接数、限速、DHT/PEX 等键的配置
        在插件重启后保持原样；只有键缺失（或 rpc-enabled / rpc-port /
        rpc-bind-address 与容器映射不一致）时才会动文件。
        """
        changed, _ = merge_settings(Path(self.config['config']),
                                    self.config['uid'], self.config['gid'])
        return changed

    def adopt_legacy_settings(self):
        """一次性采用旧版原生插件遗留的可调配置，返回是否搬了东西。

        旧版把 daemon 配置写在 <配置目录>/transmission-daemon/settings.json，而
        Docker 版 daemon 只读 <配置目录>/settings.json（镜像写死 `-g /config`），
        所以那份文件里调过的参数一直不生效，看起来也像「被默认值覆盖」。这里把其中
        与容器挂载/端口映射无关的可调项并进真正生效的文件，并把旧文件改名成
        `settings.json.legacy` 留在原处，避免下次又改错地方。只做一次。
        """
        config_folder = Path(self.config['config'])
        if self.config.get('settingsAdopted'):
            return False
        self.config['settingsAdopted'] = True
        legacy = legacy_settings_path(config_folder)
        if not legacy.is_file():
            atomic_json(self.cfgfile, self.config)
            return False
        adopted = {key: value for key, value in read_settings(legacy).items()
                   if key not in MIGRATION_SKIP_KEYS}
        if adopted:
            merged = read_settings(settings_path(config_folder))
            merged.update(adopted)
            write_settings_file(settings_path(config_folder), merged,
                                self.config['uid'], self.config['gid'])
        try:
            backup = legacy.with_name(legacy.name + LEGACY_BACKUP_SUFFIX)
            if backup.exists():
                backup.unlink()
            legacy.replace(backup)
        except OSError:
            pass
        atomic_json(self.cfgfile, self.config)
        return bool(adopted)

    def ensure_webui(self):
        """确保默认控制台已铺到 <配置目录>/webui；用户自己替换过的文件不动。"""
        return install_webui(Path(self.config['config']), self.config['uid'], self.config['gid'])

    def webui_env_stale(self, item):
        """容器是否还指向旧的控制台路径（旧版本装在 webui/<主题>/ 下）。"""
        env = item.get('Config', {}).get('Env') or []
        return ('TRANSMISSION_WEB_HOME=' + WEBUI_HOME) not in env

    @staticmethod
    def resources_stale(item):
        """容器的内存/CPU 上限和现在要求的不一致——只能在创建时设，得重建。

        512 MiB 的时代 transmission-daemon 会撞上限被 OOM 杀掉（见 MEMORY_LIMIT
        的说明），旧容器必须重建才会拿到新的上限。
        """
        host = item.get('HostConfig') or {}
        return (host.get('Memory') != MEMORY_LIMIT
                or host.get('MemorySwap') != MEMORY_LIMIT
                or host.get('NanoCpus') != CPU_LIMIT)

    def test_port(self):
        """调 Transmission 的 port-test，判断 BT 端口是否对公网开放。

        注意：**容器里** Transmission 自带的 UPnP 映射不生效（它上报的 internal
        client 是 Docker 网桥地址，路由器路由不到）。端口转发由插件在宿主机侧做，
        见 ensure_port_forward()——路由器不认 UPnP/NAT-PMP 时才需要手动转发。
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

    def schedule_snapshot(self):
        """页面用的定时设置：{'start': {'value','next'}, 'stop': {...}}。"""
        state = (self.config or {}).get('schedule') or {}
        out = {}
        for kind, _method in SCHEDULE_KINDS:
            entry = state.get(kind) or {}
            value = entry.get('value') if entry.get('value') in SCHEDULE_VALUES else 'off'
            out[kind] = {'value': value, 'next': int(entry.get('next') or 0) if value != 'off' else 0}
        return out

    def set_schedule(self, kind, value):
        """设置定时开启/关闭全部任务；value 只接受 off 或 '0'..'23'（钟点）。

        下一次执行时间按**钟点**算：今天该点还没到就是今天，已经过了就是明天。
        设置本身不会立刻执行——想马上生效就进控制台手动开始。
        """
        if not self.config:
            raise Error('请先初始化')
        if kind not in [name for name, _ in SCHEDULE_KINDS]:
            raise Error('未知的定时类型')
        if value not in SCHEDULE_VALUES:
            raise Error('不支持的定时钟点')
        entry = self.config.setdefault('schedule', {}).setdefault(kind, {})
        entry['value'] = value
        entry['next'] = next_hour_epoch(value) if value != 'off' else 0
        atomic_json(self.cfgfile, self.config)
        return self.schedule_snapshot()

    def run_schedule_due(self, now=None):
        """到点就执行「开启全部任务」/「关闭全部任务」，返回这次做了哪些动作。

        跑在插件的定时线程里，尽力而为：失败（例如服务没在跑）只记录，并把下一次
        挪到一个间隔之后，避免每分钟重试。
        """
        if not self.config:
            return []
        moment = int(now if now is not None else time.time())
        state = self.config.get('schedule') or {}
        username, password = (self.config.get('username', ''), self.saved_password())
        done = []
        for kind, method in SCHEDULE_KINDS:
            entry = state.get(kind) or {}
            value = entry.get('value')
            due = int(entry.get('next') or 0)
            if value not in SCHEDULE_HOURS or not due or moment < due:
                continue
            entry['next'] = next_hour_epoch(value, moment)
            if not username or not password:
                print('transmission: 定时%s全部任务跳过（缺少 WebUI 账号）' % ('开启' if kind == 'start' else '关闭'), flush=True)
                continue
            try:
                # 不带 ids = 全部任务
                tr_call(method, username, password)
                done.append(kind)
                print('transmission: 定时%s全部任务' % ('开启' if kind == 'start' else '关闭'), flush=True)
            except Error as exc:
                print('transmission: 定时%s全部任务失败：%s' % ('开启' if kind == 'start' else '关闭', exc), flush=True)
        if done or any((state.get(kind) or {}).get('next') for kind, _ in SCHEDULE_KINDS):
            atomic_json(self.cfgfile, self.config)
        return done

    def start(self):
        if not self.config:
            raise Error('请先初始化')
        self.check_directories()
        adopted = self.adopt_legacy_settings()
        repaired = self.ensure_settings() or adopted
        self.ensure_webui()
        item = self.owned()
        if item and (self.webui_env_stale(item) or self.resources_stale(item)):
            # 控制台路径、或内存/CPU 上限变了：这两样都只在建容器时生效，必须重建
            if item.get('State', {}).get('Running'):
                self._call('POST', '/containers/' + NAME + '/stop?t=15', timeout=90)
            self._call('DELETE', '/containers/' + NAME + '?force=1&v=1', timeout=60)
            item = None
        if item:
            if not item.get('State', {}).get('Running'):
                self._call('POST', '/containers/' + NAME + '/start')
            elif repaired:
                # daemon 只在启动时读一次 settings.json：刚补过键（或刚搬完旧版的
                # 配置）就得重启，否则它用的还是旧值（比如 rpc-bind-address=[::]
                # 在无 IPv6 的容器里绑不上 9091，Web 界面永远起不来）。
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
        # 先修端口再等就绪：docker-proxy 掉线时宿主上没人监听 51413/tcp，
        # 重启容器会把绑定重新下发。
        self.ensure_published_ports()
        # 端口真的发布出来了，再去路由器上要一个转发（尽力而为）
        self.ensure_port_forward()
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
        # 容器停了，路由器上那条转发也没人应答了；顺手撤掉，别留在路由器上
        self.remove_port_forward()
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


def tr_rpc(method, username=None, password=None, session_id='', timeout=15, arguments=None):
    """调一次 Transmission RPC；返回 (状态码, body, session-id 头)。

    daemon 首次（或 session 过期）会回 409 并带 X-Transmission-Session-Id，需带着重试。
    """
    connection = http.client.HTTPConnection('127.0.0.1', PORT, timeout=timeout)
    headers = {'Content-Type': 'application/json'}
    if username and password:
        token = base64.b64encode((username + ':' + password).encode('utf-8')).decode('ascii')
        headers['Authorization'] = 'Basic ' + token
    if session_id:
        headers['X-Transmission-Session-Id'] = session_id
    payload = {'method': method}
    if arguments is not None:
        payload['arguments'] = arguments
    body = json.dumps(payload).encode('utf-8')
    try:
        connection.request('POST', '/transmission/rpc', body, headers)
        response = connection.getresponse()
        data = response.read(4 * 1024 * 1024 + 1)
        return response.status, data, response.getheader('X-Transmission-Session-Id', '')
    except (OSError, http.client.HTTPException) as exc:
        raise Error('Transmission 未运行或尚未就绪') from exc
    finally:
        connection.close()


def tr_call(method, username, password, arguments=None, timeout=15):
    """带 409 会话握手的 RPC 调用，成功返回 arguments 字典。"""
    sid = ''
    code, body, sid = tr_rpc(method, username=username, password=password, session_id='', timeout=timeout, arguments=arguments)
    if code == 409 and sid:
        code, body, _ = tr_rpc(method, username=username, password=password, session_id=sid, timeout=timeout, arguments=arguments)
    if code == 401:
        raise Error('WebUI 账号密码错误，或 Transmission 未开启 RPC 鉴权')
    if code not in (200, 409):
        raise Error('Transmission RPC 拒绝操作（HTTP ' + str(code) + '）')
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise Error('Transmission RPC 返回了非 JSON 响应') from exc
    if not isinstance(data, dict):
        raise Error('Transmission RPC 响应格式无效')
    result = data.get('result')
    if result and result != 'success':
        raise Error('Transmission RPC：' + str(result)[:200])
    args = data.get('arguments')
    return args if isinstance(args, dict) else {}


def tr_rpc_probe(username=None, password=None):
    """就绪探测：401/409 都说明 daemon 已在监听。"""
    code, _, session = tr_rpc('session-get', username=username, password=password)
    if code == 409 and session:
        code, _, _ = tr_rpc('session-get', username=username, password=password, session_id=session)
    if code in (200, 401, 409):
        return True
    raise Error('Transmission Web 尚未就绪')
