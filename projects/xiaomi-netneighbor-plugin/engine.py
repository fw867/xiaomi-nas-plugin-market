#!/usr/bin/env python3
"""网络邻居插件的核心逻辑：WSD 回应器接管 + Samba 共享目录管理。

这个模块只做三件事，全部与系统交互的地方都留了注入点（`runner`、`samba_mgr`、
`systemctl`、`responder_factory`），所以单元测试可以在完全不碰系统的情况下跑：

1. **发现服务（可选开关）**：页面上的「网络发现」开关打开时，启动时
   `systemctl stop wsdd` + `killall wsdd` 兜底，再用 systemd drop-in
   （`/etc/systemd/system/wsdd.service.d/netneighbor.conf`）把官方单元变成
   **空操作**，然后在**本进程内**用 `wsd.WsdResponder` 起一个自己的回应器
   （官方 wsdd 缺 `wsd:AppSequence`、又不响应 WS-Transfer `Get`，见 wsd.py 的说明）；
   关闭时（或停止/卸载）发 Bye、删掉 drop-in、`daemon-reload` 并把官方服务起回来。
   开关状态落盘在 `<DATA_DIR>/settings.json`，服务重启后按落盘状态生效。
2. **账号 → 共享目录**：解析 `/etc/config/sambauser`、`/etc/config/sambashare` 与
   `/etc/samba/users.map`，列出每个账号当前有哪些共享；新增/删除走 `smb_mgr.sh`。
   批量新增（`add_shares`）在**所有** `add_dir` 之后只跑一次 `init_config` + reload，
   再逐个核对（`share_in_config`）。
3. **命名空间**：插件自己建的共享叫 `<账号>_nb_<序号>`，永远不和 App 生成的
   `<账号>_<id>`（以及 `public`）撞名；删除时也只允许删自己这一族。

真机（NAS 192.168.1.8）实测确认的硬约束（别按「看起来合理」改回去）：

- `smb_mgr.sh shares add_dir` 的 `<user_list>` 要传**账号名**（如 `fw867`），
  `smb_mgr.sh` 自己会加 `samba` 前缀；传 `sambafw867` 会在 `pdbedit` 那步失败（exit 1）。
- **共享的身份是 `/etc/config/sambashare` 的段 id**（`smb_mgr.sh` 的 `add_dir`/`del_dir`
  用的就是它），`option name` 是资源管理器里显示的**共享名**，也就是 `/var/etc/smb.conf`
  里的 section 名。真机样例：段 id `u3943892_nb_1` → `option name '照片-3943892'` →
  `[照片-3943892]`。`/etc/config/sambauser` 的段 id 是 `<NAS用户>_<序号>`，账号名在
  `option name`，`list dirs` 是**目录路径**，不是共享段 id。
- 接管官方 wsdd 走 systemd drop-in（`ExecStart`/`ExecStop`/`ExecReload` 置成
  `/bin/true`）：官方 `smb_mgr.sh` 收尾那句 `systemctl restart wsdd` 依旧返回 0。
  早年用 `systemctl mask` 会让那一步必然返回 1，用户在 App 里看到「保存共享失败」
  （即使 smb.conf 已经写好）——所以不要退回 mask。
- 清理残留的 wsdd 进程要用**真机上存在的**命令：这台 NAS 没有 `pkill`（找不到命令，
  exit 127），只有 `killall`（官方 `/usr/bin/wsd` 包装脚本用的也是 `killall wsdd`），
  见 `resolve_kill()`；两个都没有就如实记日志，不假装清理过。
- **不要依赖 `smb_mgr.sh reload` 的退出码**：它最后一步是 `systemctl restart wsdd`，
  这一步在有些机器/有些版本上会返回非 0（虽然 smb.conf 其实已经生成好）。
  所以这里自己走 `init_config` + `systemctl reload smb nmb` 两步，再回头核对配置。
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import wsd

VERSION = '0.1.0'
PLUGIN_KEY = 'netneighbor'

# ---------------------------------------------------------------------------
# 路径与外部命令（测试里全部打桩；Env 覆盖是为了在别处排查问题）
# ---------------------------------------------------------------------------
APP_ROOT = Path(os.environ.get('APP_ROOT', '/etc'))
VAR_ETC = Path(os.environ.get('VAR_ETC', '/var/etc'))
DATA_DIR = Path(os.environ.get('DATA_DIR', '/data/plugin/netneighbor'))
STATE_FILE = DATA_DIR / 'settings.json'
WEB_DIR = Path(os.environ.get('WEB_DIR', str(Path(__file__).resolve().parent / 'web')))

SAMBASHARE_CONFIG = APP_ROOT / 'config' / 'sambashare'
SAMBAUSER_CONFIG = APP_ROOT / 'config' / 'sambauser'
SAMBA_CONFIG = APP_ROOT / 'config' / 'samba'
SMB_USER_MAP = APP_ROOT / 'samba' / 'users.map'
SMB_CONF = VAR_ETC / 'smb.conf'

SMB_MGR = os.environ.get('SMB_MGR') or shutil.which('smb_mgr.sh') or '/usr/bin/smb_mgr.sh'
SYSTEMCTL = os.environ.get('SYSTEMCTL') or shutil.which('systemctl') or '/bin/systemctl'
WSDD_UNIT = os.environ.get('WSDD_UNIT', 'wsdd')
# 清理残留 wsdd 进程的兜底命令：真机（192.168.1.8）**没有 pkill**（找不到命令，exit 127），
# 只有 killall，官方 `/usr/bin/wsd` 包装脚本用的也是 `killall wsdd`。所以优先 killall。
KILL_CANDIDATES = tuple(os.environ.get('KILL_CANDIDATES', 'killall pkill').split())
KILL = os.environ.get('KILL', '').strip()       # 显式覆盖（排查/测试用）；空则按上面顺序探测
# 官方发现服务的「空操作」drop-in：只动我们自己的这一个文件，绝不改 /usr/lib 下的原 unit
WSDD_DROPIN_DIR = Path(os.environ.get(
    'WSDD_DROPIN_DIR', '/etc/systemd/system/%s.service.d' % WSDD_UNIT))
WSDD_DROPIN = WSDD_DROPIN_DIR / 'netneighbor.conf'
WSDD_DROPIN_TEXT = """# 由网络邻居插件写入：把官方 wsdd 变成空操作，避免两个发现服务抢 3702。
# 删除本文件并 systemctl daemon-reload 即可完全恢复官方行为。
[Service]
ExecStart=
ExecStart=/bin/true
ExecStop=
ExecStop=/bin/true
ExecReload=
ExecReload=/bin/true
RemainAfterExit=yes
"""
SMB_UNITS = tuple(os.environ.get('SMB_UNITS', 'smb nmb').split())
# 官方 wsdd 退出后 3702 一般立刻释放；给它一点时间（最多 ~3 秒）
WSD_PORT_WAIT = float(os.environ.get('WSD_PORT_WAIT', '3'))
# 回应器启动失败时重试几次：真机上「停官方 wsdd」和「自己绑 3702」之间有竞态
# （停在启动中的进程上、端口还没放开），一次失败就永久放弃会让页面一直「未运行」。
RESPONDER_START_RETRIES = int(os.environ.get('RESPONDER_START_RETRIES', '5'))
RESPONDER_START_GAP = float(os.environ.get('RESPONDER_START_GAP', '1'))

# 元数据 HTTP 服务端口：Windows 按 XAddrs 来取设备描述，5357 是 WSD 的惯例端口
METADATA_PORT = int(os.environ.get('WSD_METADATA_PORT', '5357'))
HELLO_INTERVAL = int(os.environ.get('WSD_HELLO_INTERVAL', '900'))

DEFAULT_HOSTNAME = os.environ.get('DEFAULT_HOSTNAME', 'SmartStorage')
DEFAULT_WORKGROUP = os.environ.get('DEFAULT_WORKGROUP', 'WORKGROUP')

# 允许作为共享根的目录白名单 == 用户自己已有共享所在的根 + 兜底挂载点。
#   /home/<uXXXX>/pool0/data  —— 用户数据目录（实测 /home/u3943892/pool0/data）
#   /nas/pool0                —— 存储池的对外挂载点
# 可以用 ALLOWED_ROOTS=/a:/b 显式覆盖（冒号分隔），或用 CONFIG_DERIVED_ROOTS=0 关掉推导。
FALLBACK_ROOTS = tuple(
    item for item in os.environ.get(
        'ALLOWED_ROOTS_FALLBACK', '/home/*/pool0/data:/nas/pool0').split(':') if item)
CONFIG_DERIVED_ROOTS = os.environ.get('CONFIG_DERIVED_ROOTS', '1') != '0'
SYMLINK_ROOT = '/home'

def parse_extra_roots(value) -> tuple:
    """`EXTRA_ROOTS` 的解析：冒号分隔、丢掉空白项。

    **空串 → 空元组**（一个额外根都不追加），这是「运维锁死可共享范围」的开关：
    `ALLOWED_ROOTS=/a:/b EXTRA_ROOTS=` 时 `/a:/b` 就是严格的最终白名单。
    放在常量前面是因为模块导入时就要用它解析环境变量。
    """
    return tuple(item.strip() for item in str(value or '').split(':') if item.strip())


# **追加**的允许共享根（外接设备 / U 盘 / 其它挂载点，默认 `/nas/mnt`）：
# 真机上 NAS 有个服务把 `/nas/mnt/usb` bind 到厂商自己的挂载点 `/mnt/usb-<哈希>` 上，
# 拔盘时 `/nas/mnt/usb` 这个路径就不存在了。`/nas/mnt` 下面是 usb、pa0、pa1 这些
# 挂载点，所以把整个 `/nas/mnt` 作为根，它下面的子目录就都能校验、共享。
# 与 `ALLOWED_ROOTS` 的「显式覆盖」语义不同，`EXTRA_ROOTS` 是往最终列表里**追加**：
#   最终根 = （ALLOWED_ROOTS 有则单独用它，否则 FALLBACK_ROOTS + 按配置派生）+ EXTRA_ROOTS
# 三种取值（见 parse_extra_roots()）：
#   * 未设置       → 默认 `/nas/mnt`，开箱即可共享外接设备里的目录；
#   * 显式设成空串 → **一个都不追加**：这时 `ALLOWED_ROOTS` 就是严格的最终白名单
#                    （`ALLOWED_ROOTS=/a:/b EXTRA_ROOTS=` 能锁死可共享范围）；
#   * 设了值       → 冒号分隔追加，支持单层通配（如 `/nas/mnt:/mnt/usb-*`）。
# 而且只列**当前真实存在**的根（拔盘后不留一个指向空路径的根，见 extra_roots()）。
EXTRA_ROOTS = parse_extra_roots(os.environ.get('EXTRA_ROOTS', '/nas/mnt'))

# 弹窗里「位置」分组的标签规则（纯字符串匹配，见 location_label()）：
#   /nas/pool0 或 /home/<NAS用户>/pool0 之下 → 存储池；
#   /nas/mnt/usb* 或 /mnt/usb-* 之下 → 外接设备；
#   其余（如 /nas/mnt、/nas/mnt/pa0）取末段目录名。
POOL_ROOT_PATTERNS = ('/nas/pool0', '/home/*/pool0')
EXTRA_ROOT_PATTERNS = ('/nas/mnt/usb', '/nas/mnt/usb*', '/mnt/usb-*')
# 目录浏览器里位置按钮的标题覆盖：`/nas/mnt` → 「外接存储」（末段名 mnt 对用户没意义）
BROWSE_LABEL_OVERRIDES = {'/nas/mnt': '外接存储'}

# 插件自建共享的命名空间：<账号>_nb_<序号>
SHARE_PREFIX = '_nb_'
# 段 id（共享标识）仍然是纯 ASCII；显示名（option name）可以是中文（真机：'照片-3943892'），
# 只要不含空白、路径分隔符与 smb.conf 段名里的特殊字符即可。
SHARE_NAME_PATTERN = re.compile(r'^[^\s/\\\[\]:*?"<>|\x00-\x1f\x7f]{1,64}$')
ACCOUNT_PATTERN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.\-]{0,31}$')
NAS_USER_PATTERN = re.compile(r'^u[0-9]{3,}$')
# 主机名（Windows「网络」里显示的那一个）：NetBIOS 友好，1-15 位，首字符必须是字母或数字
HOSTNAME_PATTERN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,14}$')

# 这些是 App / 系统在用的共享，插件一律不许删
PROTECTED_SHARES = frozenset({'public', 'homes', 'printers', 'print$'})

COMMAND_TIMEOUT = int(os.environ.get('COMMAND_TIMEOUT', '60'))
UCI_TIMEOUT = 10

# smb_mgr.sh 常见位置，用来在默认路径不存在时兜底
SMB_MGR_CANDIDATES = (
    '/usr/bin/smb_mgr.sh', '/usr/sbin/smb_mgr.sh', '/sbin/smb_mgr.sh',
    '/usr/local/bin/smb_mgr.sh', '/usr/local/sbin/smb_mgr.sh',
)


class Error(Exception):
    """可以直接展示给用户的中文错误。"""


class Result:
    """外部命令的结果。字段与 `subprocess.CompletedProcess` 保持一致。"""

    def __init__(self, returncode: int = 0, stdout: str = '', stderr: str = ''):
        self.returncode = int(returncode)
        self.stdout = stdout or ''
        self.stderr = stderr or ''

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def __repr__(self) -> str:                                  # pragma: no cover
        return 'Result(returncode=%r, stdout=%r, stderr=%r)' % (
            self.returncode, self.stdout, self.stderr)


def installed_version() -> str:
    """插件包的真实版本（目录名里带着版本；源码树里跑就回退到 VERSION）。"""
    parts = Path(__file__).resolve().parent.name.split('-')
    if len(parts) > 2 and parts[-1].isdigit() and parts[-2].isdigit():
        return '-'.join(parts[:-2])
    return VERSION


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def run(command: list, timeout: int = COMMAND_TIMEOUT, check: bool = False) -> Result:
    """默认的命令执行器：全部捕获 stdout/stderr，超时抛 Error 而不是挂住请求。"""
    try:
        completed = subprocess.run(
            [str(item) for item in command], capture_output=True, text=True,
            check=False, timeout=timeout)
    except FileNotFoundError as error:
        raise Error('找不到命令 %s' % command[0]) from error
    except subprocess.TimeoutExpired as error:
        raise Error('命令执行超时（%s 秒）：%s'
                    % (timeout, ' '.join(str(c) for c in command))) from error
    except OSError as error:
        raise Error('执行命令失败：%s' % error) from error
    if check and completed.returncode != 0:
        raise Error('命令执行失败：%s' % describe(command, completed))
    return Result(completed.returncode, completed.stdout, completed.stderr)


def describe(command: list, result: Result) -> str:
    """把一次外部命令的原文拼成一段可展示、可排查的文字。"""
    lines = ['$ ' + ' '.join(str(item) for item in command)]
    if result.stdout.strip():
        lines.append('stdout: ' + result.stdout.strip())
    if result.stderr.strip():
        lines.append('stderr: ' + result.stderr.strip())
    lines.append('exit code: %d' % result.returncode)
    return '\n'.join(lines)


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(text, encoding='utf-8', newline='\n')
    temporary.replace(path)


def read_json(path: Path, default):
    try:
        with open(path, encoding='utf-8') as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def write_json(path: Path, value) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, sort_keys=True) + '\n')


def normalize_discovery_enabled(value, default: bool = True) -> bool:
    """落盘的开关值解析：只有明确的假值才算「关闭」，坏值回退到默认。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ('1', 'true', 'yes', 'on'):
            return True
        if text in ('0', 'false', 'no', 'off', ''):
            return False
    return bool(default)


