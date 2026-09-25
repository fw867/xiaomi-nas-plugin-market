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
import subprocess
import threading
import time
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

# hdparm -C 的可能输出
STANDBY_STATES = {'standby', 'sleeping'}
ACTIVE_STATES = {'active/idle', 'idle', 'unknown'}

_lock = threading.Lock()
_sampler = None
_stop_sampler = threading.Event()


class Error(Exception):
    """可直接展示给用户的错误。"""


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
        apply_timeout(minutes)
        state = read_state()
        state['minutes'] = minutes
        write_state(state)
    # add_event 自己会加锁，必须放在锁外，否则死锁
    add_event(None, 'config', f'休眠时间设为 {minutes} 分钟')
    return minutes


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
        _stop_sampler.wait(SAMPLE_INTERVAL)


def start_sampler() -> None:
    global _sampler
    if _sampler and _sampler.is_alive():
        return
    _stop_sampler.clear()
    _sampler = threading.Thread(target=sampling_loop, daemon=True)
    _sampler.start()


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
    return {
        'ok': True,
        'version': VERSION,
        'appSwitch': app_switch(),
        'hdidleActive': hdidle_active(),
        'managed': managed(),
        'minutes': configured_minutes(),
        'effectiveMinutes': round(seconds / 60) if seconds else None,
        'officialMinutes': OFFICIAL_SECONDS // 60,
        'minMinutes': MIN_MINUTES,
        'maxMinutes': MAX_MINUTES,
        'presets': [10, 20, 30, 60, 120, 240],
        'disks': disk_summary(),
    }
