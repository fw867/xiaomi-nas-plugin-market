#!/usr/bin/env python3
"""小米智能存储「控制台」——只读数据采集层。

设计约束（很重要，不要在这里加写操作）：

- 只读：不写任何配置、不启停任何服务或容器、不修改任何文件。
- 不吵醒硬盘：高频轮询只用 /proc 与 /sys；
  `hdparm -C` 查询电源状态不会唤醒硬盘（与硬盘休眠插件同一结论）；
  SMART 只在硬盘不处于 standby 时读取，并缓存 5 分钟；
  盘温来自内核 drivetemp hwmon，同样在 standby 时跳过。
- 纯标准库：目标机是 poky/Yocto（Python 3.12），没有 pip 包可用。
"""

from __future__ import annotations

import json
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from http.client import HTTPConnection
from pathlib import Path
from typing import Any

PLUGIN_KEY = os.environ.get('PLUGIN_KEY', 'nasconsole')
RELEASE_DIR = Path(__file__).resolve().parent
VERSION_FILE = RELEASE_DIR / 'VERSION'
# 安装目录名：
#   商店安装 0.1.1-1790960335-238127（<清单版本>-<时间戳>-<pid>）
#   脚本安装 v0.1.0-20261003005422（v<版本>-<时间戳）
#   候选版   0.2.4-rc5-1790567139-4231
# 预发布后缀只允许点分字母数字（rc5 / beta.1），否则贪婪匹配会把后面的时间戳吃进版本号。
RELEASE_DIR_PATTERN = re.compile(r'^v?(\d+\.\d+\.\d+(?:-[A-Za-z][0-9A-Za-z.]*)*)(?:-\d+)+$')


def version_from_release_dir(path: Path) -> str:
    """从 `<...>/releases/<版本>-<时间戳>[-<pid>]/` 这样的目录名里取版本号（拿不到返回空串）。"""
    if path.parent.name != 'releases':
        return ''
    match = RELEASE_DIR_PATTERN.match(path.name)
    return match.group(1) if match else ''


def load_version() -> str:
    """版本号：真实读取安装版本，不写死。

    依次尝试：
    ① 安装目录名 —— 商店安装是 `releases/<清单版本>-<时间戳>-<pid>`，脚本安装是
       `releases/v<版本>-<时间戳>`；这是"这一次装的是什么版本"，与注册表里登记的版本
       必然一致（商店按清单写、脚本按 VERSION 文件写）；
    ② 框架登记的插件 INFO 里的 version（商店/脚本安装都会写，比包里的 VERSION 文件新）；
    ③ 同目录的 VERSION 文件（开发目录、以及上面两处都拿不到时）；
    ④ 都读不到返回空串——界面显示 `v-`，不编造版本号。
    """
    # 逐个短路求值：INFO 依赖 HOME_ROOT，它在文件更靠后的位置才定义
    for candidate in (version_from_release_dir(RELEASE_DIR), _read_info_version()):
        if candidate:
            return candidate
    return _read_version_file()


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


SMARTCTL = os.environ.get('SMARTCTL_BIN', '/usr/sbin/smartctl')
HDPARM = os.environ.get('HDPARM_BIN', '/usr/sbin/hdparm')
PLUGIN_DIR = Path(os.environ.get('PLUGIN_DIR', '/data/plugin'))
HOME_ROOT = Path(os.environ.get('HOME_ROOT', '/home'))
DOCKER_SOCKET = os.environ.get('DOCKER_SOCKET', '/var/run/docker.sock')

# 放在常量之后计算：load_version() 在极端情况下会去读 INFO（依赖 HOME_ROOT）
VERSION = load_version()

SAMPLE_INTERVAL = float(os.environ.get('SAMPLE_INTERVAL', '2'))
HISTORY_POINTS = int(os.environ.get('HISTORY_POINTS', '150'))      # 2s × 150 = 5 分钟
DRIVE_TTL = float(os.environ.get('DRIVE_TTL', '300'))              # SMART 缓存
DOCKER_TTL = float(os.environ.get('DOCKER_TTL', '15'))
DOCKER_STATS_TTL = float(os.environ.get('DOCKER_STATS_TTL', '15'))
SPIN_TTL = float(os.environ.get('SPIN_TTL', '5'))
SERVICE_TTL = float(os.environ.get('SERVICE_TTL', '30'))

# 内核里"会转"的整盘：sda / hda / nvme0n1；mmcblk 与 dm- 不是休眠对象
DISK_PATTERN = re.compile(r'^(sd[a-z]+|hd[a-z]+|nvme\d+n\d+)$')
BLOCK_PATTERN = re.compile(r'^(sd[a-z]+\d*|hd[a-z]+\d*|nvme\d+n\d+(p\d+)?|md\d+|mmcblk\d+(p\d+)?|dm-\d+)$')

# 这些文件系统不是"存储"，不参与容量统计
PSEUDO_FS = {
    'proc', 'sysfs', 'devtmpfs', 'devpts', 'tmpfs', 'cgroup', 'cgroup2', 'mqueue',
    'debugfs', 'tracefs', 'securityfs', 'pstore', 'bpf', 'autofs', 'configfs',
    'fusectl', 'ramfs', 'nsfs', 'binfmt_misc', 'squashfs', 'erofs', 'overlay',
    'hugetlbfs', 'rpc_pipefs', 'fuse.gvfsd-fuse', 'fuse.portal',
}

# systemd 的 ReadWritePaths= 会在服务自己的 mount namespace 里给这个目录建一个
# bind mount，于是 /proc/mounts 里会多出一条自己的安装目录。它不是真实存储，
# 不该出现在"容量与挂载"里。
OWN_MOUNT = os.environ.get('OWN_MOUNT', '/data/plugin/xiaomi-nas-console')


def is_own_mount(point: str) -> bool:
    return point == OWN_MOUNT or point.startswith(OWN_MOUNT.rstrip('/') + '/')

# 概览里固定关注的系统服务：unit → 中文标签
WATCHED_SERVICES = {
    'findex.service': '文件索引',
    'fsearch.service': '搜索服务',
    'album.service': '相册',
    'filemgr.service': '文件管理',
    'mediacenter.service': '影视中心',
    'hdidle.service': '硬盘休眠',
    'smb.service': 'SMB 共享',
    'nmb.service': 'NetBIOS',
    'nginx.service': 'Web 服务',
    'docker.service': 'Docker',
    'mosquitto.service': 'MQTT',
    'avahi-daemon.service': 'mDNS',
    'minas.ups_monitor.service': 'UPS 监控',
    'minas.button_monitor.service': '按键监控',
    'minas.netcheck.service': '网络检测',
    'mdmonitor.service': 'RAID 监控',
    'transcode_mgr.service': '转码管理',
}

_log_lock = threading.Lock()


def log(message: str) -> None:
    stamp = time.strftime('%Y-%m-%d %H:%M:%S')
    with _log_lock:
        print(f'[{stamp}] {message}', flush=True)


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def read_text(path: str | Path) -> str:
    try:
        return Path(path).read_text(encoding='utf-8', errors='replace')
    except OSError:
        return ''


def run(command: list[str], timeout: float = 10) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=False, timeout=timeout)


