"""Bounded Home Assistant container lifecycle (Docker Engine API).

官方镜像 ghcr.io/home-assistant/home-assistant：只挂载一路用户目录（配置目录 →
容器内 /config），插件只做固定容器的启停与目录重挂，不代理 Home Assistant 的 API。

与同族插件（Jellyfin / Emby）的关键差别只有一处**有意为之**：Home Assistant 官方
容器**不支持以非 root 用户运行**（镜像里的 s6-overlay 与 hass 启动流程都假定 uid 0，
社区上游也明确不支持 PUID/PGID，见
https://community.home-assistant.io/t/official-docker-image-runs-hass-as-root/956857）。
所以这里不设 container_config() 的 `User`（同族插件设的是所选目录的属主），而是沿用
镜像默认的 root；但仍要求用户选的配置目录属于**非 root 的 NAS 用户**——HA 在容器里
以 root 写入 /config 时，宿主机上看到的属主仍是那个用户，小米客户端的文件管理才会
正常显示与操作。
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
# 官方镜像的**首选**标签：滚动更新，永远指向最新版。
IMAGE = 'ghcr.io/home-assistant/home-assistant:stable'
# 厂商 dockerd 太旧时的固定回退标签：2026.2.0 是最后一个仍然用 gzip 压缩层的版本。
#
# 背景（2026-10 在 NAS 上实测定位）：厂商 dockerd 是 20.10.17，而 Home Assistant 的
# arm64 镜像从 2026.3.0 起所有层都改成 `application/vnd.oci.image.layer.v1.tar+zstd`
# —— **zstd 层需要 Docker 23.0+** 才能解包（`archive/tar: invalid tar header`，
# `failed to register layer`）。所以老 Docker 上拉 stable 一定失败，必须钉在
# 2026.2.0（gzip 层，715 MB，实测可拉）。
# 等到厂商把 Docker 升到 23+，下面的 choose_image() 会自动回到 stable，无需改配置。
IMAGE_STABLE_TAG = 'stable'
IMAGE_LEGACY_TAG = '2026.2.0'
IMAGE_STABLE = IMAGE
IMAGE_LEGACY = 'ghcr.io/home-assistant/home-assistant:' + IMAGE_LEGACY_TAG
# zstd 层所需的最低 Docker Engine 版本（22.x 及以前都不行）。
ZSTD_MIN_DOCKER = (23, 0)
# 状态卡片上的「镜像」一项：滚动标签，没有可写死的语义版本号。
IMAGE_VERSION = 'stable（滚动标签）'
NAME = 'xiaomi-plugin-homeassistant'
LABEL = 'io.xiaomi-plugin.homeassistant.owner'
# Home Assistant 的 Web UI / API 端口。用 host 网络时容器直接占用宿主机的 8123
# （容器内外同一个端口、同一个网络命名空间），插件自己的服务端口是 18200，
# 号段分开，不会互撞。
PORT = 8123
# 重新 start 一个停着的容器后，等它重新 bind 8123 的宽限期（秒）与轮询间隔。
# Home Assistant 启动到监听通常只要几秒到十几秒，0.5 秒一探足够灵敏。
# 宽限期取 60 秒而不是 20 秒：真机上（机械盘 + 首次初始化 / 刚升级完 / 正在装集成）
# HA 从启动到 bind 8123 超过 20 秒是常见情况，宽限期太短会把它误判成"绑定坏了"，
# 白白做一次停→删→重建（每次重建都要再等一遍启动）。60 秒足够覆盖正常启动，
# 又能在真的坏掉时及时修复。
PORT_GRACE_SECONDS = 60
PORT_GRACE_POLL = 0.5
# 网络模式：host。
# 为什么不用桥接 + 端口映射：Home Assistant 的自动发现（mDNS/SSDP、DHCP、蓝牙/串口
# 之外的网络集成、以及 `network_mode: host` 才报得准的来源地址）在桥接下都不完整，
# 官方文档也建议 host。host 模式下容器直接用宿主机的网络栈，8123 就是宿主机 8123，
# 所以**不再发布端口**：PortBindings / ExposedPorts 在 host 模式下没有意义，
# 写上反而会让人以为做了映射。
# 代价：容器与宿主机共享网络栈，安全性弱于桥接；本插件仍然不挂 docker.sock、
# 不加 privileged、保留全部资源上限与 no-new-privileges。
NETWORK_MODE = 'host'
DOCKER_SOCKET = os.environ.get('DOCKER_SOCKET', '/var/run/docker.sock')

# 显式关掉容器健康检查。当前官方镜像自带一个 30 秒一次的 HEALTHCHECK
# （`curl -f http://localhost:8123/manifest.json` 之类），每打一次 Home Assistant
# 都会重写 SQLite 的 -shm/-wal；文件一改，系统的 findex 索引服务（fanotify）立刻写
# /nas/sys，而 /nas/sys 建在跨两块盘的 RAID1（md0）上——两块机械盘因此每 30 秒被
# 唤醒一次，永远进不了休眠，每天多出约 1.4 GB/盘的无谓写入。
# 关掉健康检查不影响 Home Assistant 自身功能，服务是否可用仍由插件页面的就绪状态
# （打 /manifest.json）反映。完整定位过程见
# projects/xiaomi-disk-sleep-plugin/tools/disk-activity-report.py。
HEALTHCHECK_OFF = {'Test': ['NONE']}

# 容器资源上限。Home Assistant 本身不重，但一旦接上历史数据库、摄像头或
# 各种集成，内存会长到 1 GiB 以上，所以按同族的 2 GiB 给足，避免被自己的
# memcg 上限 OOM kill 掉。MemorySwap 与 Memory 相同＝不额外给 swap：宿主机
# 本来也没有 swap，换页出去只会更慢。
MEMORY_LIMIT = 2048 * 1024 * 1024
CPU_LIMIT = 2 * 10 ** 9

# 插件给容器设的环境变量键。
CONTAINER_ENV_KEYS = ('TZ',)
# 插件**已经不再设置**、但旧容器里可能还残留的环境变量键（目前没有）。
# 留着这个空元组是为了让 stale_env() 的两个方向都成立：将来删掉某个键时，
# 旧容器会在下次启动时自动重建，而不是带着一个不再需要的变量一直跑。
REMOVED_ENV_KEYS = ()


def container_env():
    """要往容器里塞的环境变量（只放必需的）。

    只设时区：Home Assistant 自己的配置（时区、单位、集成）都保存在 /config 里，
    插件不替它写任何东西。
    """
    return ['TZ=Asia/Shanghai']


class Error(RuntimeError):
    pass


def installed_version():
    """插件包的真实版本号。

    商店安装器把包解压到 <releaseRoot>/releases/<版本>-<时间戳>-<pid>/ 下，
    current 是指向它的符号链接，所以本文件所在目录名里就带着版本；源码树里跑
    （开发、预览）时解析不出来，回退到 VERSION。
    """
    parts = Path(__file__).resolve().parent.name.split('-')
    if len(parts) > 2 and parts[-1].isdigit() and parts[-2].isdigit():
        return '-'.join(parts[:-2])
    return VERSION


def parse_docker_version(text):
    """把 Docker 版本号解析成可比较的数字元组。

    只取开头的纯数字段：`20.10.17` → `(20, 10, 17)`、`24` → `(24,)`、
    `26.1.0-rc1` → `(26, 1, 0)`、`v23.0.1` → `(23, 0, 1)`。
    **不能用字符串比较**：`'20.10.17' < '23.0.0'` 碰巧对，但 `'9.0' > '20.0'`
    这种就错了，而且 `26.1` 与 `26.1.0` 会被判成不同。解析不出来返回空元组。
    """
    match = re.match(r'\s*v?(\d+(?:\.\d+)*)', str(text or ''))
    if not match:
        return ()
    return tuple(int(part) for part in match.group(1).split('.'))


def compare_versions(left, right):
    """数字元组比较，返回 -1 / 0 / 1。

    缺位的部分按 0 补齐，所以 `26.1` 与 `26.1.0` 相等、`24` 与 `24.0.0` 相等。
    """
    left, right = tuple(left or ()), tuple(right or ())
    length = max(len(left), len(right))
    for index in range(length):
        one = left[index] if index < len(left) else 0
        other = right[index] if index < len(right) else 0
        if one != other:
            return -1 if one < other else 1
    return 0


def docker_supports_zstd(daemon_version):
    """该 dockerd 版本能不能解包 zstd 层（Home Assistant 2026.3.0+ 用的格式）。

    解析不出守护进程版本（探测失败、字段缺失）时返回 False——调用方要区分
    「版本未知」与「版本太旧」：未知时先用首选 stable，由 pull 的回退兜底；
    确认太旧才直接钉到 2026.2.0。
    """
    parsed = parse_docker_version(daemon_version)
    if not parsed:
        return False
    return compare_versions(parsed, ZSTD_MIN_DOCKER) >= 0


def choose_image(daemon_version):
    """按 dockerd 的能力选镜像。

    首选永远是 `stable`（不写死"只能用 2026.2.0"）：
      · 版本 ≥ 23（能解 zstd）→ stable；
      · 版本 < 23（确认太旧，如厂商的 20.10.17）→ 固定 2026.2.0；
      · **版本未知**（`/version` 查不到、字段缺失）→ 仍然先试 stable，
        因为拉不动时 pull_with_fallback() 会自动改用 2026.2.0。
    """
    parsed = parse_docker_version(daemon_version)
    if not parsed:
        return IMAGE_STABLE
    return IMAGE_STABLE if compare_versions(parsed, ZSTD_MIN_DOCKER) >= 0 else IMAGE_LEGACY


def image_candidates(daemon_version):
    """要按顺序尝试的镜像列表：首选在前，另一个作为兜底重试一次。

    顺序由守护进程版本决定；两个候选互为兜底（stable 失败 → 2026.2.0，
    2026.2.0 失败 → stable）。
    """
    preferred = choose_image(daemon_version)
    return [preferred] + [image for image in (IMAGE_STABLE, IMAGE_LEGACY) if image != preferred]


def image_version_label(image):
    """状态卡片上「镜像」一项的显示文案。"""
    tag = str(image).rpartition(':')[2] or str(image)
    if tag == IMAGE_STABLE_TAG:
        return 'stable（滚动标签）'
    return tag + '（旧 Docker 不支持 zstd 层时的固定版本）'


def layer_error(message):
    """这个 Docker 报错是不是"镜像层解不开"。

    厂商 dockerd 20.10 遇到 zstd 层时报的是
    `failed to register layer: Error processing tar file(exit status 1):
     archive/tar: invalid tar header`——判断依据就是这几类关键字。
    """
    text = str(message or '').lower()
    return any(marker in text for marker in
               ('register layer', 'invalid tar header', 'application/vnd.oci.image.layer.v1.tar+zstd',
                'zstd', 'unsupported compression', 'unsupported media type'))


LAYER_ERROR_LIMIT = 400


def summarize_docker_error(text, limit=LAYER_ERROR_LIMIT):
    """把 Docker 的原始报错压成一行可读摘要（截断到 limit 个字符）。

    必须把底层原因带出来：这次真机排查就是被"请检查镜像网络、端口 8123 和可用资源"
    这种笼统提示耽误的——真正的原因是 `archive/tar: invalid tar header`。
    """
    if text is None:
        return ''
    if isinstance(text, bytes):
        text = text.decode('utf-8', 'replace')
    collapsed = re.sub(r'\s+', ' ', str(text)).strip()
    if len(collapsed) > limit:
        collapsed = collapsed[:limit] + '…'
    return collapsed


def pull_error_summary(stream):
    """从 `/images/create` 的流式响应里取出 Docker 报的错（没有则空串）。

    响应是一行一个 JSON：`{"status":"Pulling fs layer"}`、
    `{"errorDetail":{"message":"failed to register layer: …"},"error":"…"}`。
    """
    messages = []
    for line in (stream or b'').splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(event, dict):
            continue
        detail = event.get('errorDetail')
        message = ''
        if isinstance(detail, dict):
            message = str(detail.get('message') or '')
        if not message:
            message = str(event.get('error') or '')
        if message:
            messages.append(message)
    return summarize_docker_error('；'.join(messages))


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

    用途见 check_directories()：厂商的存储池 /nas/pool0 是 FUSE（fuse.cfs），
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


def read_ha_version(folder):
    """读用户配置目录里的 `.HA_VERSION`（Home Assistant 自己写的版本号）。

    HA Core 每次启动都会把当前版本写进 /config/.HA_VERSION，一行纯文本，例如
    `2024.12.5`。它比镜像 tag 更准（stable 是滚动标签，看不出具体版本），所以
    页面优先显示它。读不到（首次启动、文件被删、没权限）就返回空串，由调用方
    退回镜像口径——这只是展示信息，任何情况下都不该因此报错。
    """
    try:
        text = (Path(folder) / '.HA_VERSION').read_text(encoding='utf-8', errors='replace')
    except OSError:
        return ''
    return text.strip()[:32]


def homeassistant_info():
    """读取 Home Assistant 无需认证的公开信息；HTTP 200 即服务已就绪。

    `/manifest.json` 是 HA 前端的静态清单（应用名、版本），不需要登录，
    也不需要 HA 已经完成初始化向导，适合当就绪探针。
    """
    connection = http.client.HTTPConnection('127.0.0.1', PORT, timeout=6)
    try:
        connection.request('GET', '/manifest.json')
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
        raise Error('Home Assistant 未运行或尚未就绪') from exc
    finally:
        connection.close()


def container_config(config, image=IMAGE_STABLE):
    """固定的容器配置：一路 bind 挂载、host 网络与资源限制。

    只挂载配置目录（→ /config）：Home Assistant 的数据库、集成配置、自动化、
    密钥都在这里面；它把 /media、/share、/ssl、/backup 都当成 /config 下的
    普通子目录，所以不需要再引入媒体目录。

    `image` 由调用方按 dockerd 能力决定（见 choose_image()）：默认 stable，
    老 Docker 上换成 2026.2.0。Engine 内部一律用 Engine.container_config()，
    它会带上已解析好的镜像。
    """
    return {
        'Image': image,
        # 刻意不设 User：官方容器要求以 root 运行（见模块开头的说明）。
        # 宿主机上看到的属主仍是所选配置目录的属主（非 root 的 NAS 用户）。
        # 也刻意不写 ExposedPorts / HostConfig.PortBindings：host 模式下它们
        # 不生效（见 NETWORK_MODE 的说明），8123 就是宿主机自己的 8123。
        'Env': container_env(),
        'Labels': {LABEL: config['owner']},
        # 不要继承官方镜像的 30 秒健康检查（见 HEALTHCHECK_OFF 的说明）。
        'Healthcheck': dict(HEALTHCHECK_OFF),
        'HostConfig': {
            'NetworkMode': NETWORK_MODE,
            'RestartPolicy': {'Name': 'no'},
            'Memory': MEMORY_LIMIT,
            'MemorySwap': MEMORY_LIMIT,
            'NanoCpus': CPU_LIMIT,
            'PidsLimit': 512,
            'SecurityOpt': ['no-new-privileges:true'],
            'LogConfig': {'Type': 'json-file', 'Config': {'max-size': '5m', 'max-file': '2'}},
            # 数据持久化：用户选的配置目录（HA 自己再在里面建 media/share/ssl/backup）
            'Mounts': [
                {'Type': 'bind', 'Source': config['config'], 'Target': '/config'},
            ],
        },
    }


def port_is_listening(host='127.0.0.1', port=PORT, timeout=2):
    """宿主机上这个 TCP 端口是否有人在监听。

    host 网络模式下没有「端口发布」可查，容器的 8123 就是宿主机的 8123，所以
    判断容器有没有真的把 Web UI/API 提供出来，只能看这个端口有没有在监听。
    只连本机回环，不接受外部传入地址。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(timeout)
        try:
            return probe.connect_ex((host, int(port))) == 0
        except OSError:
            return False


