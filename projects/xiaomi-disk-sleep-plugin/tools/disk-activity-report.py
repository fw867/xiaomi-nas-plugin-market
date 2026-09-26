#!/usr/bin/env python3
"""硬盘活动体检：查清「到底是谁在写盘、为什么硬盘不休眠」。

在小米智能存储（NAS）上以 root 运行：:

    python3 disk-activity-report.py                # 采样 60 秒
    python3 disk-activity-report.py --seconds 180  # 采样 3 分钟
    python3 disk-activity-report.py --depth 8      # 扫描更深的目录

脚本只读：除了把系统索引的事件库复制到 /tmp 里查询，不改动任何配置，
不重启任何服务，不写任何业务数据。

它回答三个层层递进的问题：

1. 是哪块盘、哪个分区在写？hdidle 日志只给整盘计数，掩盖了分区差异。
2. 写的是哪些文件？本机 /proc/<pid>/io 不存在（内核未开
   CONFIG_TASK_IO_ACCOUNTING），iotop / pidstat -d 都拿不到数据，所以走
   「文件大小 + mtime 增量」和「系统索引的 fanotify 事件库」两条路。
3. 这些文件属于哪个插件 / 容器？用 Docker 容器的 bind 挂载反查。

关键背景：小米存储把系统数据库分区 /nas/sys 放在 md0 上，而 md0 是
**跨两块物理盘的 RAID1**。任何一次写 /nas/sys 都会被镜像到两块盘，
所以只要还有东西在写 /nas/sys，两块盘都不可能休眠。

注意：为了让「文件改动」可对比，脚本必须遍历目录，遍历本身会产生元数据
读取（可能触发 atime 回写）。因此块设备计数在第 1 节先单独采样，不受扫描
影响；第 2 节的改动清单是独立的一小段窗口。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

BLOCK_PREFIXES = ('/dev/sd', '/dev/hd', '/dev/nvme', '/dev/md', '/dev/mmcblk', '/dev/dm-')
SKIP_FSTYPES = {'proc', 'sysfs', 'devtmpfs', 'devpts', 'tmpfs', 'cgroup', 'cgroup2',
                'debugfs', 'tracefs', 'securityfs', 'pstore', 'bpf', 'configfs',
                'mqueue', 'hugetlbfs', 'fusectl', 'autofs', 'nsfs', 'binfmt_misc',
                'ramfs', 'squashfs', 'overlay', 'erofs'}
# 会转的盘（休眠对象）；mmcblk / dm-* 是内置存储与加密映射，不参与休眠
HDD_PATTERN = re.compile(r'^(sd[a-z]+|hd[a-z]+|nvme\d+n\d+)$')
DEVICE_PATTERN = re.compile(r'^(sd[a-z]+\d*|hd[a-z]+\d*|nvme\d+n\d+(p\d+)?|md\d+|mmcblk\d+(p\d+)?|dm-\d+)$')

CONTAINER_DIR = Path('/data/docker_data/containers')
PLUGIN_DIR = Path('/data/plugin')
FINDEX_DIR = Path('/nas/sys/findex')
CONTAINER_LABEL = re.compile(r'^io\.xiaomi-plugin\.([^.]+)\.owner$')


# ---------------------------------------------------------------------------
# 基础读取
# ---------------------------------------------------------------------------

def read_uptime() -> float:
    try:
        return float(Path('/proc/uptime').read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


def read_diskstats() -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    try:
        lines = Path('/proc/diskstats').read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        f = line.split()
        if len(f) < 10:
            continue
        out[f[2]] = {
            'read_sectors': int(f[5]),
            'write_sectors': int(f[9]),
            'read_ios': int(f[3]),
            'write_ios': int(f[7]),
        }
    return out


def read_mounts() -> list[dict[str, str]]:
    mounts: list[dict[str, str]] = []
    try:
        lines = Path('/proc/mounts').read_text().splitlines()
    except OSError:
        return mounts
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        device, mountpoint, fstype = parts[0], parts[1], parts[2]
        if fstype in SKIP_FSTYPES or not device.startswith(BLOCK_PREFIXES):
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
        text = Path('/proc/mdstat').read_text()
    except OSError:
        return arrays
    current: str | None = None
    for line in text.splitlines():
        head = re.match(r'^(md\d+)\s*:\s*active\s+(?:\([^)]*\)\s+)?(\w+)\s+(.*)$', line)
        if head:
            current = head.group(1)
            arrays[current] = {
                'level': head.group(2),
                'members': re.findall(r'\b([a-z]+\d+)\[\d+\]', head.group(3)),
            }
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


def backing_physical(device_name: str, arrays: dict[str, dict]) -> list[str]:
    """这个设备最终落在哪些物理盘上。md0 → ['sda', 'sdb']。"""
    if device_name in arrays:
        return sorted({physical_disk(m) for m in arrays[device_name]['members']})
    return [physical_disk(device_name)]


def is_hdd_backed(device_name: str, arrays: dict[str, dict]) -> bool:
    return any(HDD_PATTERN.match(disk) for disk in backing_physical(device_name, arrays))


def human(sectors_per_second: float) -> str:
    kb = sectors_per_second / 2.0
    if kb >= 1024:
        return '%9.2f MB/s' % (kb / 1024)
    return '%9.1f KB/s' % kb


def guess_time_scale(max_value: int, uptime_seconds: float) -> float:
    """事件库的时间列名字叫 _ms，实际单位不一定；按开机时长倒推。"""
    for scale in (1e6, 1e3, 1.0):
        if 0 < max_value / scale <= max(uptime_seconds, 1.0) * 1.05:
            return scale
    return 1e3


# ---------------------------------------------------------------------------
# 目录扫描
# ---------------------------------------------------------------------------

def walk_files(root: str, max_depth: int, budget: float) -> tuple[dict[str, tuple[int, int]], bool]:
    """返回 {路径: (大小, mtime_ns)} 与「是否因超时被截断」。"""
    found: dict[str, tuple[int, int]] = {}
    started = time.time()
    truncated = False
    stack: list[tuple[str, int]] = [(root, 0)]
    while stack:
        directory, depth = stack.pop()
        if time.time() - started > budget:
            truncated = True
            break
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
                    st = entry.stat(follow_symlinks=False)
                    found[entry.path] = (st.st_size, st.st_mtime_ns)
            except OSError:
                continue
    return found, truncated


# ---------------------------------------------------------------------------
# 系统索引的 fanotify 事件库
# ---------------------------------------------------------------------------

def read_event_log(db_name: str, table: str, window_seconds: float, limit: int,
                   uptime: float) -> tuple[list[dict], str, float]:
    """读取 findex 的事件库；事件就是「某个文件被改动」的原始记录。"""
    source = FINDEX_DIR / db_name
    if not source.is_file():
        return [], '不存在 %s' % source, 1e3
    tmp = tempfile.mkdtemp(prefix='disk-activity-')
    try:
        for suffix in ('', '-wal', '-shm'):
            candidate = Path(str(source) + suffix)
            if candidate.is_file():
                try:
                    shutil.copy2(candidate, Path(tmp) / candidate.name)
                except OSError:
                    pass
        con = sqlite3.connect(str(Path(tmp) / source.name))
        con.row_factory = sqlite3.Row
        columns = [row[1] for row in con.execute('pragma table_info("%s")' % table)]
        if 'event_time_ms' not in columns:
            con.close()
            return [], '事件库结构不认识（没有 event_time_ms 列）', 1e3
        latest = con.execute('select max(event_time_ms) from "%s"' % table).fetchone()[0]
        if latest is None:
            con.close()
            return [], '事件库为空', 1e3
        scale = guess_time_scale(int(latest), uptime)
        floor = int(latest) - int(window_seconds * scale)
        rows = con.execute(
            'select * from "%s" where event_time_ms >= ? order by event_time_ms desc limit ?' % table,
            (floor, limit),
        )
        events = [dict(row) for row in rows]
        con.close()
        return events, '最近 %.0f 分钟内有 %d 条记录' % (window_seconds / 60.0, len(events)), scale
    except (sqlite3.Error, OSError) as error:
        return [], '读取失败：%s' % error, 1e3
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# 归属反查
# ---------------------------------------------------------------------------

def container_binds() -> list[dict[str, str]]:
    """每个容器把哪些宿主机目录挂进了容器，用来反查文件属于谁。"""
    found: list[dict[str, str]] = []
    if not CONTAINER_DIR.is_dir():
        return found
    for entry in sorted(CONTAINER_DIR.iterdir()):
        config = entry / 'config.v2.json'
        if not config.is_file():
            continue
        try:
            data = json.loads(config.read_text(encoding='utf-8', errors='replace'))
        except (OSError, ValueError):
            continue
        info = data.get('Config', {})
        image = str(info.get('Image') or '?').split('@')[0]
        name = str(data.get('Name') or '').lstrip('/') or str(info.get('Hostname') or entry.name[:12])
        running = bool(data.get('State', {}).get('Running'))
        plugin = ''
        for key in (info.get('Labels') or {}):
            match = CONTAINER_LABEL.match(str(key))
            if match:
                plugin = match.group(1)
                break
        for target, mount in (data.get('MountPoints') or {}).items():
            source = (mount or {}).get('Source')
            if not source or not isinstance(source, str):
                continue
            found.append({
                'source': source.rstrip('/'),
                'target': str(target),
                'container': name,
                'plugin': plugin,
                'image': image,
                'running': '运行中' if running else '已停止',
            })
    return found


def plugin_names() -> dict[str, str]:
    names: dict[str, str] = {}
    if not PLUGIN_DIR.is_dir():
        return names
    for entry in PLUGIN_DIR.iterdir():
        meta = entry / 'current' / 'plugin-meta.json'
        if not meta.is_file():
            continue
        try:
            data = json.loads(meta.read_text(encoding='utf-8', errors='replace'))
            names[entry.name] = str(data.get('name') or entry.name)
        except (OSError, ValueError):
            names[entry.name] = entry.name
    return names


def pool_variants(source: str, hdd_mountpoints: list[str]) -> list[str]:
    """cfs 聚合层把 /nas/pool0 映射到 /nas/mnt/pa*，同一个目录有两个路径。

    文件实际在底层挂载点上，而容器挂载写在聚合层路径上，所以两个都要试。
    """
    variants = [source]
    match = re.match(r'^(/nas/pool\d+)(/.*)?$', source)
    if match:
        tail = match.group(2) or ''
        for mountpoint in hdd_mountpoints:
            variants.append(mountpoint.rstrip('/') + tail)
    return variants


def match_owner(path: str, binds: list[dict[str, str]], plugins: dict[str, str],
                hdd_mountpoints: list[str]) -> str:
    best = ''
    for bind in binds:
        for variant in pool_variants(bind['source'], hdd_mountpoints):
            if path == variant or path.startswith(variant + '/'):
                if len(variant) > len(best):
                    label = bind['container']
                    if bind['plugin']:
                        label = '%s / 插件 %s' % (bind['container'], plugins.get(bind['plugin'], bind['plugin']))
                    best = '容器 %s（%s，%s）' % (label, bind['image'], bind['running'])
    if best:
        return best
    match = re.match(r'^/data/plugin/([^/]+)/', path)
    if match:
        return '插件 %s（%s）' % (plugins.get(match.group(1), match.group(1)), match.group(1))
    return ''


def writable_fds(prefixes: tuple[str, ...]) -> list[dict[str, str]]:
    """哪些进程在硬盘上持有可写文件描述符。

    容器里的进程看到的是容器内路径（如 /config/...），所以容器进程可能匹配不上；
    这种文件靠第 5 节的容器挂载反查归属。
    """
    hits: list[dict[str, str]] = []
    if not prefixes:
        return hits
    for entry in os.listdir('/proc'):
        if not entry.isdigit():
            continue
        try:
            comm = Path('/proc/%s/comm' % entry).read_text().strip()
            cmdline = Path('/proc/%s/cmdline' % entry).read_bytes()
        except OSError:
            continue
        cmdline_text = cmdline.replace(b'\0', b' ').decode('utf-8', 'replace').strip()[:120]
        fd_dir = '/proc/%s/fd' % entry
        try:
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
                with open('/proc/%s/fdinfo/%s' % (entry, fd)) as handle:
                    for line in handle:
                        if line.startswith('flags:'):
                            flags = int(line.split()[1], 8)
                            break
            except (OSError, ValueError):
                continue
            if flags & 3:
                hits.append({'pid': entry, 'comm': comm, 'cmd': cmdline_text,
                             'mode': 'RW' if (flags & 3) == 2 else 'W', 'file': target})
    return hits


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------

def section(title: str) -> None:
    print()
    print('=' * 78)
    print(title)
    print('=' * 78)


def main() -> int:
    parser = argparse.ArgumentParser(description='硬盘活动体检：谁在写盘、为什么不休眠')
    parser.add_argument('--seconds', type=int, default=60, help='块设备采样时长（默认 60 秒）')
    parser.add_argument('--depth', type=int, default=7, help='目录扫描深度（默认 7）')
    parser.add_argument('--budget', type=float, default=25.0, help='每个挂载点扫描的时间上限（秒）')
    parser.add_argument('--top', type=int, default=12, help='每个榜单显示多少条')
    parser.add_argument('--no-scan', action='store_true', help='跳过目录扫描，只看块设备计数')
    args = parser.parse_args()

    mounts = read_mounts()
    arrays = read_mdstat()
    member_of: dict[str, str] = {}
    for array, info in arrays.items():
        for member in info['members']:
            member_of[member] = array
    boot_wall = time.time() - read_uptime()
    all_stats = read_diskstats()

    hdd_mounts = [m for m in mounts if is_hdd_backed(m['name'], arrays)]
    hdd_mountpoints = [m['mountpoint'] for m in hdd_mounts]

    # -- 0. 环境 ----------------------------------------------------------
    section('0. 环境')
    print('时间        : %s' % time.strftime('%Y-%m-%d %H:%M:%S'))
    print('Python      : %s' % sys.version.split()[0])
    print('每进程 I/O  : %s' % ('可用' if Path('/proc/self/io').exists() else
                          '不可用（内核未开 CONFIG_TASK_IO_ACCOUNTING）→ iotop / pidstat -d 拿不到数据'))
    print()
    print('块设备挂载（★ 为会转的机械盘，休眠对象）：')
    for mount in mounts:
        star = '★' if mount['mountpoint'] in hdd_mountpoints else ' '
        note = ''
        if mount['name'] in arrays:
            note = '   → %s %s，成员 %s' % (mount['name'], arrays[mount['name']]['level'],
                                        ' + '.join(arrays[mount['name']]['members']))
        elif mount['name'] in member_of:
            note = '   ← %s 的成员' % member_of[mount['name']]
        print('  %s %-16s → %-24s %-8s%s' % (star, mount['device'], mount['mountpoint'], mount['fstype'], note))

    # -- 1. 块设备采样（先做，避免被目录扫描干扰）-------------------------
    section('1. 采样 %d 秒：哪个分区在写' % args.seconds)
    print('（只看块设备计数，此时脚本不做任何目录遍历）')
    before = read_diskstats()
    started = time.time()
    time.sleep(args.seconds)
    after = read_diskstats()
    elapsed = max(time.time() - started, 0.001)

    def rate(name: str, key: str) -> float:
        old, new = before.get(name), after.get(name)
        if not old or not new:
            return 0.0
        return (new[key] - old[key]) / elapsed

    print()
    print('%-22s %-12s %-32s %11s %11s' % ('挂载点', '设备', '角色', '写入', '读取'))
    busy_mounts: list[str] = []
    for mount in hdd_mounts:
        name = mount['name']
        if not DEVICE_PATTERN.match(name):
            continue
        if name in arrays:
            role = '%s %s: %s' % (name, arrays[name]['level'],
                                  ' + '.join(arrays[name]['members']))
        else:
            role = '物理盘 %s 的分区' % physical_disk(name)
        write = rate(name, 'write_sectors')
        print('%-22s %-12s %-32s %11s %11s' % (
            mount['mountpoint'], name, role, human(write), human(rate(name, 'read_sectors'))))
        if write > 0.5:
            busy_mounts.append(mount['mountpoint'])

    print()
    print('物理盘合计（整盘，含该盘所有分区）：')
    for disk in sorted(n for n in all_stats if HDD_PATTERN.match(n)):
        print('  %-10s 写入 %s   读取 %s' % (disk, human(rate(disk, 'write_sectors')),
                                          human(rate(disk, 'read_sectors'))))
    if not busy_mounts:
        print()
        print('采样窗口内所有机械盘挂载点都没有写入 → 硬盘本来就应该能休眠。')
        print('若日志仍显示 activity detected，说明活动是阵发性的，用 --seconds 加大窗口再试。')

    mirrored: list[str] = []
    for array, info in arrays.items():
        members = info['members']
        if info['level'] != 'raid1' or len(members) < 2:
            continue
        rates = [rate(m, 'write_sectors') for m in members]
        if max(rates) > 1 and max(rates) - min(rates) < max(0.5, max(rates) * 0.02):
            mirrored.append('%s（%s）' % (array, ' + '.join(members)))
    if mirrored:
        print()
        print('⚠ RAID1 镜像写入：%s' % '，'.join(mirrored))
        print('  成员盘写入量几乎完全相等 ⇒ 这些写入来自阵列上的文件系统。')
        print('  写阵列一次 = 写所有成员盘，所以只要阵列上有活动，成员盘全都醒着。')

    # -- 2. 热点文件 ------------------------------------------------------
    hot: list[dict] = []
    if not args.no_scan:
        section('2. 哪些文件正在被改动')
        gap = max(15.0, min(45.0, args.seconds / 2.0))
        # 很多「定时写盘」正好是 30 秒一轮（健康检查、定时任务）。对比窗口如果
        # 也是 30 秒，两次遍历可能都落在同一次写入之后，整轮都看不见，所以刻意
        # 避开 30 秒的整数倍。
        if abs(gap % 30.0) < 5.0:
            gap += 7.0
        targets = hdd_mounts if not busy_mounts else [m for m in hdd_mounts if m['mountpoint'] in busy_mounts]
        print('遍历 %s（深度 %d），间隔 %.0f 秒再看一次；' % (
            '、'.join(m['mountpoint'] for m in targets) or '（无目标）', args.depth, gap))
        print('遍历本身只读元数据，但可能触发 atime 回写，故与第 1 节的采样分开。')
        first_pass: dict[str, tuple[dict[str, tuple[int, int]], bool]] = {}
        for mount in targets:
            first_pass[mount['mountpoint']] = walk_files(mount['mountpoint'], args.depth, args.budget)
        scan_start = time.time()
        time.sleep(gap)
        for mount in targets:
            second, cut = walk_files(mount['mountpoint'], args.depth, args.budget)
            first, cut_first = first_pass[mount['mountpoint']]
            changed = []
            for path, (size, mtime) in second.items():
                old = first.get(path)
                if old is None:
                    changed.append((0, path, '新增'))
                elif size != old[0] or mtime != old[1]:
                    changed.append((size - old[0], path, '改动'))
            changed.sort(key=lambda item: -abs(item[0]))
            note = '（扫描被时间上限截断，可加大 --budget）' if (cut or cut_first) else ''
            print()
            print('--- %s（%s）扫到 %d 个文件，变化 %d 个%s' % (
                mount['mountpoint'], mount['device'], len(second), len(changed), note))
            for delta, path, kind in changed[:args.top]:
                print('  %+10d B  %s  %s' % (delta, kind, path))
                hot.append({'path': path, 'delta': delta, 'mount': mount['mountpoint']})
            if not changed:
                print('  （没有文件被改动 → 写入来自文件系统自身：元数据、journal、WAL 原地重写）')
        print()
        print('（本次对比窗口约 %.0f 秒，含遍历耗时）' % (time.time() - scan_start))

    # -- 3. 系统索引的事件记录 -------------------------------------------
    section('3. 系统文件索引（fanotify）记录到的最近改动')
    print('小米存储的 findexd 用 fanotify 监听硬盘，被改动的文件都会记进事件库，')
    print('这是本机唯一能直接「点名到文件路径」的现成记录。')
    window = max(args.seconds * 10.0, 3600.0)
    event_tally: dict[str, int] = {}
    for db_name, table in (('raw_event_log.db', 'raw_event_log'),
                           ('normalized_event_log.db', 'normalized_event_log')):
        events, note, scale = read_event_log(db_name, table, window, 200, read_uptime())
        print()
        print('--- %s：%s' % (db_name, note))
        if not events:
            continue
        tally: dict[str, int] = {}
        newest = 0
        for event in events:
            key = '%s/%s' % (str(event.get('parent_path') or '').rstrip('/'),
                             event.get('name') or '')
            tally[key] = tally.get(key, 0) + 1
            event_tally[key] = max(event_tally.get(key, 0), tally[key])
            newest = max(newest, int(event.get('event_time_ms') or 0))
        if newest:
            print('    最近一条约在 %s（按开机时长换算，仅供参考）' % time.strftime(
                '%Y-%m-%d %H:%M:%S', time.localtime(boot_wall + newest / scale)))
        for key, count in sorted(tally.items(), key=lambda kv: -kv[1])[:args.top]:
            print('    %4d 次  %s' % (count, key))

    # -- 4. 持有可写句柄的进程 -------------------------------------------
    section('4. 正在硬盘上持有可写文件句柄的进程')
    prefixes = tuple(m.rstrip('/') for m in hdd_mountpoints)
    hits = writable_fds(prefixes)
    fd_owner: dict[str, set[str]] = {}
    for hit in hits:
        fd_owner.setdefault(hit['file'], set()).add('%s(pid %s)' % (hit['comm'], hit['pid']))
    if not hits:
        print('（没有进程直接持有可写句柄 → 写入来自文件系统元数据或内核回写）')
    grouped: dict[str, list[dict[str, str]]] = {}
    for hit in hits:
        grouped.setdefault('%s (pid %s)' % (hit['comm'], hit['pid']), []).append(hit)
    for key in sorted(grouped, key=lambda k: -len(grouped[k])):
        items = grouped[key]
        print('  %-30s %2d 个句柄   %s' % (key, len(items), items[0]['cmd']))
        for item in items[:4]:
            print('        [%s] %s' % (item['mode'], item['file']))
        if len(items) > 4:
            print('        …… 共 %d 个' % len(items))
    print()
    print('（容器内的进程看到的是容器路径，如 /config/...，不在上面的列表里；')
    print('  它们的文件归属见第 5 节的容器挂载反查。）')

    # -- 5. 归属 ----------------------------------------------------------
    section('5. 热点文件属于谁')
    binds = container_binds()
    plugins = plugin_names()
    if hot:
        for item in hot[:args.top]:
            who = []
            if item['path'] in fd_owner:
                who.append('进程 ' + '、'.join(sorted(fd_owner[item['path']])))
            bind_owner = match_owner(item['path'], binds, plugins, hdd_mountpoints)
            if bind_owner:
                who.append(bind_owner)
            print('  %s' % item['path'])
            print('      改动 %+d B   归属：%s' % (
                item['delta'], '；'.join(who) if who else '系统自身（未匹配到容器或插件）'))
    else:
        print('（本次没有扫到变化文件；写入可能只发生在文件系统元数据上，')
        print('  属正常现象——但元数据写入仍然会唤醒硬盘。）')
    if event_tally:
        print()
        print('系统索引（fanotify）最近记录到的改动文件（这是最可靠的「点名」证据）：')
        for path, count in sorted(event_tally.items(), key=lambda kv: -kv[1])[:args.top]:
            bind_owner = match_owner(path, binds, plugins, hdd_mountpoints)
            print('  %s' % path)
            print('      最近 1 小时内被改 %d 次   归属：%s' % (
                count, bind_owner or '系统自身（未匹配到容器或插件）'))
    on_disk = [b for b in binds if b['source'].startswith('/nas')]
    if on_disk:
        print()
        print('硬盘上的容器挂载（宿主机目录 → 容器）：')
        for bind in on_disk:
            label = bind['container']
            if bind['plugin']:
                label = '%s / %s' % (label, plugins.get(bind['plugin'], bind['plugin']))
            print('  %-52s → %-28s %-8s [%s]' % (bind['source'], label, bind['target'], bind['running']))
    else:
        print()
        print('（没有容器把目录挂到硬盘上）')

    # -- 6. 结论 ----------------------------------------------------------
    section('6. 结论与建议')
    raid_busy = [m for m in hdd_mounts if m['name'] in arrays and m['mountpoint'] in busy_mounts]
    if mirrored:
        print('· 硬盘写入主要来自 RAID 阵列，而不是用户数据分区：')
        for mount in raid_busy:
            print('    %s（%s）' % (mount['mountpoint'], mount['device']))
        print('  这类分区放的是系统数据库（文件索引、相册、媒体库等）。')
        print('  只要系统服务还在写它，阵列上每一块盘都会一直被唤醒；')
        print('  这时候「是哪个插件」不是重点，重点是「谁触发了这些系统写入」。')
    if hot:
        owners: dict[str, int] = {}
        for item in hot:
            names = []
            if item['path'] in fd_owner:
                names.append('进程 ' + '、'.join(sorted(fd_owner[item['path']])))
            bind_owner = match_owner(item['path'], binds, plugins, hdd_mountpoints)
            if bind_owner:
                names.append(bind_owner)
            label = '；'.join(names) if names else '系统自身'
            owners[label] = owners.get(label, 0) + abs(item['delta'])
        print('· 按改动字节数排序的写入来源：')
        for who, total in sorted(owners.items(), key=lambda kv: -kv[1])[:6]:
            print('    %-64s %d B' % (who, total))
    if event_tally:
        event_owners: dict[str, int] = {}
        for path, count in event_tally.items():
            label = match_owner(path, binds, plugins, hdd_mountpoints) or '系统自身'
            event_owners[label] = event_owners.get(label, 0) + count
        print('· 按系统索引事件次数排序的触发来源：')
        for who, total in sorted(event_owners.items(), key=lambda kv: -kv[1])[:6]:
            print('    %-64s %d 次' % (who, total))
        print('  有事件就说明有文件被改动，改动的文件所在分区会被写入；')
        print('  如果它同时落在 RAID 成员盘上，两块盘都会被唤醒。')
    if not mirrored and not hot:
        print('· 采样窗口内没有发现明显写入。可加大 --seconds 再试。')

    print()
    print('排查方法（逐层往下定位，本脚本就是照这个顺序出报告的）：')
    print('  1) hdidle 日志只给整盘计数，先看分区：写的是用户数据分区，还是系统分区？')
    print('  2) 若写入落在 RAID 成员上且各成员写入量对称，问题在「谁写阵列」，')
    print('     而不在「哪个分区」——阵列每次写入都会唤醒所有成员盘。')
    print('  3) 定位文件：/proc/<pid>/io 不可用时，用文件 mtime + 大小增量（第 2 节）')
    print('     和系统 fanotify 事件库（第 3 节）点名到具体文件。')
    print('  4) 定位归属：用容器 bind 挂载（第 5 节）反查文件属于哪个插件。')
    print('  5) 验证：把可疑容器停 2 分钟，再跑一次本脚本，看写入是否消失。')
    print('     确认后对症处理（换目录、放宽定时任务、停掉不必要的服务）。')
    print()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