def load_settings(path=None) -> dict:
    """插件自己的设置（元数据端口、Hello 周期、发现开关、主机名）。

    坏文件不该拖垮启动：每项都独立校验，读不懂的就回退到默认值。
    `path` 默认是模块级的 `STATE_FILE`；Engine 会传自己 `data_dir` 下的那份，
    这样单元测试的沙箱与真机数据目录不会互相影响。
    """
    saved = read_json(Path(path) if path else STATE_FILE, {})
    settings = {
        'metadataPort': METADATA_PORT,
        'helloInterval': HELLO_INTERVAL,
        'discoveryEnabled': True,       # 默认接管（升级前的行为），关闭后重启也不会自动接管
        'hostname': '',
    }
    if isinstance(saved, dict):
        for key in ('metadataPort', 'helloInterval'):
            value = saved.get(key)
            if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
                settings[key] = int(value)
        if 'discoveryEnabled' in saved:
            settings['discoveryEnabled'] = normalize_discovery_enabled(
                saved.get('discoveryEnabled'))
        name = saved.get('hostname')
        if isinstance(name, str) and hostname_valid(name):
            settings['hostname'] = name.strip()
    settings['metadataPort'] = max(1, min(65535, int(settings['metadataPort'])))
    settings['helloInterval'] = max(0, int(settings['helloInterval']))
    return settings


def save_settings(settings: dict, path=None) -> None:
    write_json(Path(path) if path else STATE_FILE, settings)


# ---------------------------------------------------------------------------
# uci 配置解析
# ---------------------------------------------------------------------------

def _unquote(text: str) -> str:
    """去掉 uci 单引号/双引号，并解开转义。"""
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ('"', "'"):
        inner = text[1:-1]
        if text[0] == '"':
            inner = inner.replace('\\"', '"').replace('\\\\', '\\')
        return inner
    return text


def parse_uci(text: str) -> list:
    """解析 uci 导出文本，返回 [{'type','name','options','lists'}, ...]。

    只要够用就行：`config <type> ['<name>']`、`option k 'v'`、`list k 'v'`。
    未带名字的 section 用 `cfg%04d` 造一个稳定名字（真实文件里都有名字）。
    """
    sections: list = []
    current = None
    anonymous = 0
    for raw in (text or '').splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        keyword, _, rest = line.partition(' ')
        keyword = keyword.strip()
        rest = rest.strip()
        if keyword == 'config':
            parts = rest.split(None, 1)
            if not parts:
                continue
            kind = parts[0]
            if len(parts) > 1:
                name = _unquote(parts[1])
            else:
                anonymous += 1
                name = 'cfg%04d' % anonymous
            current = {'type': kind, 'name': name, 'options': {}, 'lists': {}}
            sections.append(current)
            continue
        if current is None or keyword not in ('option', 'list'):
            continue
        key, _, value = rest.partition(' ')
        key = key.strip()
        value = _unquote(value)
        if not key:
            continue
        if keyword == 'option':
            current['options'][key] = value
        else:
            current['lists'].setdefault(key, []).append(value)
    return sections


def parse_smb_user_map(text: str) -> dict:
    """`/etc/samba/users.map`：`samba<账号> = <账号>` → {'samba账号': '账号'}。"""
    mapping: dict = {}
    for raw in (text or '').splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        key, _, value = line.partition('=')
        key, value = key.strip(), value.strip()
        if key and value:
            mapping[key] = value
    return mapping


def parse_dirs(value) -> list:
    """`list dirs 'a' 'b'` 与 `option dirs 'a b'` 两种写法都接受。"""
    if isinstance(value, (list, tuple)):
        items = []
        for entry in value:
            items.extend(str(entry).split())
        return items
    return str(value or '').split()


def smb_conf_sections(text: str) -> list:
    """`/var/etc/smb.conf` 里的 section 名（`[照片-3943892]` → 照片-3943892）。"""
    names = []
    for raw in (text or '').splitlines():
        line = raw.strip()
        if line.startswith('[') and line.endswith(']'):
            names.append(line[1:-1].strip())
    return names


# ---------------------------------------------------------------------------
# smb_mgr.sh 命令拼装（纯函数，便于断言 argv）
# ---------------------------------------------------------------------------

def add_dir_command(share_name: str, path: str, user_list, share_point: str = '',
                    force_user: str = '', smb_mgr: str = '') -> list:
    """`smb_mgr.sh shares add_dir <share_name> <path> "<user_list>" <share_point> [force_user]`

    注意 `<user_list>` 是**账号名**（`fw867`），不是 `sambafw867`：smb_mgr.sh 自己加前缀。
    """
    users = user_list if isinstance(user_list, str) else ' '.join(str(i) for i in user_list)
    command = [smb_mgr or SMB_MGR, 'shares', 'add_dir', share_name, path, users,
               share_point or share_name]
    if force_user:
        command.append(force_user)
    return command


def del_dir_command(share_name: str, smb_mgr: str = '') -> list:
    return [smb_mgr or SMB_MGR, 'shares', 'del_dir', share_name]


def add_user_command(share_name: str, user_name: str, smb_mgr: str = '') -> list:
    return [smb_mgr or SMB_MGR, 'shares', 'add_user', share_name, user_name]


def del_user_command(share_name: str, user_name: str, smb_mgr: str = '') -> list:
    return [smb_mgr or SMB_MGR, 'shares', 'del_user', share_name, user_name]


def init_config_command(smb_mgr: str = '') -> list:
    return [smb_mgr or SMB_MGR, 'init_config']


def reload_units_command() -> list:
    """`reload` 的替代方案：两个服务一起 reload。"""
    return ['systemctl', 'reload', *SMB_UNITS]


# smb_mgr.sh 失败时常见的英文短语 → 中文原因
MESSAGE_HINTS = (
    ('exist', '共享名已存在'),
    ('not found', '目标不存在'),
    ('no such', '目标不存在'),
    ('permission', '权限不足'),
    ('denied', '权限不足'),
    ('not a directory', '不是一个目录'),
    ('invalid', '参数无效'),
    ('usage', 'smb_mgr.sh 用法不匹配'),
    ('fail', 'smb_mgr.sh 报告失败'),
)


def translate_failure(text: str, action: str) -> str:
    lowered = (text or '').lower()
    for needle, message in MESSAGE_HINTS:
        if needle in lowered:
            return '%s：%s' % (action, message)
    return '%s失败' % action


# ---------------------------------------------------------------------------
# 发现服务身份（主机名 / 工作组）
# ---------------------------------------------------------------------------

def samba_identity() -> tuple:
    """主机名与工作组：优先 `/etc/config/samba`，读不到就用默认值。"""
    return (samba_config_value('name', DEFAULT_HOSTNAME),
            samba_config_value('workgroup', DEFAULT_WORKGROUP))


def samba_config_value(option: str, default: str = '') -> str:
    """先试 `uci -q get samba.global.<option>`，再直接解析配置文件。"""
    uci = shutil.which('uci') or '/sbin/uci'
    if uci and os.path.exists(uci):
        try:
            result = run([uci, '-q', 'get', 'samba.global.%s' % option], timeout=UCI_TIMEOUT)
            value = (result.stdout or '').strip()
            if result.ok and value:
                return value
        except Error:
            pass
    return config_option(SAMBA_CONFIG, 'samba', option, default)


def config_option(path: Path, section_type: str, option: str, default: str = '') -> str:
    """从 uci 配置文件里取某个 section 的 option（按 section 类型匹配）。"""
    try:
        # encoding 必须显式给：默认走 locale（中文 Windows 上是 cp936），
        # 读到 UTF-8 的 uci 文件会整段被替换成乱码，解析结果永远是空。
        text = Path(path).read_text(encoding='utf-8', errors='replace')
    except OSError:
        return default
    for section in parse_uci(text):
        if section['type'] != section_type:
            continue
        value = section['options'].get(option)
        if value:
            return value
    return default


