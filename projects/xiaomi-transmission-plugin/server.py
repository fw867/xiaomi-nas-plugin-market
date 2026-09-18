#!/usr/bin/env python3
"""Transmission 下载插件：管理随包携带的 transmission-daemon，并提供中文设置界面。

设计要点（与仓库其它插件保持一致）：

* 只用 Python 标准库，不依赖 pip，也不假设 NAS 上有 Entware；
  transmission 及其全部 .so 依赖由 `scripts/fetch_runtime.py` 从 Entware 的
  aarch64 仓库提取后随包携带，运行时通过 LD_LIBRARY_PATH 指向随包 lib 目录。
* 发布包解压后所有文件权限统一为 0644（打包脚本固定写 external_attr），
  所以可执行文件必须在启动前显式 chmod，不能依赖归档里的权限位。
* transmission 4.x 的 settings.json 仍是 kebab-case（`download-dir`），
  5.x 才会默认切到 snake_case。这里按磁盘上现有文件的风格写入，并在
  完全没有 settings.json 时回落到 kebab-case（即 4.x 的默认风格）。
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import signal
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


# ---------------------------------------------------------------------------
# 路径与环境
# ---------------------------------------------------------------------------

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "18140"))
WEB_DIR = Path(os.environ.get("WEB_DIR", Path(__file__).resolve().parent / "web"))
RUNTIME_DIR = Path(os.environ.get("RUNTIME_DIR", Path(__file__).resolve().parent))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data/plugin/transmission/data"))

BIN_DIR = RUNTIME_DIR / "bin"
LIB_DIR = RUNTIME_DIR / "lib"
DAEMON_NAME = "transmission-daemon"
DAEMON = BIN_DIR / DAEMON_NAME
# 随包携带的全部可执行文件（transmission-cli 包里含 create/edit/show 三个小工具），
# 打包会把它们写成 0644，所以启动前要逐个补执行位。
CLI_BINARIES = (
    DAEMON_NAME,
    "transmission-remote",
    "transmission-cli",
    "transmission-create",
    "transmission-edit",
    "transmission-show",
)
# Entware 二进制的 ELF 解释器写死为 /opt/lib/ld-linux-aarch64.so.1（Entware
# 的安装路径）。设备上没有 /opt（根文件系统只读），直接执行会报
# 「无法执行：找不到需要的文件」。所以随包带上加载器，显式调用。
LOADER = LIB_DIR / "ld-linux-aarch64.so.1"


_LOADER_WARNED = {"value": False}


def runtime_argv(binary: Path, *args: str) -> list[str]:
    """用随包加载器 + 随包库目录调用 Entware 二进制。

    缺 LOADER 时退回直接执行——现场在那台设备上必然报
    「无法执行：找不到需要的文件」，所以这里明确警告一次，
    免得只看到一个含糊的 exec 错误。
    """
    if LOADER.is_file():
        return [str(LOADER), "--library-path", str(LIB_DIR), str(binary), *args]
    if not _LOADER_WARNED["value"]:
        _LOADER_WARNED["value"] = True
        print(
            f"警告：缺少随包加载器 {LOADER}，无法用 --library-path 启动二进制；"
            "请确认打包时 lib/ 下包含它（否则运行时会报「无法执行：找不到需要的文件」）。",
            file=sys.stderr, flush=True,
        )
    return [str(binary), *args]


SETTINGS_FILE = DATA_DIR / "settings.json"
PID_FILE = DATA_DIR / "transmission-daemon.pid"
LOG_FILE = DATA_DIR / "transmission.log"
# 记录用户是否希望 daemon 运行，用于开机后自动拉起（transmission 自己不管这个）。
PLUGIN_STATE_FILE = DATA_DIR / "plugin-state.json"

# daemon 的 RPC 只监听回环。默认端口刻意避开 transmission 的 9091，
# 免得与用户自己装的 transmission 冲突。
RPC_PORT = int(os.environ.get("RPC_PORT", "19191"))
RPC_PATH = "/transmission/"

# 请求体上限：设置接口和 RPC 透传共用（RPC 还能带 metainfo）。
MAX_BODY_BYTES = 4 * 1024 * 1024

# 下载目录默认值：优先放到用户在设备上能看到的共享目录（qbittorrent 插件
# 用同一个 LOCAL_ROOT 约定），取不到就退回插件自己的数据目录。
def default_download_dir(local_root: str, data_dir: Path) -> str:
    local_root = (local_root or "").strip().rstrip("/")
    return f"{local_root}/TransmissionDownloads" if local_root else str(Path(data_dir) / "downloads")


DEFAULT_DOWNLOAD_DIR = default_download_dir(os.environ.get("LOCAL_ROOT", ""), DATA_DIR)

# 界面里 transmission-web-control 的入口（ui 目录下的相对路径）。
WEB_CONTROL_DIRNAME = "twc"

STATE_LOCK = threading.Lock()
WRITE_LOCK = threading.Lock()

VERSION_CACHE: dict[str, object] = {"mtime": None, "value": None}
SESSION_ID: dict[str, str] = {"value": ""}


# ---------------------------------------------------------------------------
# 设置项：逻辑名 → transmission settings.json 键
#
# 键名来源：transmission 4.0.6 的 docs/Editing-Configuration-Files.md。
# 4.1 起文档改用 snake_case（TR_SAVE_VERSION_FORMAT=5 才是默认），
# 4.x 默认仍是 kebab-case，所以两种都记录，写入时按现有文件风格选择。
# ---------------------------------------------------------------------------

def _field(logical, label, kind, group, help_text, **extra):
    field = {
        "id": logical,
        "kebab": logical,
        "snake": logical.replace("-", "_"),
        "label": label,
        "kind": kind,
        "group": group,
        "help": help_text,
        # 数值型（int）字段必须显式给出单位；其它类型统一为空串，
        # 这样界面可以无脑读 field.unit，不用判断键存不存在。
        "unit": "",
    }
    field.update(extra)
    return field


FIELDS: list[dict] = [
    _field(
        "download-dir", "下载目录", "path", "basic",
        "种子文件的保存位置，必须是绝对路径。目录不存在时会自动创建。",
        unit="", default=DEFAULT_DOWNLOAD_DIR,
    ),
    # —— 并发（同时上传数 / 同时下载数）——
    _field(
        "upload-slots-per-torrent", "同时上传数（每任务上传槽位）", "int", "concurrency",
        "每个任务同时上传给多少个连接。Transmission 4.x 的准确字段名是 "
        "upload-slots-per-torrent；4.x 已没有 max-peers-* 这类字段"
        "（1.4x 的 max-peers-global / max-peers-per-torrent 早已改名）。",
        unit="个", default=14, minimum=1, maximum=64,
    ),
    _field(
        "seed-queue-enabled", "限制同时做种任务数", "bool", "concurrency",
        "开启后最多只让 seed-queue-size 个任务同时做种（上传）。", default=False,
    ),
    _field(
        "seed-queue-size", "同时做种任务数上限", "int", "concurrency",
        "见「限制同时做种任务数」。", unit="个", default=10, minimum=1, maximum=1000,
    ),
    _field(
        "download-queue-enabled", "限制同时下载任务数", "bool", "concurrency",
        "开启后最多只让 download-queue-size 个任务同时下载。", default=True,
    ),
    _field(
        "download-queue-size", "同时下载任务数上限", "int", "concurrency",
        "见「限制同时下载任务数」。", unit="个", default=5, minimum=1, maximum=1000,
    ),
    _field(
        "queue-stalled-enabled", "不把低速任务计入并发上限", "bool", "concurrency",
        "开启后，长时间没有数据交换的任务视为「停滞」，不占用上面的并发名额。",
        default=True,
    ),
    _field(
        "queue-stalled-minutes", "低速判定时间", "int", "concurrency",
        "多少分钟没有数据交换算作停滞。", unit="分钟", default=30, minimum=1, maximum=1440,
    ),
    # —— 连接数 ——
    _field(
        "peer-limit-global", "全局连接数", "int", "limits",
        "所有任务加起来的最大对等连接数。", unit="个", default=240,
        minimum=1, maximum=10000,
    ),
    _field(
        "peer-limit-per-torrent", "单种连接数", "int", "limits",
        "每个任务的最大对等连接数，必须不大于全局连接数才真正生效。", unit="个",
        default=60, minimum=1, maximum=2000,
    ),
    # —— 限速 ——
    _field(
        "speed-limit-up-enabled", "启用上传限速", "bool", "bandwidth",
        "开启后按下面的数值限制全局上传速度。", default=False,
    ),
    _field(
        "speed-limit-up", "上传限速", "int", "bandwidth",
        "全局上传速度上限。", unit="KB/s", default=100, minimum=1, maximum=1048576,
    ),
    _field(
        "speed-limit-down-enabled", "启用下载限速", "bool", "bandwidth",
        "开启后按下面的数值限制全局下载速度。", default=False,
    ),
    _field(
        "speed-limit-down", "下载限速", "int", "bandwidth",
        "全局下载速度上限。", unit="KB/s", default=100, minimum=1, maximum=1048576,
    ),
    # —— 时段限速（alt-speed 系列）——
    _field(
        "alt-speed-enabled", "启用时段限速（限速模式）", "bool", "schedule",
        "手动打开的「限速模式」；开启下面的时段计划后，由计划自动切换。", default=False,
    ),
    _field(
        "alt-speed-up", "时段限速·上传达限", "int", "schedule",
        "限速模式下的上传速度上限。", unit="KB/s", default=50, minimum=1, maximum=1048576,
    ),
    _field(
        "alt-speed-down", "时段限速·下传达限", "int", "schedule",
        "限速模式下的下载速度上限。", unit="KB/s", default=50, minimum=1, maximum=1048576,
    ),
    _field(
        "alt-speed-time-enabled", "启用限速时段计划", "bool", "schedule",
        "开启后在指定星期与时间段自动进入限速模式。", default=False,
    ),
    _field(
        "alt-speed-time-begin", "时段开始", "clock", "schedule",
        "以零点起的分钟数存储（默认 540 = 09:00），界面按 HH:MM 显示。",
        unit="", default=540, minimum=0, maximum=1439,
    ),
    _field(
        "alt-speed-time-end", "时段结束", "clock", "schedule",
        "以零点起的分钟数存储（默认 1020 = 17:00），界面按 HH:MM 显示。",
        unit="", default=1020, minimum=0, maximum=1439,
    ),
    _field(
        "alt-speed-time-day", "生效星期", "weekdays", "schedule",
        "7 位位图：周日 1、周一 2、周二 4、周三 8、周四 16、周五 32、周六 64；"
        "全年无休 127、工作日 62、周末 65。",
        unit="", default=127, minimum=0, maximum=127,
    ),
    # —— 远程访问（RPC 监听与账号验证）——
    _field(
        "rpc-bind-address", "RPC 监听地址", "choice", "auth",
        "0.0.0.0 监听所有网卡，局域网内其它设备可直接连接 RPC（如 Transmission Remote）；"
        "127.0.0.1 只允许本机访问。绑定到 0.0.0.0 时建议同时开启账号验证。",
        unit="", default="127.0.0.1", choices=["0.0.0.0", "127.0.0.1"],
    ),
    _field(
        "rpc-authentication-required", "开启账号验证", "bool", "auth",
        "只读回报：实际是否要求鉴权。由「RPC 监听地址」与是否配齐用户名密码"
        "共同决定——选 0.0.0.0 必须填齐用户名和密码，否则自动退回只监听本机。",
        default=False,
    ),
    _field(
        "rpc-username", "登录用户名", "text", "auth",
        "开启账号验证时使用的用户名。",
        unit="", default="",
    ),
    _field(
        "rpc-password", "登录密码", "password", "auth",
        "留空表示不修改现有密码。Transmission 保存时会对密码加盐哈希，"
        "插件不会把已保存的密码回显到界面。",
        unit="", default="",
    ),
]

FIELDS_BY_ID = {field["id"]: field for field in FIELDS}
ISSUE_IDS = [field["id"] for field in FIELDS]

GROUP_LABELS = {
    "basic": "下载位置",
    "concurrency": "并发与任务数",
    "limits": "连接数",
    "bandwidth": "速度限制",
    "schedule": "时段限速",
    "auth": "远程访问",
}

GROUPS = [{"id": gid, "label": label} for gid, label in GROUP_LABELS.items()]


def detect_style(raw: dict) -> str:
    """判断 settings.json 用的是 kebab-case 还是 snake_case。

    只有同时存在某个已知键时才下结论，否则回落到 kebab-case：
    我们的随包 daemon 是 4.0.6，默认写 kebab-case。
    """
    override = os.environ.get("SETTINGS_KEY_STYLE", "auto").strip().lower()
    if override in ("kebab", "snake"):
        return override
    if not isinstance(raw, dict):
        return "kebab"
    known_kebab = {field["kebab"] for field in FIELDS} | set(MANAGED_KEYS)
    known_snake = {field["snake"] for field in FIELDS} | {
        key.replace("-", "_") for key in MANAGED_KEYS
    }
    keys = set(raw)
    if keys & known_snake:
        return "snake"
    if keys & known_kebab:
        return "kebab"
    return "kebab"


def key_for(logical: str, style: str) -> str:
    field = FIELDS_BY_ID.get(logical)
    if field is not None:
        return field["snake"] if style == "snake" else field["kebab"]
    return logical.replace("-", "_") if style == "snake" else logical


# daemon 的 RPC 端点必须由插件固定，因此这些键每次写入都强制覆盖。
# rpc-bind-address / 账号验证三项由界面配置，不在此列。
# 白名单关闭：绑定到 0.0.0.0 后它只会拦住所有外部访问，
# 访问控制改由「开启账号验证」承担。
MANAGED_KEYS = {
    "rpc-enabled": True,
    "rpc-port": RPC_PORT,
    "rpc-whitelist-enabled": False,
    "rpc-host-whitelist-enabled": False,
    "rpc-url": RPC_PATH,
}


# ---------------------------------------------------------------------------
# settings.json 读写
# ---------------------------------------------------------------------------

def read_settings() -> dict:
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_settings(settings: dict) -> None:
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = SETTINGS_FILE.with_name(SETTINGS_FILE.name + ".plugin.tmp")
    temporary.write_text(
        json.dumps(settings, ensure_ascii=False, indent=4) + "\n", encoding="utf-8"
    )
    os.chmod(temporary, 0o644)
    temporary.replace(SETTINGS_FILE)
    os.chmod(SETTINGS_FILE, 0o644)


def effective_values(raw: dict, style: str) -> dict:
    values = {}
    for field in FIELDS:
        key = key_for(field["id"], style)
        value = raw.get(key, field["default"])
        values[field["id"]] = value
    return values


def merge_managed(raw: dict, style: str, values: dict) -> dict:
    merged = dict(raw) if isinstance(raw, dict) else {}
    for key, value in MANAGED_KEYS.items():
        merged[key_for(key, style)] = value
    if not merged.get(key_for("download-dir", style)):
        merged[key_for("download-dir", style)] = DEFAULT_DOWNLOAD_DIR
    # 界面字段首次写入时补默认值（只补缺失的，不覆盖已有值）。
    # rpc-password 排除在外：它的「默认」是空串，写进去会把密码清掉。
    for logical in ("rpc-bind-address", "rpc-authentication-required", "rpc-username"):
        key = key_for(logical, style)
        if key not in merged:
            merged[key] = FIELDS_BY_ID[logical]["default"]
    for logical, value in values.items():
        merged[key_for(logical, style)] = value
    return merged


class SettingsError(ValueError):
    def __init__(self, message: str, fields: dict | None = None):
        super().__init__(message)
        self.fields = fields or {}


class DaemonError(RuntimeError):
    pass


def normalize_remote_access(values: dict) -> dict:
    """落实「要开 0.0.0.0 远程访问就必须配齐用户名密码，否则只听 127.0.0.1」。

    实测 transmission 4.0.6 的白名单并不能豁免鉴权（开鉴权后带白名单的回环
    请求同样返回 401），所以不能用「白名单放行本机 + 鉴权挡外部」的组合。
    这里的规则是硬保证：凭据不全就绝不对外监听。
    """
    bind = str(values.get("rpc-bind-address", "") or "")
    username = str(values.get("rpc-username", "") or "").strip()
    password = str(values.get("rpc-password", "") or "")
    if not password:
        # 界面留空表示沿用已保存的口令
        password = str(read_credential().get("password", "") or "")
    if bind == "0.0.0.0" and username and password:
        return {"rpc-bind-address": "0.0.0.0", "rpc-authentication-required": True}
    return {"rpc-bind-address": "127.0.0.1", "rpc-authentication-required": False}


def coerce_int(field: dict, value: object) -> int:
    """把界面传来的值收紧成 int，并做范围检查。

    ``bool`` 是 ``int`` 的子类，必须单独排除，否则 True 会被当成 1 静默通过。
    """
    if isinstance(value, bool) or not isinstance(value, int):
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            value = int(value.strip())
        else:
            raise SettingsError(f"{field['label']}必须是整数")
    if value < field["minimum"] or value > field["maximum"]:
        raise SettingsError(
            f"{field['label']}必须在 {field['minimum']} 到 {field['maximum']} 之间"
        )
    return int(value)


def coerce_path(field: dict, value: object) -> str:
    if not isinstance(value, str):
        raise SettingsError(f"{field['label']}必须是路径字符串")
    text = value.strip()
    if not text or "\x00" in text or len(text) > 4096:
        raise SettingsError(f"{field['label']}不是有效路径")
    if not text.startswith("/"):
        raise SettingsError(f"{field['label']}必须是绝对路径（以 / 开头）")
    return text


def validate_settings(values: object) -> dict:
    """白名单校验：只有本插件声明过的设置项才允许写入。

    未声明的键一律拒绝，因此请求体无法把任意字段塞进 settings.json。
    """
    if not isinstance(values, dict):
        raise SettingsError("设置内容必须是对象")
    clean: dict = {}
    problems: dict = {}
    for logical, value in values.items():
        field = FIELDS_BY_ID.get(logical)
        if field is None:
            problems[str(logical)] = "未知设置项"
            continue
        try:
            if field["kind"] == "bool":
                if not isinstance(value, bool):
                    raise SettingsError(f"{field['label']}必须是开关（true/false）")
                clean[logical] = value
            elif field["kind"] == "path":
                clean[logical] = coerce_path(field, value)
            elif field["kind"] == "choice":
                if value not in field["choices"]:
                    raise SettingsError(
                        f"{field['label']}只能是 {' 或 '.join(field['choices'])}"
                    )
                clean[logical] = value
            elif field["kind"] in ("text", "password"):
                if not isinstance(value, str):
                    raise SettingsError(f"{field['label']}必须是文本")
                if len(value) > 128 or any(ch in value for ch in "\r\n\x00"):
                    raise SettingsError(f"{field['label']}含有不允许的字符或过长")
                # 密码留空表示「不修改」，不写进 settings.json
                if field["kind"] == "password" and not value:
                    continue
                clean[logical] = value
            else:  # int / clock / weekdays 都是整数
                clean[logical] = coerce_int(field, value)
        except SettingsError as error:
            problems[logical] = str(error)
    if problems:
        raise SettingsError("设置项校验未通过", problems)
    return clean


def ensure_download_dir(values: dict) -> None:
    directory = values.get("download-dir")
    if not directory:
        return
    path = Path(directory)
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise SettingsError(
            f"下载目录无法创建：{error}", {"download-dir": "无法创建目录"}
        ) from error
    if not os.access(path, os.W_OK):
        raise SettingsError("下载目录不可写", {"download-dir": "目录不可写"})


def apply_settings(values: object, restart: bool = True) -> dict:
    """写 settings.json 并按需重启 daemon。

    顺序很重要：transmission 只在退出时回写 settings.json，
    所以必须「先停 daemon → 再写文件 → 再启动」，否则改动会被覆盖。
    """
    clean = validate_settings(values)
    ensure_download_dir(clean)
    was_running = daemon_running()
    stopped = False
    if was_running and restart:
        ok, error = stop_daemon()
        if not ok:
            raise DaemonError(f"无法停止 transmission-daemon：{error}")
        stopped = True
    with WRITE_LOCK:
        raw = read_settings()
        style = detect_style(raw)
        # 用「现有值 + 本次提交」求出最终值，再据此决定监听地址与鉴权开关
        effective = effective_values(raw, style)
        effective.update(clean)
        clean.update(normalize_remote_access(effective))
        # 记住明文口令：daemon 侧存的是哈希，插件自己发 RPC 时用得上
        username = str(effective.get("rpc-username", "") or "").strip()
        new_password = str(clean.get("rpc-password", "") or "")
        if new_password:
            write_credential(username, new_password)
        elif username:
            existing = read_credential()
            if existing.get("password"):
                write_credential(username, str(existing["password"]))
        write_settings(merge_managed(raw, style, clean))
    started = False
    if stopped:
        ok, error = start_daemon()
        if not ok:
            raise DaemonError(f"设置已保存，但 transmission-daemon 启动失败：{error}")
        started = True
    return {
        "values": effective_values(read_settings(), detect_style(read_settings())),
        "restarted": started,
        "daemonRunning": daemon_running(),
    }


# ---------------------------------------------------------------------------
# daemon 生命周期
# ---------------------------------------------------------------------------

def ensure_runtime_executables() -> None:
    """发布包解压后权限是 0644，可执行文件必须先补上执行位。"""
    for candidate in (*(BIN_DIR / name for name in CLI_BINARIES), LOADER):
        if not candidate.is_file():
            continue
        if os.access(candidate, os.X_OK):
            continue
        try:
            os.chmod(candidate, 0o755)
        except OSError:
            pass


def daemon_env() -> dict:
    """子进程环境。

    真正决定加载哪些 .so 的是 `runtime_argv()` 里的
    `--library-path <lib>`——ld.so 的该选项会覆盖 LD_LIBRARY_PATH。
    这里仍导出 LD_LIBRARY_PATH，只为 LOADER 缺失、退回直接执行时
    还有机会找到库；两条路径指向同一个目录，不冲突。
    """
    env = dict(os.environ)
    existing = env.get("LD_LIBRARY_PATH", "")
    env["LD_LIBRARY_PATH"] = f"{LIB_DIR}:{existing}" if existing else str(LIB_DIR)
    env.setdefault("HOME", str(DATA_DIR))
    # 让 daemon 自带的 web 服务指向随包的 transmission-web-control。
    # 不设的话，用浏览器直接打开 RPC 端口会报
    # 「Couldn't find Transmission's web interface files!」——
    # 随包二进制没有编译内置 web 界面。
    web_control = WEB_DIR / WEB_CONTROL_DIRNAME
    if (web_control / "index.html").is_file():
        env["TRANSMISSION_WEB_HOME"] = str(web_control)
    return env


def _run(command: list[str], timeout: float = 10.0) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            command, capture_output=True, text=True, check=False, timeout=timeout,
            env=daemon_env(),
        )
    except (OSError, subprocess.SubprocessError) as error:
        return subprocess.CompletedProcess(command, 127, "", str(error))


def daemon_version() -> str | None:
    if not DAEMON.is_file():
        return None
    try:
        stamp = DAEMON.stat().st_mtime
    except OSError:
        return None
    if VERSION_CACHE["mtime"] == stamp:
        return VERSION_CACHE["value"]  # type: ignore[return-value]
    ensure_runtime_executables()
    result = _run(runtime_argv(DAEMON, "--version"), timeout=15.0)
    text = f"{result.stdout}\n{result.stderr}"
    match = re.search(r"\d+\.\d+(?:\.\d+)?", text)
    version = match.group(0) if match else None
    VERSION_CACHE["mtime"] = stamp
    VERSION_CACHE["value"] = version
    return version


def read_pid() -> int | None:
    try:
        text = PID_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


def process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if not Path(f"/proc/{pid}").exists():
        return False
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        # /proc 不可用（例如非 Linux 上的单元测试）时退回「存在即可」。
        return True
    command = raw.replace(b"\x00", b" ").decode("utf-8", "replace")
    return DAEMON_NAME in command


# ---------------------------------------------------------------------------
# RPC 凭据
# ---------------------------------------------------------------------------
# transmission 把 rpc-password 以加盐哈希存进 settings.json，且实测无法用该哈希
# 通过 RPC 鉴权（会返回 401，见 README「远程访问」）。所以开启鉴权后，插件必须
# 自己记住用户设置的明文口令，才能继续向回环 daemon 发请求。
# 文件权限 0600，与 admin token 同级的本机机密。
CREDENTIAL_FILE = DATA_DIR / "rpc-credential.json"


def read_credential() -> dict:
    try:
        data = json.loads(CREDENTIAL_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_credential(username: str, password: str) -> None:
    CREDENTIAL_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = CREDENTIAL_FILE.with_name(CREDENTIAL_FILE.name + ".plugin.tmp")
    temporary.write_text(
        json.dumps({"username": username, "password": password}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    temporary.replace(CREDENTIAL_FILE)
    os.chmod(CREDENTIAL_FILE, 0o600)


def auth_header() -> dict:
    """回环 RPC 请求要带的 Basic 凭据；没配置就返回空。"""
    credential = read_credential()
    username = str(credential.get("username", ""))
    password = str(credential.get("password", ""))
    if not username or not password:
        return {}
    token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def rpc_request(payload: dict, timeout: float = 3.0) -> tuple[int, dict, bytes]:
    """调用 daemon 的 RPC。返回 (状态码, 响应头, 响应体)。"""
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    headers.update(auth_header())
    if SESSION_ID["value"]:
        headers["X-Transmission-Session-Id"] = SESSION_ID["value"]
    connection = HTTPConnection("127.0.0.1", RPC_PORT, timeout=timeout)
    try:
        connection.request("POST", RPC_PATH + "rpc", body=body, headers=headers)
        response = connection.getresponse()
        data = response.read()
        return response.status, dict(response.getheaders()), data
    finally:
        connection.close()


def rpc_alive() -> bool:
    try:
        status, headers, _ = rpc_request({"method": "session-stats"}, timeout=2.0)
    except (OSError, HTTPException):
        return False
    session = headers.get("X-Transmission-Session-Id") or headers.get(
        "x-transmission-session-id"
    )
    if session:
        SESSION_ID["value"] = session
    # 200 = 正常；409 = daemon 在跑但要求带上 session id。
    return status in (200, 409)


def session_stats() -> dict | None:
    for _ in range(2):
        try:
            status, headers, data = rpc_request({"method": "session-stats"})
        except (OSError, HTTPException):
            return None
        session = headers.get("X-Transmission-Session-Id") or headers.get(
            "x-transmission-session-id"
        )
        if session:
            SESSION_ID["value"] = session
        if status == 409:
            continue
        if status != 200:
            return None
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            return None
        arguments = payload.get("arguments")
        return arguments if isinstance(arguments, dict) else None
    return None


def daemon_running() -> bool:
    pid = read_pid()
    if pid is not None and process_exists(pid):
        return True
    return rpc_alive()


def start_daemon(timeout: float = 25.0) -> tuple[bool, str]:
    if daemon_running():
        return True, ""
    if not DAEMON.is_file():
        return False, f"未找到 {DAEMON}（请先运行 scripts/fetch_runtime.py）"
    ensure_runtime_executables()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # 先把 settings.json 补齐（含固定 RPC 端点），避免 daemon 用默认 0.0.0.0:9091 起。
    with WRITE_LOCK:
        raw = read_settings()
        style = detect_style(raw)
        write_settings(merge_managed(raw, style, {}))
    try:
        log = open(LOG_FILE, "ab")  # noqa: SIM115 - 交给子进程持有
    except OSError as error:
        return False, f"无法写入日志文件：{error}"
    try:
        subprocess.Popen(
            runtime_argv(DAEMON, "-f", "-g", str(DATA_DIR), "-e", str(LOG_FILE),
                         "-x", str(PID_FILE)),
            cwd=str(RUNTIME_DIR),
            env=daemon_env(),
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except OSError as error:
        log.close()
        return False, str(error)
    finally:
        log.close()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if rpc_alive():
            return True, ""
        time.sleep(0.3)
    return False, f"等待 {timeout:g} 秒仍未就绪，请查看 {LOG_FILE}"


def find_daemon_pids() -> list[int]:
    """扫描 /proc 找出属于本插件数据目录的 daemon 进程。

    兜底用：早期版本启动时没写 pid 文件（transmission 4.x 的开关是 -x，
    不是 -P），一旦漏了就会「进程在跑但停不掉」。
    匹配「本插件二进制完整路径 + 本插件数据目录」两个条件——只用
    daemon 名会误伤任何命令行里恰好含该字符串的进程（例如一段 grep 命令）。
    """
    marker = str(BIN_DIR / DAEMON_NAME)
    data_marker = str(DATA_DIR)
    found: list[int] = []
    proc = Path("/proc")
    if not proc.is_dir():
        return found
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        command = raw.replace(b"\x00", b" ").decode("utf-8", "replace")
        if marker in command and data_marker in command:
            found.append(int(entry.name))
    return found


def terminate_pids(pids: list[int], timeout: float) -> None:
    """先 SIGTERM 再按需 SIGKILL，等它们退出。"""
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            continue
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(process_exists(pid) for pid in pids):
            return
        time.sleep(0.2)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            continue
    time.sleep(1.0)


def stop_daemon(timeout: float = 20.0) -> tuple[bool, str]:
    pid = read_pid()
    if pid is None:
        # 没有 pid 文件：先扫进程兜底（历史遗留的实例会走到这里）
        orphans = find_daemon_pids()
        if orphans:
            terminate_pids(orphans, timeout)
            try:
                PID_FILE.unlink()
            except OSError:
                pass
            return True, ""
        if rpc_alive():
            # 端口有响应但没有我们的进程——那是别人启的实例，不动它
            return False, "RPC 端口有响应，但不是本插件启动的 daemon，无法安全停止"
        return True, ""
    if not process_exists(pid):
        try:
            PID_FILE.unlink()
        except OSError:
            pass
        return True, ""
    terminate_pids([pid], timeout)
    try:
        PID_FILE.unlink()
    except OSError:
        pass
    return True, ""


def restart_daemon() -> tuple[bool, str]:
    ok, error = stop_daemon()
    if not ok:
        return False, error
    return start_daemon()


def read_plugin_state() -> dict:
    try:
        data = json.loads(PLUGIN_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_plugin_state(state: dict) -> None:
    PLUGIN_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = PLUGIN_STATE_FILE.with_name(PLUGIN_STATE_FILE.name + ".plugin.tmp")
    temporary.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.chmod(temporary, 0o644)
    temporary.replace(PLUGIN_STATE_FILE)


def set_enabled(enabled: bool) -> None:
    """记住「用户希望 daemon 运行」，插件服务重启/设备重启后据此自动拉起。"""
    state = read_plugin_state()
    state["enabled"] = bool(enabled)
    write_plugin_state(state)


def should_autostart() -> bool:
    return bool(read_plugin_state().get("enabled")) and DAEMON.is_file()


def status_payload() -> dict:
    running = daemon_running()
    stats = session_stats() if running else None
    raw = read_settings()
    style = detect_style(raw)
    values = effective_values(raw, style)
    return {
        "ok": True,
        "daemonRunning": running,
        "autostart": should_autostart(),
        "pid": read_pid(),
        "version": daemon_version(),
        "runtimeInstalled": DAEMON.is_file(),
        "webControlInstalled": (WEB_DIR / WEB_CONTROL_DIRNAME / "index.html").is_file(),
        "webControlPath": f"{WEB_CONTROL_DIRNAME}/index.html",
        "configDir": str(DATA_DIR),
        "downloadDir": values.get("download-dir"),
        "settingsPath": str(SETTINGS_FILE),
        "settingsKeyStyle": style,
        "rpcPort": RPC_PORT,
        "rpcBind": str(values.get("rpc-bind-address") or "127.0.0.1"),
        "rpcAuthRequired": bool(values.get("rpc-authentication-required")),
        "rpcUsername": str(values.get("rpc-username") or ""),
        "session": {
            "torrentCount": (stats or {}).get("torrentCount"),
            "activeTorrentCount": (stats or {}).get("activeTorrentCount"),
            "pausedTorrentCount": (stats or {}).get("pausedTorrentCount"),
            "downloadSpeed": ((stats or {}).get("downloadSpeed")),
            "uploadSpeed": ((stats or {}).get("uploadSpeed")),
        } if stats else None,
    }


def settings_payload() -> dict:
    raw = read_settings()
    style = detect_style(raw)
    values = effective_values(raw, style)
    fields = []
    for field in FIELDS:
        value = values.get(field["id"])
        # 绝不回显已保存的密码：settings.json 里存的是加盐哈希，
        # 回显既没用又会泄漏。界面里留空即表示不修改。
        if field["kind"] == "password":
            value = ""
        fields.append(
            {
                "id": field["id"],
                "label": field["label"],
                "kind": field["kind"],
                "group": field["group"],
                "unit": field.get("unit", ""),
                "help": field["help"],
                "minimum": field.get("minimum"),
                "maximum": field.get("maximum"),
                "choices": field.get("choices"),
                "default": field.get("default"),
                "storageKey": key_for(field["id"], style),
                "value": value,
            }
        )
    return {
        "ok": True,
        "groups": GROUPS,
        "fields": fields,
        "values": values,
        "settingsKeyStyle": style,
        "settingsPath": str(SETTINGS_FILE),
        "defaults": {field["id"]: field.get("default") for field in FIELDS},
    }


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "XiaomiTransmission/0.1"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{self.address_string()} [{self.log_date_time_string()}] {fmt % args}")

    def _send(self, status: int, body: bytes, content_type: str, headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if not body:
            return
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # 对端已经走了，不要再去碰这个 socket。
            self.close_connection = True

    def _json(self, status: int, value: object, headers: dict | None = None) -> None:
        self._send(status, json_bytes(value), "application/json; charset=utf-8", headers)

    def _read_raw_body(self) -> bytes:
        """把请求体从连接里读出来。

        **一个请求的 rfile 只能读一次**：正文是流式的，读完就没了。
        所有分支都必须走这里（或拿到它的返回值），不要各自去读 rfile，
        否则后读的那一方会一直阻塞到客户端超时。
        """
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError as error:
            # 长度都读不出来，流已经没法对齐，关掉连接。
            self.close_connection = True
            raise ValueError("无效请求长度") from error
        if length < 0 or length > MAX_BODY_BYTES:
            # 超限的正文不读，也就无法把连接状态对齐，索性关掉连接。
            self.close_connection = True
            raise ValueError("请求体过大")
        if not length:
            return b""
        chunks: list[bytes] = []
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) != length:
            # 客户端没发完就断了，连接已不可用，别试图回复。
            self.close_connection = True
            raise ValueError("请求体不完整")
        return raw

    # ---- 静态文件 ----

    def _serve_static(self, path: str) -> None:
        relative = "index.html" if path in ("", "/") else path.lstrip("/")
        try:
            root = WEB_DIR.resolve()
            candidate = (WEB_DIR / relative).resolve()
        except OSError:
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})
            return
        if candidate != root and root not in candidate.parents:
            self._json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "forbidden"})
            return
        if not candidate.is_file():
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})
            return
        mime = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        cache = "no-store" if candidate.suffix in (".html", ".json") else "public, max-age=300"
        try:
            body = candidate.read_bytes()
        except OSError:
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})
            return
        self._send(HTTPStatus.OK, body, mime, {"Cache-Control": cache})

    # ---- RPC 反代 ----

    def _proxy_rpc(self, payload: bytes) -> None:
        """把已经读出来的 RPC 报文原样转发给 daemon。

        payload 由 do_POST 通过 _read_raw_body() 读好后传进来——这里绝不能再读
        self.rfile（同一段正文已经被消费掉了，再读会阻塞到超时）。
        """
        if len(payload) > MAX_BODY_BYTES:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "请求体过大"})
            return
        headers = {"Content-Type": "application/json"}
        # 客户端自己带了 Authorization 就原样透传；否则用插件保存的口令，
        # 这样开启鉴权后内嵌的 transmission-web-control 依然可用。
        if self.headers.get("Authorization"):
            headers["Authorization"] = self.headers["Authorization"]
        else:
            headers.update(auth_header())
        session = self.headers.get("X-Transmission-Session-Id")
        if session:
            headers["X-Transmission-Session-Id"] = session
        connection = None
        try:
            connection = HTTPConnection("127.0.0.1", RPC_PORT, timeout=15)
            connection.request("POST", RPC_PATH + "rpc", body=payload, headers=headers)
            response = connection.getresponse()
            body = response.read()
            status = response.status
            out: dict = {}
            session_out = response.getheader("X-Transmission-Session-Id")
            if session_out:
                out["X-Transmission-Session-Id"] = session_out
            content_type = response.getheader("Content-Type") or "application/json"
        except (OSError, HTTPException) as error:
            self._json(
                HTTPStatus.BAD_GATEWAY,
                {"ok": False, "error": f"transmission-daemon 未就绪：{error}"},
            )
            return
        finally:
            if connection is not None:
                connection.close()
        self._send(status, body, content_type, out)

    # ---- 路由 ----

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/healthz":
            self._json(HTTPStatus.OK, {"ok": True})
            return
        if path == "/api/status":
            self._json(HTTPStatus.OK, status_payload())
            return
        if path == "/api/settings":
            self._json(HTTPStatus.OK, settings_payload())
            return
        if path in ("/", "/index.html"):
            self._serve_static("index.html")
            return
        self._serve_static(path)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        # 一个请求只能读一次正文：/rpc 要原样透传，其余分支要解析成 JSON。
        # 两条路都从 _read_raw_body() 取，谁都不许再碰 self.rfile。
        try:
            payload = self._read_raw_body()
        except ValueError as error:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(error)})
            return

        if path == "/rpc":
            self._proxy_rpc(payload)
            return

        try:
            body = json.loads(payload or b"{}")
        except json.JSONDecodeError:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "请求体必须是 JSON"})
            return
        if not isinstance(body, dict):
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "请求体必须是对象"})
            return

        with STATE_LOCK:
            if path == "/api/settings":
                if "values" not in body:
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        {"ok": False, "error": "请求体缺少 values 字段"},
                    )
                    return
                restart = body.get("restart", True)
                if not isinstance(restart, bool):
                    self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "restart 必须是开关"})
                    return
                try:
                    result = apply_settings(body["values"], restart=restart)
                except SettingsError as error:
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        {"ok": False, "error": str(error), "fields": error.fields},
                    )
                    return
                except (DaemonError, OSError) as error:
                    self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(error)})
                    return
                self._json(HTTPStatus.OK, {"ok": True, **result})
                return

            if path == "/api/action":
                action = str(body.get("action", ""))
                if action == "start":
                    ok, error = start_daemon()
                    if ok:
                        set_enabled(True)
                elif action == "stop":
                    ok, error = stop_daemon()
                    if ok:
                        set_enabled(False)
                elif action == "restart":
                    ok, error = restart_daemon()
                    if ok:
                        set_enabled(True)
                else:
                    self._json(
                        HTTPStatus.BAD_REQUEST, {"ok": False, "error": f"未知操作：{action}"}
                    )
                    return
                if not ok:
                    self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": error})
                    return
                self._json(HTTPStatus.OK, status_payload())
                return

        self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})


def main() -> int:
    if not DAEMON.is_file():
        print(
            f"警告：{DAEMON} 不存在，界面可以打开但无法启动下载服务；"
            "请在仓库里运行 scripts/fetch_runtime.py 后重新打包安装。",
            file=sys.stderr, flush=True,
        )
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ensure_runtime_executables()
    if should_autostart():
        ok, error = start_daemon()
        print(
            f"开机自启：transmission-daemon {'已启动' if ok else '启动失败：' + error}",
            flush=True,
        )
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Xiaomi Transmission plugin listening on http://{HOST}:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