def service_address(host):
    """Home Assistant 界面地址（host 模式下就是宿主机自己的 8123）。"""
    return 'http://' + str(host) + ':' + str(PORT)


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
        # dockerd 版本只在第一次用到时查一次（见 daemon_version()）；None 表示还没查过。
        self._daemon_version = None
        # 本次会话实际用过的镜像；配置里的 image 是跨重启的持久记录。
        self._image = ''
        self.cfgfile = self.data / 'settings.json'
        self.config = None
        if self.cfgfile.exists():
            try:
                loaded = json.loads(self.cfgfile.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                loaded = None
            # 只认带 owner 的配置：owner 是容器归属令牌，缺了就没法安全接管容器。
            # config 留空是合法的——那表示配置目录在插件私有目录里。
            if isinstance(loaded, dict) and loaded.get('owner'):
                self.config = loaded

    def _call(self, method, path, body=None, timeout=30, ok=(200, 201, 204)):
        if self.dev:
            raise Error('预览模式不会操作 Docker')
        status, data = docker_api(method, path, body, timeout)
        if status not in ok:
            raise Error('Docker 操作失败（HTTP %s）：%s；端口 %s，未修改其他容器'
                        % (status, summarize_docker_error(data, 300) or '无详细原因', PORT))
        return data

    def inspect(self):
        if self.dev:
            raise Error('预览模式不会操作 Docker')
        status, data = docker_api('GET', '/containers/' + NAME + '/json')
        if status == 404:
            return None
        if status != 200:
            raise Error('Docker 操作失败（HTTP %s）：%s；端口 %s，未修改其他容器'
                        % (status, summarize_docker_error(data, 300) or '无详细原因', PORT))
        item = json.loads(data)
        return item if isinstance(item, dict) else None

    def pull(self, image=None):
        """拉一个镜像；失败时把 Docker 的原始报错带出来。

        `image` 省略时按 dockerd 能力自动选（见 choose_image()）。报错里必须带上
        底层原因——真机上就是 `failed to register layer: … archive/tar: invalid tar
        header`（zstd 层 + 老 Docker），笼统的"请检查镜像网络"会把排查带偏。
        """
        target = image or self.image()
        reference, _, digest = target.partition('@')
        repository, _, tag = reference.partition(':')
        query = urllib.parse.urlencode({
            'fromImage': repository + ('@' + digest if digest else ''),
            'tag': tag or 'latest',
        })
        if self.dev:
            raise Error('预览模式不会操作 Docker')
        stream = self._call('POST', '/images/create?' + query, timeout=1800)
        detail = pull_error_summary(stream)
        if detail:
            raise Error('拉取镜像 %s 失败：%s' % (target, detail))

    def pull_with_fallback(self, candidates=None):
        """按候选顺序拉镜像，第一个成功的就是本次要用的；最多重试一次。

        首选由 dockerd 版本决定（见 image_candidates()），另一个作为兜底：
        stable 因 zstd 层失败 → 试 2026.2.0；反之亦然。两个都失败时抛出的错误里
        保留各自 Docker 的原始报错（拼接、截断），方便一眼看出是层格式问题还是
        网络问题。
        """
        images = list(candidates or image_candidates(self.daemon_version()))
        problems = []
        for index, image in enumerate(images):
            try:
                self.pull(image)
            except Error as exc:
                problems.append(str(exc))
                if index + 1 < len(images):
                    print('homeassistant: 拉取 %s 失败（%s），改用 %s 重试一次'
                          % (image, summarize_docker_error(exc, 160), images[index + 1]), flush=True)
                continue
            self.record_image(image)
            if image != images[0]:
                print('homeassistant: 最终使用镜像 %s（首选 %s 不可用）' % (image, images[0]), flush=True)
            return image
        raise Error('两个候选镜像都拉不下来：' + '；'.join(problems))

    def daemon_version(self):
        """dockerd 的版本号（`/version` 的 `Version` 字段）；拿不到返回空串。

        预览模式下不查（不会操作 Docker）。结果缓存在 self._daemon_version：
        成功或失败都记住，避免每次 snapshot() 都去打一次 API——Docker 不可用时
        那样每次刷新页面都会白等一个超时。
        """
        if self._daemon_version is not None:
            return self._daemon_version
        if self.dev:
            self._daemon_version = ''
            return self._daemon_version
        try:
            status, data = docker_api('GET', '/version', timeout=10)
        except Error:
            self._daemon_version = ''
            return self._daemon_version
        if status != 200:
            self._daemon_version = ''
            return self._daemon_version
        try:
            payload = json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {}
        value = payload.get('Version') if isinstance(payload, dict) else ''
        self._daemon_version = str(value or '')
        return self._daemon_version

    def image(self):
        """本次要用的镜像。

        优先级：配置里记着的那个（上次成功拉下来的，重装/重启后不再重复试错）
        → 按 dockerd 版本选 → 解析不出守护进程版本时用首选 stable（由
        pull_with_fallback 兜底重试）。
        """
        recorded = self.config.get('image') if self.config else ''
        if isinstance(recorded, str) and recorded:
            return recorded
        return choose_image(self.daemon_version())

    def record_image(self, image):
        """记下实际使用的镜像（写进 settings.json 与 snapshot）。"""
        self._image = image
        if self.config and self.config.get('image') != image:
            self.config['image'] = image

    def image_reason(self):
        """页面上说明「为什么用这个镜像」。"""
        recorded = self.config.get('image') if self.config else ''
        version = self.daemon_version()
        if isinstance(recorded, str) and recorded and recorded != choose_image(version):
            return '上次实际拉下来的是这个镜像（已记在插件设置里，重启后继续用）'
        if docker_supports_zstd(version):
            return 'Docker %s 支持 zstd 层，用最新的 stable' % version
        if version:
            return ('Docker %s 太旧（zstd 压缩层需要 %s+），固定到 %s'
                    % (version, '.'.join(str(part) for part in ZSTD_MIN_DOCKER), IMAGE_LEGACY_TAG))
        return '读不到 dockerd 版本，先用 stable；拉不动会自动改用 %s' % IMAGE_LEGACY_TAG

    def container_config(self, image=None):
        """按当前配置与选定镜像生成容器创建参数。"""
        return container_config(self.config, image or self.image())

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
        还残留在旧容器里。本插件的 REMOVED_ENV_KEYS 是空集（从没删过键），但
        保留这段判断，将来删键时旧容器会自动重建。
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

    @staticmethod
    def network_stale(item):
        """容器的网络模式不是现在要求的（只能在创建时生效，得重建）。

        Home Assistant 用 host 网络（见 NETWORK_MODE 的说明）。早期若是桥接 +
        端口映射建的容器，端口发布同样是创建时固定的，只能停 → 删 → 重建。
        """
        host = item.get('HostConfig') or {}
        return (host.get('NetworkMode') or 'default') != NETWORK_MODE

    @staticmethod
    def resources_stale(item):
        """容器的内存/CPU 上限和现在要求的不一致（只能在创建时生效，得重建）。

        Docker 20.10 的 `POST /containers/<id>/update` 对这类资源上限不生效，
        所以只能停 → 删 → 按新配置重建（bind 挂载的 /config 不受影响）。
        """
        host = item.get('HostConfig') or {}
        return (host.get('Memory') != MEMORY_LIMIT
                or host.get('MemorySwap') != MEMORY_LIMIT
                or host.get('NanoCpus') != CPU_LIMIT)

    def recreate_if_stale(self, item):
        """旧配置只能靠重建生效：继承的健康检查、已删除的环境变量、过期的资源上限、网络模式。

        健康检查、资源上限与网络模式只能在创建容器时决定（Docker 20.10 的
        `POST /containers/<id>/update` 接受 Healthcheck 并返回 200，但不生效；
        资源上限与端口发布同理）；环境变量也是创建时写入。这里停容器 → 删除 →
        交给调用方按新配置重建。/config 是 bind 挂载，Home Assistant 的配置与
        数据不受影响。返回 True 表示已经把它删掉了。
        """
        reasons = []
        if self.inherited_healthcheck(item):
            reasons.append('镜像自带的健康检查')
        if self.stale_env(item):
            reasons.append('过期的环境变量')
        if self.resources_stale(item):
            host = item.get('HostConfig') or {}
            reasons.append('过期的资源上限（内存 %s MB → %s MB）'
                           % (int((host.get('Memory') or 0) / 1048576),
                              int(MEMORY_LIMIT / 1048576)))
        if self.network_stale(item):
            reasons.append('过期的网络模式（→ ' + NETWORK_MODE + '）')
        if not reasons:
            return False
        if item.get('State', {}).get('Running'):
            self._call('POST', '/containers/' + NAME + '/stop?t=30', timeout=90)
        self._call('DELETE', '/containers/' + NAME, ok=(200, 204, 404))
        print('homeassistant: 重建容器（%s）' % '、'.join(reasons), flush=True)
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
        client_version = ''
        if self.config and not self.busy:
            try:
                item = self.owned()
                running = bool(item and item.get('State', {}).get('Running'))
            except Error as exc:
                error = str(exc)
            if running:
                try:
                    homeassistant_info()
                    ready = True
                except Error:
                    ready = False
            # 版本优先读用户配置目录里的 .HA_VERSION（HA 自己写的），
            # 容器没跑、或者还没写过时退回镜像口径。
            client_version = read_ha_version(self.config.get('config') or (self.data / 'config'))
        return {
            'version': installed_version(),
            'configured': bool(self.config),
            'running': running,
            'ready': ready,
            'busy': self.busy,
            'error': error,
            'preview': self.dev,
            'port': PORT,
            'directory': self.config.get('config_relative', '') if self.config else '',
            'configDirectory': self.config.get('config_relative', '') if self.config else '',
            # 状态卡片显示完整绝对路径：相对路径在「存储位置」多于一个时看不出在哪
            'config_abs': self.config.get('config', '') if self.config else '',
            # 配置目录留在插件私有目录（不属于任何存储位置）时为真，前端据此加一句说明
            'config_private': bool(self.config) and not (
                self.config.get('config_root') or self.config.get('config_relative')),
            'roots': self.locations(),
            # 客户端版本：优先 .HA_VERSION，读不到就是空串（前端会退回镜像口径）
            'clientVersion': client_version,
            # 实际使用的镜像与选择原因（老 Docker 上会自动固定到 2026.2.0）
            'image': self.image(),
            'imageVersion': image_version_label(self.image()),
            'imageReason': self.image_reason(),
            'dockerVersion': self.daemon_version(),
            'healthcheckOff': bool(self.config and self.config.get('healthcheck_off')),
        }

    def _resolve_config(self, choice, root_index=None):
        """按插件规则校验并解析配置目录（setup 与 reconfigure 共用）。

        返回 {config_index, config_relative, config, config_root, uid, gid}：
        选择可以是绝对路径（前端提交）或根内相对路径（旧语义）；留空表示配置放在
        插件私有目录（此时 config_root 为空串）。uid/gid 是所选目录的属主，用于
        创建私有目录时对齐属主；容器本身仍以 root 运行（见模块开头说明）。
        """
        if choice is None or (isinstance(choice, str) and not choice.strip()):
            # 没选外部目录：配置落在插件私有目录，外部看不到，也不占用户存储
            folder = self.data / 'config'
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(folder, 0o700)
            stat = folder.stat()
            if not stat.st_uid or not stat.st_gid:
                # data 目录归 root（systemd 以当前用户跑，正常不会到这）：
                # 这种情况下没法保证属主，宁可直接拒绝
                raise Error('插件私有目录不可用，请选择用户存储里的配置目录')
            return {
                'config_index': 0,
                'config_relative': '',
                'config': folder,
                'config_root': '',
                'uid': stat.st_uid,
                'gid': stat.st_gid,
            }
        if not isinstance(choice, str) or len(choice) > 1024:
            raise Error('配置目录无效')
        index, relative = self.locate(choice, root_index)
        folder = confined(self.roots[index], relative)
        if ',' in str(folder):
            raise Error('Docker 挂载目录不能包含逗号')
        stat = folder.stat()
        if not stat.st_uid or not stat.st_gid:
            raise Error('所选目录须由非 root 的 NAS 用户拥有')
        return {
            'config_index': index,
            'config_relative': relative,
            'config': folder,
            'config_root': str(self.roots[index]),
            'uid': stat.st_uid,
            'gid': stat.st_gid,
        }

    def setup(self, config_choice='', config_root=None):
        """初始化：目录选择可以是绝对路径（前端提交）或根内相对路径（旧语义）。

        留空表示把配置目录放在插件私有目录里（Home Assistant 会在其中新建
        configuration.yaml、.storage、home-assistant_v2.db 等）。
        """
        if self.config:
            raise Error('已完成初始化；现有 Home Assistant 配置不会被覆盖')
        chosen = self._resolve_config(config_choice, config_root)
        folder = chosen['config']

        self._call('GET', '/info')
        if self.inspect() is not None:
            raise Error('同名容器已存在，拒绝覆盖')

        stat = folder.stat()
        self.config = {
            'owner': secrets.token_hex(24),
            'uid': chosen['uid'],
            'gid': chosen['gid'],
            # config_root 记下这个选择落在哪个存储位置，启动校验时据此回到同一个根；
            # 私有目录时为空串（不属于任何存储位置）。
            'config_root': chosen['config_root'],
            'config': str(folder),
            'config_relative': chosen['config_relative'],
            'config_device': stat.st_dev,
            'config_inode': stat.st_ino,
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

    def reconfigure(self, config_choice):
        """修改配置目录：保留数据，按新宿主路径重建容器。

        传 None 表示「配置目录不动」；传空串表示改回插件私有目录。
        成功返回最新状态，失败抛出原因（已回滚）。
        """
        with self._operation():
            self._do_reconfigure(config_choice)
        return self.snapshot()

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

    def _do_reconfigure(self, config_choice):
        """换目录但保留数据：备份配置 → 按新 bind 重建容器 → 失败回滚配置与原容器。

        config_choice 为 None 表示配置目录不动（`POST /api/service/reconfigure`
        不带 configPath 时的语义），此时直接按当前配置重建。
        """
        if not self.config:
            raise Error('请先初始化')
        if config_choice is None:
            chosen = {
                'config_index': 0,
                'config_relative': self.config.get('config_relative', ''),
                'config': Path(self.config.get('config') or (self.data / 'config')),
                'config_root': self.config.get('config_root', ''),
                'uid': self.config.get('uid') or 0,
                'gid': self.config.get('gid') or 0,
            }
            if str(chosen['config']) != str(self.config.get('config', '')):
                raise Error('配置目录不可用，请重新选择')
        else:
            chosen = self._resolve_config(config_choice)
        old_config = dict(self.config)
        stat = chosen['config'].stat()
        new_config = dict(old_config)
        new_config.update({
            'uid': chosen['uid'],
            'gid': chosen['gid'],
            'config_root': chosen['config_root'],
            'config': str(chosen['config']),
            'config_relative': chosen['config_relative'],
            'config_device': stat.st_dev,
            'config_inode': stat.st_ino,
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
        print('homeassistant: 已更换目录（备份 %s），容器已按新挂载重建' % backup.name, flush=True)

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

        用户数据（配置目录里的 configuration.yaml、.storage、数据库等）里的文件
        一个都不删、不移、不改；本插件没有路由器端口映射之类的副作用，容器就是
        唯一的系统改动。
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
        print('homeassistant: 已重新初始化（容器与插件配置已清理，备份 %s；用户目录未改动）'
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
        print('homeassistant: %s 所在的 %s（%s）每次挂载都会换设备号/inode 号，已更新记录'
              % (label, fstype, point), flush=True)
        return True

    def _config_root(self):
        """启动校验该用哪个根（存储位置）。

        配置里记下的 config_root 优先（即使 LOCAL_ROOTS 变了也回到同一个根）；
        旧配置没有这个字段，就按记录下来的**绝对路径**前缀反查所属位置（不是按
        相对路径——相对路径在多个位置里可能同名，反查不出来时只能退化为第一个
        根，那样报的错会指错方向）。私有目录（config_root 为空串）不进这条路
        ——它不在任何存储位置下。
        """
        recorded = self.config.get('config_root')
        if isinstance(recorded, str) and recorded:
            return Path(recorded)
        absolute = self.config.get('config')
        if isinstance(absolute, str) and absolute:
            index = root_index_of(self.roots, absolute)
            if index >= 0:
                return self.roots[index]
        return self.root

    def check_directories(self):
        """启动前的目录校验：外部配置目录要回到原来的位置且身份一致。

        配置在插件私有目录时（config_root 与 config_relative 都为空）不需要校验：
        它就在 /data 底下，不随存储挂载变化。
        """
        if not (self.config.get('config_root') or self.config.get('config_relative')):
            return
        label = '配置目录'
        root = self._config_root()
        try:
            folder = confined(root, self.config['config_relative'])
        except Error as exc:
            # 相对路径解析不出来：多半是存储没挂上（相对路径落到了空目录上）。
            # 带上「配置目录」这个称呼，页面上的提示才有指向性。
            raise Error('%s不可用（%s），拒绝启动；请先检查存储挂载' % (label, exc)) from exc
        try:
            stat = folder.stat()
        except OSError as exc:
            raise Error(label + '不可用（%s），拒绝启动；请先检查存储挂载' % exc.strerror) from exc
        if str(folder) != self.config['config']:
            raise Error(label + '身份已变化，拒绝启动；请先检查存储挂载')
        if self._identity_ok(folder, stat, 'config_device', 'config_inode', label):
            atomic_json(self.cfgfile, self.config)

    def create_container(self):
        """按当前配置建容器并启动；调用方保证镜像已在本地。"""
        self.check_directories()
        self._call('POST', '/containers/create?name=' + NAME,
                   body=self.container_config(), timeout=120)
        self._call('POST', '/containers/' + NAME + '/start')

    def start(self):
        if not self.config:
            raise Error('请先初始化')
        self.check_directories()
        item = self.owned()
        restart = False               # 已经存在的容器、这次要不要再启动一次
        if item and self.recreate_if_stale(item):
            # 旧容器带着镜像自带的 30 秒健康检查、残留着不再需要的环境变量、
            # 或还是桥接 + 端口映射建的，都只能靠重建去掉（见方法说明）。
            # 升级后插件服务重启、或用户点「启动服务」时都会走到这里。
            # 重建时镜像可能已经在本地（甚至换了候选），所以先确认一次：
            # 拉取失败会带出 Docker 的原始报错（例如 zstd 层的 invalid tar header）。
            self.pull_with_fallback()
            item = None
            self.create_container()
        elif item is None:
            # 镜像不在本地：先查 dockerd 版本决定拉哪个（老 Docker 上 HA 的
            # 2026.3.0+ 用 zstd 层，20.10 解不开），失败再换另一个候选重试一次。
            self.pull_with_fallback()
            self.create_container()
        elif not item.get('State', {}).get('Running'):
            restart = True
        if restart:
            self._call('POST', '/containers/' + NAME + '/start')
            self._ensure_port(restart=True)
        # 到这里容器要么是新建的（配置里写死了健康检查 NONE），
        # 要么本来就带着 NONE，所以可以标记为已关闭。
        self.config['healthcheck_off'] = True
        self.config['enabled'] = True
        atomic_json(self.cfgfile, self.config)
        # 首次启动 Home Assistant 要建数据库、跑 onboard 流程，可能较慢；
        # 超时只影响提示，容器本身已经起来了。
        for _ in range(150):
            try:
                homeassistant_info()
                return
            except Error:
                time.sleep(2)
        raise Error('容器已启动，但 Home Assistant 尚未就绪；可稍后刷新，首次初始化可能需要数分钟')

    def _ensure_port(self, restart=False, window=PORT_GRACE_SECONDS):
        """host 网络下的端口自检：容器在跑但宿主机 8123 一直没人监听，就重建一次。

        host 模式没有「端口发布」可以查，容器的 8123 就是宿主机的 8123，所以只能看
        宿主机这个端口在不在监听（`port_is_listening()`）。但「容器起来了」和
        「Home Assistant 装配完配置、开始监听」之间隔着几秒到几分钟，所以：

        - 新建容器（首次初始化 / 换目录）根本不在这里判：就绪与否交给
          `homeassistant_info()`（打 /manifest.json）的轮询，免得把「还在启动」
          误判成「要重建」。
        - 「容器本来就停着、这次只是重新 start」才自检，而且先给 window 秒宽限
          （默认 60 秒；Home Assistant 从启动到 bind 8123 通常几秒到十几秒，
          真机上超过 20 秒也不罕见，宽限期给足才不会误判）。宽限期内没
          监听才停 → 删 → 重建一次；重建后仍没监听就如实报错——那种情况多半是
          宿主机上 8123 被别的程序占着，或者镜像/目录有问题。
        - 容器本来就跑着（刷新页面 / 插件服务重启）根本不会走到这里。

        返回 True 表示端口在监听。
        """
        if port_is_listening():
            return True
        if not restart:
            return False
        # 按次数数宽限期（window / 间隔），不用墙钟：这样测试里把 time.sleep 打桩掉
        # 也能确定性地跑完，不会因为 sleep 不推进时间而空转。
        attempts = max(1, int(max(0.0, float(window)) / PORT_GRACE_POLL))
        for _ in range(attempts):
            time.sleep(PORT_GRACE_POLL)
            if port_is_listening():
                return True
        print('homeassistant: 宿主机 %d 端口等待 %g 秒仍没有监听，重建容器修复'
              % (PORT, window), flush=True)
        self._rebuild_container()
        if port_is_listening():
            return True
        raise Error('容器已启动，但宿主机 %d 端口没有监听；请检查 8123 是否被其它程序占用'
                    % PORT)

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
                    self.setup(data.get('path', ''), data.get('pathRoot'))
                    self.start()
                elif action == 'reconfigure':
                    # configPath 缺失（None）＝保持当前配置目录；传空串才是改成插件私有目录
                    self._do_reconfigure(data.get('configPath'))
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