def hostname_valid(name) -> bool:
    """主机名（WSD 宣告名）是否合法：1-15 位、NetBIOS 友好。

    每条都不满足就返回 False（`hostname_error()` 负责给出中文原因）。
    """
    return HOSTNAME_PATTERN.match(str(name or '').strip()) is not None


def hostname_error(name) -> str:
    """非法主机名的中文原因；空串表示合法。"""
    text = str(name or '').strip()
    if not text:
        return '请填写主机名'
    if len(text) > 15:
        return '主机名最长 15 个字符（当前 %d 个）：Windows 用的是 NetBIOS 名字' % len(text)
    if not re.match(r'^[A-Za-z0-9]', text):
        return '主机名要以字母或数字开头'
    if not HOSTNAME_PATTERN.match(text):
        return '主机名只能包含字母、数字、点、下划线与短横线'
    return ''


def validate_hostname(name) -> str:
    """校验并返回主机名；非法时抛中文 Error。"""
    text = str(name or '').strip()
    message = hostname_error(text)
    if message:
        raise Error(message)
    return text


def lan_xaddrs(address: str, port: int) -> list:
    """XAddrs 就是 Windows 来取设备描述的地址。"""
    if not address:
        return []
    return ['http://%s:%d/' % (address, port)]


# ---------------------------------------------------------------------------
# 共享命名与路径白名单（纯函数）
# ---------------------------------------------------------------------------

def path_exists(path) -> bool:
    """文件系统探测做成可注入的：受管路径是 POSIX 语义，测试要在任意平台造沙箱。"""
    return os.path.exists(path)


def path_isdir(path) -> bool:
    return os.path.isdir(path)


def path_realpath(path) -> str:
    return os.path.realpath(path)


def path_listdir(path) -> list:
    """列目录做成可注入的钩子：测试要在沙箱里造目录树，不能真去读 /home。"""
    return os.listdir(path)


def path_islink(path) -> bool:
    """是不是符号链接（列目录时要把软链跳过）。"""
    return os.path.islink(path)


def normalize_roots(roots) -> list:
    """受管路径一律用 POSIX 语义规范化。

    插件跑在 Linux 上，而这些是**配置里的路径字符串**；用 `os.path.normpath`
    在 Windows 上会把 `/home/...` 变成 `\\home\\...`，让白名单比对失效（测试也会挂）。
    """
    values = []
    for item in roots or ():
        text = str(item).strip()
        if text:
            values.append(posixpath.normpath(text))
    return values


def root_match(path: str, root: str) -> bool:
    """支持 `/home/*/pool0/data` 这种单层通配；否则按目录前缀匹配。"""
    if '*' in root:
        pattern = '^' + re.escape(root).replace(r'\*', '[^/]+') + '$'
        return re.match(pattern, path) is not None
    return path == root or path.startswith(root.rstrip('/') + '/')


def dedupe(values) -> list:
    """去重保序：白名单/位置列表的顺序对页面展示有意义，不要用 set 打乱。"""
    unique: list = []
    for item in values:
        if item not in unique:
            unique.append(item)
    return unique


def under_pattern(path: str, pattern: str) -> bool:
    """`path` 是 `pattern` 本身或落在它之下；`pattern` 支持单层 `*`（如 `/home/*/pool0`）。"""
    regex = '^' + re.escape(str(pattern)).replace(r'\*', '[^/]+') + r'(?:/.*)?$'
    return re.match(regex, str(path)) is not None


def location_label(path) -> str:
    """弹窗里「位置」分组用的标签（纯函数，便于单测）。

    - `/nas/pool0` 或 `/home/<NAS用户>/pool0` 之下 → 「存储池」（内置存储池）；
    - `/nas/mnt/usb*` 或 `/mnt/usb-*` 之下 → 「外接设备」（U 盘，含厂商自己的挂载点）；
    - 其它取末段目录名（用户自定义 `EXTRA_ROOTS` 时也得有个能看懂的标题）。
    """
    text = posixpath.normpath(str(path or '').strip() or '/')
    for pattern in POOL_ROOT_PATTERNS:
        if under_pattern(text, pattern):
            return '存储池'
    for pattern in EXTRA_ROOT_PATTERNS:
        if under_pattern(text, pattern):
            return '外接设备'
    return posixpath.basename(text.rstrip('/')) or text


def browse_location_label(path) -> str:
    """目录浏览器里「位置」按钮的文字：`/nas/mnt` 显示成「外接存储」。

    末段名 `mnt` 对用户没意义，所以浏览弹窗里单独覆盖一下；其它路径（含
    `/nas/mnt/usb` 这类子目录、`/nas/mnt/pa0`）仍然按 `location_label()` 的规则来。
    """
    text = posixpath.normpath(str(path or '').strip() or '/')
    return BROWSE_LABEL_OVERRIDES.get(text) or location_label(text)


def relative_pieces(relative) -> list:
    """把浏览接口的**相对路径**拆成一段段（空段与 `.` 丢掉）。

    `..` 与绝对路径直接拒绝：受管路径是 POSIX 语义，不能用 `Path` 拼
    （Windows 上会变成反斜杠，白名单比对与子目录名都会错）。
    """
    text = str(relative or '').strip()
    if text.startswith('/'):
        raise Error('浏览路径要填相对路径（不要以 / 开头）')
    pieces = []
    for piece in text.split('/'):
        piece = piece.strip()
        if not piece or piece == '.':
            continue
        if piece == '..':
            raise Error('浏览路径里不允许出现 ..')
        pieces.append(piece)
    return pieces


def within_roots(path: str, roots) -> bool:
    return any(root_match(path, root) for root in normalize_roots(roots))


def extra_roots() -> list:
    """`EXTRA_ROOTS` 里**当前真实存在**的追加根（外接设备）；`EXTRA_ROOTS=` 时为空。

    只保留存在的：拔盘时 `/nas/mnt/usb` 不存在，就不该出现在白名单里（否则页面/校验
    会给出一个指向空路径的根）。`*` 只吃一层（`/mnt/usb-*`）：列父目录，把匹配到的
    真实目录逐个收进来。这是路径探测唯一的地方，测试通过可注入钩子
    （`path_isdir` / `path_listdir`）在沙箱里造 U 盘。
    """
    found: list = []
    for root in normalize_roots(EXTRA_ROOTS):
        if '*' in root:
            parent = posixpath.dirname(root)
            if not parent or not path_isdir(parent):
                continue
            try:
                names = path_listdir(parent)
            except OSError:
                continue
            for name in sorted(names, key=lambda item: str(item)):
                candidate = posixpath.join(parent, str(name))
                if root_match(candidate, root) and path_isdir(candidate):
                    found.append(candidate)
            continue
        if path_isdir(root):
            found.append(root)
    return dedupe(found)


def configured_extra_roots() -> list:
    """`EXTRA_ROOTS` 的配置值（**不**判断存在性）；`EXTRA_ROOTS=` 时为空列表。

    弹窗要拿它把「配置了但当前没插上」的位置也显示出来（标成未接入），
    白名单那边则用 `extra_roots()`（只留存在的）。
    """
    return dedupe(normalize_roots(EXTRA_ROOTS))


def data_root_for(root: Path, user_id: str) -> str:
    """`/home/u3943892/pool0/data`：白名单里的用户数据根（按实际挂载点推导）。"""
    if not user_id:
        return ''
    return str(Path(root) / 'home' / user_id / 'pool0' / 'data')


def paths_root_for(root: Path) -> str:
    return str(Path(root) / 'nas' / 'pool0')


def derive_allowed_roots(sections, root: Path = None) -> list:
    """从现有配置推导白名单：账号的 `option user` + 已有共享的 `path` 的父目录。

    这样插件只可能把目录共享到用户本来就能共享的地方（同一挂载点内），
    不会凭空放行 `/etc`、`/` 之类。
    """
    root = Path(root) if root is not None else Path('/')
    values: list = []
    user_ids = []
    for section in sections.get('sambauser', []) or []:
        user_id = str(section['options'].get('user', '') or '').strip()
        if NAS_USER_PATTERN.match(user_id):
            user_ids.append(user_id)
            values.append(data_root_for(root, user_id))
    for section in sections.get('sambashare', []) or []:
        path = str(section['options'].get('path', '') or '').strip()
        if path.startswith('/'):
            parent = posixpath.dirname(posixpath.normpath(path))
            if parent and parent != '/':
                values.append(parent)
    if not user_ids:
        values.extend(str(item) for item in FALLBACK_ROOTS)
    if not any(str(item).startswith(str(Path(root) / 'nas' / 'pool0')) for item in values):
        values.append(paths_root_for(root))
    return normalize_roots(values)


def resolve_allowed_roots(sections=None, root: Path = None, extra=None) -> list:
    """白名单最终值：环境变量显式覆盖 > 按配置推导 > 兜底默认值，**再追加** EXTRA_ROOTS。

    `ALLOWED_ROOTS` 仍然是**显式覆盖**（给了就只用它，现有测试依赖这一点）；
    `EXTRA_ROOTS`（外接设备 / U 盘）是**追加**来源，两种情况下都并进最终列表里。
    把 `EXTRA_ROOTS` **显式设成空串**就不追加任何额外根，此时 `ALLOWED_ROOTS`
    就是严格的最终白名单（运维可用它锁死可共享范围，见 parse_extra_roots()）。
    `extra` 默认取 `extra_roots()`：只包含当前真实存在的追加根，拔盘后不会留下空根。
    """
    if extra is None:
        extra = extra_roots()
    override = os.environ.get('ALLOWED_ROOTS', '').strip()
    if override:
        values = normalize_roots(override.split(':'))
    else:
        values = normalize_roots(FALLBACK_ROOTS)
        if CONFIG_DERIVED_ROOTS and sections:
            values.extend(derive_allowed_roots(sections, root))
        if not values:
            values = normalize_roots(FALLBACK_ROOTS)
    values.extend(normalize_roots(extra))
    return dedupe(values)


def validate_share_path(path, roots, label: str = '目录') -> str:
    """校验共享目录：绝对路径、无 `..`、在允许的根目录内、存在且是目录。

    同时校验符号链接解析后的真实路径（`/home` 下的软链是常见越界手法）。
    """
    text = str(path or '').strip()
    if not text:
        raise Error('请填写要共享的%s路径' % label)
    if not text.startswith('/'):
        raise Error('%s必须是绝对路径（以 / 开头）' % label)
    normalized = posixpath.normpath(text)
    if any(piece == '..' for piece in normalized.split('/')):
        raise Error('%s里不允许出现 ..' % label)
    allowed = normalize_roots(roots)
    if allowed and not within_roots(normalized, allowed):
        raise Error('该%s不在允许的根目录内。允许的根目录：%s'
                    % (label, '、'.join(allowed)))
    # 文件系统探测走可注入的钩子：受管路径是 POSIX 语义，测试要在任意平台上
    # 造沙箱就得能替换掉 exists/isdir/realpath。
    if not path_exists(normalized):
        raise Error('%s不存在：%s' % (label, normalized))
    if not path_isdir(normalized):
        raise Error('%s不是目录：%s' % (label, normalized))
    real = path_realpath(normalized)
    if allowed:
        # 符号链接解析后的真实路径也必须落在白名单（或用户主目录）内
        if not (within_roots(real, allowed) or real.startswith(SYMLINK_ROOT.rstrip('/') + '/')):
            raise Error('%s解析到的真实路径不在允许范围内：%s' % (label, real))
    return normalized


def share_name_part(value: str) -> str:
    """账号名里不该出现的东西换成下划线，避免拼出奇怪的共享名。"""
    cleaned = re.sub(r'[^A-Za-z0-9_.\-]', '_', str(value or '')).strip('_.-')
    return cleaned or 'user'


def plugin_share_name(account: str, taken) -> str:
    """`<账号>_nb_<最小可用序号>`；序号从 1 开始，跳过一切已占用的名字。"""
    prefix = '%s%s' % (share_name_part(account), SHARE_PREFIX)
    used = {str(name) for name in (taken or ())}
    index = 1
    while '%s%d' % (prefix, index) in used:
        index += 1
    return '%s%d' % (prefix, index)


