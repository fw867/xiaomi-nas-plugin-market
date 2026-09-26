#!/usr/bin/env python3
"""硬盘休眠：与系统设置联动，并可自定义休眠时间、查看休眠/唤醒日志。

官方实现（本插件不改动它，只做覆盖）：
  * 开关真源是 uci 的 `system.disk.hibernate`（App 的系统设置里那个开关）
  * `/usr/lib/systemd/system/hdidle.service` 里写死了 30 分钟：
        ExecStart=/bin/sh -c 'if [ "$(uci get system.disk.hibernate)" = "1" ];
                              then exec /usr/bin/hdidle -n -i 1800; fi'
  * 因此在 /etc/systemd/system/hdidle.service.d/ 放一个 drop-in 覆盖 ExecStart，
    把 1800 换成用户设置的值。开关、官方 unit、App 侧逻辑全部保持原样，
    所以 App 里的开关与本插件双向联动；卸载时删掉 drop-in 即回到官方行为。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from collections import deque
from pathlib import Path

VERSION = '0.1.0'
PLUGIN_KEY = 'disksleep'
DATA_DIR = Path(os.environ.get('DATA_DIR', '/data/plugin/disk-sleep'))
STATE_FILE = Path(os.environ.get('STATE_FILE', str(DATA_DIR / 'state.json')))
EVENT_FILE = Path(os.environ.get('EVENT_FILE', str(DATA_DIR / 'events.jsonl')))
RUN_DIR = Path(os.environ.get('RUN_DIR', str(DATA_DIR / 'run')))

UCI = shutil.which('uci') or '/sbin/uci'
HDPARM = shutil.which('hdparm') or '/usr/sbin/hdparm'
HDIDLE = shutil.which('hdidle') or '/usr/bin/hdidle'
HDIDLE_UNIT = 'hdidle.service'
DROPIN_DIR = Path('/etc/systemd/system/hdidle.service.d')
DROPIN_FILE = DROPIN_DIR / '10-plugin-timeout.conf'

# 官方 unit 里写死的 30 分钟，作为「未接管」时的说明与回退值
OFFICIAL_SECONDS = 1800
MIN_MINUTES = 5
MAX_MINUTES = 720
SAMPLE_INTERVAL = 20
MAX_EVENTS = 4000

# ---------------------------------------------------------------------------
# 「谁在写盘」诊断用到的路径与参数。都可用环境变量覆盖，方便测试。
# ---------------------------------------------------------------------------
PROC_ROOT = Path(os.environ.get('PROC_ROOT', '/proc'))
PROC_DISKSTATS = Path(os.environ.get('PROC_DISKSTATS', '/proc/diskstats'))
PROC_MOUNTS = Path(os.environ.get('PROC_MOUNTS', '/proc/mounts'))
PROC_MDSTAT = Path(os.environ.get('PROC_MDSTAT', '/proc/mdstat'))
FINDEX_DIR = Path(os.environ.get('FINDEX_DIR', '/nas/sys/findex'))
CONTAINER_DIR = Path(os.environ.get('CONTAINER_DIR', '/data/docker_data/containers'))
PLUGIN_ROOT = Path(os.environ.get('PLUGIN_ROOT', '/data/plugin'))

# 块设备计数的环形缓冲：15 个点 × 20 秒 = 最近 5 分钟
ACTIVITY_SAMPLES = 15
# 连续两次「主动扫描」之间的最短间隔（防止连点「重新扫描」）
ACTIVITY_SCAN_INTERVAL = 30
# 归属映射（容器挂载 + 插件清单）的缓存时长
ACTIVITY_OWNER_CACHE = 300
# 系统索引事件库的缓存时长
ACTIVITY_EVENT_CACHE = 300
# 目录扫描深度：系统库分区很小，全扫；用户数据盘只扫浅层，靠事件库补细节
ACTIVITY_DEPTH_SYSTEM = 8
ACTIVITY_DEPTH_DATA = 4
ACTIVITY_FILE_LIMIT = 20000
ACTIVITY_WRITER_LIMIT = 20
ACTIVITY_EVENT_LIMIT = 200
ACTIVITY_EVENT_WINDOW = 3600

# 只统计会转的机械盘；mmcblk（内置存储）与 dm-*（加密映射）不是休眠对象
HDD_PATTERN = re.compile(r'^(sd[a-z]+|hd[a-z]+|nvme\d+n\d+)$')
BLOCK_PREFIXES = ('/dev/sd', '/dev/hd', '/dev/nvme', '/dev/md', '/dev/mmcblk', '/dev/dm-')

# hdparm -C 的可能输出
STANDBY_STATES = {'standby', 'sleeping'}
ACTIVE_STATES = {'active/idle', 'idle', 'unknown'}

_lock = threading.Lock()
_sampler = None
_stop_sampler = threading.Event()

# 活动诊断的运行时状态（采样线程写，HTTP 线程读，用 _activity_lock 保护）
_activity_lock = threading.Lock()
_activity_samples: deque = deque(maxlen=ACTIVITY_SAMPLES)
_activity_files: dict[str, tuple[int, int]] = {}
_activity_writers: list[dict] = []
_activity_writers_at = 0.0
_activity_events: list[dict] = []
_activity_events_at = 0.0
_activity_events_note = ''
_activity_owners: list[dict] = []
_activity_owners_at = 0.0
_activity_mountpoints: list[str] = []


class Error(Exception):
    """可直接展示给用户的错误。"""


def installed_version() -> str:
    """插件包的真实版本。

    商店安装器把包解压到 <releaseRoot>/releases/<版本>-<时间戳>-<pid>/ 下，
    current 是指向它的符号链接，所以本文件所在目录名里就带着版本。
    源码树里直接跑（开发、测试）时解析不出来，回退到 VERSION。
    """
    parts = Path(__file__).resolve().parent.name.split('-')
    if len(parts) > 2 and parts[-1].isdigit() and parts[-2].isdigit():
        return '-'.join(parts[:-2])
    return VERSION


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def run(command: list[str], timeout: int = 15) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=False, timeout=timeout)


def atomic_write(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(text, encoding='utf-8', newline='\n')
    os.chmod(temporary, mode)
    temporary.replace(path)


# ---------------------------------------------------------------------------
# 官方开关（uci system.disk.hibernate）
# ---------------------------------------------------------------------------

def app_switch() -> bool:
    """读取 App 系统设置里的「硬盘休眠」开关。"""
    result = run([UCI, 'get', 'system.disk.hibernate'])
    return result.returncode == 0 and result.stdout.strip() == '1'


def set_app_switch(enabled: bool) -> None:
    """写回 App 的同一个开关，并像官方那样启停 hdidle.service。"""
    value = '1' if enabled else '0'
    if run([UCI, 'set', f'system.disk.hibernate={value}']).returncode != 0:
        raise Error('写入系统休眠开关失败')
    if run([UCI, 'commit', 'system']).returncode != 0:
        raise Error('提交系统配置失败')
    action = 'start' if enabled else 'stop'
    # 停用时官方 unit 会因为 uci=0 直接退出，这里照官方行为同步启停
    run(['systemctl', action, HDIDLE_UNIT], timeout=30)


# ---------------------------------------------------------------------------
# 插件自己的设置
# ---------------------------------------------------------------------------

def read_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_state(state: dict) -> None:
    atomic_write(STATE_FILE, json.dumps(state, ensure_ascii=False, indent=2) + '\n', mode=0o600)


def configured_minutes() -> int:
    """插件设置的目标休眠分钟数；未设置过就是官方默认 30。"""
    try:
        minutes = int(read_state().get('minutes', OFFICIAL_SECONDS // 60))
    except (TypeError, ValueError):
        minutes = OFFICIAL_SECONDS // 60
    return max(MIN_MINUTES, min(MAX_MINUTES, minutes))


def managed() -> bool:
    """drop-in 是否存在（即插件是否接管了休眠时间）。"""
    return DROPIN_FILE.is_file()


def dropin_text(seconds: int) -> str:
    return (
        '# 由「硬盘休眠」插件生成：覆盖官方 unit 里写死的 30 分钟。\n'
        '# 删除本文件（或卸载插件）即回到官方行为。\n'
        '[Service]\n'
        'ExecStart=\n'
        'ExecStart=/bin/sh -c \'if [ "$(uci get system.disk.hibernate 2>/dev/null)" = "1" ]; '
        f'then exec {HDIDLE} -n -i {seconds}; fi\'\n'
    )


def apply_timeout(minutes: int) -> None:
    """写入 drop-in 并让 hdidle 用新时长重启（开关为关时不动服务）。"""
    seconds = minutes * 60
    atomic_write(DROPIN_FILE, dropin_text(seconds), mode=0o644)
    if run(['systemctl', 'daemon-reload'], timeout=30).returncode != 0:
        raise Error('systemd 重载失败')
    if app_switch():
        if run(['systemctl', 'restart', HDIDLE_UNIT], timeout=30).returncode != 0:
            raise Error('hdidle 重启失败，请查看系统日志')


def restore_official() -> None:
    """删除 drop-in，恢复官方 30 分钟。"""
    if DROPIN_FILE.exists():
        DROPIN_FILE.unlink()
    if DROPIN_DIR.is_dir() and not any(DROPIN_DIR.iterdir()):
        DROPIN_DIR.rmdir()
    run(['systemctl', 'daemon-reload'], timeout=30)
    if app_switch():
        run(['systemctl', 'restart', HDIDLE_UNIT], timeout=30)


def set_minutes(minutes: int) -> int:
    if not isinstance(minutes, int) or isinstance(minutes, bool):
        raise Error('休眠时间必须是整数分钟')
    if not MIN_MINUTES <= minutes <= MAX_MINUTES:
        raise Error(f'休眠时间需在 {MIN_MINUTES} 到 {MAX_MINUTES} 分钟之间')
    with _lock:
        state = read_state()
        state['minutes'] = minutes
        write_state(state)
        # 已经接管时才动 drop-in；还没接管就先记账，等开启接管时生效。
        # 这样「保存时间」不会顺手把接管打开。
        if managed():
            apply_timeout(minutes)
    # add_event 自己会加锁，必须放在锁外，否则死锁
    add_event(None, 'config', f'休眠时间设为 {minutes} 分钟')
    return minutes


def set_takeover(enabled: bool) -> None:
    """一个开关同时管住「系统休眠开关」和「插件接管」。

    打开：先把 drop-in 落地（用当前设置的分钟数），再打开 uci 开关并启动守护。
    关闭：先停守护，再关 uci 开关，最后删掉 drop-in（交还官方 30 分钟）。
    顺序不能反：开着守护却先删 drop-in，会有一瞬间按官方配置跑。
    """
    if not isinstance(enabled, bool):
        raise Error('接管开关必须是布尔值')
    with _lock:
        if enabled:
            apply_timeout(configured_minutes())
            set_app_switch(True)
        else:
            set_app_switch(False)
            restore_official()
    add_event(None, 'switch', '开启插件接管' if enabled else '关闭插件接管')


def hdidle_active() -> bool:
    return run(['systemctl', 'is-active', '--quiet', HDIDLE_UNIT]).returncode == 0


def hdidle_command() -> str:
    """当前 hdidle 实际使用的命令行，便于确认生效时长。"""
    result = run(['systemctl', 'show', HDIDLE_UNIT, '-p', 'ExecStart', '--value'])
    if result.returncode != 0:
        return ''
    match = re.search(r'argv\[\]=(.*?);?\s*$', result.stdout.strip(), re.S)
    if not match:
        return ''
    argv = [item.strip() for item in match.group(1).split(';') if item.strip()]
    return ' '.join(item.strip('"') for item in argv)


def effective_seconds() -> int | None:
    """从 hdidle 命令行解析出实际生效的秒数；解析不到返回 None。"""
    command = hdidle_command()
    match = re.search(r'-i\s+(\d+)', command)
    return int(match.group(1)) if match else None


# ---------------------------------------------------------------------------
# 磁盘状态与事件
# ---------------------------------------------------------------------------

def disk_devices() -> list[str]:
    devices = []
    for entry in sorted(Path('/sys/block').glob('sd*')):
        node = Path('/dev') / entry.name
        if node.exists():
            devices.append(entry.name)
    return devices


def disk_model(name: str) -> str:
    base = Path('/sys/block') / name / 'device'
    for candidate in (base / 'model', Path('/sys/block') / name / 'device' / 'vendor'):
        try:
            text = candidate.read_text(encoding='utf-8').strip()
        except OSError:
            continue
        if text:
            return re.sub(r'\s+', ' ', text)
    return ''


def disk_state(name: str) -> str:
    """hdparm -C 查询电源状态；该查询不会把盘唤醒。"""
    result = run([HDPARM, '-C', f'/dev/{name}'], timeout=10)
    match = re.search(r'drive state is:\s*(.+)', result.stdout)
    if not match:
        return 'unknown'
    return match.group(1).strip().lower()


def is_standby(state: str) -> bool:
    return state in STANDBY_STATES


def add_event(device: str | None, kind: str, detail: str = '') -> None:
    entry = {'at': int(time.time()), 'device': device or '', 'kind': kind, 'detail': detail}
    with _lock:
        EVENT_FILE.parent.mkdir(parents=True, exist_ok=True)
        with EVENT_FILE.open('a', encoding='utf-8', newline='\n') as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + '\n')
        trim_events()


def trim_events() -> None:
    try:
        lines = EVENT_FILE.read_text(encoding='utf-8').splitlines()
    except OSError:
        return
    if len(lines) <= MAX_EVENTS:
        return
    atomic_write(EVENT_FILE, '\n'.join(lines[-MAX_EVENTS:]) + '\n')


def recent_events(limit: int = 200) -> list[dict]:
    try:
        lines = EVENT_FILE.read_text(encoding='utf-8').splitlines()
    except OSError:
        return []
    events = []
    for line in lines[-limit:]:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            events.append(item)
    events.reverse()  # 最新在最上面
    return events


def last_event(device: str, kind: str) -> int | None:
    for item in recent_events(1000):
        if item.get('device') == device and item.get('kind') == kind:
            return int(item.get('at') or 0) or None
    return None


def sample_once(last: dict[str, str]) -> dict[str, str]:
    """采样一次盘状态，把状态跃迁写进事件日志，返回新的状态表。"""
    current: dict[str, str] = {}
    for name in disk_devices():
        state = disk_state(name)
        current[name] = state
        previous = last.get(name)
        if previous is None or previous == state:
            continue
        if is_standby(state) and not is_standby(previous):
            add_event(name, 'standby', f'{previous} → {state}')
        elif not is_standby(state) and is_standby(previous):
            add_event(name, 'wake', f'{previous} → {state}')
    return current


def sampling_loop() -> None:
    last: dict[str, str] = {}
    while not _stop_sampler.is_set():
        try:
            last = sample_once(last)
        except Exception:
            pass
        try:
            activity_tick()
        except Exception:
            pass
        _stop_sampler.wait(SAMPLE_INTERVAL)


def start_sampler() -> None:
    global _sampler
    if _sampler and _sampler.is_alive():
        return
    _stop_sampler.clear()
    _sampler = threading.Thread(target=sampling_loop, daemon=True)
    _sampler.start()


# ---------------------------------------------------------------------------
# 硬盘活动诊断：到底是谁在写盘
#
# hdidle 的日志只报整盘计数（reads/writes），看不出是哪个分区、哪个文件、哪个
# 插件的写入。这里把三件事拼起来：
#   1) /proc/diskstats 增量 → 哪个分区在写，并识别 RAID1 镜像写入
#   2) 文件大小 + mtime 增量 → 哪些文件被改（只在有人看面板时才扫）
#   3) 系统索引 findexd 的 fanotify 事件库 → 具体文件路径（最可靠的"点名"）
# 再用容器的 bind 挂载反查文件属于哪个插件。
#
# 为什么不用 iotop / pidstat：本机内核没开 CONFIG_TASK_IO_ACCOUNTING，
# /proc/<pid>/io 根本不存在，进程级 I/O 统计拿不到任何数据。
#
# 重要：扫目录、读事件库都会读硬盘（可能唤醒它），所以这些只在用户主动打开
# 「谁在写盘」或点「重新扫描」时做一次，绝不后台轮询。块设备计数是纯 /proc
# 读取，永远是免费、也永远在采的。
# ---------------------------------------------------------------------------

def read_uptime() -> float:
    try:
        return float((PROC_ROOT / 'uptime').read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


def read_diskstats() -> dict[str, dict[str, int]]:
    """{设备名: {read_sectors, write_sectors}}；字段位置见 proc(5)。"""
    stats: dict[str, dict[str, int]] = {}
    try:
        lines = PROC_DISKSTATS.read_text().splitlines()
    except OSError:
        return stats
    for line in lines:
        fields = line.split()
        if len(fields) < 10:
            continue
        try:
            stats[fields[2]] = {'read_sectors': int(fields[5]), 'write_sectors': int(fields[9])}
        except ValueError:
            continue
    return stats


def read_mounts() -> list[dict[str, str]]:
    """块设备挂载点；tmpfs/overlay/fuse 这些不是休眠对象，直接跳过。"""
    skip = ('proc', 'sysfs', 'devtmpfs', 'devpts', 'tmpfs', 'cgroup', 'cgroup2', 'debugfs',
            'tracefs', 'securityfs', 'pstore', 'bpf', 'configfs', 'mqueue', 'hugetlbfs',
            'fusectl', 'autofs', 'nsfs', 'binfmt_misc', 'ramfs', 'squashfs', 'overlay', 'erofs')
    mounts: list[dict[str, str]] = []
    try:
        lines = PROC_MOUNTS.read_text().splitlines()
    except OSError:
        return mounts
    for line in lines:
        fields = line.split()
        if len(fields) < 3:
            continue
        device, mountpoint, fstype = fields[0], fields[1], fields[2]
        if fstype in skip or not device.startswith(BLOCK_PREFIXES):
            continue
        mounts.append({
            'device': device,
            'name': os.path.basename(device),
            'mountpoint': mountpoint.replace('\\040', ' '),
            'fstype': fstype,
        })
    return mounts


def read_mdstat() -> dict[str, dict]:
    """{'md0': {'level': 'raid1', 'members': ['sdb1', 'sda1']}}"""
    arrays: dict[str, dict] = {}
    try:
        text = PROC_MDSTAT.read_text()
    except OSError:
        return arrays
    current = None
    for line in text.splitlines():
        head = re.match(r'^(md\d+)\s*:\s*active\s+(?:\([^)]*\)\s+)?(\w+)\s+(.*)$', line)
        if head:
            current = head.group(1)
            arrays[current] = {'level': head.group(2),
                               'members': re.findall(r'\b([a-z]+\d+)\[\d+\]', head.group(3))}
            continue
        if current and line.startswith(' '):
            for member in re.findall(r'\b([a-z]+\d+)\[\d+\]', line):
                if member not in arrays[current]['members']:
                    arrays[current]['members'].append(member)
    return arrays


def physical_disk(name: str) -> str:
    """sdb1 → sdb；nvme0n1p2 → nvme0n1；md0 → md0。"""
    match = re.match(r'^(nvme\d+n\d+)p\d+$', name) or re.match(r'^(mmcblk\d+)p\d+$', name)
    if match:
        return match.group(1)
    match = re.match(r'^(sd[a-z]+|hd[a-z]+)\d+$', name)
    if match:
        return match.group(1)
    return name


def backing_physical(name: str, arrays: dict[str, dict]) -> list[str]:
    """这个设备最终落在哪些物理盘上：md0 → ['sda', 'sdb']。"""
    if name in arrays:
        return sorted({physical_disk(member) for member in arrays[name]['members']})
    return [physical_disk(name)]


def is_hdd_backed(name: str, arrays: dict[str, dict]) -> bool:
    return any(HDD_PATTERN.match(disk) for disk in backing_physical(name, arrays))


def scan_files(root: str, max_depth: int) -> dict[str, tuple[int, int]]:
    """{路径: (大小, mtime_ns)}；只读元数据，不读文件内容。"""
    found: dict[str, tuple[int, int]] = {}
    stack: list[tuple[str, int]] = [(root, 0)]
    while stack and len(found) < ACTIVITY_FILE_LIMIT:
        directory, depth = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    if depth + 1 <= max_depth:
                        stack.append((entry.path, depth + 1))
                elif entry.is_file(follow_symlinks=False):
                    info = entry.stat(follow_symlinks=False)
                    found[entry.path] = (info.st_size, info.st_mtime_ns)
            except OSError:
                continue
    return found


def open_holders(prefixes: tuple[str, ...]) -> dict[str, list[str]]:
    """{文件: [进程名(pid)]}：谁在硬盘上持有可写句柄。

    容器内的进程看到的是容器路径（如 /config/...），所以它们不会出现在这里；
    这类文件的归属靠容器挂载反查（见 owner_for）。
    """
    holders: dict[str, list[str]] = {}
    if not prefixes:
        return holders
    try:
        entries = os.listdir(PROC_ROOT)
    except OSError:
        return holders
    for entry in entries:
        if not entry.isdigit():
            continue
        fd_dir = str(PROC_ROOT / entry / 'fd')
        try:
            comm = (PROC_ROOT / entry / 'comm').read_text().strip()
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(os.path.join(fd_dir, fd))
            except OSError:
                continue
            if not target.startswith(prefixes):
                continue
            try:
                flags = 0
                with open(os.path.join(fd_dir, '..', 'fdinfo', fd)) as handle:
                    for line in handle:
                        if line.startswith('flags:'):
                            flags = int(line.split()[1], 8)
                            break
            except (OSError, ValueError):
                continue
            if flags & 3:
                holders.setdefault(target, []).append('%s(%s)' % (comm, entry))
    return holders


def record_activity(min_gap: float = 0) -> None:
    """把一次块设备计数放进环形缓冲。纯 /proc 读取，不碰硬盘。"""
    stats = read_diskstats()
    if not stats:
        return
    now = time.time()
    with _activity_lock:
        if min_gap and _activity_samples and now - _activity_samples[-1][0] < min_gap:
            return
        _activity_samples.append((now, stats))


def refresh_writers(force: bool = False) -> None:
    """扫一遍机械盘挂载点，和上一次比出被改动的文件。

    这一步会读硬盘目录的元数据（可能触发 atime 回写、把盘唤醒），所以只在
    用户主动打开「谁在写盘」或点「重新扫描」时调用，绝不做后台轮询。
    """
    global _activity_files, _activity_writers, _activity_writers_at
    now = time.time()
    with _activity_lock:
        if not force and now - _activity_writers_at < ACTIVITY_SCAN_INTERVAL:
            return
    arrays = read_mdstat()
    mounts = [m for m in read_mounts() if is_hdd_backed(m['name'], arrays)]
    current: dict[str, tuple[int, int]] = {}
    for mount in mounts:
        depth = ACTIVITY_DEPTH_SYSTEM if mount['name'] in arrays else ACTIVITY_DEPTH_DATA
        current.update(scan_files(mount['mountpoint'], depth))
    with _activity_lock:
        previous, _activity_files = _activity_files, current
    changed: list[dict] = []
    if previous:
        for path, info in current.items():
            old = previous.get(path)
            if old is None:
                changed.append({'path': path, 'delta': 0, 'kind': 'new'})
            elif old != info:
                changed.append({'path': path, 'delta': info[0] - old[0], 'kind': 'mod'})
        changed.sort(key=lambda item: -abs(item['delta']))
    if changed:
        prefixes = tuple(m['mountpoint'].rstrip('/') for m in mounts)
        holders = open_holders(prefixes)
        for item in changed:
            item['heldBy'] = sorted(set(holders.get(item['path'], [])))
    with _activity_lock:
        _activity_writers = changed[:ACTIVITY_WRITER_LIMIT]
        # 第一次只建立基线，比对不出东西，所以不更新时间戳——
        # 面板据此显示「尚未扫描」，而不是「没有文件被改动」。
        if previous:
            _activity_writers_at = now


def read_event_db(db_name: str, table: str) -> tuple[dict[str, int], str]:
    """读 findex 的 fanotify 事件库，返回 {文件路径: 记录条数}。

    事件库在 /nas/sys 上，直接用 SQLite 打开会碰到 -shm；为了绝对不写系统分区，
    先把 db/-wal/-shm 复制到私有临时目录（服务开了 PrivateTmp，就是内存盘）再查。
    """
    source = FINDEX_DIR / db_name
    if not source.is_file():
        return {}, '未找到系统索引事件库 %s' % source
    temporary = tempfile.mkdtemp(prefix='disk-sleep-')
    try:
        for suffix in ('', '-wal', '-shm'):
            candidate = Path(str(source) + suffix)
            if candidate.is_file():
                try:
                    shutil.copy2(candidate, Path(temporary) / candidate.name)
                except OSError:
                    pass
        connection = sqlite3.connect(str(Path(temporary) / source.name))
        try:
            rows = connection.execute(
                'select parent_path, name from "%s" order by event_time_ms desc limit 300' % table)
            counts: dict[str, int] = {}
            for row in rows:
                key = '%s/%s' % (str(row[0] or '').rstrip('/'), row[1] or '')
                counts[key] = counts.get(key, 0) + 1
            return counts, ''
        finally:
            connection.close()
    except (sqlite3.Error, OSError) as error:
        return {}, '事件库读取失败：%s' % error
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


def refresh_events(force: bool = False) -> None:
    """汇总两个事件库里最近记录到的文件（去重，取较大的计数）。"""
    global _activity_events, _activity_events_at, _activity_events_note
    now = time.time()
    with _activity_lock:
        if not force and now - _activity_events_at < ACTIVITY_EVENT_CACHE:
            return
    merged: dict[str, int] = {}
    note = ''
    for db_name, table in (('raw_event_log.db', 'raw_event_log'),
                           ('normalized_event_log.db', 'normalized_event_log')):
        counts, reason = read_event_db(db_name, table)
        if counts:
            for path, count in counts.items():
                merged[path] = max(merged.get(path, 0), count)
        elif reason and not note:
            note = reason
    ranked = sorted(({'path': path, 'count': count} for path, count in merged.items()),
                    key=lambda item: -item['count'])
    with _activity_lock:
        _activity_events = ranked[:ACTIVITY_EVENT_LIMIT]
        _activity_events_at = now
        _activity_events_note = note


def refresh_owners(force: bool = False) -> None:
    """容器 bind 挂载 + 机械盘挂载点，用于把文件路径反查到插件。"""
    global _activity_owners, _activity_owners_at, _activity_mountpoints
    now = time.time()
    with _activity_lock:
        if not force and now - _activity_owners_at < ACTIVITY_OWNER_CACHE:
            return
    arrays = read_mdstat()
    mountpoints = [m['mountpoint'].rstrip('/') for m in read_mounts()
                   if is_hdd_backed(m['name'], arrays)]
    owners: list[dict] = []
    if CONTAINER_DIR.is_dir():
        for entry in sorted(CONTAINER_DIR.iterdir()):
            config = entry / 'config.v2.json'
            if not config.is_file():
                continue
            try:
                data = json.loads(config.read_text(encoding='utf-8', errors='replace'))
            except (OSError, ValueError):
                continue
            container = str(data.get('Name') or '').lstrip('/') or entry.name[:12]
            image = str((data.get('Config') or {}).get('Image') or '?').split('@')[0]
            running = bool((data.get('State') or {}).get('Running'))
            for target, mount in (data.get('MountPoints') or {}).items():
                source = (mount or {}).get('Source')
                if not isinstance(source, str) or not source:
                    continue
                owners.append({'source': source.rstrip('/'), 'target': str(target),
                               'container': container, 'image': image, 'running': running})
    with _activity_lock:
        _activity_owners = owners
        _activity_mountpoints = mountpoints
        _activity_owners_at = now


def pool_variants(source: str) -> list[str]:
    """cfs 聚合层把 /nas/pool0 映射到 /nas/mnt/pa*：同一个目录有两个路径。

    容器挂载里写的是聚合层路径，而文件实际落在底层挂载点上，所以两种都要试。
    """
    variants = [source]
    match = re.match(r'^(/nas/pool\d+)(/.*)?$', source)
    if match:
        tail = match.group(2) or ''
        with _activity_lock:
            mountpoints = list(_activity_mountpoints)
        for mountpoint in mountpoints:
            variants.append(mountpoint.rstrip('/\\') + tail)
    return variants


def is_under(path: str, candidate: str) -> bool:
    """路径是否就是 candidate 或它的子项（两平台分隔符都认）。"""
    if path == candidate:
        return True
    return path.startswith(candidate + '/') or path.startswith(candidate + os.sep)


def owner_for(path: str) -> str:
    """把文件路径反查成「哪个插件/容器在动它」。"""
    with _activity_lock:
        owners = list(_activity_owners)
    best, label = 0, ''
    for owner in owners:
        for candidate in pool_variants(owner['source']):
            if is_under(path, candidate) and len(candidate) > best:
                best = len(candidate)
                label = '%s（%s，%s）' % (owner['container'], owner['image'],
                                         '运行中' if owner['running'] else '已停止')
    return label


def plugin_labels() -> dict[str, str]:
    """{插件目录名: 显示名}"""
    names: dict[str, str] = {}
    if not PLUGIN_ROOT.is_dir():
        return names
    for entry in PLUGIN_ROOT.iterdir():
        meta = entry / 'current' / 'plugin-meta.json'
        if not meta.is_file():
            continue
        try:
            data = json.loads(meta.read_text(encoding='utf-8', errors='replace'))
            names[entry.name] = str(data.get('name') or entry.name)
        except (OSError, ValueError):
            names[entry.name] = entry.name
    return names


def activity_tick() -> None:
    """采样线程每轮调一次。

    只记块设备计数——那是纯 /proc 读取，不碰硬盘。目录扫描和事件库读取
    一律留给用户主动触发的请求，否则插件自己就会把盘弄醒。
    """
    record_activity()


def write_rate_summary() -> dict:
    """只用已经采到的块设备计数回答「现在有没有在写盘」，完全不碰硬盘。

    状态卡每 10 秒刷新一次，所以这里必须是免费的——目录扫描和事件库读取
    都放在展开「谁在写盘」面板之后才做。
    """
    with _activity_lock:
        samples = list(_activity_samples)
    if len(samples) < 2:
        return {'hasWrites': False, 'summary': '', 'sampling': True}
    window = samples[-1][0] - samples[0][0]
    if window <= 0:
        return {'hasWrites': False, 'summary': '', 'sampling': True}
    first, last = samples[0][1], samples[-1][1]
    arrays = read_mdstat()
    rows: list[tuple[float, str]] = []
    for mount in read_mounts():
        name = mount['name']
        if not is_hdd_backed(name, arrays) or name not in first or name not in last:
            continue
        rate = (last[name]['write_sectors'] - first[name]['write_sectors']) / window / 2.0
        if rate > 0.25:
            rows.append((rate, mount['mountpoint']))
    rows.sort(reverse=True)
    return {
        'hasWrites': bool(rows),
        'summary': '、'.join('%s %.1f KB/s' % (point, rate) for rate, point in rows[:3]),
        'sampling': False,
    }


def activity_snapshot(force: bool = False) -> dict:
    """「谁在写盘」面板的数据。

    会读硬盘的两件事（扫目录、读事件库）只在 force（打开标签页 / 点重新扫描）
    或还没有缓存时做一次；之后每 10 秒的自动刷新只读缓存，不产生磁盘 I/O，
    免得用户把标签页开着就把盘一直弄醒。
    """
    record_activity(min_gap=5)
    refresh_owners()
    with _activity_lock:
        writers_at = _activity_writers_at
        events_at = _activity_events_at
    if force or not writers_at:
        refresh_writers(force=force)
    if force or not events_at:
        refresh_events(force=force)
    arrays = read_mdstat()
    mounts = read_mounts()
    hdd_mounts = [m for m in mounts if is_hdd_backed(m['name'], arrays)]

    with _activity_lock:
        samples = list(_activity_samples)
        writers = [dict(item) for item in _activity_writers]
        writers_at = _activity_writers_at
        events = [dict(item) for item in _activity_events]
        events_at = _activity_events_at
        events_note = _activity_events_note
        owners = list(_activity_owners)

    window = 0.0
    rates: dict[str, dict[str, float]] = {}
    if len(samples) >= 2:
        window = samples[-1][0] - samples[0][0]
        first, last = samples[0][1], samples[-1][1]
        if window > 0:
            for name in set(first) & set(last):
                rates[name] = {
                    'write': (last[name]['write_sectors'] - first[name]['write_sectors']) / window,
                    'read': (last[name]['read_sectors'] - first[name]['read_sectors']) / window,
                }

    def kb(name: str, key: str) -> float:
        return round(rates.get(name, {}).get(key, 0.0) / 2.0, 1)

    busy: list[dict] = []
    for mount in hdd_mounts:
        name = mount['name']
        row = {
            'mountpoint': mount['mountpoint'],
            'device': name,
            'fstype': mount['fstype'],
            'array': name if name in arrays else '',
            'level': arrays[name]['level'] if name in arrays else '',
            'members': list(arrays[name]['members']) if name in arrays else [],
            'writeKBps': kb(name, 'write'),
            'readKBps': kb(name, 'read'),
            'mirrored': False,
        }
        row['busy'] = row['writeKBps'] > 0.25
        busy.append(row)

    disks = []
    for disk in sorted(name for name in rates if HDD_PATTERN.match(name)):
        disks.append({'device': disk, 'writeKBps': kb(disk, 'write'), 'readKBps': kb(disk, 'read')})

    mirrored: list[str] = []
    for array, info in arrays.items():
        members = info['members']
        if info['level'] != 'raid1' or len(members) < 2:
            continue
        member_rates = [rates.get(m, {}).get('write', 0.0) for m in members]
        if max(member_rates) > 1 and max(member_rates) - min(member_rates) < max(0.5, max(member_rates) * 0.02):
            mirrored.append('%s（%s）' % (array, ' + '.join(members)))
            for row in busy:
                if row['array'] == array:
                    row['mirrored'] = True

    labels = plugin_labels()

    def decorate(path: str) -> str:
        owner = owner_for(path)
        if owner:
            return owner
        match = re.match(r'^/data/plugin/([^/]+)/', path)
        if match:
            return '%s（插件 %s）' % (labels.get(match.group(1), match.group(1)), match.group(1))
        return ''

    for item in writers:
        item['owner'] = decorate(item['path'])
    for item in events:
        item['owner'] = decorate(item['path'])

    notes: list[str] = []
    if window <= 0:
        notes.append('正在采样：请稍候片刻让速率出来（每 %d 秒一个采样点）。' % SAMPLE_INTERVAL)
    if mirrored:
        notes.append('检测到 RAID1 镜像写入：%s。成员盘写入量几乎相等，'
                     '说明写入来自阵列上的文件系统——写一次阵列，每块成员盘都要写一次。'
                     % '，'.join(mirrored))
        raid_points = '、'.join(row['mountpoint'] for row in busy if row['mirrored'])
        if raid_points:
            notes.append('%s 放的是系统数据库（文件索引、相册、媒体库），不是用户数据；'
                         '只要系统服务还在写它，所有成员盘都会被唤醒。' % raid_points)
    if busy and not any(row['busy'] for row in busy):
        notes.append('采样窗口内机械盘没有写入，硬盘本来就应该能休眠。'
                     '如果 hdidle 日志仍在报活动，说明写入是阵发性的，多等一会儿再看。')
    if not hdd_mounts:
        notes.append('没有检测到机械盘挂载点。')
    if writers_at and time.time() - writers_at > ACTIVITY_SCAN_INTERVAL * 3:
        notes.append('文件清单是上次扫描的结果；点「重新扫描」才会重新读硬盘。')

    return {
        'ok': True,
        'sampledAt': int(samples[-1][0]) if samples else 0,
        'windowSeconds': round(window, 1),
        'sampling': window <= 0,
        'sampleInterval': SAMPLE_INTERVAL,
        'mounts': busy,
        'disks': disks,
        'mirrored': mirrored,
        'writers': writers,
        'writersAt': int(writers_at) if writers_at else 0,
        'events': events,
        'eventsAt': int(events_at) if events_at else 0,
        'eventsNote': events_note,
        'owners': [dict(owner) for owner in owners],
        'notes': notes,
    }


# ---------------------------------------------------------------------------
# 快照
# ---------------------------------------------------------------------------

def hdidle_log(limit: int = 100) -> list[dict]:
    """hdidle 写进 journal 的原始记录（倒序）。"""
    result = run(
        ['journalctl', '-u', HDIDLE_UNIT, '--no-pager', '-o', 'json', '-n', str(limit)],
        timeout=20,
    )
    if result.returncode != 0:
        return []
    entries = []
    for line in result.stdout.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        message = item.get('MESSAGE')
        if isinstance(message, bytes):  # journal 里可能带非 UTF-8
            message = message.decode('utf-8', 'replace')
        if not isinstance(message, str):
            continue
        stamp = item.get('__REALTIME_TIMESTAMP')
        try:
            when = int(int(stamp) / 1_000_000) if stamp else 0
        except (TypeError, ValueError):
            when = 0
        entries.append({'at': when, 'message': message})
    entries.reverse()
    return entries


def disk_summary() -> list[dict]:
    items = []
    for name in disk_devices():
        state = disk_state(name)
        items.append({
            'device': name,
            'model': disk_model(name),
            'state': state,
            'standby': is_standby(state),
            'lastStandby': last_event(name, 'standby'),
            'lastWake': last_event(name, 'wake'),
        })
    return items


def snapshot() -> dict:
    seconds = effective_seconds()
    switch = app_switch()
    managed_now = managed()
    return {
        'ok': True,
        'version': installed_version(),
        'appSwitch': switch,
        'hdidleActive': hdidle_active(),
        'managed': managed_now,
        # 页面上那个双态按钮的状态：系统开关和插件接管都开着才算「已接管」
        'active': bool(switch and managed_now),
        'minutes': configured_minutes(),
        'effectiveMinutes': round(seconds / 60) if seconds else None,
        'officialMinutes': OFFICIAL_SECONDS // 60,
        'minMinutes': MIN_MINUTES,
        'maxMinutes': MAX_MINUTES,
        'presets': [10, 20, 30, 60, 120, 240],
        'disks': disk_summary(),
        'activity': write_rate_summary(),
    }
