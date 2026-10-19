#!/usr/bin/env python3
"""SSH 开关插件：手动启停 dropbear，并可选开机自动保持运行。

背景：/lib/minas/boot_check.sh 的 ssh_check() 每次开机检查
sysmode=factory / channel=develop / RPMB 标志 ssh_en=true，
三者皆不满足时执行 `systemctl stop dropbear.socket` 关闭 SSH。
部分设备 RPMB 写入失效（mitee_tool rpmb set ssh_en true 报
"rpmb set verify failed"），该标志无法持久化。

插件提供两种「开机自动保持」的守护方式（可单独用，也可同时用）：

- cron：每分钟巡检一次，发现 dropbear 没跑就拉起。兼容性最好，代价是每分钟一次检查。
- hotplug：往厂商的 syshotplug 框架里放一个存储池挂载钩子
  （/etc/syshotplug/pool/98.ssh-control，ACTION=mounted 时被调用）。事件驱动、不轮询；
  但本机实测池挂载事件比 boot_check 的 ssh_check 早几秒，所以钩子内部会 fork 一个
  后台任务、等 ssh_check 跑完再拉起并复查几分钟（见 hotplug.sh 的注释）。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "18130"))
BASE_PATH = os.environ.get("BASE_PATH", "/")
WEB_DIR = Path(os.environ.get("WEB_DIR", Path(__file__).resolve().parent / "web"))
RUNTIME_DIR = Path(os.environ.get("RUNTIME_DIR", Path(__file__).resolve().parent))
STATE_DIR = Path(os.environ.get("SSH_STATE_DIR", "/data/plugin/ssh-control/state"))
STATE_FILE = Path(os.environ.get("STATE_FILE", "/data/plugin/ssh-control/state.json"))
DROPBEAR_UNIT = "dropbear.socket"
KEEPALIVE_SCRIPT = RUNTIME_DIR / "keepalive.sh"
CRON_TAG = "sshcontrol"
LOCK = threading.Lock()

# 存储池挂载钩子（厂商 syshotplug 框架）。路径可用环境变量覆盖，便于测试。
HOTPLUG_SCRIPT = RUNTIME_DIR / "hotplug.sh"
HOTPLUG_DIR = Path(os.environ.get("HOTPLUG_DIR", "/etc/syshotplug/pool"))
HOTPLUG_NAME = "98.ssh-control"
# /etc 是 overlay，写入 /etc/syshotplug/pool 会落到这里；用来向用户报告"已持久化"
HOTPLUG_UPPER_DIR = Path(os.environ.get("HOTPLUG_UPPER_DIR", "/data/etc/upper/syshotplug/pool"))

# ---------------------------------------------------------------------------
# 执行通道修复（mi-shell => bash 的可逆 bind-mount 垫片）
#
# 背景（真机实证）：这台固件的 dropbear 被厂商改成了**固定 exec /usr/sbin/mi-shell**
# （二进制里硬编码该路径），而 mi-shell 是个 JSON 协议受限壳，只看 stdin、**忽略
# `-c 命令`**，于是 `ssh host 'id'` 认证成功却什么都不执行。
# 根分区是只读 erofs，改不了二进制；用 bind-mount 把 mi-shell 顶替成"转发 bash"的
# 垫片即可，`umount` 立刻还原（原文件从未被修改）。
# ---------------------------------------------------------------------------
MI_SHELL = Path(os.environ.get("MI_SHELL_PATH", "/usr/sbin/mi-shell"))
SHIM_SOURCE = RUNTIME_DIR / "mi-shell-shim"
SHIM_PATH = Path(os.environ.get("SSH_SHIM_PATH", str(STATE_DIR / "mi-shell-shim")))
RESTORE_SOURCE = RUNTIME_DIR / "98.ssh-restore"
RESTORE_NAME = "98.ssh-restore"
PUBKEY_MASTER = Path(os.environ.get("SSH_PUBKEY_MASTER", str(STATE_DIR / "ssh-pubkey.txt")))
SHADOW_MASTER = Path(os.environ.get("SSH_SHADOW_MASTER", str(STATE_DIR / "root-shadow")))
RESTORE_LOG = Path(os.environ.get("SSH_RESTORE_LOG", str(STATE_DIR / "restore.log")))
PASSWD_FILE = Path(os.environ.get("PASSWD_FILE", "/etc/passwd"))
SHADOW_FILE = Path(os.environ.get("SHADOW_FILE", "/etc/shadow"))
PROC_MOUNTS = Path(os.environ.get("PROC_MOUNTS", "/proc/1/mounts"))
# 本服务有 PrivateTmp=true ⇒ systemd 给它单独建了 mount namespace，
# 服务内 `mount` 对主机（以及 dropbear）**无效**（实测坐实）。所以挂载/卸载/跑钩子
# 都要通过 nsenter 进 PID 1 的 namespace 去做。
NSENTER_CANDIDATES = ("/bin/nsenter", "/usr/bin/nsenter", "/usr/sbin/nsenter")
DROPBEAR_AUTHORIZED_KEYS = Path(
    os.environ.get("DROPBEAR_AUTHORIZED_KEYS", "/etc/dropbear/authorized_keys")
)
PLUGIN_DIR = Path(os.environ.get("PLUGIN_DIR", "/data/plugin"))
STORE_REGISTRATION = Path(
    os.environ.get("STORE_REGISTRATION", "/data/plugin/community-store/state/store.json")
)

# 守护方式：cron / hotplug / both
METHODS = ("cron", "hotplug", "both")
DEFAULT_METHOD = "cron"


# ---------------------------------------------------------------------------
# systemd / cron 交互
# ---------------------------------------------------------------------------

def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=False)


def ssh_running() -> bool:
    return _run(["systemctl", "is-active", "--quiet", DROPBEAR_UNIT]).returncode == 0


def ssh_enabled() -> bool:
    return _run(["systemctl", "is-enabled", "--quiet", DROPBEAR_UNIT]).returncode == 0


def start_ssh() -> tuple[bool, str]:
    result = _run(["systemctl", "start", DROPBEAR_UNIT])
    if result.returncode != 0:
        return False, (result.stderr or result.stdout or "启动失败").strip()
    return True, ""


def stop_ssh() -> tuple[bool, str]:
    result = _run(["systemctl", "stop", DROPBEAR_UNIT])
    if result.returncode != 0:
        return False, (result.stderr or result.stdout or "停止失败").strip()
    return True, ""


def read_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(STATE_FILE)


def autostart_enabled() -> bool:
    return bool(read_state().get("autostart"))


def _cron_lines() -> list[str]:
    result = _run(["crontab", "-l"])
    if result.returncode != 0:
        return []
    return result.stdout.splitlines()


def _write_cron(lines: list[str]) -> bool:
    payload = "\n".join(line for line in lines if line.strip())
    result = subprocess.run(
        ["crontab", "-"],
        input=payload + "\n" if payload else "",
        capture_output=True, text=True, check=False,
    )
    return result.returncode == 0


def sync_cron(enabled: bool) -> tuple[bool, str]:
    """根据开关状态增删 cron 守护条目。

    用 `sh <脚本>` 调用而不是直接执行：发布包解压后文件权限统一为 0644，
    直接执行会因缺少可执行位失败。
    """
    lines = [line for line in _cron_lines() if CRON_TAG not in line]
    if enabled:
        lines.append(f"* * * * * sh {KEEPALIVE_SCRIPT} #@{CRON_TAG}")
    if not _write_cron(lines):
        return False, "写入 crontab 失败"
    return True, ""


# ---------------------------------------------------------------------------
# 存储池挂载钩子（厂商 syshotplug 框架）
# ---------------------------------------------------------------------------

def hotplug_path() -> Path:
    return HOTPLUG_DIR / HOTPLUG_NAME


def hotplug_installed() -> bool:
    """钩子在位且可执行——syshotplug 只执行带可执行位的文件。"""
    target = hotplug_path()
    return target.is_file() and os.access(target, os.X_OK)


def hotplug_persisted() -> bool:
    """是否已经落在 overlay 的持久层（/data/etc/upper/...），重启后还在。"""
    return (HOTPLUG_UPPER_DIR / HOTPLUG_NAME).is_file()


def hotplug_up_to_date() -> bool:
    try:
        return hotplug_path().read_bytes() == HOTPLUG_SCRIPT.read_bytes()
    except OSError:
        return False


def install_hotplug() -> tuple[bool, str]:
    try:
        payload = HOTPLUG_SCRIPT.read_bytes()
    except OSError as error:
        return False, f"读取钩子脚本失败：{error}"
    target = hotplug_path()
    temporary = target.with_name(target.name + ".tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_bytes(payload)
        # 必须补可执行位：发布包解压出来是 0644，而 syshotplug 会跳过不可执行的文件
        os.chmod(temporary, 0o755)
        temporary.replace(target)
    except OSError as error:
        return False, f"安装钩子失败：{error}"
    return True, ""


def remove_hotplug() -> tuple[bool, str]:
    target = hotplug_path()
    try:
        target.unlink()
    except FileNotFoundError:
        return True, ""
    except OSError as error:
        return False, f"删除钩子失败：{error}"
    return True, ""


def sync_hotplug(enabled: bool) -> tuple[bool, str]:
    if enabled:
        return install_hotplug()
    return remove_hotplug()


def current_method() -> str:
    method = read_state().get("method")
    return method if method in METHODS else DEFAULT_METHOD


def sync_methods(enabled: bool) -> tuple[bool, str]:
    """按当前方式开关 cron 与 hotplug 两条守护路径。"""
    method = current_method()
    ok, error = sync_cron(enabled and method in ("cron", "both"))
    if not ok:
        return False, error
    ok, error = sync_hotplug(enabled and method in ("hotplug", "both"))
    if not ok:
        return False, error
    return True, ""


def set_autostart(enabled: bool) -> tuple[bool, str]:
    ok, error = sync_methods(enabled)
    if not ok:
        return False, error
    state = read_state()
    state["autostart"] = enabled
    state.setdefault("method", current_method())
    write_state(state)
    return True, ""


def set_method(method: str) -> tuple[bool, str]:
    if method not in METHODS:
        return False, f"未知方式：{method}"
    state = read_state()
    state["method"] = method
    write_state(state)
    if autostart_enabled():
        ok, error = sync_methods(True)
        if not ok:
            return False, error
    return True, ""


# ---------------------------------------------------------------------------
# 执行通道：参数解析（一律从本机实际状态推导，不写死任何用户 id）
# ---------------------------------------------------------------------------

def detect_user_id() -> str:
    """当前 NAS 用户 id（纯数字）。优先插件注册信息，其次最新的 u*.list。"""
    override = os.environ.get("SSH_CONTROL_USER_ID", "").strip()
    if override:
        return override.lstrip("u")
    try:
        data = json.loads(STORE_REGISTRATION.read_text(encoding="utf-8"))
        user = str(data.get("user", "")).strip()
        if user:
            return user.lstrip("u")
    except (OSError, json.JSONDecodeError):
        pass
    try:
        lists = sorted(PLUGIN_DIR.glob("u*.list"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        lists = []
    return lists[0].stem.lstrip("u") if lists else ""


def resolve_pool_data(user_id: str) -> str:
    """池里的 data 目录；不存在则返回空串（调用方据此判断"不适用"）。"""
    override = os.environ.get("SSH_CONTROL_POOL_DATA", "")
    if override:
        return override
    if not user_id:
        return ""
    candidate = Path(f"/home/u{user_id}/pool0/data")
    return str(candidate) if candidate.is_dir() else ""


def resolve_root_home() -> str:
    """root 的 home 必须是持久层（/home 在池上，能扛 OTA）。

    已经在 /home 下就沿用；否则用厂商工具创建的那个（`/home/rootx`）。
    """
    override = os.environ.get("SSH_CONTROL_ROOT_HOME", "")
    if override:
        return override
    home = ""
    try:
        for line in PASSWD_FILE.read_text(encoding="utf-8").splitlines():
            if line.startswith("root:"):
                fields = line.split(":")
                if len(fields) > 5:
                    home = fields[5]
                break
    except OSError:
        pass
    if home.startswith("/home/"):
        return home
    return "/home/rootx"


def restore_hook_path() -> Path:
    return HOTPLUG_DIR / RESTORE_NAME


def mi_shell_shim_mounted() -> bool:
    """垫片是否已挂上（读 /proc/mounts，不依赖 mountpoint 命令）。"""
    try:
        text = PROC_MOUNTS.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    needle = f" {MI_SHELL} "
    return any(needle in line for line in text.splitlines())


def restore_hook_installed() -> bool:
    target = restore_hook_path()
    return target.is_file() and os.access(target, os.X_OK)


def authorized_keys_present() -> bool:
    root_home = resolve_root_home()
    for candidate in (DROPBEAR_AUTHORIZED_KEYS,
                      Path(root_home) / ".ssh" / "authorized_keys"):
        try:
            if candidate.is_file() and candidate.stat().st_size > 0:
                return True
        except OSError:
            continue
    return False


def render_restore_hook() -> str:
    """把模板里的占位符换成本机实际值（钩子因此自包含，不需要读外部配置）。"""
    try:
        template = RESTORE_SOURCE.read_text(encoding="utf-8")
    except OSError as error:
        raise RuntimeError(f"读取钩子模板失败：{error}") from error
    user_id = detect_user_id()
    values = {
        "@USER_ID@": user_id or "unknown",
        "@POOL_DATA@": resolve_pool_data(user_id) or "(unknown)",
        "@ROOT_HOME@": resolve_root_home(),
        "@PUBKEY_FILE@": str(PUBKEY_MASTER),
        "@SHADOW_FILE@": str(SHADOW_MASTER),
        "@SHIM@": str(SHIM_PATH),
        "@LOG@": str(RESTORE_LOG),
        "@MI_SHELL@": str(MI_SHELL),
    }
    rendered = template
    for placeholder, value in values.items():
        rendered = rendered.replace(placeholder, value)
    return rendered


def ensure_shim() -> tuple[bool, str]:
    """把 mi-shell-shim 落到插件自己的 state 目录（0700 目录 / 0755 文件）。"""
    try:
        payload = SHIM_SOURCE.read_bytes()
    except OSError as error:
        return False, f"读取垫片失败：{error}"
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(STATE_DIR, 0o700)
        temporary = SHIM_PATH.with_name(SHIM_PATH.name + ".tmp")
        temporary.write_bytes(payload)
        os.chmod(temporary, 0o755)
        temporary.replace(SHIM_PATH)
    except OSError as error:
        return False, f"写入垫片失败：{error}"
    return True, ""


def ensure_pubkey_master() -> tuple[bool, str]:
    """把当前有效的公钥收进母本（钩子靠它恢复 authorized_keys）。"""
    keys: list[str] = []
    root_home = resolve_root_home()
    for source in (DROPBEAR_AUTHORIZED_KEYS,
                   Path(root_home) / ".ssh" / "authorized_keys"):
        try:
            for line in source.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and line not in keys:
                    keys.append(line)
        except OSError:
            continue
    if not keys:
        return False, "没有读到任何公钥（先配置公钥登录再用一键修复）"
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        temporary = PUBKEY_MASTER.with_name(PUBKEY_MASTER.name + ".tmp")
        temporary.write_text("\n".join(keys) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(PUBKEY_MASTER)
    except OSError as error:
        return False, f"写入公钥母本失败：{error}"
    return True, ""


def ensure_shadow_master() -> tuple[bool, str]:
    """把 root 当前的密码哈希收进母本（为空/锁定时跳过）。"""
    current = ""
    try:
        for line in SHADOW_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("root:"):
                fields = line.split(":")
                current = fields[1] if len(fields) > 1 else ""
                break
    except OSError as error:
        return False, f"读取 /etc/shadow 失败：{error}"
    if not current or current.startswith("!") or current.startswith("*"):
        return True, ""  # 没有可用哈希就不写母本，钩子会跳过这一步
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        temporary = SHADOW_MASTER.with_name(SHADOW_MASTER.name + ".tmp")
        temporary.write_text(current + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(SHADOW_MASTER)
    except OSError as error:
        return False, f"写入哈希母本失败：{error}"
    return True, ""


def install_restore_hook() -> tuple[bool, str]:
    try:
        rendered = render_restore_hook()
    except RuntimeError as error:
        return False, str(error)
    target = restore_hook_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(rendered, encoding="utf-8")
        os.chmod(temporary, 0o755)
        temporary.replace(target)
    except OSError as error:
        return False, f"安装钩子失败：{error}"
    return True, ""


def remove_restore_hook() -> tuple[bool, str]:
    try:
        restore_hook_path().unlink()
    except FileNotFoundError:
        return True, ""
    except OSError as error:
        return False, f"删除钩子失败：{error}"
    return True, ""


def _host_run(command: list[str]) -> subprocess.CompletedProcess[str]:
    """在 **PID 1 的 mount namespace** 里执行命令。

    为什么必须这样：unit 里有 `PrivateTmp=true`，systemd 据此给服务建了一个
    private mount namespace（实测 `mnt:[4026532708]` vs PID1 的 `mnt:[4026531841]`）。
    服务内直接 `mount --bind` 只是"自己看得见"——`/proc/1/mounts` 里没有，
    dropbear 也就用不上。用 `nsenter -t 1 -m` 落到主机命名空间才真正生效。

    没有 nsenter 时退回本命名空间（老行为，至少不会崩）。
    """
    getuid = getattr(os, "geteuid", None)
    is_root = bool(getuid and getuid() == 0)
    if is_root and Path("/proc/1/ns/mnt").exists():
        for candidate in NSENTER_CANDIDATES:
            if Path(candidate).exists():
                return _run([candidate, "-t", "1", "-m", "--"] + command)
    return _run(command)


def mount_shim() -> tuple[bool, str]:
    if not MI_SHELL.exists():
        return False, f"{MI_SHELL} 不存在，本机不适用"
    if mi_shell_shim_mounted():
        return True, ""
    if not os.access(SHIM_PATH, os.R_OK):
        return False, f"垫片不存在：{SHIM_PATH}"
    result = _host_run(["mount", "--bind", str(SHIM_PATH), str(MI_SHELL)])
    if result.returncode != 0:
        error = (result.stderr or result.stdout or "mount 失败").strip()
        return False, error
    return True, ""


def unmount_shim() -> tuple[bool, str]:
    if not mi_shell_shim_mounted():
        return True, ""
    result = _host_run(["umount", str(MI_SHELL)])
    if result.returncode != 0:
        error = (result.stderr or result.stdout or "umount 失败").strip()
        return False, error
    return True, ""


def run_restore_hook() -> tuple[bool, str]:
    """立刻跑一次钩子，把三件事一次性落实（不等下次开机）。

    同样走 nsenter：钩子里就包含 `mount --bind`，在私有 namespace 里跑等于白跑。
    """
    target = restore_hook_path()
    if not target.is_file():
        return False, "钩子不存在"
    result = _host_run(["/bin/sh", str(target)])
    if result.returncode != 0:
        return False, (result.stderr or result.stdout or "钩子执行失败").strip()
    return True, ""


def exec_channel_status() -> dict:
    applicable = MI_SHELL.exists()
    installed = restore_hook_installed()
    mounted = mi_shell_shim_mounted()
    keys = authorized_keys_present()
    if not applicable:
        state = "not-applicable"
    elif installed and mounted and keys:
        state = "fixed"
    else:
        state = "broken"
    return {
        "state": state,
        "applicable": applicable,
        "hookInstalled": installed,
        "shimMounted": mounted,
        "authorizedKeys": keys,
        "shimFile": SHIM_PATH.is_file(),
        "hookPath": str(restore_hook_path()),
        "shimPath": str(SHIM_PATH),
        "miShellPath": str(MI_SHELL),
    }


def fix_exec_channel() -> tuple[bool, str]:
    """一键修复：垫片 + 公钥/哈希母本 + 钩子 + 立刻挂上。全程幂等。"""
    for step in (ensure_shim, ensure_pubkey_master, ensure_shadow_master, install_restore_hook):
        ok, error = step()
        if not ok:
            return False, error
    ok, error = run_restore_hook()
    if not ok:
        return False, error
    ok, error = mount_shim()
    if not ok:
        return False, error
    state = read_state()
    state["execFix"] = True
    write_state(state)
    return True, ""


def revert_exec_channel() -> tuple[bool, str]:
    """一键还原：卸载垫片 + 删钩子。**不动公钥登录本身**，也不删已配置的公钥。"""
    ok, error = unmount_shim()
    if not ok:
        return False, error
    ok, error = remove_restore_hook()
    if not ok:
        return False, error
    state = read_state()
    state["execFix"] = False
    write_state(state)
    return True, ""


def exec_channel_wanted() -> bool:
    """插件启动时是否要自动装。默认装；用户显式「一键还原」过就不再自动装回来。"""
    return read_state().get("execFix") is not False


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "XiaomiSshControl/0.1"

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"{self.address_string()} [{self.log_date_time_string()}] {fmt % args}")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, value: object) -> None:
        self._send(status, json_bytes(value), "application/json; charset=utf-8")

    def _status(self) -> dict:
        method = current_method()
        return {
            "ok": True,
            "sshRunning": ssh_running(),
            "sshEnabled": ssh_enabled(),
            "autostart": autostart_enabled(),
            "unit": DROPBEAR_UNIT,
            "method": method,
            "hotplug": {
                "installed": hotplug_installed(),
                "persisted": hotplug_persisted(),
                "upToDate": hotplug_up_to_date(),
                "path": str(hotplug_path()),
                "name": HOTPLUG_NAME,
            },
            "exec": exec_channel_status(),
        }

    def _serve_static(self, path: str) -> None:
        relative = "index.html" if path in ("", "/") else path.lstrip("/")
        candidate = (WEB_DIR / relative).resolve()
        try:
            candidate.relative_to(WEB_DIR.resolve())
        except ValueError:
            self._json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "forbidden"})
            return
        if not candidate.is_file():
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})
            return
        import mimetypes
        mime = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        cache = "no-store" if candidate.name == "index.html" else "public, max-age=300"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mime)
        self.send_header("Cache-Control", cache)
        body = candidate.read_bytes()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/status":
            self._json(HTTPStatus.OK, self._status())
            return
        if path == "/healthz":
            self._json(HTTPStatus.OK, {"ok": True})
            return
        if path in ("/", "/index.html"):
            self._serve_static("index.html")
            return
        self._serve_static(path)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "无效请求长度"})
            return
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "请求体必须是 JSON"})
            return
        if not isinstance(body, dict):
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "请求体必须是对象"})
            return

        with LOCK:
            if path == "/api/action":
                action = str(body.get("action", ""))
                if action == "start":
                    ok, error = start_ssh()
                elif action == "stop":
                    # 手动停止同时关闭自启，避免守护把它拉回来
                    ok, error = stop_ssh()
                    if ok:
                        set_autostart(False)
                else:
                    self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": f"未知操作：{action}"})
                    return
                if not ok:
                    self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": error})
                    return
                self._json(HTTPStatus.OK, self._status())
                return

            if path == "/api/autostart":
                enabled = bool(body.get("enabled"))
                chosen = body.get("method")
                if chosen is not None:
                    ok, error = set_method(str(chosen))
                    if not ok:
                        self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": error})
                        return
                ok, error = set_autostart(enabled)
                if not ok:
                    self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": error})
                    return
                if enabled and not ssh_running():
                    start_ssh()
                self._json(HTTPStatus.OK, self._status())
                return

            if path == "/api/method":
                ok, error = set_method(str(body.get("method", "")))
                if not ok:
                    self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": error})
                    return
                if autostart_enabled() and not ssh_running():
                    start_ssh()
                self._json(HTTPStatus.OK, self._status())
                return

            if path == "/api/exec/fix":
                ok, error = fix_exec_channel()
                if not ok:
                    self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": error})
                    return
                self._json(HTTPStatus.OK, self._status())
                return

            if path == "/api/exec/revert":
                ok, error = revert_exec_channel()
                if not ok:
                    self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": error})
                    return
                self._json(HTTPStatus.OK, self._status())
                return

        self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})


def main() -> int:
    # CLI：给外部（例如应用店的开机钩子）做一次幂等修复，跑完即退出，不常驻。
    if "--fix-exec" in sys.argv[1:]:
        ok, error = fix_exec_channel()
        print(f"fix-exec: {'ok' if ok else error}", flush=True)
        return 0 if ok else 1
    if "--exec-status" in sys.argv[1:]:
        print(json.dumps(exec_channel_status(), ensure_ascii=False), flush=True)
        return 0

    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    # 插件启动时**幂等**装好执行通道修复（用户显式「一键还原」过就不再装回来）
    if exec_channel_wanted():
        ok, error = fix_exec_channel()
        print(f"exec channel fix: {'ok' if ok else error}", flush=True)
    else:
        print("exec channel fix: skipped (reverted by user)", flush=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Xiaomi SSH control listening on http://{HOST}:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