def is_plugin_share(share_name: str, account: str = '') -> bool:
    """插件自建共享：`<账号>_nb_<序号>`；给账号时要求前缀一致。"""
    text = str(share_name or '')
    if not text:
        return False
    if account:
        prefix = '%s%s' % (share_name_part(account), SHARE_PREFIX)
        if not text.startswith(prefix):
            return False
        text = text[len(prefix):]
        return re.match(r'^\d+$', text) is not None
    return re.search(r'(?:^|_)nb_\d+$', text) is not None


def validate_share_name(name: str) -> str:
    text = str(name or '').strip()
    if not SHARE_NAME_PATTERN.match(text):
        raise Error('共享名无效：只能包含字母、数字、下划线、点与短横线（1-64 位）')
    return text


def validate_user_list(value, default: str = '') -> str:
    """`<user_list>` 是**账号名**（空格分隔多个）；空的话回退到该共享的账号。

    smb_mgr.sh 内部自己加 `samba` 前缀（实测传 `sambafw867` 会在 pdbedit 那步失败），
    所以这里只接受账号名，并明确拒绝 `sambaXXX` 形式的输入。
    """
    if isinstance(value, (list, tuple)):
        items = [str(item) for item in value]
    else:
        items = str(value or '').split()
    items = [item.strip() for item in items if str(item).strip()]
    if not items and default:
        items = [str(default)]
    cleaned = []
    for item in items:
        if not ACCOUNT_PATTERN.match(item):
            raise Error('账号名不合法：%s（只能包含字母、数字、_ . -）' % item)
        if item.startswith('samba') and ACCOUNT_PATTERN.match(item[5:] or ''):
            raise Error('user_list 要填账号名（如 fw867），不要填 SMB 账号 %s；'
                        'smb_mgr.sh 会自己加 samba 前缀' % item)
        cleaned.append(item)
    if not cleaned:
        raise Error('至少要有一个可访问的账号')
    return ' '.join(cleaned)


def validate_force_user(value) -> str:
    """`force_user` 是 NAS 用户号（如 u3943892），不是账号名。

    页面上的勾选框发的是 `'auto'`（或空）：交给调用方回退到 sambauser 里的 `option user`。
    """
    text = str(value or '').strip()
    if not text or text == 'auto':
        return ''
    if not (NAS_USER_PATTERN.match(text) or ACCOUNT_PATTERN.match(text)):
        raise Error('force_user 不合法：%s' % text)
    return text