def run_text(command: list[str], timeout: float = 10) -> str:
    try:
        result = run(command, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ''
    return (result.stdout or '').strip()


def round1(value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 1)


# ---------------------------------------------------------------------------
# /proc 解析（纯函数，便于单测）
# ---------------------------------------------------------------------------

def parse_cpu_times(text: str) -> dict[str, int] | None:
    """从 /proc/stat 取第一行汇总时间（单位 jiffies）。"""
    for line in text.splitlines():
        if line.startswith('cpu '):
            parts = [int(item) for item in line.split()[1:] if item.isdigit()]
            if len(parts) < 4:
                return None
            idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
            return {'total': sum(parts), 'idle': idle}
    return None


def cpu_percent(first: dict[str, int] | None, second: dict[str, int] | None) -> float | None:
    if not first or not second:
        return None
    total = second['total'] - first['total']
    idle = second['idle'] - first['idle']
    if total <= 0:
        return None
    busy = max(0.0, min(1.0, (total - idle) / total))
    return round1(busy * 100)


def parse_meminfo(text: str) -> dict[str, int]:
    """返回字节数的字典（原始键名，值已从 kB 换算）。"""
    values: dict[str, int] = {}
    for line in text.splitlines():
        key, separator, rest = line.partition(':')
        if not separator:
            continue
        first = rest.strip().split(' ')[0]
        if first.isdigit():
            values[key.strip()] = int(first) * 1024
    return values


def mem_summary(values: dict[str, int]) -> dict[str, int]:
    total = values.get('MemTotal', 0)
    available = values.get('MemAvailable', values.get('MemFree', 0))
    cached = values.get('Cached', 0) + values.get('SReclaimable', 0) - values.get('Shmem', 0)
    swap_total = values.get('SwapTotal', 0)
    swap_free = values.get('SwapFree', 0)
    return {
        'total': total,
        'available': available,
        'used': max(0, total - available),
        'cached': max(0, cached),
        'buffers': values.get('Buffers', 0),
        'swap_total': swap_total,
        'swap_used': max(0, swap_total - swap_free),
    }


def parse_net_dev(text: str, skip: tuple[str, ...] = ('lo',)) -> dict[str, tuple[int, int]]:
    """网卡累计字节数：{name: (rx, tx)}。"""
    counters: dict[str, tuple[int, int]] = {}
    for line in text.splitlines():
        if ':' not in line or line.startswith('Inter-') or line.startswith(' face'):
            continue
        name, _, rest = line.partition(':')
        name = name.strip()
        if not name or name in skip:
            continue
        fields = rest.split()
        if len(fields) < 9:
            continue
        try:
            counters[name] = (int(fields[0]), int(fields[8]))
        except ValueError:
            continue
    return counters


def parse_diskstats(text: str) -> dict[str, tuple[int, int]]:
    """块设备累计扇区数：{name: (sectors_read, sectors_written)}。"""
    counters: dict[str, tuple[int, int]] = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 10:
            continue
        name = fields[2]
        if not BLOCK_PATTERN.match(name):
            continue
        try:
            counters[name] = (int(fields[5]), int(fields[9]))
        except ValueError:
            continue
    return counters


def rates(first: dict[str, Any] | None, second: dict[str, Any] | None,
          elapsed: float, scale: float = 1.0) -> dict[str, dict[str, float]]:
    """两个计数快照之间的速率（每秒）。缺失的设备按 0 计。"""
    if not first or not second or elapsed <= 0:
        return {}
    result: dict[str, dict[str, float]] = {}
    for name, values in second.items():
        before = first.get(name, (0, 0))
        result[name] = {
            'in': round1(max(0.0, (values[0] - before[0]) * scale / elapsed)),
            'out': round1(max(0.0, (values[1] - before[1]) * scale / elapsed)),
        }
    return result


def parse_mounts(text: str) -> list[dict[str, str]]:
    """真实存储的挂载点（过滤伪文件系统与只读镜像）。"""
    entries: list[dict[str, str]] = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        source, point, fstype, options = fields[0], fields[1], fields[2], fields[3]
        if fstype in PSEUDO_FS:
            continue
        if fstype.startswith('fuse.'):
            continue
        entries.append({
            'source': source,
            'point': _unescape_mount(point),
            'fstype': fstype,
            'options': options,
            'readonly': 'ro' in options.split(','),
        })
    return entries


def _unescape_mount(value: str) -> str:
    return (value.replace('\\040', ' ').replace('\\011', '\t')
            .replace('\\012', '\n').replace('\\134', '\\'))


def parse_mdstat(text: str) -> list[dict[str, Any]]:
    """解析 /proc/mdstat，返回每个阵列的级别、成员与同步进度。"""
    arrays: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in text.splitlines():
        match = re.match(r'^(md\d+)\s*:\s*(\S+)\s*(\S+)?\s*(.*)$', line)
        if match:
            name, state, level, rest = match.groups()
            members = re.findall(r'([a-z0-9]+)\[\d+\]', rest or '')
            current = {
                'name': name,
                'state': state,
                'level': (level or '').strip(),
                'members': members,
                'blocks': None,
                'health': '',
                'sync': '',
            }
            arrays.append(current)
            continue
        if current is None:
            continue
        blocks = re.search(r'(\d+)\s+blocks', line)
        if blocks and current['blocks'] is None:
            current['blocks'] = int(blocks.group(1))
        health = re.search(r'\[(\d+)/(\d+)\]\s*\[([U_]+)\]', line)
        if health:
            current['health'] = f'{health.group(2)}/{health.group(1)} [{health.group(3)}]'
        if 'recovery' in line or 'resync' in line or 'check' in line:
            current['sync'] = ' '.join(line.split())
    return arrays


def parse_systemctl_units(text: str) -> list[dict[str, str]]:
    """解析 `systemctl list-units --type=service --state=running --plain --no-legend`。"""
    units: list[dict[str, str]] = []
    for line in text.splitlines():
        fields = line.split(None, 4)
        if len(fields) < 4 or not fields[0].endswith('.service'):
            continue
        units.append({
            'unit': fields[0],
            'load': fields[1] if len(fields) > 1 else '',
            'active': fields[2] if len(fields) > 2 else '',
            'sub': fields[3] if len(fields) > 3 else '',
            'description': fields[4].strip() if len(fields) > 4 else '',
        })
    return units


def parse_crontab(text: str) -> list[dict[str, str]]:
    """挑出会周期性碰存储的任务（唤醒硬盘的候选）。"""
    watched = ('nasadm', 'balance', 'dsync', 'plugincenter', 'sp_', 'trash', 'clean_temp', 'mediacenterc')
    rows: list[dict[str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if not any(token in line for token in watched):
            continue
        fields = line.split(None, 5)
        if len(fields) < 6:
            continue
        command, tag = fields[5], ''
        if ' #@' in command:
            command, _, tag = command.partition(' #@')
        rows.append({'schedule': ' '.join(fields[:5]), 'command': command.strip(), 'tag': tag.strip()})
    return rows


def parse_hdidle_timeout(*texts: str) -> int | None:
    """从 hdidle 的 unit / drop-in 里取空闲秒数（-i 1800）。"""
    for text in texts:
        match = re.search(r'-i\s+(\d+)', text or '')
        if match:
            return int(match.group(1))
    return None


def plausible_temperature(value: Any) -> float | None:
    """磁盘温度合理区间过滤。ATA 属性 194 的 raw 有时是打包值（如 201864052776），
    直接显示会变成天文数字，所以这里只接受 0–100 ℃。"""
    try:
        number = float(str(value).split(' ')[0])
    except (TypeError, ValueError):
        return None
    return round1(number) if 0 <= number <= 100 else None


def parse_smart_json(payload: dict[str, Any]) -> dict[str, Any]:
    """把 `smartctl -j` 的输出压成界面需要的字段。"""
    out: dict[str, Any] = {
        'model': (payload.get('model_name') or payload.get('device', {}).get('name') or '').strip(),
        'serial': (payload.get('serial_number') or '').strip(),
        'firmware': (payload.get('firmware_version') or '').strip(),
        'rotation': payload.get('rotation_rate'),
        'capacity': payload.get('user_capacity', {}).get('bytes'),
        'health': '',
        'power_on_hours': None,
        'start_stop': None,
        'load_cycle': None,
        'reallocated': None,
        'pending': None,
        'uncorrectable': None,
        'temperature': None,
        'attributes': [],
    }
    health = payload.get('smart_status') or {}
    if isinstance(health, dict) and 'passed' in health:
        out['health'] = 'PASSED' if health['passed'] else 'FAILED'
    # 盘温优先取 smartctl 顶层的结构化字段（ATA/SATA 都会给）
    temperature = payload.get('temperature') or {}
    if isinstance(temperature, dict):
        out['temperature'] = plausible_temperature(temperature.get('current'))
    for table in (payload.get('ata_smart_attributes', {}) or {}).get('table', []) or []:
        name = table.get('name', '')
        raw = table.get('raw', {}) or {}
        entry = {
            'id': table.get('id'),
            'name': name,
            'value': table.get('value'),
            'worst': table.get('worst'),
            'threshold': table.get('thresh'),
            'raw': raw.get('value'),
            'raw_string': raw.get('string'),
            'failing': bool(table.get('when_failed')),
        }
        out['attributes'].append(entry)
        if name == 'Power_On_Hours':
            out['power_on_hours'] = raw.get('value')
        elif name == 'Start_Stop_Count':
            out['start_stop'] = raw.get('value')
        elif name == 'Load_Cycle_Count':
            out['load_cycle'] = raw.get('value')
        elif name == 'Reallocated_Sector_Ct':
            out['reallocated'] = raw.get('value')
        elif name == 'Current_Pending_Sector':
            out['pending'] = raw.get('value')
        elif name == 'Offline_Uncorrectable':
            out['uncorrectable'] = raw.get('value')
        elif name == 'Temperature_Celsius' and out['temperature'] is None:
            out['temperature'] = plausible_temperature(raw.get('value'))
    return out


def parse_smart_text(text: str) -> dict[str, Any]:
    """`smartctl -j` 不可用时的兜底解析（只取关键属性）。"""
    out: dict[str, Any] = {'health': '', 'temperature': None, 'attributes': []}
    match = re.search(r'SMART overall-health self-assessment test result:\s*(\S+)', text)
    if match:
        out['health'] = match.group(1).upper()
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 10 or not fields[0].isdigit():
            continue
        name = fields[1]
        raw = fields[9]
        out['attributes'].append({'id': int(fields[0]), 'name': name, 'raw': raw,
                                  'value': fields[3], 'worst': fields[4], 'threshold': fields[5]})
        if name == 'Power_On_Hours':
            out['power_on_hours'] = raw
        elif name == 'Reallocated_Sector_Ct':
            out['reallocated'] = raw
        elif name == 'Current_Pending_Sector':
            out['pending'] = raw
        elif name == 'Temperature_Celsius' and out['temperature'] is None:
            out['temperature'] = plausible_temperature(raw)
    if 'Device Model:' in text:
        out['model'] = re.search(r'Device Model:\s*(.+)', text).group(1).strip()
    if 'Serial Number:' in text:
        out['serial'] = re.search(r'Serial Number:\s*(.+)', text).group(1).strip()
    if out['health'] == 'FAILED!':
        out['health'] = 'FAILED'
    return out


# ---------------------------------------------------------------------------
# 采样线程：只读 /proc 与 /sys，不产生磁盘 I/O
# ---------------------------------------------------------------------------

class Sampler:
    """每 SAMPLE_INTERVAL 秒取一次 CPU / 内存 / 网络 / 磁盘速率快照。"""

    def __init__(self, interval: float = SAMPLE_INTERVAL, points: int = HISTORY_POINTS) -> None:
        self.interval = interval
        self.history: deque[dict[str, Any]] = deque(maxlen=points)
        self.latest: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._prev_cpu: dict[str, int] | None = None
        self._prev_net: dict[str, tuple[int, int]] = {}
        self._prev_disk: dict[str, tuple[int, int]] = {}
        self._prev_at: float = 0.0
        self._spin_lock = threading.Lock()
        self._spin_cache: dict[str, Any] = {'at': 0.0, 'states': {}}

    # -- 生命周期 ---------------------------------------------------------
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name='sampler', daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.sample()
            except Exception as error:                                  # noqa: BLE001
                log(f'采样失败：{error!r}')
            self._stop.wait(self.interval)

    # -- 采样 -------------------------------------------------------------
    def sample(self) -> dict[str, Any]:
        now = time.time()
        cpu_times = parse_cpu_times(read_text('/proc/stat'))
        net = parse_net_dev(read_text('/proc/net/dev'))
        disk = parse_diskstats(read_text('/proc/diskstats'))
        mem = mem_summary(parse_meminfo(read_text('/proc/meminfo')))
        elapsed = now - self._prev_at if self._prev_at else 0.0
        percentages = cpu_percent(self._prev_cpu, cpu_times)
        net_rates = rates(self._prev_net, net, elapsed, scale=1.0)
        disk_rates = rates(self._prev_disk, disk, elapsed, scale=512.0)
        mem_percent = round(mem['used'] * 100 / mem['total']) if mem['total'] else None
        sample = {
            'at': int(now),
            'cpu': percentages,
            'mem_percent': mem_percent,
            'net': {name: value for name, value in net_rates.items() if name != 'lo'},
            'disk': disk_rates,
        }
        self._prev_cpu, self._prev_net, self._prev_disk, self._prev_at = cpu_times, net, disk, now
        with self._lock:
            self.latest = sample
            self.history.append({'at': sample['at'], 'cpu': percentages, 'mem': mem_percent,
                                 'net': sum((v['in'] + v['out']) for v in sample['net'].values()),
                                 'disk': sum((v['in'] + v['out']) for v in sample['disk'].values())})
        return sample

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self.latest)

    def history_payload(self) -> dict[str, Any]:
        with self._lock:
            points = list(self.history)
        return {
            'interval': self.interval,
            'at': [p['at'] for p in points],
            'cpu': [p['cpu'] for p in points],
            'mem': [p['mem'] for p in points],
            'net': [p['net'] for p in points],
            'disk': [p['disk'] for p in points],
        }

    # -- 硬盘电源状态（hdparm -C 不唤醒盘）--------------------------------
    def spin_states(self) -> dict[str, str]:
        with self._spin_lock:
            if time.time() - self._spin_cache['at'] < SPIN_TTL:
                return dict(self._spin_cache['states'])
        states: dict[str, str] = {}
        for name in physical_disks():
            states[name] = disk_state(name)
        with self._spin_lock:
            self._spin_cache = {'at': time.time(), 'states': states}
        return dict(states)


def physical_disks() -> list[str]:
    """会转的整盘（/dev/sdX、/dev/hdX、/dev/nvmeXnY），已插好设备节点的。"""
    disks: list[str] = []
    base = Path('/sys/block')
    if not base.is_dir():
        return disks
    for entry in sorted(base.iterdir()):
        if not DISK_PATTERN.match(entry.name):
            continue
        if (Path('/dev') / entry.name).exists():
            disks.append(entry.name)
    return disks


def disk_rotational(name: str) -> int | None:
    text = read_text(f'/sys/block/{name}/queue/rotational').strip()
    return int(text) if text.isdigit() else None


def disk_state(name: str) -> str:
    """`hdparm -C` 的电源状态；该 ioctl 不会把盘唤醒。"""
    output = run_text([HDPARM, '-C', f'/dev/{name}'], timeout=8)
    match = re.search(r'drive state is:\s*(.+)', output)
    return match.group(1).strip().lower() if match else 'unknown'


def disk_temperature(name: str) -> float | None:
    """内核 drivetemp hwmon 报的盘温（读的是驱动缓存值）。"""
    try:
        for hwmon in sorted(Path('/sys/class/hwmon').glob('hwmon*')):
            if read_text(hwmon / 'name').strip() != 'drivetemp':
                continue
            block = (hwmon / 'device').resolve()
            if block.name != name:
                continue
            raw = read_text(hwmon / 'temp1_input').strip()
            if raw.lstrip('-').isdigit():
                return round1(int(raw) / 1000)
    except OSError:
        return None
    return None


# ---------------------------------------------------------------------------
# 磁盘 / SMART（低频，带缓存）
# ---------------------------------------------------------------------------

_drive_lock = threading.Lock()
_drive_cache: dict[str, Any] = {'at': 0.0, 'payload': None}


def read_smart(name: str) -> dict[str, Any]:
    """读一块盘的 SMART；standby 时直接跳过（不唤醒）。"""
    try:
        result = run([SMARTCTL, '-j', '-n', 'standby,0', '-i', '-A', '-H', f'/dev/{name}'], timeout=25)
    except (OSError, subprocess.SubprocessError) as error:
        return {'error': f'无法执行 smartctl：{error}'}
    text = result.stdout or ''
    if result.returncode & 0x02 and not text.strip():
        return {'skipped': 'standby'}
    if text.strip().startswith('{'):
        try:
            parsed = parse_smart_json(json.loads(text))
            parsed['exit'] = result.returncode
            return parsed
        except json.JSONDecodeError:
            pass
    if 'STANDBY' in text.upper() and 'Device is in STANDBY mode' in text:
        return {'skipped': 'standby'}
    parsed = parse_smart_text(text)
    parsed['exit'] = result.returncode
    return parsed


def disk_payload(name: str, state: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        'dev': name,
        'rotational': disk_rotational(name),
        'state': state,
        'standby': state in ('standby', 'sleeping'),
        'temperature': None,
        'model': read_text(f'/sys/block/{name}/device/model').strip(),
    }
    if not payload['standby']:
        # 盘温优先用内核 drivetemp（读的是驱动缓存值），SMART 只作兜底
        payload['temperature'] = disk_temperature(name)
        smart = read_smart(name)
        if 'skipped' in smart:
            payload['skipped'] = smart['skipped']
        else:
            for key, value in smart.items():
                if key == 'attributes':
                    payload['attributes'] = [
                        item for item in value
                        if item.get('name') in (
                            'Reallocated_Sector_Ct', 'Current_Pending_Sector',
                            'Offline_Uncorrectable', 'Power_On_Hours',
                            'Start_Stop_Count', 'Load_Cycle_Count',
                            'Temperature_Celsius', 'Power_Cycle_Count',
                        )]
                elif key == 'temperature':
                    if payload['temperature'] is None:
                        payload['temperature'] = value
                else:
                    payload[key] = value
    if not payload.get('model'):
        payload['model'] = read_text(f'/sys/block/{name}/device/vendor').strip()
    size = read_text(f'/sys/block/{name}/size').strip()
    if size.isdigit():
        payload['bytes'] = int(size) * 512
    return payload


def storage_payload(sampler: Sampler) -> dict[str, Any]:
    """磁盘、分区、阵列与容量；SMART 结果缓存 DRIVE_TTL 秒。"""
    states = sampler.spin_states()
    with _drive_lock:
        fresh = time.time() - _drive_cache['at'] < DRIVE_TTL and _drive_cache['payload'] is not None
        if fresh:
            payload = json.loads(json.dumps(_drive_cache['payload']))
    if not fresh:
        drives = [disk_payload(name, states.get(name, 'unknown')) for name in physical_disks()]
        mounts = []
        for entry in parse_mounts(read_text('/proc/mounts')):
            if is_own_mount(entry['point']):
                continue
            usage = filesystem_usage(entry['point'])
            if usage is None:
                continue
            entry.update(usage)
            mounts.append(entry)
        payload = {
            'drives': drives,
            'mounts': mounts,
            'arrays': parse_mdstat(read_text('/proc/mdstat')),
            'at': int(time.time()),
        }
        with _drive_lock:
            _drive_cache['at'] = time.time()
            _drive_cache['payload'] = payload
    payload = json.loads(json.dumps(payload))
    payload['states'] = states
    payload['cached_at'] = payload.get('at')
    return payload


def filesystem_usage(point: str) -> dict[str, int] | None:
    try:
        stat = os.statvfs(point)
    except OSError:
        return None
    total = stat.f_blocks * stat.f_frsize
    if total <= 0:
        return None
    free = stat.f_bavail * stat.f_frsize
    used = total - stat.f_bfree * stat.f_frsize
    percent = round(used * 100 / total) if total else 0
    return {'total': total, 'used': used, 'free': free, 'percent': percent}


# ---------------------------------------------------------------------------
# Docker（只读，经 /var/run/docker.sock 的 Engine API）
# ---------------------------------------------------------------------------

class _UDSConnection(HTTPConnection):
    def __init__(self, socket_path: str, timeout: float = 8) -> None:
        super().__init__('localhost', timeout=timeout)
        self._socket_path = socket_path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._socket_path)
        self.sock = sock


class DockerClient:
    """只发 GET，绝不启停容器。"""

    def __init__(self, socket_path: str = DOCKER_SOCKET, timeout: float = 8) -> None:
        self.socket_path = socket_path
        self.timeout = timeout

    def get(self, path: str) -> Any:
        connection = _UDSConnection(self.socket_path, self.timeout)
        try:
            connection.request('GET', path)
            response = connection.getresponse()
            body = response.read()
            if response.status >= 400:
                raise RuntimeError(f'docker api {path} -> {response.status}')
            return json.loads(body.decode('utf-8', 'replace'))
        finally:
            connection.close()

    def available(self) -> bool:
        return os.path.exists(self.socket_path)


_docker_lock = threading.Lock()
_docker_cache: dict[str, Any] = {'at': 0.0, 'payload': None}


def parse_container_stats(payload: dict[str, Any]) -> dict[str, Any]:
    cpu = payload.get('cpu_stats', {}) or {}
    pre = payload.get('precpu_stats', {}) or {}
    cpu_total = (cpu.get('cpu_usage', {}) or {}).get('total_usage', 0)
    pre_total = (pre.get('cpu_usage', {}) or {}).get('total_usage', 0)
    system_total = cpu.get('system_cpu_usage') or 0
    pre_system = pre.get('system_cpu_usage') or 0
    online = (cpu.get('online_cpus') or len((cpu.get('cpu_usage', {}) or {}).get('percpu_usage') or []) or 1)
    percent = None
    if system_total > pre_system and cpu_total >= pre_total:
        percent = round1((cpu_total - pre_total) / (system_total - pre_system) * online * 100)
    memory = payload.get('memory_stats', {}) or {}
    networks = payload.get('networks', {}) or {}
    blkio = (payload.get('blkio_stats', {}) or {}).get('io_service_bytes_recursive') or []
    block_read = sum(item.get('value', 0) for item in blkio if item.get('op') == 'Read')
    block_write = sum(item.get('value', 0) for item in blkio if item.get('op') == 'Write')
    return {
        'cpu': percent,
        'mem': memory.get('usage'),
        'mem_limit': memory.get('limit'),
        'net_rx': sum(item.get('rx_bytes', 0) for item in networks.values()),
        'net_tx': sum(item.get('tx_bytes', 0) for item in networks.values()),
        'block_read': block_read,
        'block_write': block_write,
        'pids': (payload.get('pids_stats', {}) or {}).get('current'),
    }


def docker_payload(with_stats: bool = True) -> dict[str, Any]:
    client = DockerClient()
    if not client.available():
        return {'active': False, 'error': 'Docker 未运行（找不到 /var/run/docker.sock）', 'containers': []}
    with _docker_lock:
        cached = _docker_cache['payload']
        if cached and time.time() - _docker_cache['at'] < DOCKER_TTL and (not with_stats or cached.get('with_stats')):
            return json.loads(json.dumps(cached))
    try:
        version = client.get('/version')
        info = client.get('/info')
        containers = client.get('/containers/json?all=1')
    except Exception as error:                                          # noqa: BLE001
        return {'active': True, 'error': f'Docker API 读取失败：{error}', 'containers': []}
    rows = []
    for item in containers:
        labels = item.get('Labels') or {}
        ports = []
        for port in item.get('Ports') or []:
            if port.get('PublicPort'):
                ports.append(f"{port.get('PublicPort')}→{port.get('PrivatePort')}/{port.get('Type', 'tcp')}")
        row = {
            'id': (item.get('Id') or '')[:12],
            'name': (item.get('Names') or ['?'])[0].lstrip('/'),
            'image': item.get('Image', ''),
            'state': item.get('State', ''),
            'status': item.get('Status', ''),
            'created': item.get('Created'),
            'ports': ports,
            'compose': labels.get('com.docker.compose.project', ''),
            'system': 'miot_central' in (item.get('Names') or [''])[0],
        }
        rows.append(row)
    if with_stats:
        for row in rows:
            if row['state'] != 'running':
                continue
            try:
                row.update(parse_container_stats(client.get(f"/containers/{row['id']}/stats?stream=false")))
            except Exception:                                           # noqa: BLE001
                row['stats_error'] = True
    payload = {
        'active': True,
        'version': (version or {}).get('Version', ''),
        'api_version': (version or {}).get('ApiVersion', ''),
        'images': (info or {}).get('Images'),
        'containers_total': (info or {}).get('Containers'),
        'containers_running': (info or {}).get('ContainersRunning'),
        'containers': rows,
        'with_stats': with_stats,
        'at': int(time.time()),
    }
    with _docker_lock:
        _docker_cache['at'] = time.time()
        _docker_cache['payload'] = payload
    return json.loads(json.dumps(payload))


# ---------------------------------------------------------------------------
# 服务、插件与系统设置
# ---------------------------------------------------------------------------

_service_lock = threading.Lock()
_service_cache: dict[str, Any] = {'at': 0.0, 'payload': None}


def plugin_records() -> list[dict[str, Any]]:
    """已安装插件：注册表 + INFO 的真实版本。"""
    records: dict[str, dict[str, Any]] = {}
    for path in sorted(PLUGIN_DIR.glob('u*.list')):
        user = path.name[:-5]
        try:
            registry = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(registry, dict):
            continue
        for key, value in registry.items():
            if not isinstance(value, dict):
                continue
            info = value.get('info', {}) if isinstance(value.get('info'), dict) else {}
            entry = records.setdefault(key, {
                'key': key,
                'name': info.get('name', key),
                'version': info.get('version', ''),
                'id': info.get('id'),
                'status': value.get('status', ''),
                'enable': bool(value.get('enable')),
                'install': bool(value.get('install')),
                'port': info.get('port'),
                'icon': value.get('icon', ''),
                'users': [],
                'unit': '',
                'unit_active': '',
                'ui_version': '',
            })
            entry['users'].append(user)
            info_path = HOME_ROOT / user / 'plugin' / key / 'INFO'
            try:
                ui_info = json.loads(info_path.read_text(encoding='utf-8'))
                entry['ui_version'] = str(ui_info.get('version', ''))
            except (OSError, json.JSONDecodeError):
                pass
    units = {path.name: path.name for path in Path('/etc/systemd/system').glob('xiaomi-*.service')}
    for entry in records.values():
        candidates = [unit for unit in units if _unit_matches(unit, entry['key'])]
        if candidates:
            entry['unit'] = sorted(candidates, key=len)[0]
    active = run_text(['systemctl', 'is-active'] + [e['unit'] for e in records.values() if e['unit']], timeout=10)
    states = active.splitlines() if active else []
    for entry, state in zip([e for e in records.values() if e['unit']], states):
        entry['unit_active'] = state.strip()
    return sorted(records.values(), key=lambda item: (str(item.get('id')), item['key']))


def _unit_matches(unit: str, key: str) -> bool:
    stem = unit[:-len('.service')].replace('xiaomi-', '').replace('-', '')
    return stem == key or stem.startswith(key) or key.startswith(stem)


def services_payload(sampler: Sampler) -> dict[str, Any]:
    with _service_lock:
        cached = _service_cache['payload']
        if cached and time.time() - _service_cache['at'] < SERVICE_TTL:
            return json.loads(json.dumps(cached))
    units = parse_systemctl_units(run_text(
        ['systemctl', 'list-units', '--type=service', '--state=running', '--plain', '--no-legend'],
        timeout=15))
    watched_names = list(WATCHED_SERVICES)
    states = run_text(['systemctl', 'is-active'] + watched_names, timeout=15).splitlines()
    watched = []
    for name, state in zip(watched_names, states):
        watched.append({
            'unit': name,
            'label': WATCHED_SERVICES[name],
            'active': state.strip() or 'unknown',
        })
    payload = {
        'units': units,
        'watched': watched,
        'plugins': plugin_records(),
        'settings': settings_payload(),
        'crontab': parse_crontab(run_text(['crontab', '-l'], timeout=10)),
        'at': int(time.time()),
    }
    with _service_lock:
        _service_cache['at'] = time.time()
        _service_cache['payload'] = payload
    return json.loads(json.dumps(payload))


def settings_payload() -> dict[str, Any]:
    hibernate = run_text(['uci', 'get', 'system.disk.hibernate'], timeout=6)
    fan_enable = run_text(['uci', 'get', 'system.fan.enable'], timeout=6)
    fan_mode = run_text(['uci', 'get', 'system.fan.mode'], timeout=6)
    unit = read_text('/usr/lib/systemd/system/hdidle.service')
    drop_ins = ''.join(read_text(path) for path in
                       sorted(Path('/etc/systemd/system/hdidle.service.d').glob('*.conf')))
    timeout_seconds = parse_hdidle_timeout(drop_ins, unit)
    return {
        'hibernate': hibernate == '1',
        'hibernate_timeout_minutes': round(timeout_seconds / 60) if timeout_seconds else None,
        'hibernate_source': 'drop-in' if parse_hdidle_timeout(drop_ins) else 'system',
        'fan_enable': fan_enable == '1',
        'fan_mode': fan_mode or 'unknown',
        'model': read_text('/proc/device-tree/model').strip('\x00').strip(),
        'kernel': platform.release(),
        'hostname': socket.gethostname(),
    }


# ---------------------------------------------------------------------------
# 已安装插件的桌面图标
#
# 插件页面本来只挂在 443（小米客户端证书 + 令牌）。桌面入口的 nginx server 块里
# include 了同一批 /plugin/... 配置，所以这些页面在 8085 上也能打开——桌面端因此
# 可以用 iframe 把它们当成"应用窗口"来用。这里负责列出插件、判断哪些有网页界面，
# 并探测一次页面是否真的能打开（结果缓存 5 分钟，探测走本机回环，不碰硬盘）。
# ---------------------------------------------------------------------------

PLUGIN_ICON_DIR = Path(os.environ.get('PLUGIN_ICON_DIR', '/data/plugin/www/icon'))
PLUGIN_PROBE_TTL = float(os.environ.get('PLUGIN_PROBE_TTL', '300'))
_plugin_lock = threading.Lock()
_plugin_cache: dict[str, Any] = {'at': 0.0, 'payload': None}


def _probe_plugin(path: str) -> int:
    """本机探测插件页面状态码；0 表示连不上（比如桌面入口被关掉了）。"""
    request = urllib.request.Request(f'http://127.0.0.1:{LAN_PORT}{path}')
    try:
        with urllib.request.urlopen(request, timeout=4) as response:
            response.read(512)
            return int(response.status)
    except urllib.error.HTTPError as error:
        return int(error.code)
    except Exception:                                                       # noqa: BLE001
        return 0


def plugin_catalog() -> dict[str, Any]:
    with _plugin_lock:
        cached = _plugin_cache['payload']
        if cached and time.time() - _plugin_cache['at'] < PLUGIN_PROBE_TTL:
            return json.loads(json.dumps(cached))

    items: list[dict[str, Any]] = []
    for record in plugin_records():
        key = str(record.get('key') or '')
        if not key or key == 'nasconsole':            # 控制台自己就是桌面，不用再摆一个图标
            continue
        users = [str(user) for user in (record.get('users') or [])]
        owner = ''
        for user in users:
            if (HOME_ROOT / user / 'plugin' / key / 'src' / 'ui').is_dir():
                owner = user
                break
        owner = owner or (users[-1] if users else '')
        has_ui = bool(owner) and (HOME_ROOT / owner / 'plugin' / key / 'src' / 'ui' / 'index.html').is_file()
        web_path = f'/plugin/{owner}/{key}/index.html' if (has_ui and owner) else ''
        if not web_path or _probe_plugin(web_path) != 200:
            # 只有小米客户端里有界面的应用（影视/中枢/百度网盘…）桌面端打不开，
            # 干脆不摆图标——它们仍然会在「应用」窗口的清单里。
            continue
        icon_name = Path(str(record.get('icon') or '').split('?')[0]).name
        icon = icon_name if icon_name and (PLUGIN_ICON_DIR / icon_name).is_file() else ''
        items.append({
            'key': key,
            'name': record.get('name') or key,
            'id': record.get('id'),
            'version': record.get('version') or '',
            'owner': owner,
            'icon': icon,
            'web_path': web_path,
            'web_status': 200,
            'web_ready': True,
            'unit_active': record.get('unit_active') or '',
        })
    items.sort(key=lambda item: str(item.get('id')))
    payload = {
        'plugins': items,
        'web_ready': len(items),
        'total': len(items),
        'at': int(time.time()),
    }
    with _plugin_lock:
        _plugin_cache['at'] = time.time()
        _plugin_cache['payload'] = payload
    return json.loads(json.dumps(payload))


# ---------------------------------------------------------------------------
# 文件浏览（可浏览、可预览、可下载；上传/新建/重命名/删除见下面的"写操作"一节）
#
# 只允许在这些根目录里操作；所有路径都要 resolve 之后再判断是否落在根目录内，
# 因此符号链接指到根目录外也会被拒绝。
# ---------------------------------------------------------------------------

FILE_ROOTS: list[tuple[str, str]] = [
    (path, label) for path, label in (
        (os.environ.get('FILE_ROOT_POOL', '/nas/pool0'), '共享空间'),
        ('/nas/mnt/pa0', '数据盘 1'),
        ('/nas/mnt/pa1', '数据盘 2'),
        ('/nas/sys', '系统库'),
        (os.environ.get('FILE_ROOT_USB', '/mnt'), '外接存储（USB）'),
        ('/home', '用户目录'),
        ('/data', '内部数据'),
        ('/log', '日志'),
    ) if path
]
FILE_ENTRY_LIMIT = int(os.environ.get('FILE_ENTRY_LIMIT', '3000'))
FILE_TEXT_LIMIT = int(os.environ.get('FILE_TEXT_LIMIT', str(256 * 1024)))
FILE_INLINE_LIMIT = int(os.environ.get('FILE_INLINE_LIMIT', str(64 * 1024 * 1024)))

INLINE_IMAGE_TYPES = {
    '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.png': 'image/png',
    '.gif': 'image/gif', '.webp': 'image/webp', '.bmp': 'image/bmp',
    '.avif': 'image/avif',
}
TEXT_TYPES = {
    '.txt': 'text/plain; charset=utf-8', '.md': 'text/plain; charset=utf-8',
    '.log': 'text/plain; charset=utf-8', '.json': 'text/plain; charset=utf-8',
    '.yml': 'text/plain; charset=utf-8', '.yaml': 'text/plain; charset=utf-8',
    '.conf': 'text/plain; charset=utf-8', '.ini': 'text/plain; charset=utf-8',
    '.csv': 'text/plain; charset=utf-8', '.sh': 'text/plain; charset=utf-8',
    '.py': 'text/plain; charset=utf-8', '.js': 'text/plain; charset=utf-8',
    '.css': 'text/plain; charset=utf-8', '.xml': 'text/plain; charset=utf-8',
    '.toml': 'text/plain; charset=utf-8', '.env': 'text/plain; charset=utf-8',
}
BINARY_TYPES = {
    '.mp4': 'video/mp4', '.mkv': 'video/x-matroska', '.mov': 'video/quicktime',
    '.mp3': 'audio/mpeg', '.flac': 'audio/flac', '.m4a': 'audio/mp4',
    '.pdf': 'application/pdf', '.zip': 'application/zip', '.7z': 'application/x-7z-compressed',
    '.rar': 'application/vnd.rar', '.gz': 'application/gzip', '.tar': 'application/x-tar',
    '.iso': 'application/x-iso9660-image', '.apk': 'application/vnd.android.package-archive',
    '.epub': 'application/epub+zip',
}


def file_shortcuts() -> list[dict[str, Any]]:
    rows = []
    for path, label in FILE_ROOTS:
        try:
            exists = Path(path).is_dir()
        except OSError:
            exists = False
        rows.append({'path': path, 'label': label, 'exists': exists})
    return rows


def _root_pairs() -> list[tuple[Path, str]]:
    pairs = []
    for path, label in FILE_ROOTS:
        candidate = Path(path)
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved.is_dir():
            pairs.append((resolved, label))
    return pairs


def resolve_user_path(raw: str | None) -> Path:
    """把用户给的路径变成真实路径，并确保它落在允许的根目录里。"""
    if not raw:
        raise RuntimeError('路径必须是绝对路径')
    candidate = Path(raw)
    if not candidate.is_absolute():
        raise RuntimeError('路径必须是绝对路径')
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as error:
        raise RuntimeError('路径不存在') from error
    except OSError as error:
        raise RuntimeError(f'无法访问该路径：{error}') from error
    for root, _label in _root_pairs():
        if resolved == root or root in resolved.parents:
            return resolved
    raise RuntimeError('路径不在允许浏览的范围内')


def _entry_kind(entry: os.DirEntry[str]) -> str:
    try:
        if entry.is_symlink():
            return 'link'
        if entry.is_dir(follow_symlinks=False):
            return 'dir'
        if entry.is_file(follow_symlinks=False):
            return 'file'
    except OSError:
        return 'other'
    return 'other'


def file_kind(path: Path) -> str:
    extension = path.suffix.lower()
    if extension in INLINE_IMAGE_TYPES:
        return 'image'
    if extension in TEXT_TYPES:
        return 'text'
    if extension in BINARY_TYPES:
        return 'media' if BINARY_TYPES[extension].startswith(('video', 'audio')) else 'archive'
    return 'file'


def list_directory(path: Path) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    truncated = False
    try:
        scanner = os.scandir(path)
    except PermissionError as error:
        raise RuntimeError('没有权限读取这个目录') from error
    except OSError as error:
        raise RuntimeError(f'无法读取目录：{error}') from error
    with scanner:
        for entry in scanner:
            if len(entries) >= FILE_ENTRY_LIMIT:
                truncated = True
                break
            kind = _entry_kind(entry)
            try:
                info = entry.stat(follow_symlinks=False)
                size = info.st_size
                mtime = int(info.st_mtime)
            except OSError:
                size = None
                mtime = None
            entry_path = Path(entry.path)
            rows_kind = kind
            if kind == 'link':
                try:
                    target = entry_path.resolve(strict=True)
                    rows_kind = 'dir' if target.is_dir() else 'file'
                except OSError:
                    rows_kind = 'link'
            entries.append({
                'name': entry.name,
                'kind': rows_kind,
                'link': kind == 'link',
                'size': None if rows_kind == 'dir' else size,
                'mtime': mtime,
                'hidden': entry.name.startswith('.'),
                'type': file_kind(entry_path) if rows_kind == 'file' else 'dir',
            })
    entries.sort(key=lambda item: (item['kind'] != 'dir', item['name'].lower()))
    parent = None
    if path.parent != path:
        try:
            resolved_parent = path.parent.resolve()
            for root, _label in _root_pairs():
                if resolved_parent == root or root in resolved_parent.parents:
                    parent = str(path.parent)
                    break
        except OSError:
            parent = None
    return {
        'path': str(path),
        'name': path.name or str(path),
        'parent': parent,
        'entries': entries,
        'truncated': truncated,
        'limit': FILE_ENTRY_LIMIT,
        'readonly': True,
        'shortcuts': file_shortcuts(),
        'at': int(time.time()),
    }


def read_text_preview(path: Path, limit: int | None = None) -> dict[str, Any]:
    budget = FILE_TEXT_LIMIT if limit is None else limit
    try:
        size = path.stat().st_size
    except OSError as error:
        raise RuntimeError(f'无法读取文件：{error}') from error
    if not path.is_file():
        raise RuntimeError('不是普通文件')
    with path.open('rb') as handle:
        raw = handle.read(budget)
    return {
        'path': str(path),
        'name': path.name,
        'size': size,
        'truncated': size > len(raw),
        'text': raw.decode('utf-8', 'replace'),
    }


def stream_size(path: Path, limit: int | None = None) -> int:
    size = path.stat().st_size
    if limit is not None:
        return min(size, limit)
    return size


def inline_headers_for(path: Path) -> tuple[str, str]:
    """返回（Content-Type, Content-Disposition）。

    只有白名单里的图片允许 inline；其它一律 attachment —— 否则在同一个源上
    内联渲染 HTML/SVG 之类的文件等于给自己开 XSS。
    """
    extension = path.suffix.lower()
    if extension in INLINE_IMAGE_TYPES:
        return INLINE_IMAGE_TYPES[extension], 'inline'
    return TEXT_TYPES.get(extension, 'application/octet-stream'), 'attachment'


def download_headers_for(path: Path) -> tuple[str, str]:
    extension = path.suffix.lower()
    content_type = (INLINE_IMAGE_TYPES.get(extension)
                    or TEXT_TYPES.get(extension)
                    or BINARY_TYPES.get(extension)
                    or 'application/octet-stream')
    return content_type, 'attachment'


# ---------------------------------------------------------------------------
# 写操作：上传 / 新建目录 / 重命名 / 删除（删除=移到本插件自己的回收站）
#
# 关于回收站：厂商自己的 `. #trash/fmgr/{files,info}` 是由 filemgr 的索引维护的，
# 手工把文件挪进去，客户端的「回收站」也列不出来（索引里没有），只会变成看不见的
# 垃圾。所以这里用我们自己的 `.console-trash/`：同盘 rename（瞬间完成）、
# index.json 记录原路径，文件窗口里可以恢复或彻底删除。
# ---------------------------------------------------------------------------

TRASH_DIR_NAME = '.console-trash'
NAME_MAX_BYTES = 255
UPLOAD_CHUNK = 256 * 1024


def validate_name(name: Any) -> str:
    if not isinstance(name, str):
        raise RuntimeError('名字无效')
    clean = name.strip()
    if not clean or clean in ('.', '..'):
        raise RuntimeError('名字不能为空')
    if '/' in clean or '\\' in clean or '\x00' in clean:
        raise RuntimeError('名字里不能包含路径分隔符')
    if len(clean.encode('utf-8')) > NAME_MAX_BYTES:
        raise RuntimeError('名字过长')
    return clean


def file_root_for(path: Path) -> Path | None:
    for root, _label in _root_pairs():
        if path == root or root in path.parents:
            return root
    return None


def is_root_path(path: Path) -> bool:
    return file_root_for(path) == path


def inside_roots(path: Path) -> bool:
    if file_root_for(path) is not None:
        return True
    try:
        return file_root_for(path.resolve()) is not None
    except OSError:
        return False


def ensure_in_roots(path: Path, what: str = '路径') -> None:
    """写操作的纵深防御：HTTP 层已经 resolve 过一次，这里再确认一遍。"""
    if not inside_roots(path):
        raise RuntimeError(f'{what}不在允许操作的范围内')


def in_trash(path: Path) -> bool:
    return TRASH_DIR_NAME in path.parts


def trash_dir(root: Path) -> Path:
    return root / TRASH_DIR_NAME


def _trash_index_path(root: Path) -> Path:
    return trash_dir(root) / 'index.json'


def _load_trash_index(root: Path) -> dict[str, Any]:
    try:
        payload = json.loads(_trash_index_path(root).read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        payload = {}
    items = payload.get('items') if isinstance(payload, dict) else None
    return {'items': items if isinstance(items, list) else []}


def _save_trash_index(root: Path, index: dict[str, Any]) -> None:
    target = _trash_index_path(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(target, json.dumps(index, ensure_ascii=False, indent=2) + '\n')


def _entry_count(path: Path) -> int:
    if path.is_file():
        return 1
    count = 0
    for _root, dirs, files in os.walk(path):
        count += len(dirs) + len(files)
    return count


def move_to_trash(path: Path) -> dict[str, Any]:
    if is_root_path(path):
        raise RuntimeError('不能删除存储根目录')
    if in_trash(path):
        raise RuntimeError('回收站里的内容请用「彻底删除」')
    root = file_root_for(path)
    if root is None:
        raise RuntimeError('路径不在允许的范围内')
    files_dir = trash_dir(root) / 'files'
    files_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    entry_id = f'{stamp}-{secrets.token_hex(3)}'
    while (files_dir / entry_id).exists():
        entry_id = f'{stamp}-{secrets.token_hex(3)}'
    trashed = files_dir / entry_id
    kind = 'dir' if path.is_dir() else 'file'
    size = None if kind == 'dir' else path.stat().st_size
    os.rename(path, trashed)                      # 同一文件系统，瞬间完成
    item = {
        'id': entry_id,
        'root': str(root),
        'name': path.name,
        'original': str(path),
        'trashed': str(trashed),
        'kind': kind,
        'size': size,
        'at': int(time.time()),
    }
    index = _load_trash_index(root)
    index['items'].insert(0, item)
    _save_trash_index(root, index)
    log(f'移到回收站：{path} -> {trashed}')
    return item


def trash_payload() -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for root, _label in _root_pairs():
        index = _load_trash_index(root)
        for item in index['items']:
            if not Path(str(item.get('trashed', ''))).exists():
                continue
            items.append(item)
    items.sort(key=lambda item: int(item.get('at') or 0), reverse=True)
    return {'items': items, 'total': len(items), 'at': int(time.time())}


def restore_from_trash(entry_id: str) -> dict[str, Any]:
    for root, _label in _root_pairs():
        index = _load_trash_index(root)
        for item in list(index['items']):
            if item.get('id') != entry_id:
                continue
            source = Path(str(item['trashed']))
            target = Path(str(item['original']))
            if not source.exists():
                index['items'].remove(item)
                _save_trash_index(root, index)
                raise RuntimeError('回收站里的内容已经不存在了')
            if target.exists():
                raise RuntimeError(f'{target} 已经存在，无法恢复')
            target.parent.mkdir(parents=True, exist_ok=True)
            os.rename(source, target)
            index['items'].remove(item)
            _save_trash_index(root, index)
            log(f'从回收站恢复：{source} -> {target}')
            return {'ok': True, 'path': str(target)}
    raise RuntimeError('回收站里没有这一项')


def purge_trash(entry_ids: list[str] | None = None) -> dict[str, Any]:
    removed: list[str] = []
    freed = 0
    for root, _label in _root_pairs():
        index = _load_trash_index(root)
        keep: list[dict[str, Any]] = []
        for item in index['items']:
            entry_id = str(item.get('id'))
            if entry_ids and entry_id not in entry_ids:
                keep.append(item)
                continue
            target = Path(str(item.get('trashed', '')))
            if target.exists():
                freed += _tree_size(target)
                shutil.rmtree(target, ignore_errors=True) if target.is_dir() else target.unlink(missing_ok=True)
            removed.append(entry_id)
        if len(keep) != len(index['items']):
            index['items'] = keep
            _save_trash_index(root, index)
    return {'removed': removed, 'freed': freed}


def _tree_size(path: Path) -> int:
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


def create_folder(parent: Path, name: str) -> dict[str, Any]:
    ensure_in_roots(parent)
    if not parent.is_dir():
        raise RuntimeError('目标不是目录')
    clean = validate_name(name)
    target = parent / clean
    if target.exists():
        raise RuntimeError('同名文件或目录已存在')
    target.mkdir(parents=False)
    log(f'新建目录：{target}')
    return {'ok': True, 'path': str(target), 'name': clean}


def rename_entry(path: Path, name: str) -> dict[str, Any]:
    ensure_in_roots(path)
    if is_root_path(path):
        raise RuntimeError('不能重命名存储根目录')
    if in_trash(path):
        raise RuntimeError('回收站里的内容不能重命名')
    clean = validate_name(name)
    if clean == path.name:
        return {'ok': True, 'path': str(path), 'name': clean, 'unchanged': True}
    target = path.parent / clean
    if target.exists():
        raise RuntimeError('同名文件或目录已存在')
    os.rename(path, target)
    log(f'重命名：{path} -> {target}')
    return {'ok': True, 'path': str(target), 'name': clean}


def save_upload(directory: Path, name: str, stream: Any, length: int, overwrite: bool = False) -> dict[str, Any]:
    ensure_in_roots(directory)
    if not directory.is_dir():
        raise RuntimeError('目标不是目录')
    clean = validate_name(name)
    target = directory / clean
    if target.is_dir():
        raise RuntimeError('同名目录已存在')
    if target.exists() and not overwrite:
        raise RuntimeError('同名文件已存在')
    temporary = directory / f'.{clean}.uploading-{os.getpid()}'
    written = 0
    try:
        with temporary.open('wb') as handle:
            while written < length:
                chunk = stream.read(min(UPLOAD_CHUNK, length - written))
                if not chunk:
                    break
                handle.write(chunk)
                written += len(chunk)
        if written != length:
            raise RuntimeError('上传中断，文件不完整')
        os.replace(temporary, target)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise
    log(f'上传：{target}（{written} 字节）')
    return {'ok': True, 'path': str(target), 'name': clean, 'size': written}


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------

def host_payload() -> dict[str, Any]:
    uptime = 0.0
    try:
        uptime = float(read_text('/proc/uptime').split()[0])
    except (IndexError, ValueError):
        pass
    loads = [0.0, 0.0, 0.0]
    try:
        loads = [float(item) for item in read_text('/proc/loadavg').split()[:3]]
    except ValueError:
        pass
    freq_mhz = None
    raw_freq = read_text('/sys/devices/system/cpu/cpufreq/policy0/scaling_cur_freq').strip()
    if raw_freq.isdigit():
        freq_mhz = round(int(raw_freq) / 1000)
    max_freq = None
    raw_max = read_text('/sys/devices/system/cpu/cpufreq/policy0/cpuinfo_max_freq').strip()
    if raw_max.isdigit():
        max_freq = round(int(raw_max) / 1000)
    cores = sum(1 for line in read_text('/proc/cpuinfo').splitlines() if line.startswith('processor'))
    return {
        'uptime': int(uptime),
        'loads': loads,
        'cores': cores,
        'freq_mhz': freq_mhz,
        'freq_max_mhz': max_freq,
        'governor': read_text('/sys/devices/system/cpu/cpufreq/policy0/scaling_governor').strip(),
        'cpu_temp': cpu_temperature(),
        'hostname': socket.gethostname(),
        'kernel': platform.release(),
        'model': read_text('/proc/device-tree/model').strip('\x00').strip(),
    }


def cpu_temperature() -> float | None:
    for zone in sorted(Path('/sys/class/thermal').glob('thermal_zone*')):
        if read_text(zone / 'type').strip() not in ('cpu-thermal', 'cpu_thermal', 'soc-thermal'):
            continue
        raw = read_text(zone / 'temp').strip()
        if raw.lstrip('-').isdigit():
            return round1(int(raw) / 1000)
    return None


# ---------------------------------------------------------------------------
# 桌面 web 入口：地址探测 + 开关
#
# 这是整个插件唯一的写操作，而且只碰一个文件——它自己的 nginx 入口配置
# （/etc/nginx/conf.d/xiaomi-nas-console-lan.conf）。启用时从插件目录里的模板
# 原子写入，停用时删除；每次改动都先 `nginx -t`，失败就回滚，绝不留下坏配置。
# ---------------------------------------------------------------------------

LAN_CONF = Path(os.environ.get('LAN_CONF', '/etc/nginx/conf.d/xiaomi-nas-console-lan.conf'))
# 入口模板默认取本 release 目录里的那份（安装包与脚本安装都放在 <release>/lan/ 下），
# 单元文件里的 LAN_TEMPLATE 指向 current/lan/…，两条路都落到同一个文件。
LAN_TEMPLATE = Path(os.environ.get('LAN_TEMPLATE',
                                   RELEASE_DIR / 'lan' / 'xiaomi-nas-console-lan.conf'))
LAN_PORT = int(os.environ.get('LAN_PORT', '5001'))
ADMIN_TOKEN_FILE = Path(os.environ.get('ADMIN_TOKEN_FILE',
                                       '/data/plugin/xiaomi-nas-console/admin-token'))
STATE_FILE = Path(os.environ.get('STATE_FILE', '/data/plugin/xiaomi-nas-console/state.json'))

_toggle_lock = threading.Lock()


def atomic_write(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(text, encoding='utf-8')
    os.chmod(temporary, mode)
    temporary.replace(path)


def primary_ipv4() -> str:
    """主用网卡的 IPv4。用 UDP connect 探测路由选路，不会真的发包。"""
    probe = None
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(('223.5.5.5', 80))
        address = probe.getsockname()[0]
        if address and address != '0.0.0.0':
            return address
    except OSError:
        pass
    finally:
        if probe is not None:
            probe.close()
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return ''


def lan_enabled() -> bool:
    return LAN_CONF.is_file()


def load_state() -> dict[str, Any]:
    try:
        payload = json.loads(STATE_FILE.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def save_state(values: dict[str, Any]) -> None:
    state = load_state()
    state.update(values)
    atomic_write(STATE_FILE, json.dumps(state, ensure_ascii=False, indent=2) + '\n')


def lan_port() -> int:
    """当前桌面入口端口。

    以**已安装的 nginx 配置**为准（那才是真正在监听的端口），没有配置时再用状态文件，
    最后才是安装时的默认值 —— 这样界面显示的地址不会和实际监听端口不一致。
    """
    if LAN_CONF.is_file():
        try:
            match = re.search(r'listen\s+(?:\[::\]:)?(\d+);',
                              LAN_CONF.read_text(encoding='utf-8', errors='replace'))
        except OSError:
            match = None
        if match:
            port = int(match.group(1))
            if 1024 <= port <= 65535:
                return port
    value = load_state().get('lan_port')
    if isinstance(value, int) and 1024 <= value <= 65535:
        return value
    return LAN_PORT


def validate_port(value: Any) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise RuntimeError('端口必须是数字') from None
    if not 1024 <= port <= 65535:
        raise RuntimeError('端口范围是 1024–65535（1024 以下需要特权且容易和系统服务冲突）')
    return port


def port_available(port: int) -> bool:
    """端口是否可用。

    必须带 SO_REUSEADDR：否则刚刚关闭的监听端口会因为 TIME_WAIT 连接被判成"被占用"
    （nginx 自己也是这么绑的，带 REUSEADDR 仍然能检出真正在 LISTEN 的进程）。
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(('0.0.0.0', port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def render_lan_conf(port: int) -> str:
    template = LAN_TEMPLATE.read_text(encoding='utf-8')
    if '__LAN_PORT__' in template:
        return template.replace('__LAN_PORT__', str(port))
    # 兼容安装时已经写死端口的旧模板
    return re.sub(r'(listen\s+(?:\[::\]:)?)\d+;', lambda match: f'{match.group(1)}{port};', template)


def desktop_url() -> str:
    address = primary_ipv4() or 'NAS-IP'
    return f'http://{address}:{lan_port()}/'


def nginx_test() -> tuple[bool, str]:
    result = run(['nginx', '-t'], timeout=25)
    output = (result.stderr or result.stdout or '').strip()
    return result.returncode == 0, output


def nginx_reload() -> bool:
    for command in (['systemctl', 'reload', 'nginx'], ['nginx', '-s', 'reload']):
        try:
            if run(command, timeout=25).returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            continue
    return False


def set_lan_enabled(enabled: bool) -> dict[str, Any]:
    """启用/停用桌面 web 入口。返回改后的状态；失败时抛 RuntimeError（已回滚）。"""
    with _toggle_lock:
        if enabled == lan_enabled():
            return {'enabled': enabled, 'changed': False}
        if enabled:
            if not LAN_TEMPLATE.is_file():
                raise RuntimeError(f'找不到入口模板 {LAN_TEMPLATE}，无法启用')
            atomic_write(LAN_CONF, render_lan_conf(lan_port()), mode=0o644)
            ok, output = nginx_test()
            if not ok:
                LAN_CONF.unlink(missing_ok=True)
                raise RuntimeError(f'nginx 配置校验失败，已回滚为停用：{output}')
        else:
            LAN_CONF.unlink(missing_ok=True)
            ok, output = nginx_test()
            if not ok:                                              # 理论上不会发生
                raise RuntimeError(f'删除入口后 nginx 校验失败，请检查 /etc/nginx：{output}')
        if not nginx_reload():
            if enabled:
                LAN_CONF.unlink(missing_ok=True)
                raise RuntimeError('nginx 重载失败，已回滚为停用')
            raise RuntimeError('nginx 重载失败，请手动执行 systemctl reload nginx')
        log(f'桌面 web 入口已{"启用" if enabled else "停用"}')
        return {'enabled': enabled, 'changed': True}


def set_lan_port(value: Any) -> dict[str, Any]:
    """改桌面入口端口：重写自己的 nginx 配置 + nginx -t + reload，失败回滚。"""
    port = validate_port(value)
    with _toggle_lock:
        current = lan_port()
        if port == current:
            return {'port': current, 'changed': False}
        if not LAN_TEMPLATE.is_file():
            raise RuntimeError(f'找不到入口模板 {LAN_TEMPLATE}，无法改端口')
        if not port_available(port):
            raise RuntimeError(f'端口 {port} 已被占用，请换一个')
        enabled = lan_enabled()
        backup = LAN_CONF.read_text(encoding='utf-8') if LAN_CONF.is_file() else ''
        if enabled:
            atomic_write(LAN_CONF, render_lan_conf(port), mode=0o644)
            ok, output = nginx_test()
            if not ok:
                atomic_write(LAN_CONF, backup) if backup else LAN_CONF.unlink(missing_ok=True)
                raise RuntimeError(f'nginx 配置校验失败，端口未改：{output}')
            if not nginx_reload():
                atomic_write(LAN_CONF, backup) if backup else LAN_CONF.unlink(missing_ok=True)
                nginx_reload()
                raise RuntimeError('nginx 重载失败，端口未改')
        save_state({'lan_port': port})
        log(f'桌面入口端口已改为 {port}（{"已生效" if enabled else "入口当前停用"}）')
        return {'port': port, 'changed': True, 'enabled': enabled}


def save_admin_token(path: Path, token: str) -> None:
    atomic_write(path, token + '\n', mode=0o600)


def new_admin_token() -> str:
    return secrets.token_hex(16)



def overview_payload(sampler: Sampler) -> dict[str, Any]:
    sample = sampler.snapshot()
    mem = mem_summary(parse_meminfo(read_text('/proc/meminfo')))
    net_counters = parse_net_dev(read_text('/proc/net/dev'))
    primary = ''
    best = -1
    for name, (rx, tx) in net_counters.items():
        if name.startswith(('docker', 'veth', 'br-', 'sit', 'end')) or rx + tx < best:
            continue
        primary, best = name, rx + tx
    ifaces = []
    for name, value in sorted((sample.get('net') or {}).items()):
        ifaces.append({'name': name, 'in': value['in'], 'out': value['out'],
                       'rx': net_counters.get(name, (0, 0))[0],
                       'tx': net_counters.get(name, (0, 0))[1]})
    return {
        'at': int(time.time()),
        'host': host_payload(),
        'cpu': sample.get('cpu'),
        'mem': mem,
        'net': {'primary': primary, 'ifaces': ifaces},
        'io': sample.get('disk') or {},
        'spin': sampler.spin_states(),
        'version': VERSION,
    }