def account_to_smb_user(account: str, mapping: dict) -> str:
    """`<账号>` → SMB 账号（仅用于**展示**；命令行参数不能用它）。"""
    account = str(account or '').strip()
    if not account:
        return ''
    smb = 'samba%s' % account
    if (mapping or {}).get(smb) == account:
        return smb
    for smb_name, mapped in (mapping or {}).items():
        if mapped == account and smb_name:
            return smb_name
    return smb


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class Engine:
    """把「发现服务」与「共享目录」两块能力包在一起，方便 HTTP 层与测试使用。"""

    def __init__(self, data_dir=None, runner=None, responder_factory=None,
                 samba_mgr=None, systemctl=None, start_responder=True, on_log=None):
        self.data_dir = Path(data_dir) if data_dir else DATA_DIR
        # 落盘设置（开关 + 主机名 + 端口/Hello 周期）；模块级同名常量是默认位置
        self.state_file = self.data_dir / 'settings.json'
        self.identity_file = self.data_dir / 'wsd.json'
        self.samba_mgr = samba_mgr or SMB_MGR
        self.systemctl = systemctl or SYSTEMCTL
        # runner / responder_factory 都做成实例属性：测试可以整体替换，不需要碰系统
        self.runner = runner if runner is not None else (lambda: self._run)
        self.responder_factory = responder_factory or (
            lambda **kwargs: wsd.WsdResponder(**kwargs))
        self.log_lines: list = []
        self.lock = threading.RLock()
        self.discovery: dict = {
            'running': False, 'startedAt': 0, 'msg': '', 'managed': False,
            'dropin': False, 'metadataPosts': 0, 'lastMetadataAt': 0, 'port': 0,
            'enabled': True,
        }
        self.settings = load_settings(self.state_file)
        self.explicit_hostname = str(self.settings.get('hostname') or '').strip()
        self.hostname, self.workgroup = samba_identity()
        self.hostname = self._resolve_hostname()
        self.discovery['enabled'] = bool(self.settings.get('discoveryEnabled', True))
        self.account_cache: tuple = (0.0, None)
        self.share_cache: tuple = (0.0, None)
        # 白名单缓存：(时间戳, 值, 当时的追加根列表)——外接设备可插拔，见 allowed_roots()
        self.root_cache: tuple = (0.0, None, ())
        self.on_log = on_log or (lambda message: None)
        self.responder = None
        if start_responder:
            self.start()

    # ---- 身份（主机名 / 工作组）------------------------------------------
    def _resolve_hostname(self) -> str:
        """主机名优先级：settings['hostname'] > `/etc/config/samba` 的 `option name` > 默认值。"""
        if self.explicit_hostname:
            return self.explicit_hostname
        return samba_config_value('name', DEFAULT_HOSTNAME)

    # ---- 日志 -----------------------------------------------------------
    def log(self, message: str) -> None:
        line = '%s %s' % (time.strftime('%H:%M:%S'), message)
        self.log_lines.append(line)
        del self.log_lines[:-300]
        self.on_log(line)

    def recent_log(self, limit: int = 60) -> list:
        return list(self.log_lines)[-max(1, int(limit)):]

    # ---- 命令执行 -------------------------------------------------------
    def _run(self, command: list, timeout: int = COMMAND_TIMEOUT) -> Result:
        return run(command, timeout=timeout)

    def exec(self, command: list, timeout: int = COMMAND_TIMEOUT) -> Result:
        """统一出口：任何外部命令都经过这里（便于打桩与统计）。"""
        return self.runner()([str(item) for item in command], timeout)

    def smb(self, command: list, action: str, timeout: int = COMMAND_TIMEOUT) -> Result:
        """跑 smb_mgr.sh 并把 stdout/stderr 原文带回去，失败给中文原因。"""
        result = self.exec(command, timeout=timeout)
        if not result.ok:
            raise Error('%s\n%s' % (
                translate_failure('%s %s' % (result.stdout, result.stderr), action),
                describe(command, result)))
        return result

    def samba_mgr_path(self) -> str:
        if os.path.exists(self.samba_mgr):
            return self.samba_mgr
        for candidate in SMB_MGR_CANDIDATES:
            if os.path.exists(candidate):
                return candidate
        return self.samba_mgr

    # ---- 配置读取 -------------------------------------------------------
    def _config_path(self, name: str) -> Path:
        mapping = {
            'sambauser': SAMBAUSER_CONFIG,
            'sambashare': SAMBASHARE_CONFIG,
            'samba': SAMBA_CONFIG,
            'users.map': SMB_USER_MAP,
            'smb.conf': SMB_CONF,
        }
        if name not in mapping:
            raise Error('未知配置：%s' % name)
        return mapping[name]

    def read_config(self, name: str) -> str:
        path = self._config_path(name)
        try:
            return path.read_text(encoding='utf-8', errors='replace')
        except OSError as error:
            raise Error('读取配置失败：%s（%s）' % (path, error)) from error

    def read_config_soft(self, name: str) -> str:
        """核对用：读不到就当空（不能因为文件缺了就把操作判失败）。"""
        try:
            return self._config_path(name).read_text(encoding='utf-8', errors='replace')
        except OSError:
            return ''

    def config_sections(self, name: str) -> list:
        return parse_uci(self.read_config(name))

    def user_map(self) -> dict:
        try:
            text = SMB_USER_MAP.read_text(encoding='utf-8', errors='replace')
        except OSError:
            return {}
        return parse_smb_user_map(text)

    def samba_users(self) -> list:
        """每个账号一段。真机字段（NAS 实测）：

            config sambauser 'u3943892_1'
                option user 'u3943892'      ← NAS 用户号
                option name 'fw867'         ← 账号名（SMB 用户是 samba<账号>）
                list dirs '/home/u3943892/pool0/data/下载'   ← 目录路径，不是共享段 id

        `sambaUser` 故意留空：它由 `/etc/samba/users.map`（`samba<账号> = <账号>`）或
        `samba<账号>` 推导。**不要**把 `option name` 当成 SMB 账号——那样会调
        `pdbedit -u fw867` 这种不存在的用户。
        """
        accounts = []
        for section in self.config_sections('sambauser'):
            options = section['options']
            account = options.get('name') or options.get('user') or section['name']
            dirs = parse_dirs(section['lists'].get('dirs', options.get('dirs', '')))
            accounts.append({
                'section': section['name'],
                'account': account,
                'user': options.get('user', ''),
                'sambaUser': '',
                'dirs': dirs,
                'id': options.get('id', ''),
            })
        return accounts

    def samba_shares(self) -> list:
        """每个共享一段。真机字段（NAS 实测）：

            config sambashare 'u3943892_nb_1'      ← 段 id 才是共享标识
                option name '照片-3943892'          ← 资源管理器里显示的名字
                option path '/home/u3943892/pool0/data/我的照片'
                option user_force 'u3943892'
                list users 'sambafw867'

        `smb_mgr.sh` 的 `add_dir`/`del_dir`、以及「是不是插件自己建的」都要用段 id，
        所以这里 `name` = 段 id，显示名单独放在 `display`。
        """
        shares = []
        for section in self.config_sections('sambashare'):
            options = section['options']
            users = list(section['lists'].get('users', []))
            if not users and options.get('users'):
                users = str(options['users']).split()
            shares.append({
                'name': section['name'],
                'display': options.get('name') or section['name'],
                'section': section['name'],
                'path': options.get('path', ''),
                'users': users,
                'forceUser': options.get('user_force', ''),
                'status': options.get('status', '1'),
                'comment': options.get('comment', ''),
            })
        return shares

    def allowed_roots(self, force: bool = False) -> list:
        """白名单：用户在配置里本来就共享到的目录的父目录 + 用户数据根 + /nas/pool0，
        再追加外接设备（`EXTRA_ROOTS`，U 盘）。

        外接设备是可插拔的，所以缓存把「当前的追加根列表」也算进有效期判断：插上/拔掉
        U 盘后下一次调用就能反映出来（既不用等 30 秒过期，也不用调用方传 force=True）。
        """
        now = time.time()
        stamp, cached, cached_extra = self.root_cache
        extra = extra_roots()
        if (not force and cached is not None and now - stamp < 30
                and list(cached_extra or ()) == extra):
            return cached
        try:
            sections = {'sambauser': self.config_sections('sambauser'),
                        'sambashare': self.config_sections('sambashare')}
        except Error:
            sections = None
        values = resolve_allowed_roots(sections, extra=extra)
        self.root_cache = (now, values, extra)
        return values

    def accounts(self, force: bool = False) -> list:
        """账号 → 它当前的共享目录（含插件自建的那些）。

        归属**不能**靠 `sambauser` 的 `list dirs`：真机上那里存的是目录路径
        （`/home/u3943892/pool0/data/下载`），不是共享段 id。真正的关联是
        `sambashare` 的 `list users` 里有没有 `samba<账号>`（见 shares()）。
        `list dirs` 只用来如实报告「配置里有目录但没有对应共享段」。
        """
        now = time.time()
        stamp, cached = self.account_cache
        if not force and cached is not None and now - stamp < 5:
            return cached
        mapping = self.user_map()
        by_account: dict = {}
        for share in self.shares(force=True):
            if share.get('account'):
                by_account.setdefault(share['account'], []).append(share)
        result = []
        for entry in self.samba_users():
            account = entry['account']
            samba_user = entry['sambaUser'] or account_to_smb_user(account, mapping)
            items = []
            for share in by_account.get(account, []):
                items.append({
                    'name': share['name'],          # UCI 段 id：增删共享用的就是它
                    'display': share['display'],
                    'path': share['path'],
                    'users': share['users'],
                    'status': share['status'],
                    'forceUser': share['forceUser'],
                    'comment': share['comment'],
                    'custom': bool(share.get('custom')),
                    'missing': False,
                    'deletable': bool(share.get('custom')),
                })
            known = {item['path'].rstrip('/') for item in items}
            for path in entry['dirs']:
                if path.rstrip('/') not in known:
                    items.append({
                        'name': '', 'display': posixpath.basename(path.rstrip('/')),
                        'path': path, 'users': [], 'status': '',
                        'forceUser': '', 'comment': '配置里有目录，但没有对应的共享段',
                        'custom': False, 'missing': True, 'deletable': False,
                    })
            result.append({
                'account': account,
                'user': entry['user'],
                'sambaUser': samba_user,
                'id': entry['id'],
                'shares': items,
                'customCount': sum(1 for item in items if item['custom']),
            })
        self.account_cache = (now, result)
        return result

    def shares(self, force: bool = False) -> list:
        now = time.time()
        stamp, cached = self.share_cache
        if not force and cached is not None and now - stamp < 5:
            return cached
        owners: dict = {}
        for entry in self.samba_users():
            owners.setdefault('samba%s' % entry['account'], entry['account'])
            if entry['sambaUser']:
                owners.setdefault(entry['sambaUser'], entry['account'])
            if entry['user']:
                owners.setdefault(entry['user'], entry['account'])
        result = []
        for share in self.samba_shares():
            account = ''
            for user in share['users']:
                if user in owners:
                    account = owners[user]
                    break
            if not account:
                for name, owner in owners.items():
                    if share['name'].startswith('%s_' % owner):
                        account = owner
                        break
            result.append({**share, 'account': account,
                           'custom': bool(account) and is_plugin_share(share['name'], account)})
        self.share_cache = (now, result)
        return result

    def account_names(self) -> set:
        return {item['account'] for item in self.accounts()}

    def taken_share_names(self) -> set:
        return {item['name'] for item in self.shares()}

    def account_entry(self, account: str) -> dict:
        for item in self.accounts():
            if item['account'] == account:
                return item
        raise Error('没有这个账号：%s' % account)

    # ---- 生效与核对 -----------------------------------------------------
    def reload_command(self) -> list:
        return [self.systemctl, 'reload', *SMB_UNITS]

    def smb_service_active(self) -> bool:
        try:
            result = self.exec([self.systemctl, 'is-active', SMB_UNITS[0]], timeout=15)
        except Error:
            return False
        return (result.stdout or '').strip().startswith('active')

    def share_in_config(self, share_name: str) -> bool:
        """核对某个共享是否真的生效了。

        `share_name` 是 **UCI 段 id**（`add_dir`/`del_dir` 用的就是它）。核对分两步：
        1. `/etc/config/sambashare` 里能按段 id 找到这一段；
        2. `/var/etc/smb.conf` 里有对应的 section——注意 smb.conf 里的段名是
           **显示名**（`sambashare` 的 `option name`，如 `照片-3943892`），
           不是段 id。直接拿段 id 去比 smb.conf 会永远为 False，让 `apply_samba`
           误报「操作未生效」（真机实测踩过）。
        """
        display = ''
        found = False
        for section in parse_uci(self.read_config_soft('sambashare')):
            if section['name'] == share_name:
                found = True
                display = section['options'].get('name') or section['name']
                break
        if not found:
            return False
        return display in smb_conf_sections(self.read_config_soft('smb.conf'))

    def apply_samba(self, share_name: str, expect_present: bool) -> dict:
        """`init_config` + `systemctl reload smb nmb`，然后核对配置里到底有没有。

        刻意**不**调用 `smb_mgr.sh reload`：它最后一步 `systemctl restart wsdd`
        在有些机器上会返回非 0（即使 smb.conf 其实已经生成好了），
        拿它的退出码当成功判定就会误报，所以这里自己走这两步并回头核对配置。
        """
        commands = []
        errors = []
        result = self.exec(init_config_command(self.samba_mgr_path()), timeout=60)
        commands.append(describe(init_config_command(self.samba_mgr_path()), result))
        if not result.ok:
            errors.append(translate_failure(
                '%s %s' % (result.stdout, result.stderr), '生成 smb.conf'))
        reload_result = self.exec(self.reload_command(), timeout=30)
        commands.append(describe(self.reload_command(), reload_result))
        if not reload_result.ok:
            # reload 失败不直接判死：下面还有配置与 is-active 两道核对
            errors.append('重新加载 Samba 服务返回非 0（exit %d）'
                          % reload_result.returncode)

        present = self.share_in_config(share_name)
        active = self.smb_service_active()
        if expect_present:
            verified = present or active
            if not verified:
                raise Error('共享 %s 没有出现在 /var/etc/smb.conf 里，操作未生效\n%s'
                            % (share_name, '\n'.join(commands)))
        else:
            verified = not present
            if not verified:
                raise Error('共享 %s 仍然出现在 /var/etc/smb.conf 里，删除未生效\n%s'
                            % (share_name, '\n'.join(commands)))
        return {
            'verified': verified,
            'inConfig': present,
            'smbActive': active,
            'initReturncode': result.returncode,
            'reloadReturncode': reload_result.returncode,
            'errors': errors,
            'stdout': result.stdout,
            'stderr': result.stderr,
            'output': '\n'.join(commands),
        }

    # ---- 共享目录：新增 -------------------------------------------------
    def _taken_for(self, account: str, extra=None) -> list:
        """已占用的共享名：账号自己的全部共享段 + 全局段 id（防止跨账号撞名）。"""
        names = {str(item['name']) for item in self.shares(force=True) if item.get('name')}
        names.update(str(item) for item in self.taken_share_names())
        names.update(str(item) for item in (extra or ()))
        return sorted(names)

    def account_dir_entries(self, account) -> list:
        """账号当前的共享目录条目（`accounts()` 里归到它名下的那些）。"""
        return list(self.account_entry(account).get('shares') or [])

    def add_share(self, account, path, share_point='', force_user='', user_list='') -> dict:
        """给某个账号再加**一个**目录：校验 → `add_dir` → init_config + reload → 核对。

        单个目录时行为与旧版完全一致（校验失败或 `add_dir` 失败都抛中文 Error，
        HTTP 层回 400），结果里除了新的 `added/skipped/errors` 之外，仍带
        `shareName/sharePoint/verified/inConfig/smbActive/reloadReturncode` 这些老字段。
        """
        result = self.add_shares(
            account, [path], share_point=share_point, force_user=force_user,
            user_list=user_list, mode='single')
        if result['added']:
            item = result['added'][0]
            if not item.get('verified'):
                # `smb_mgr.sh` 成功但配置里没有这个共享：要报错而不是假装成功
                raise Error('共享 %s 没有出现在 /var/etc/smb.conf 里，操作未生效\n%s'
                            % (item['shareName'], item.get('output', '')))
            return {**item, 'added': result['added'], 'skipped': result['skipped'],
                    'errors': list(item.get('errors') or []) + list(result['errors'])}
        if result['errors']:
            raise Error(result['errors'][0].get('error') or '添加共享目录失败')
        skipped = result['skipped'][0] if result['skipped'] else {}
        raise Error(skipped.get('error') or '没有可添加的目录')

    def add_shares(self, account, paths, share_point='', force_user='', user_list='',
                   mode='batch') -> dict:
        """给某个账号一次加多个目录。

        多目录时：先全部 `add_dir`（单个失败如实记下来、继续处理其余），随后
        **只跑一次** `init_config` + `systemctl reload smb nmb`，再逐个核对。
        `mode='single'` 时第一个目录的解析失败/已共享会抛 Error（保持旧接口行为）。
        """
        account = str(account or '').strip()
        if not account:
            raise Error('请选择要添加目录的账号')
        entry = self.account_entry(account)
        # user_list 用账号名（smb_mgr.sh 自己加 samba 前缀）；默认就是这个账号
        users = validate_user_list(user_list, default=account)
        # force_user 默认取 sambauser 里的 NAS 用户号（uXXXX）
        force = validate_force_user(force_user or entry.get('user', ''))
        roots = self.allowed_roots()
        raw = paths if isinstance(paths, (list, tuple)) else ([] if paths in (None, '') else [paths])
        values = [str(item).strip() for item in raw if str(item or '').strip()]
        single = mode == 'single'
        if not values:
            if single:
                # 保持旧接口的报错措辞（页面提示里带「路径」两个字）
                raise Error('请填写要共享的目录路径')
            raise Error('请选择要共享的目录')

        # 账号当前已共享的路径（`missing` 的不算：`list dirs` 里有目录但没有共享段时，
        # 那个目录其实**还没**共享出去，插件应该能把它共享出来）
        existing: dict = {}
        for share in self.account_dir_entries(account):
            if share.get('missing'):
                continue
            path = str(share.get('path', '')).rstrip('/')
            if path:
                existing.setdefault(path, share)

        wanted: list = []
        seen: set = set()
        for index, value in enumerate(values):
            try:
                directory = validate_share_path(value, roots)
            except Error as error:
                if single and index == 0:
                    raise
                wanted.append({'path': value, 'error': str(error)})
                continue
            if directory in seen:
                wanted.append({'path': directory, 'error': '同一个目录在一次提交里出现了两次'})
                continue
            if directory in existing:
                share = existing[directory]
                wanted.append({'path': directory,
                               'error': '这个目录已经是账号 %s 的共享：%s'
                                        % (account, share.get('name', '')),
                               'shareName': share.get('name', ''),
                               'display': share.get('display', ''),
                               'shared': True})
                continue
            seen.add(directory)
            wanted.append({'path': directory})

        resolved: list = []
        problems: list = []
        for item in wanted:
            if item.get('error'):
                problems.append(item)
                continue
            try:
                resolved.append(self._prepare_share(
                    account, item['path'], share_point, resolved))
            except Error as error:
                problems.append({'path': item['path'], 'error': str(error)})
        if single and not resolved and problems:
            first = problems[0]
            raise Error(first.get('error') or '添加共享目录失败')
        result = self._run_add_batch(account, resolved, users, force, skipped=problems)
        if single and not result['added']:            # `add_dir` 失败：旧接口用 400 报错
            first = result['errors'][0] if result['errors'] else {}
            raise Error(first.get('error') or '添加共享目录失败')
        return result

    def _prepare_share(self, account: str, directory: str, share_point, resolved) -> dict:
        """分配共享名与显示名（显示名没给就用目录名，与旧行为一致）。"""
        taken = self._taken_for(account, [item['shareName'] for item in resolved])
        share_name = plugin_share_name(account, taken)
        if share_name in taken:
            raise Error('共享名 %s 已被占用（本不该发生，请刷新后重试）' % share_name)
        point = validate_share_name(share_point) if str(share_point or '').strip() else \
            (posixpath.basename(directory.rstrip('/')) or share_name)
        return {'path': directory, 'shareName': share_name, 'sharePoint': point}

    def _run_add_batch(self, account: str, resolved: list, users: str, force: str,
                       skipped=None) -> dict:
        """`add_dir` × N → 一次 init_config + reload → 逐个核对。

        一个都没得加（全部已共享/校验失败）时不再跑收尾命令：没有改动就不该 reload。
        """
        self.account_cache = (0.0, None)
        self.share_cache = (0.0, None)
        added: list = []
        errors: list = [dict(item) for item in (skipped or [])]
        if not resolved:
            return {'added': added, 'skipped': [dict(item) for item in (skipped or [])],
                    'errors': errors, 'account': account, 'apply': None}
        for item in resolved:
            directory, share_name = item['path'], item['shareName']
            point = item['sharePoint']
            command = add_dir_command(share_name, directory, users, point, force,
                                      self.samba_mgr_path())
            try:
                result = self.smb(command, '添加共享目录')
            except Error as error:
                # 任一目录失败要如实报出来，并继续处理其余目录
                errors.append({'path': directory, 'shareName': share_name,
                               'error': str(error)})
                continue
            self.log('新增共享 %s → %s（账号 %s，force_user %s）'
                     % (share_name, directory, users, force or '—'))
            added.append({
                'shareName': share_name,
                'sharePoint': point,
                'account': account,
                'path': directory,
                'users': users.split(),
                'forceUser': force,
                'stdout': result.stdout,
                'stderr': result.stderr,
                'command': describe(command, result),
            })
        applied = self.apply_samba_batch([item['shareName'] for item in added])
        for item in added:
            state = applied['shares'].get(item['shareName'], {'present': False})
            item_errors = list(applied['errors'])
            if not state.get('present'):
                item_errors.append('共享 %s 没有出现在 /var/etc/smb.conf 里，操作未生效\n%s'
                                   % (item['shareName'], applied['output']))
                errors.append({'path': item['path'], 'shareName': item['shareName'],
                               'error': item_errors[-1]})
            item.update({
                'reloaded': applied['reloadReturncode'] == 0 or bool(state.get('present')),
                'verified': bool(state.get('present')),
                'inConfig': bool(state.get('present')),
                'smbActive': applied['smbActive'],
                'initReturncode': applied['initReturncode'],
                'reloadReturncode': applied['reloadReturncode'],
                'errors': item_errors,
                'output': item['command'] + '\n' + applied['output'],
            })
        return {'added': added, 'skipped': [dict(item) for item in (skipped or [])],
                'errors': errors, 'account': account, 'apply': applied}

    def apply_samba_batch(self, share_names, expect_present: bool = True) -> dict:
        """`init_config` + `systemctl reload smb nmb` **一次**，然后核对每个共享。

        与 `apply_samba` 的区别只有一个：批量增删时只跑一次收尾命令。

        `expect_present=True`（默认，新增路径，行为与以前完全一致）：`verified` 是
        「配置里有这个段、或者服务确实是 active」；`inConfig` 表示「全都在配置里」。
        `expect_present=False`（批量删除）：判定反过来，`verified` 是「配置里**没有**
        这个段」，`inConfig` 表示「全都不在配置里」。
        """
        names = [str(item) for item in (share_names or []) if str(item or '').strip()]
        commands = []
        errors = []
        result = self.exec(init_config_command(self.samba_mgr_path()), timeout=60)
        commands.append(describe(init_config_command(self.samba_mgr_path()), result))
        if not result.ok:
            errors.append(translate_failure(
                '%s %s' % (result.stdout, result.stderr), '生成 smb.conf'))
        reload_result = self.exec(self.reload_command(), timeout=30)
        commands.append(describe(self.reload_command(), reload_result))
        if not reload_result.ok:
            errors.append('重新加载 Samba 服务返回非 0（exit %d）' % reload_result.returncode)
        present = {name: self.share_in_config(name) for name in names}
        shares: dict = {}
        for name in names:
            found = bool(present[name])
            shares[name] = {'present': found,
                            'verified': found if expect_present else not found}
        if expect_present:
            # 与 `apply_samba` 一致：只有配置里真的缺段时才去问服务状态
            active = self.smb_service_active() if not all(present.values()) else False
            for name in names:
                shares[name]['verified'] = bool(present[name]) or active
            in_config = all(present.values()) if names else True
        else:
            # 删除路径不去问 is-active：段已从配置里消失本身就是证据
            active = False
            in_config = all(not value for value in present.values()) if names else False
        return {
            'shares': shares,
            'inConfig': in_config,
            'smbActive': active,
            'initReturncode': result.returncode,
            'reloadReturncode': reload_result.returncode,
            'errors': errors,
            'stdout': result.stdout,
            'stderr': result.stderr,
            'output': '\n'.join(commands),
        }

    # ---- 账号数据目录与目录浏览 -----------------------------------------
    def account_data_root(self, account: str) -> str:
        """账号的数据根目录：`<账号>` → `sambauser` 的 `option user` → `/home/<uXXXX>/pool0/data`。

        拿不到 NAS 用户号时，退回允许根目录里第一条 `/home/...` 形态的。
        """
        account = str(account or '').strip()
        if not account:
            raise Error('请选择账号')
        entry = self.account_entry(account)
        user_id = str(entry.get('user', '') or '').strip()
        if user_id:
            # 真机布局就是 `/home/<NAS用户>/pool0/data`；这里直接按 POSIX 拼字符串
            # （不要用 `Path`，它在 Windows 上会拼出反斜杠，白名单比对会失效）
            canonical = '/home/%s/pool0/data' % user_id
            for candidate in (canonical, data_root_for(Path('/'), user_id)):
                if candidate and path_isdir(candidate):
                    return posixpath.normpath(candidate)
            return posixpath.normpath(canonical)
        for root in self.allowed_roots():
            if root.startswith('/home/') and '*' not in root:
                return root
        return ''

    def _browse_dirs(self, root: str, account: str) -> list:
        """某个根目录下的**一层**子目录（只列目录，跳过隐藏目录与符号链接），标注已共享。

        `browse_account_dirs` 的「存储池」与「外接设备」两个位置共用这一份逻辑，
        两边的 `shared`/`shareName`/`display`/`deletable` 字段才会完全一致。
        """
        entries = []
        for name in sorted(path_listdir(root), key=lambda item: str(item)):
            text = str(name)
            if not text or text.startswith('.'):
                continue
            path = posixpath.join(root, text)
            if path_islink(path):
                continue                        # 符号链接一律不列（可能是越界的软链）
            if not path_isdir(path):
                continue
            entries.append((text, path))
        known: dict = {}
        for share in self.account_dir_entries(account):
            if share.get('missing'):
                # `list dirs` 里有目录但没有共享段：它其实**还没**共享出去，
                # 弹窗里要能勾选（否则用户永远没法把这个目录共享出来）。
                continue
            path = str(share.get('path', '')).rstrip('/')
            if path:
                known.setdefault(path, share)
        dirs = []
        for name, path in entries:
            share = known.get(path.rstrip('/'))
            dirs.append({
                'name': name,
                'path': path,
                'shared': share is not None,
                'shareName': (share or {}).get('name', ''),
                'display': (share or {}).get('display', ''),
                # 能不能由插件删掉（弹窗里决定这个勾选框可不可以取消）：
                # 只有插件自己建的 `<账号>_nb_<序号>` 才为 True；官方 App 建的那条
                # 与未共享的目录都是 False。
                'deletable': bool((share or {}).get('deletable')),
            })
        return dirs

    def browse_account_dirs(self, account: str) -> dict:
        """账号数据根目录下的**子目录**列表（只列目录，跳过隐藏目录与符号链接）。

        `dirs` 保持旧结构（老前端/老测试直接用），另外给 `locations`：弹窗按「位置」
        分组渲染——内置存储池（账号数据根）+ 外接设备（`EXTRA_ROOTS`，默认 `/nas/mnt`）。
        外接设备拔掉时 `available` 为 False、`dirs` 为空，弹窗显示「未接入」；
        这不是错误（已共享的 U 盘目录暂时不可用属于正常情况）。
        """
        account = str(account or '').strip()
        root = self.account_data_root(account)
        if not root:
            raise Error('没有找到账号 %s 的数据目录（/etc/config/sambauser 里没有可用的用户号）'
                        % account)
        if not path_exists(root):
            raise Error('数据目录不存在：%s' % root)
        if not path_isdir(root):
            raise Error('数据目录不是一个目录：%s' % root)
        dirs = self._browse_dirs(root, account)
        locations = [{'label': location_label(root), 'root': root,
                      'available': True, 'dirs': dirs}]
        present = extra_roots()
        for extra in self.extra_location_paths():
            if extra == root:                       # 极端配置下与存储池重复的根不重复列
                continue
            available = extra in present
            extra_dirs: list = []
            if available:
                try:
                    extra_dirs = self._browse_dirs(extra, account)
                except OSError as error:
                    # 挂载点掉了/没权限：不能把整个弹窗拖垮（存储池那份照旧要给出来），
                    # 位置标成不可用，用户还能手填别的路径。
                    self.log('读取 %s 失败：%s' % (extra, error))
                    available = False
            locations.append({
                'label': location_label(extra),
                'root': extra,
                'available': available,
                'dirs': extra_dirs,
            })
        return {'ok': True, 'account': account, 'root': root, 'dirs': dirs,
                'locations': locations}

    # ---- 目录浏览（「添加共享」弹窗，交互与 transmission 插件一致）--------
    def extra_location_paths(self) -> list:
        """追加根（外接存储）的展示/浏览顺序。

        通配（`/mnt/usb-*`）展开成当前存在的真实挂载点，位置就在配置顺序上；
        配置了但当前不存在的（非通配）也保留，弹窗把它标成「未接入」。
        """
        present = extra_roots()
        values: list = []
        for item in configured_extra_roots():
            if '*' in item:
                for entry in present:
                    if entry not in values and root_match(entry, item):
                        values.append(entry)
                continue
            if item not in values:
                values.append(item)
        for entry in present:                   # 兜底：present 里其它来源的根也别漏
            if entry not in values:
                values.append(entry)
        return values

    def share_locations(self, account: str) -> list:
        """弹窗里的「位置」列表：存储池（账号数据根）+ `EXTRA_ROOTS`（默认外接存储）。

        形如 `{'index': 0, 'label': '存储池', 'path': '/home/u3943892/pool0/data',
        'exists': True}`；`/nas/mnt` 的标题是「外接存储」（见 browse_location_label），
        拔盘/不存在时 `exists` 为 False（弹窗标「未接入」并禁用进入）。
        """
        account = str(account or '').strip()
        root = self.account_data_root(account)          # 账号不对时这里抛中文 Error
        locations = [{'index': 0, 'label': location_label(root), 'path': root,
                      'exists': bool(path_isdir(root))}]
        for item in self.extra_location_paths():
            if item == root:                            # 极端配置下的重复根不重复列
                continue
            locations.append({'index': len(locations), 'label': browse_location_label(item),
                              'path': item, 'exists': bool(path_isdir(item))})
        return locations

    def browse_share_dirs(self, account: str, root_index=0, relative: str = '') -> dict:
        """逐层浏览某个「位置」下的子目录（「添加共享」弹窗的后端）。

        - `root_index`：位置序号（见 `share_locations()`），默认 0 = 存储池；
        - `relative`：相对该位置的路径，空串 = 位置根目录本身；
        - 只列**一层**子目录，跳过隐藏目录与符号链接；越界（`..`/绝对路径）、
          位置不存在、目录不存在都抛中文 Error（HTTP 层回 400）。

        返回里同时带上 `locations`，弹窗一次请求就能画出位置切换与当前位置；
        `path` 是规范化后的相对路径（给「上一级」用），`absolute` 是完整绝对路径。
        """
        account = str(account or '').strip()
        locations = self.share_locations(account)
        try:
            index = int(root_index)
        except (TypeError, ValueError) as error:
            raise Error('位置无效：%s' % (root_index,)) from error
        if not 0 <= index < len(locations):
            raise Error('位置无效：%s' % index)
        entry = locations[index]
        base = entry['path']
        pieces = relative_pieces(relative)
        clean = '/'.join(pieces)
        target = posixpath.join(base, *pieces) if pieces else base
        # 相对路径已经拒绝 `..` 与绝对路径，这里再确认一次结果确实落在这个位置里
        if not within_roots(target, [base]):
            raise Error('目录不在这个位置里：%s' % target)
        if pieces and not entry['exists']:
            raise Error('这个位置当前不可用（未接入）：%s' % base)
        if not path_exists(target):
            raise Error('目录不存在：%s' % target)
        if not path_isdir(target):
            raise Error('不是一个目录：%s' % target)
        known: dict = {}
        for share in self.account_dir_entries(account):
            if share.get('missing'):
                continue
            path = str(share.get('path', '')).rstrip('/')
            if path:
                known.setdefault(path, share)
        items = []
        for name in sorted(path_listdir(target), key=lambda item: str(item)):
            text = str(name)
            if not text or text.startswith('.'):
                continue
            child = posixpath.join(target, text)
            if path_islink(child):
                continue                        # 符号链接一律不列（可能是越界的软链）
            if not path_isdir(child):
                continue
            items.append({
                'name': text,
                'path': '/'.join([clean, text]) if clean else text,   # 相对路径
                'absolute': child,                                     # 完整绝对路径
                'shared': child.rstrip('/') in known,                  # 已经共享过了
            })
        return {'ok': True, 'account': account, 'root': index, 'path': clean,
                'absolute': target, 'exists': bool(entry['exists']),
                'items': items, 'locations': locations}

    # ---- 共享目录：删除 -------------------------------------------------
    def _deletable_share(self, share_name) -> dict:
        """「这个共享能不能删」的校验：能删就返回它的配置条目，否则抛中文 Error。

        `delete_share` 与 `delete_shares` 共用，报错文案完全一致：
        空名 / 系统共享 / 找不到 / 不是插件自建的 `<账号>_nb_<序号>`。
        """
        share_name = str(share_name or '').strip()
        if not share_name:
            raise Error('请选择要删除的共享')
        if share_name in PROTECTED_SHARES:
            raise Error('系统共享 %s 不允许删除' % share_name)
        share = next((item for item in self.shares() if item['name'] == share_name), None)
        if share is None:
            raise Error('没有找到共享：%s' % share_name)
        account = share.get('account', '')
        if not account or not is_plugin_share(share_name, account):
            raise Error('只允许删除插件自己添加的共享（<账号>_nb_<序号>）；'
                        '%s 由系统或小米 App 管理' % share_name)
        return share

    def _clear_share_caches(self) -> None:
        self.account_cache = (0.0, None)
        self.share_cache = (0.0, None)

    def delete_share(self, share_name) -> dict:
        """只允许删插件自己建的：`<账号>_nb_<序号>`。"""
        share = self._deletable_share(share_name)
        name = str(share_name).strip()
        command = del_dir_command(name, self.samba_mgr_path())
        result = self.smb(command, '删除共享目录')
        self.log('删除共享 %s' % name)
        applied = self.apply_samba(name, expect_present=False)
        self._clear_share_caches()
        return {
            'shareName': name,
            'account': share.get('account', ''),
            'path': share.get('path', ''),
            'reloaded': applied['reloadReturncode'] == 0 or applied['verified'],
            'stdout': result.stdout,
            'stderr': result.stderr,
            'output': describe(command, result) + '\n' + applied['output'],
            **{key: applied[key] for key in
               ('verified', 'inConfig', 'smbActive', 'reloadReturncode', 'errors')},
        }

    def delete_shares(self, share_names) -> dict:
        """一次删多个插件自建共享：`del_dir` × N → **只跑一次** init_config + reload。

        语义与 `delete_share` 完全一致（同样的校验、同样的报错文案），区别只有一个：
        收尾命令只跑一次。先**全部**校验通过再动手：列表里只要有一条不合法
        （受保护的 / 找不到的 / 官方 App 建的），一条 `del_dir` 都不会执行。
        """
        raw = share_names if isinstance(share_names, (list, tuple)) else \
            ([] if share_names in (None, '') else [share_names])
        names: list = []
        for item in raw:
            text = str(item or '').strip()
            if text and text not in names:              # 去重 + 忽略空值
                names.append(text)
        if not names:
            raise Error('请选择要删除的共享')

        # 第一遍：全部校验（任何一条不合法都在这里抛错，此时还没跑过任何命令）
        shares = [self._deletable_share(name) for name in names]

        removed: list = []
        for share, name in zip(shares, names):
            command = del_dir_command(name, self.samba_mgr_path())
            result = self.smb(command, '删除共享目录')
            self.log('删除共享 %s' % name)
            removed.append({
                'shareName': name,
                'account': share.get('account', ''),
                'path': share.get('path', ''),
                'inConfig': True,                       # 下面按核对结果改写
                'verified': False,
                'command': describe(command, result),
                'stdout': result.stdout,
                'stderr': result.stderr,
            })

        applied = self.apply_samba_batch(names, expect_present=False)
        for item in removed:
            state = applied['shares'].get(item['shareName'], {})
            item['inConfig'] = bool(state.get('present'))
            item['verified'] = bool(state.get('verified'))
        self._clear_share_caches()

        errors = list(applied['errors'])
        for item in removed:
            if not item['verified']:
                errors.append('共享 %s 仍然出现在 /var/etc/smb.conf 里，删除未生效'
                              % item['shareName'])
        return {
            'removed': removed,
            'verified': all(item['verified'] for item in removed),
            'errors': list(dict.fromkeys(errors)),
            'initReturncode': applied['initReturncode'],
            'reloadReturncode': applied['reloadReturncode'],
            'output': applied['output'],
        }

    # ---- 官方 wsdd 接管 -------------------------------------------------
    def systemctl_command(self, *args) -> list:
        return [self.systemctl, *args]

    def unit_state(self, unit: str = '') -> str:
        """`systemctl show -p UnitFileState`：诊断用（不参与接管判定）。"""
        result = self.exec(self.systemctl_command(
            'show', '-p', 'UnitFileState', '--value', unit or WSDD_UNIT))
        return (result.stdout or '').strip()

    def kill_wsdd_processes(self) -> str:
        """兜底杀掉残留的 wsdd 进程。

        真机（192.168.1.8）**没有 pkill**（`pkill: 未找到命令`，exit 127），只有
        `killall`——官方 `/usr/bin/wsd` 包装脚本用的也是 `killall wsdd`。所以按
        `KILL_CANDIDATES` 顺序探测实际存在的那个；都没有就跳过，只依赖 systemctl。
        没有进程时 killall/pkill 都会返回非 0，这不算失败。
        """
        candidates = [KILL] if KILL else []
        for name in KILL_CANDIDATES:
            found = shutil.which(name)
            if found and found not in candidates:
                candidates.append(found)
        if not candidates:
            return '系统里没有 killall/pkill，跳过兜底清理（只依赖 systemctl stop）'
        for path in candidates:
            name = os.path.basename(path)
            argv = [path, WSDD_UNIT] if name == 'killall' else [path, '-x', WSDD_UNIT]
            try:
                result = self.exec(argv, timeout=15)
            except Error as error:
                return '兜底清理失败（%s）：%s' % (name, error)
            if result.ok:
                return '已清理残留的 wsdd 进程（%s）' % name
            return '没有需要清理的 wsdd 进程（%s）' % name
        return '兜底清理未执行'

    def wsdd_takeover(self) -> str:
        """让官方 wsdd 不再抢 3702：停服务 → 兜底清理 → 写空操作 drop-in → daemon-reload。

        用 drop-in 而不是 `systemctl mask`：mask 之后官方 `smb_mgr.sh` 收尾那句
        `systemctl restart wsdd` 会失败并让整条命令返回 1，用户在 App 里就会看到
        「保存共享失败」（虽然 smb.conf 已经写好了）。drop-in 把官方单元变成空操作，
        `restart` 依旧返回 0，官方功能没有可见回归。

        真机上踩过的两个坑，别再改回去：
        - 停止那步必须带 `systemctl` 前缀。漏掉会执行成 `stop wsdd`，日志里是
          「停止官方 wsdd失败：找不到命令 stop」，结果是官方服务一直占着 3702。
        - 停服时 `ExecStopPost --restore-wsdd` 会把官方 wsdd 拉回来，与下一次启动的
          接管存在竞态，所以停完要**自检 3702 并重试一次**。
        """
        messages = []
        for attempt in (1, 2):
            try:
                result = self.exec(self.systemctl_command('stop', WSDD_UNIT), timeout=30)
                messages.append('停止官方 wsdd' + (
                    '成功' if result.ok else '返回非 0（exit %d）：%s'
                    % (result.returncode, (result.stderr or result.stdout).strip())))
            except Error as error:
                messages.append('停止官方 wsdd失败：%s' % error)
            messages.append(self.kill_wsdd_processes())

            try:
                WSDD_DROPIN.parent.mkdir(parents=True, exist_ok=True)
                WSDD_DROPIN.write_text(WSDD_DROPIN_TEXT, encoding='utf-8', newline='\n')
                messages.append('已写入空操作 drop-in：%s' % WSDD_DROPIN)
            except OSError as error:
                messages.append('写入 drop-in 失败：%s' % error)

            try:
                result = self.exec(self.systemctl_command('daemon-reload'), timeout=30)
                messages.append('daemon-reload' + (
                    '成功' if result.ok else '返回非 0（exit %d）' % result.returncode))
            except Error as error:
                messages.append('daemon-reload 失败：%s' % error)

            if self.wait_for_wsd_port():
                break
            if attempt == 1:
                messages.append('3702 仍被占用，重试一次停止流程')

        if not self.wait_for_wsd_port(0.0):
            try:
                holder = self.wsd_port_holder() or '未知进程'
            except Error:
                holder = '未知进程'
            messages.append('3702/udp 仍被占用（%s），'
                            '回应器可能起不来；可在 NAS 上执行 '
                            '`ss -lunp | grep 3702` 排查' % holder)
        self.discovery['managed'] = True
        self.discovery['dropin'] = WSDD_DROPIN.exists()
        self.log('；'.join(messages))
        return '；'.join(messages)

    # ---- 3702 端口 -------------------------------------------------------
    def wsd_port_free(self) -> bool:
        """自己试着绑一次 3702/udp：绑得上说明官方服务确实放开了端口。

        绑定后立刻关闭（并设 SO_REUSEADDR），不影响随后 WsdResponder 的绑定。
        """
        import socket as _socket
        probe = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        try:
            probe.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
            probe.bind(('0.0.0.0', wsd.WSD_PORT))
            return True
        except OSError:
            return False
        finally:
            probe.close()

    def wsd_port_holder(self) -> str:
        """3702 被谁占着：优先 `ss -lunp`，退而求其次 `netstat -lunp`。"""
        for command in (['ss', '-lunp'], ['netstat', '-lunp']):
            try:
                result = self.exec(command, timeout=10)
            except Error:
                continue
            for line in (result.stdout or '').splitlines():
                if ':%d' % wsd.WSD_PORT in line:
                    return line.strip()
        return ''

    def wait_for_wsd_port(self, seconds: float = None) -> bool:
        """等官方 wsdd 真正退出（最多 WSD_PORT_WAIT 秒）。"""
        deadline = time.monotonic() + (WSD_PORT_WAIT if seconds is None else float(seconds))
        while True:
            try:
                if self.wsd_port_free():
                    return True
            except Exception as error:                          # noqa: BLE001 只是自检
                self.log('检查 3702 端口失败：%s' % error)
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.2)

    def wsdd_restore(self) -> str:
        """把官方 wsdd 放回去：删掉我们的 drop-in → daemon-reload → start wsdd。

        幂等：drop-in 不在就直接跳过；unit 不存在也只是记一条日志，不抛异常
        （这条路径会被 `server.py --restore-wsdd` 在服务停机后调用）。
        """
        messages = []
        # 先确保 3702 已经由我们放开（重复调用时 responder 通常已经是 None）
        self.stop_responder()
        try:
            WSDD_DROPIN.unlink()
            messages.append('已删除 drop-in：%s' % WSDD_DROPIN)
        except FileNotFoundError:
            messages.append('没有需要清理的 drop-in')
        except OSError as error:
            messages.append('删除 drop-in 失败：%s' % error)

        for command, label in (
            (self.systemctl_command('daemon-reload'), 'daemon-reload'),
            (self.systemctl_command('start', WSDD_UNIT), '启动官方 wsdd'),
        ):
            try:
                result = self.exec(list(command), timeout=30)
                if result.ok:
                    messages.append(label + '成功')
                else:
                    messages.append('%s返回非 0（exit %d）：%s'
                                    % (label, result.returncode,
                                       (result.stderr or result.stdout).strip()))
            except Error as error:
                messages.append('%s失败：%s' % (label, error))
        self.discovery['managed'] = False
        self.discovery['dropin'] = WSDD_DROPIN.exists()
        self.log('；'.join(messages))
        return '；'.join(messages)

    # ---- 发现服务生命周期 -----------------------------------------------
    def new_responder(self):
        settings = self.settings
        return self.responder_factory(
            hostname=self.hostname,
            workgroup=self.workgroup,
            port=int(settings['metadataPort']),
            state_file=str(self.identity_file),
            hello_interval=int(settings['helloInterval']),
            on_log=self.on_responder_log,
        )

    def on_responder_log(self, message: str) -> None:
        text = str(message or '')
        if text.startswith('wsd http: POST'):
            action = ''
            if ' action=' in text:
                action = text.rsplit(' action=', 1)[1].strip()
            if action in ('', 'Get'):
                # 只有 WS-Transfer Get 才是 Windows 真的来取设备描述
                self.discovery['metadataPosts'] = int(self.discovery['metadataPosts']) + 1
                self.discovery['lastMetadataAt'] = int(time.time())
        self.log('wsd: ' + text)

    def start(self) -> None:
        """按落盘的开关启动发现服务。

        - 开关打开（默认）：先接管官方 wsdd，再起本进程内的回应器；
        - 开关关闭：只把官方 wsdd 放回去（幂等），不起回应器，
          `discovery['running']` 保持 False。
        """
        with self.lock:
            self.hostname, self.workgroup = samba_identity()
            self.hostname = self._resolve_hostname()
            self.responder = None
            enabled = bool(self.settings.get('discoveryEnabled', True))
            self.discovery['enabled'] = enabled
            if not enabled:
                self.wsdd_restore()
                self.discovery['running'] = False
                self.discovery['enabled'] = False
                self.discovery['msg'] = '网络发现已关闭（官方 wsdd 已还原，插件未接管）'
                self.log(self.discovery['msg'])
                return
            self.wsdd_takeover()
            self._start_responder_locked()

    def _start_responder_locked(self) -> bool:
        """起回应器并把状态写进 `self.discovery`；返回是否成功。

        回应器启动要重试几次：真机上「停官方 wsdd」与「自己绑 3702」之间存在竞态
        （官方进程可能还在退出中），一次失败就永久放弃会让页面一直显示「未运行」。
        """
        last_error = None
        for attempt in range(1, max(1, RESPONDER_START_RETRIES) + 1):
            responder = self.new_responder()
            try:
                responder.start()
            except Exception as error:                          # noqa: BLE001 端口被占也不能拖垮插件页
                last_error = error
                try:
                    responder.stop()                            # 失败实例自己收拾（见 wsd.py）
                except Exception:                               # noqa: BLE001
                    pass
                if attempt < max(1, RESPONDER_START_RETRIES):
                    self.log('WSD 回应器第 %d 次启动失败：%s，%.1f 秒后重试'
                             % (attempt, error, RESPONDER_START_GAP))
                    time.sleep(max(0.0, RESPONDER_START_GAP))
                    continue
                break
            self.responder = responder
            self.discovery['running'] = True
            self.discovery['startedAt'] = int(time.time())
            self.discovery['port'] = int(self.settings['metadataPort'])
            self.discovery['msg'] = '发现服务运行中'
            self.log('WSD 回应器已启动：%s / %s（%s）'
                     % (self.hostname, self.workgroup, self.xaddrs_text()))
            return True
        self.discovery['running'] = False
        self.discovery['msg'] = 'WSD 回应器启动失败：%s' % last_error
        self.responder = None
        self.log(self.discovery['msg'])
        return False

    def xaddrs_text(self) -> str:
        try:
            return self.responder.xaddrs if self.responder else ''
        except Exception:                                       # noqa: BLE001
            return ''

    def stop_responder(self) -> None:
        responder, self.responder = self.responder, None
        if responder is None:
            return
        try:
            responder.stop()                                    # 发 Bye，Windows 里的图标会消失
        except Exception as error:                              # noqa: BLE001
            self.log('停止 WSD 回应器出错：%s' % error)
        self.discovery['running'] = False
        self.discovery['msg'] = '发现服务已停止'

    def restart_responder(self, times: int = 2) -> dict:
        """页面上的「重新宣告」：重建回应器并重发 Hello。"""
        with self.lock:
            self.stop_responder()
            self.hostname, self.workgroup = samba_identity()
            self.hostname = self._resolve_hostname()
            responder = self.new_responder()
            try:
                responder.start()
                self.responder = responder
                responder.announce_hello(times=times)
            except Exception as error:                          # noqa: BLE001
                self.responder = None
                self.discovery['running'] = False
                self.discovery['msg'] = '重新宣告失败：%s' % error
                self.log(self.discovery['msg'])
                raise Error(self.discovery['msg']) from error
            self.discovery['running'] = True
            self.discovery['startedAt'] = int(time.time())
            self.discovery['port'] = int(self.settings['metadataPort'])
            self.discovery['msg'] = '已重新宣告'
            self.log('已重新宣告（Hello x%d）' % max(1, times))
        return self.snapshot()

    # ---- 开关与主机名（页面上的两个写操作）------------------------------
    def set_discovery_enabled(self, enabled) -> dict:
        """打开 = 接管官方 wsdd + 起回应器；关闭 = 发 Bye + 把官方 wsdd 原样还原。

        状态落盘到 `settings.json`，所以服务重启后按这里的结果生效。
        """
        target = normalize_discovery_enabled(enabled, bool(self.settings.get('discoveryEnabled')))
        with self.lock:
            self.settings['discoveryEnabled'] = bool(target)
            save_settings(self.settings, self.state_file)
            self.discovery['enabled'] = bool(target)
            if target:
                self.hostname, self.workgroup = samba_identity()
                self.hostname = self._resolve_hostname()
                self.wsdd_takeover()
                self._start_responder_locked()
                self.log('网络发现已打开：已接管官方 wsdd 并启动回应器')
            else:
                # 幂等：发 Bye → 删 drop-in → daemon-reload → start wsdd。
                # 官方 wsdd 本来就在跑、drop-in 不存在时也要成功返回。
                self.wsdd_restore()
                self.discovery['running'] = False
                self.discovery['enabled'] = False
                self.discovery['managed'] = False
                self.discovery['msg'] = '网络发现已关闭（官方 wsdd 已还原，插件未接管）'
                self.log(self.discovery['msg'])
        return self.snapshot()

    def set_hostname(self, name=None, reset: bool = False) -> dict:
        """改 WSD 宣告的主机名：校验 → 落盘 → 重建回应器并重发 Hello。

        `reset=True` 表示**恢复默认**：清掉 `settings['hostname']`，回退到从
        `/etc/config/samba` 读到的名字（读不到才是内置默认值）。`name` 为空串仍然
        按非法输入处理（页面上的「保存」会先挡住空输入）。
        """
        if reset:
            value = ''
        else:
            value = validate_hostname(name)
        with self.lock:
            self.settings['hostname'] = value
            save_settings(self.settings, self.state_file)
            self.explicit_hostname = value
            previous = self.hostname
            self.hostname, self.workgroup = samba_identity()
            self.hostname = self._resolve_hostname()
        if not bool(self.settings.get('discoveryEnabled', True)):
            # 关闭状态下不起回应器：名字已经落盘，下次打开时生效
            self.log('主机名已保存为 %s（网络发现已关闭，下次打开时生效）'
                     % (value or self.hostname))
            return self.snapshot()
        with self.lock:
            self.stop_responder()                               # 先发 Bye，Windows 里的旧名字消失
            if not self._start_responder_locked():
                raise Error('主机名已保存为 %s，但回应器重建失败：%s'
                            % (value or self.hostname, self.discovery['msg']))
            responder = self.responder
            try:
                responder.announce_hello(times=2)               # 用新名字立刻重新宣告
            except Exception as error:                          # noqa: BLE001
                self.log('重新宣告失败（主机名已生效）：%s' % error)
            if reset:
                self.discovery['msg'] = '已恢复默认主机名 %s' % self.hostname
                self.log('主机名 %s → %s（恢复默认，已重建回应器并重发 Hello）'
                         % (previous or '—', self.hostname))
            else:
                self.discovery['msg'] = '已切换到主机名 %s' % value
                self.log('主机名 %s → %s（已重建回应器并重发 Hello）' % (previous or '—', value))
        return self.snapshot()

    def shutdown(self) -> None:
        """服务停止：先发 Bye，再把官方 wsdd 放回去。"""
        self.stop_responder()
        self.wsdd_restore()

    # ---- 状态快照 -------------------------------------------------------
    def settings_snapshot(self) -> dict:
        """页面要用的落盘设置：开关与主机名。"""
        return {
            'discoveryEnabled': bool(self.settings.get('discoveryEnabled', True)),
            'hostname': self.hostname,
        }

    def status(self) -> dict:
        responder = self.responder
        xaddrs = []
        address = ''
        identity = ''
        metadata_port = int(self.settings['metadataPort'])
        if responder is not None:
            try:
                xaddrs = [responder.xaddrs]
                address = getattr(responder, 'address', '') or ''
                identity = getattr(responder, 'identity', '') or ''
                metadata_port = int(getattr(responder, 'port', metadata_port) or metadata_port)
            except Exception:                                   # noqa: BLE001
                xaddrs = []
        if not xaddrs and address:
            xaddrs = lan_xaddrs(address, metadata_port)
        enabled = bool(self.settings.get('discoveryEnabled', True))
        running = bool(self.discovery['running'])
        discovery = {
            'running': running,
            'enabled': enabled,
            'startedAt': int(self.discovery['startedAt']),
            'message': self.discovery['msg'] or (
                '发现服务运行中' if running else (
                    '网络发现已关闭' if not enabled else '发现服务未运行')),
            'hostname': self.hostname,
            'workgroup': self.workgroup,
            'xaddrs': xaddrs,
            'address': address,
            'identity': identity,
            'metadataPort': metadata_port,
            'managed': bool(self.discovery['managed']),
            'wsddDropin': bool(self.discovery['dropin']),
            'wsddUnit': WSDD_UNIT,
            'wsddDropinPath': str(WSDD_DROPIN),
            'metadataPosts': int(self.discovery['metadataPosts']),
            'lastMetadataAt': int(self.discovery['lastMetadataAt']),
            'helloInterval': int(self.settings['helloInterval']),
        }
        return {
            'ok': True,
            'version': installed_version(),
            # 页面只读 settings（开关 + 主机名）；discovery 里的工作组/地址/官方 wsdd 状态
            # 等字段仍然返回，方便真机排查，只是页面不再显示。
            'settings': self.settings_snapshot(),
            'discovery': discovery,
            'accounts': self.accounts(),
            'allowedRoots': self.allowed_roots(),
            'sambaMgr': self.samba_mgr_path(),
            'sambaMgrFound': os.path.exists(self.samba_mgr_path()),
            'smbUnits': list(SMB_UNITS),
        }

    def snapshot(self) -> dict:
        return self.status()


# ---------------------------------------------------------------------------
# 供 CLI / 测试使用的轻量入口
# ---------------------------------------------------------------------------

def restore_wsdd(data_dir=None, runner=None) -> str:
    """只做「把官方 wsdd 放回去」这一件事，不启动回应器、不起 HTTP 服务。"""
    engine = Engine(data_dir=data_dir, runner=runner, start_responder=False)
    return engine.wsdd_restore()
