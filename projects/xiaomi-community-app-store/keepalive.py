#!/usr/bin/env python3
"""社区插件自愈：server.list 条目注入 + 每插件备份 + 定时自愈。

背景（本机实测）
----------------
小米 NAS 的 `plugincenter` 会拿云端下发的 `server.list`（本地副本在
`/data/plugin/server.list`）和本地已安装插件做对账：

* 打开 App 的「应用市场」会触发一次对账；
* 云端 `server.list` 里只有厂商自己的那几条，社区插件不在其中；
* 对账时 `plugincenter` 会把「不在 server.list 里」的插件当成非法安装，
  执行 force uninstall —— 删目录、删注册表条目、停服务。

所以光靠「开机兜底 + 端口表」挡不住**运行期**的对账删除。本模块补三件事：

1. **条目注入**：把我们在册的 10 个插件的条目（从各自 `INFO` 生成，或复用
   注册表里已有的记录）补进 `/data/plugin/server.list` 的 `.data` 里，
   让对账时它们「在册」。**只增不改**：厂商条目与已在的条目一律不动。
2. **每插件备份**：把 `/data/plugin/<dir>/` 打包到
   `/data/backup/plugins/<dir>.tar.gz`（**排除 `data/`** 等用户数据目录），
   这样目录被删也能还原。
3. **自愈循环**：每 2 分钟检查一次 —— server.list 缺条目就注入、插件目录
   缺失就从备份恢复、服务没跑就拉起，并写一行结构化日志。
   既由市场服务自己的后台线程驱动，也装了 root cron 作双保险（两处都幂等）。

安全边界
--------
* 只处理 :data:`PLUGINS` 里列出的 10 个插件，**绝不改动厂商的 5 条**；
* 备份**排除用户数据目录**（`data`），不复制任何用户内容；
* 所有写入都是「临时文件 + 原子替换」，不会写出半截 JSON。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

# --------------------------------------------------------------------------- 配置

DEFAULT_INTERVAL = 120  # 秒；社区经验值：应用市场对账后 150 秒内自愈
DEFAULT_SERVER_LIST = Path("/data/plugin/server.list")
DEFAULT_PLUGIN_ROOT = Path("/data/plugin")
DEFAULT_BACKUP_ROOT = Path("/data/backup/plugins")
DEFAULT_LOG = Path("/data/plugin/community-store/state/keepalive.log")
DEFAULT_CRON_TAG = "#@community.keepalive"

# 备份时永远跳过的子树（用户数据 / 缓存 / 日志），通配按 basename 匹配
BACKUP_EXCLUDE_NAMES = {
    "data",
    "__pycache__",
    ".git",
    "node_modules",
}
BACKUP_MAX_BYTES = 512 * 1024 * 1024  # 单个插件超过 512 MB 就不打包，只记日志

# 非交互环境（cron）里 iptables/ss 等常在 /sbin、/usr/sbin
SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


@dataclass(frozen=True)
class Plugin:
    """一个社区插件在盘上的三套标识。"""

    directory: str          # /data/plugin/<directory>/
    key: str                # 注册表 / server.list 里的 key
    unit: str               # systemd 单元名
    port: int               # 本地 HTTP 端口
    title: str              # 中文名（INFO 缺失时兜底显示）

    @property
    def dir_path(self) -> Path:
        return plugin_root() / self.directory


PLUGINS: tuple[Plugin, ...] = (
    Plugin("community-store", "communitystore", "xiaomi-community-store", 18119, "插件市场"),
    Plugin("xiaomi-device-manager", "devicemanager", "xiaomi-device-manager", 18080, "设备管家"),
    Plugin("xiaomi-nas-console", "nasconsole", "xiaomi-nas-console", 18100, "控制台"),
    Plugin("router-center", "rtrcenter", "xiaomi-router-center", 18101, "路由器中心"),
    Plugin("ssh-control", "sshcontrol", "xiaomi-ssh-control", 18130, "SSH 开关"),
    Plugin("transmission", "transmission", "xiaomi-transmission", 18140, "Transmission"),
    Plugin("disk-sleep", "disksleep", "xiaomi-disk-sleep", 18160, "硬盘休眠"),
    Plugin("jellyfin", "jellyfin", "xiaomi-jellyfin", 18170, "Jellyfin"),
    Plugin("fwclient", "fwclient", "xiaomi-fwclient", 18180, "内网穿透"),
    Plugin("netneighbor", "netneighbor", "xiaomi-netneighbor", 18190, "网络邻居"),
)

PLUGIN_BY_KEY = {item.key: item for item in PLUGINS}
OUR_KEYS = frozenset(PLUGIN_BY_KEY)


# --------------------------------------------------------------------------- 路径

def host_root() -> Path:
    """测试用：把绝对路径挂到一个临时根下。"""
    override = os.environ.get("XCM_HOST_ROOT", "").strip()
    return Path(override) if override else Path("/")


def plugin_root() -> Path:
    return host_root() / "data/plugin"


def _override(env_name: str, default: Path) -> Path:
    value = os.environ.get(env_name, "").strip()
    return Path(value) if value else default


def server_list_path() -> Path:
    return _override("XCM_SERVER_LIST", host_root() / DEFAULT_SERVER_LIST.relative_to("/"))


def backup_root() -> Path:
    return _override("XCM_BACKUP_ROOT", host_root() / DEFAULT_BACKUP_ROOT.relative_to("/"))


def log_path() -> Path:
    return _override("XCM_KEEPALIVE_LOG", host_root() / DEFAULT_LOG.relative_to("/"))


def registry_path(user_id: str) -> Path:
    """注册表文件：`/data/plugin/u<uid>.list`。

    设备上的文件名带 `u` 前缀（`u3943892.list`），而 CLI 传进来的可能是纯数字
    （`3943892`）。两种都接受：优先 `u<id>.list`，不存在再退回 `<id>.list`。
    """
    root = plugin_root()
    candidates = []
    if user_id:
        candidates.append(root / f"{user_id}.list")
        if not user_id.startswith("u"):
            candidates.insert(0, root / f"u{user_id}.list")
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[0] if candidates else root / "unknown.list"


# --------------------------------------------------------------------------- 工具

def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _write_json_atomic(path: Path, value: dict[str, Any], mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".keepalive.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, mode)
    temporary.replace(path)


def _append_log(message: str) -> None:
    """一行一条，超过 256 KB 时截断重开（避免无限增长）。"""
    path = log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file() and path.stat().st_size > 256 * 1024:
            tail = path.read_text(encoding="utf-8", errors="replace").splitlines()[-200:]
            path.write_text("\n".join(tail) + "\n", encoding="utf-8")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:
        pass  # 日志失败绝不影响自愈本身


def read_plugin_info(item: Plugin) -> dict[str, Any]:
    """读插件自己的 INFO（current/ 优先，找不到就扫 releases/）。"""
    candidates: list[Path] = [item.dir_path / "current" / "INFO", item.dir_path / "INFO"]
    releases = item.dir_path / "releases"
    if releases.is_dir():
        try:
            candidates.extend(sorted(releases.glob("*/INFO"), reverse=True))
        except OSError:
            pass
    for candidate in candidates:
        info = _read_json(candidate) if candidate.is_file() else None
        if info:
            return info
    return {}


def _registry_record(user_id: str, item: Plugin) -> dict[str, Any] | None:
    registry = _read_json(registry_path(user_id)) if user_id else None
    if not registry:
        return None
    record = registry.get(item.key)
    return record if isinstance(record, dict) else None


def build_server_entry(user_id: str, item: Plugin) -> dict[str, Any]:
    """构造与厂商 server.list 同构的条目：{"info": {...}, "resource": {...}}。

    优先复用注册表里已有的记录（端口、版本、图标都对得上）；
    注册表里没有时从插件 INFO 生成。
    """
    record = _registry_record(user_id, item)
    info: dict[str, Any] = {}

    if record and isinstance(record.get("info"), dict):
        info.update(record["info"])

        frontend = record.get("frontend")
        if isinstance(frontend, dict):
            info.setdefault("desc", frontend.get("desc", ""))

    local_info = read_plugin_info(item)
    info.setdefault("plugin", item.key)
    info.setdefault("name", local_info.get("name") or item.title)
    info.setdefault("version", local_info.get("version", "0.0.0"))
    info.setdefault("tags", local_info.get("tags") or ["tool"])
    info.setdefault("desc", local_info.get("desc", ""))
    info.setdefault("developer", local_info.get("developer", "Kingwell Community"))
    info.setdefault("publisher", local_info.get("publisher", "community"))
    info.setdefault("system", False)
    info.setdefault("port", str(item.port))
    info.setdefault("type", "standard")
    info.setdefault("ext", {"admin": True})

    if record and isinstance(record.get("info"), dict) and record["info"].get("id") is not None:
        info.setdefault("id", record["info"]["id"])

    resource = {
        "mpk": "",
        "icon": f"/icon/{item.key}.icon",
        "source": "community",
    }
    if record and isinstance(record.get("resource"), dict):
        resource.update(record["resource"])

    return {"info": info, "resource": resource}


# --------------------------------------------------------------------------- ① server.list 注入

def load_server_list() -> dict[str, Any]:
    value = _read_json(server_list_path())
    if value is None:
        value = {}
    value.setdefault("code", 0)
    value.setdefault("message", "")
    if not isinstance(value.get("data"), dict):
        value["data"] = {}
    return value


def inject_server_entries(
    document: dict[str, Any],
    entries: dict[str, dict[str, Any]],
) -> list[str]:
    """把缺失的条目补进 `.data`；**只增不改**，返回新增的 key 列表。"""
    data = document.setdefault("data", {})
    if not isinstance(data, dict):
        document["data"] = data = {}
    added: list[str] = []
    for key, entry in entries.items():
        if key in data:
            continue  # 已在（可能是厂商下发的）→ 一律不动
        data[key] = entry
        added.append(key)
    return added


def repair_placeholder_versions(
    document: dict[str, Any],
    entries: dict[str, dict[str, Any]],
) -> list[str]:
    """把「我们自己插件」里版本为空或 `0.0.0` 的条目修成真实版本。

    场景：早期注入时注册表路径拼错（`3943892.list` vs `u3943892.list`），
    版本回落成占位值 `0.0.0`。这里只动**我们 PLUGINS 表里的 key**，且只在
    现有版本是占位值时才改；厂商条目与其它任何条目一律不碰。
    """
    data = document.get("data")
    if not isinstance(data, dict):
        return []
    repaired: list[str] = []
    for key, entry in entries.items():
        existing = data.get(key)
        if not isinstance(existing, dict):
            continue
        info = existing.get("info")
        if not isinstance(info, dict):
            continue
        current = str(info.get("version") or "").strip()
        if current and current != "0.0.0":
            continue
        wanted = str((entry.get("info") or {}).get("version") or "").strip()
        if not wanted or wanted == "0.0.0":
            continue
        info["version"] = wanted
        repaired.append(key)
    return repaired


def vendor_baseline_path() -> Path:
    """厂商条目基线（放在 /data 下，抗 OTA）。

    `plugincenter` 对账会**同时**清空注册表与 server.list，而影视 / 中枢功能 /
    监控存储 / 百度网盘 / 迅雷 这几条**没法凭空重建** —— 缺了它们，App 的应用市场
    就会"少几个"。所以留一份原样快照，缺失时只增补回。
    """
    override = os.environ.get("XCM_VENDOR_BASELINE")
    if override:
        return Path(override)
    return host_root() / "data/backup/plugin-registry/vendor-entries.json"


def vendor_baseline() -> dict[str, Any]:
    data = _read_json(vendor_baseline_path())
    if not isinstance(data, dict):
        return {}
    return {
        key: value for key, value in data.items()
        if isinstance(value, dict) and isinstance(value.get("info"), dict)
    }


def restore_vendor_entries(document: dict[str, Any]) -> list[str]:
    """把基线里的厂商条目**只增**地补回 server.list。

    规则（与我们自己的条目一致）：
      * 已存在的厂商条目**一律不动**（哪怕字段看起来"旧"）；
      * **不删除**任何条目；
      * 只补"基线里有、文件里没有"的。
    """
    data = document.get("data")
    if not isinstance(data, dict):
        data = {}
        document["data"] = data
    added: list[str] = []
    for key, record in vendor_baseline().items():
        if key in data:
            continue
        entry: dict[str, Any] = {"info": record["info"]}
        if isinstance(record.get("resource"), dict):
            entry["resource"] = record["resource"]
        data[key] = entry
        added.append(key)
    return added


def heal_server_list(user_id: str) -> dict[str, Any]:
    path = server_list_path()
    document = load_server_list()
    entries = {item.key: build_server_entry(user_id, item) for item in PLUGINS}
    added = inject_server_entries(document, entries)
    repaired = repair_placeholder_versions(document, entries)
    vendor_added = restore_vendor_entries(document)
    if added or repaired or vendor_added:
        _write_json_atomic(path, document)
    return {
        "path": str(path),
        "added": added,
        "repaired": repaired,
        "vendor_added": vendor_added,
        "total_our_keys": len(entries),
        "present_after": sum(1 for key in entries if key in (document.get("data") or {})),
    }


# --------------------------------------------------------------------------- ② 每插件备份

def _tar_filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    name = Path(info.name).name
    if name in BACKUP_EXCLUDE_NAMES:
        return None
    return info


def _dir_size(path: Path, limit: int) -> int:
    total = 0
    for root, dirnames, filenames in os.walk(path):
        dirnames[:] = [d for d in dirnames if d not in BACKUP_EXCLUDE_NAMES]
        for name in filenames:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
            if total > limit:
                return total
    return total


def backup_plugin(item: Plugin) -> dict[str, Any]:
    source = item.dir_path
    if not source.is_dir():
        return {"key": item.key, "ok": False, "reason": "plugin dir missing"}

    size = _dir_size(source, BACKUP_MAX_BYTES)
    if size > BACKUP_MAX_BYTES:
        return {"key": item.key, "ok": False, "reason": f"too large ({size} bytes)"}

    target_dir = backup_root()
    target_dir.mkdir(parents=True, exist_ok=True)
    archive = target_dir / f"{item.directory}.tar.gz"
    temporary = target_dir / f"{item.directory}.tar.gz.tmp"

    try:
        with tarfile.open(temporary, "w:gz") as bundle:
            bundle.add(source, arcname=item.directory, filter=_tar_filter, recursive=True)
        temporary.replace(archive)
    except (OSError, tarfile.TarError) as error:
        temporary.unlink(missing_ok=True)
        return {"key": item.key, "ok": False, "reason": str(error)}

    meta = {
        "key": item.key,
        "directory": item.directory,
        "version": read_plugin_info(item).get("version", ""),
        "bytes": archive.stat().st_size,
        "created": int(time.time()),
    }
    _write_json_atomic(target_dir / f"{item.directory}.json", meta, mode=0o644)
    return {"key": item.key, "ok": True, **meta}


def backup_all() -> list[dict[str, Any]]:
    return [backup_plugin(item) for item in PLUGINS]


def restore_plugin(item: Plugin) -> dict[str, Any]:
    archive = backup_root() / f"{item.directory}.tar.gz"
    if not archive.is_file():
        return {"key": item.key, "ok": False, "reason": "no backup"}

    destination = plugin_root()
    destination.mkdir(parents=True, exist_ok=True)
    staging = destination / f".keepalive-staging-{item.directory}"
    try:
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive, "r:gz") as bundle:
            # 归档是我们自己从 /data/plugin/<dir> 打的，里面含**绝对路径软链**
            # （`current -> /data/plugin/<dir>/releases/<ver>`）。因此不能用
            # filter="data"（会以「link to an absolute path」拒绝），要用
            # fully_trusted；旧 Python 没有 filter 参数时退回默认行为。
            try:
                bundle.extractall(staging, filter="fully_trusted")  # noqa: S202
            except TypeError:
                bundle.extractall(staging)  # noqa: S202
        restored = staging / item.directory
        if not restored.is_dir():
            return {"key": item.key, "ok": False, "reason": "archive layout unexpected"}
        if item.dir_path.exists():
            shutil.rmtree(item.dir_path, ignore_errors=True)
        restored.replace(item.dir_path)
        return {"key": item.key, "ok": True}
    except (OSError, tarfile.TarError) as error:
        return {"key": item.key, "ok": False, "reason": str(error)}
    finally:
        shutil.rmtree(staging, ignore_errors=True)


# --------------------------------------------------------------------------- ③ 服务自愈

def _run(command: list[str], timeout: int = 30) -> tuple[int, str]:
    environment = dict(os.environ)
    environment["PATH"] = environment.get("PATH") or SAFE_PATH
    if "/sbin" not in environment["PATH"]:
        environment["PATH"] = environment["PATH"] + ":" + SAFE_PATH
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout,
            env=environment, check=False,
        )
        return completed.returncode, (completed.stdout or completed.stderr or "").strip()
    except (OSError, subprocess.SubprocessError) as error:
        return 127, str(error)


def service_active(unit: str) -> bool:
    code, _ = _run(["systemctl", "is-active", "--quiet", unit], timeout=15)
    return code == 0


def ensure_service(item: Plugin, start: bool = True) -> dict[str, Any]:
    if service_active(item.unit):
        return {"key": item.key, "unit": item.unit, "state": "active"}
    if not start:
        return {"key": item.key, "unit": item.unit, "state": "inactive"}
    code, output = _run(["systemctl", "start", item.unit], timeout=60)
    return {
        "key": item.key,
        "unit": item.unit,
        "state": "started" if code == 0 else "start-failed",
        "detail": output[-200:] if code != 0 else "",
    }


def ensure_current(item: Plugin) -> dict[str, Any]:
    """保证 `<插件>/current` 是指向某个 releases/<版本> 的**可用**软链。

    真机踩到的坑：`plugincenter` 对账有时**只删 `current`、不删整个目录**
    （实测 `ssh-control` 的 `current` 在 20:05 还在、20:32 就没了，而目录完好）。
    这种「目录在但 current 没了」的状态下：
      * 服务的 `ExecStart` 指向 `current/...` → 重启就起不来；
      * `data/plugin/<key>/current/keepalive.sh` 之类的 cron 会变成每分钟空转。
    只看「目录是否存在」是发现不了的，所以单独补这一层。
    """
    releases = item.dir_path / "releases"
    if not releases.is_dir():
        return {"key": item.key, "state": "no-releases"}

    current = item.dir_path / "current"
    # exists() 会跟随软链：软链坏了返回 False，所以还要看 is_symlink()
    if current.exists():
        return {"key": item.key, "state": "ok"}

    try:
        candidates = [path for path in releases.iterdir() if path.is_dir()]
    except OSError as error:
        return {"key": item.key, "state": "read-failed", "detail": str(error)}
    if not candidates:
        return {"key": item.key, "state": "no-release-dirs"}

    newest = max(candidates, key=lambda path: path.stat().st_mtime)
    try:
        if current.is_symlink():
            current.unlink()
        current.symlink_to(newest)
    except OSError as error:
        return {"key": item.key, "state": "relink-failed", "detail": str(error)}
    return {"key": item.key, "state": "relinked", "target": str(newest)}


def is_our_port_listening(item: Plugin) -> bool:
    """用 ss 粗查端口（无 ss 时退回 /proc/net/tcp）。"""
    code, output = _run(["ss", "-ltn"], timeout=15)
    if code == 0:
        return f":{item.port} " in output or f":{item.port}\t" in output
    try:
        text = Path("/proc/net/tcp").read_text(encoding="ascii", errors="replace")
    except OSError:
        return False
    needle = f":{item.port:04X}"
    return any(line.split()[1].endswith(needle) for line in text.splitlines()[1:] if len(line.split()) > 1)


# --------------------------------------------------------------------------- 自愈主流程

@dataclass
class HealReport:
    timestamp: int
    server_list_added: list[str]
    restored: list[str]
    started: list[str]
    relinked: list[str]
    problems: list[str]
    vendor_restored: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.server_list_added or self.restored or self.started
                    or self.relinked or self.vendor_restored or self.problems)

    def as_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "server_list_added": self.server_list_added,
            "restored": self.restored,
            "started": self.started,
            "relinked": self.relinked,
            "vendor_restored": self.vendor_restored,
            "problems": self.problems,
        }

    def summary(self) -> str:
        parts = []
        if self.server_list_added:
            parts.append("server.list+" + ",".join(self.server_list_added))
        if self.vendor_restored:
            parts.append("vendor+" + ",".join(self.vendor_restored))
        if self.restored:
            parts.append("restored=" + ",".join(self.restored))
        if self.relinked:
            parts.append("relinked=" + ",".join(self.relinked))
        if self.started:
            parts.append("started=" + ",".join(self.started))
        if self.problems:
            parts.append("problems=" + ";".join(self.problems))
        return " ".join(parts) or "ok"


def heal_once(user_id: str, *, do_backup: bool = False, do_restore: bool = True) -> HealReport:
    added: list[str] = []
    restored: list[str] = []
    started: list[str] = []
    relinked: list[str] = []
    problems: list[str] = []
    vendor_restored: list[str] = []

    # ① server.list 条目注入（含厂商条目的只增补回）
    try:
        result = heal_server_list(user_id)
        added = list(result.get("added") or [])
        vendor_restored = list(result.get("vendor_added") or [])
    except Exception as error:  # noqa: BLE001 自愈不能因为一步失败就整体崩
        problems.append(f"server.list:{error}")

    for item in PLUGINS:
        # ② 目录缺失 → 从备份还原
        if do_restore and not item.dir_path.is_dir():
            outcome = restore_plugin(item)
            if outcome.get("ok"):
                restored.append(item.key)
                # 目录是刚还原的：此时旧进程可能还活着（删目录不会杀进程），
                # 端口在听但文件已丢（实测会返回 500）。强制重启让它加载新文件。
                _run(["systemctl", "restart", item.unit], timeout=60)
                continue
            problems.append(f"{item.key}:{outcome.get('reason', 'restore failed')}")

        if not item.dir_path.is_dir():
            continue

        # ③ current 软链：对账可能只删它、留目录（实测 ssh-control）
        current_state = ensure_current(item)
        if current_state.get("state") == "relinked":
            relinked.append(item.key)
            # 服务启动时就绑在 current/... 上，重链后必须重启
            _run(["systemctl", "restart", item.unit], timeout=60)
            continue
        if current_state.get("state") not in ("ok",):
            problems.append(f"{item.key}:current-{current_state.get('state')}")

        # ④ 服务/端口
        state = ensure_service(item)
        if state.get("state") in ("started", "start-failed"):
            if state["state"] == "started":
                started.append(item.key)
            else:
                problems.append(f"{item.key}:start-failed:{state.get('detail', '')}")
        elif state.get("state") == "active" and not is_our_port_listening(item):
            problems.append(f"{item.key}:port {item.port} not listening")

    if do_backup:
        for outcome in backup_all():
            if not outcome.get("ok"):
                problems.append(f"backup:{outcome.get('key')}:{outcome.get('reason')}")

    report = HealReport(int(time.time()), added, restored, started, relinked, problems,
                        vendor_restored)
    _append_log(report.summary())
    return report


# --------------------------------------------------------------------------- 后台线程

class HealLoop(threading.Thread):
    """在市场服务进程内跑的自愈线程（daemon，不挡住退出）。"""

    def __init__(self, user_id: str, interval: int = DEFAULT_INTERVAL,
                 stop_event: threading.Event | None = None):
        super().__init__(name="community-keepalive", daemon=True)
        self.user_id = user_id
        self.interval = max(30, interval)
        self._stop = stop_event or threading.Event()

    def run(self) -> None:  # pragma: no cover - 线程体，靠集成测试覆盖
        while not self._stop.is_set():
            try:
                heal_once(self.user_id)
            except Exception as error:  # noqa: BLE001
                _append_log(f"heal-loop-error: {error}")
            self._stop.wait(self.interval)

    def stop(self) -> None:
        self._stop.set()


def start_background(user_id: str, interval: int = DEFAULT_INTERVAL) -> HealLoop:
    loop = HealLoop(user_id, interval)
    loop.start()
    return loop


# --------------------------------------------------------------------------- cron

CRON_LINE_TEMPLATE = (
    "* * * * * cd /data/plugin/community-store/current && "
    "/usr/bin/python3 keepalive.py --once --user-id {user} >/dev/null 2>&1 {tag}"
)


def install_cron(user_id: str, tag: str = DEFAULT_CRON_TAG) -> dict[str, Any]:
    """把自愈任务加进 root crontab（用 `crontab -`，与开机钩子同一套机制）。"""
    line = CRON_LINE_TEMPLATE.format(user=user_id, tag=tag)
    code, current = _run(["crontab", "-l"], timeout=20)
    if code not in (0, 1):
        return {"ok": False, "reason": f"crontab -l failed: {current[:120]}"}
    if "keepalive.py" in current:
        return {"ok": True, "changed": False, "line": line}
    merged = "\n".join([entry for entry in [current, line] if entry.strip()])
    # 用管道写入
    environment = dict(os.environ)
    environment["PATH"] = environment.get("PATH") or SAFE_PATH
    try:
        completed = subprocess.run(
            ["crontab", "-"], input=merged + "\n", capture_output=True,
            text=True, timeout=20, env=environment, check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return {"ok": False, "reason": str(error)}
    if completed.returncode != 0:
        return {"ok": False, "reason": (completed.stderr or "").strip()[:120]}
    return {"ok": True, "changed": True, "line": line}


def remove_cron() -> dict[str, Any]:
    code, current = _run(["crontab", "-l"], timeout=20)
    if code != 0:
        return {"ok": True, "changed": False}
    kept = [line for line in current.splitlines() if "keepalive.py" not in line]
    environment = dict(os.environ)
    environment["PATH"] = environment.get("PATH") or SAFE_PATH
    try:
        subprocess.run(
            ["crontab", "-"], input="\n".join(kept) + "\n", capture_output=True,
            text=True, timeout=20, env=environment, check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return {"ok": False, "reason": str(error)}
    return {"ok": True, "changed": True}


# --------------------------------------------------------------------------- CLI

def status() -> dict[str, Any]:
    rows = []
    for item in PLUGINS:
        current = item.dir_path / "current"
        rows.append({
            "key": item.key,
            "directory": item.directory,
            "port": item.port,
            "dir": item.dir_path.is_dir(),
            "current": current.exists(),
            "active": service_active(item.unit),
            "listening": is_our_port_listening(item),
            "backup": (backup_root() / f"{item.directory}.tar.gz").is_file(),
        })
    server = load_server_list()
    present = sum(1 for key in OUR_KEYS if key in (server.get("data") or {}))
    return {
        "plugins": rows,
        "server_list": str(server_list_path()),
        "server_list_present": present,
        "server_list_expected": len(OUR_KEYS),
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="社区插件自愈（server.list 注入 / 备份 / 拉起）")
    parser.add_argument("--user-id", default=os.environ.get("NAS_USER_ID", ""))
    parser.add_argument("--once", action="store_true", help="只跑一轮自愈")
    parser.add_argument("--backup", action="store_true", help="打包全部插件的备份")
    parser.add_argument("--restore", metavar="KEY", help="从备份还原某个插件的目录")
    parser.add_argument("--status", action="store_true", help="打印状态表")
    parser.add_argument("--install-cron", action="store_true", help="安装 root cron 双保险")
    parser.add_argument("--remove-cron", action="store_true", help="移除 cron 条目")
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL, help="后台循环间隔（秒）")
    parser.add_argument("--loop", action="store_true", help="前台跑自愈循环（调试用）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if args.status:
        payload = status()
    elif args.backup:
        payload = {"backups": backup_all()}
    elif args.restore:
        item = PLUGIN_BY_KEY.get(args.restore)
        if not item:
            print(f"未知插件：{args.restore}", file=sys.stderr)
            return 2
        payload = restore_plugin(item)
    elif args.install_cron:
        payload = install_cron(args.user_id)
    elif args.remove_cron:
        payload = remove_cron()
    elif args.loop:
        while True:
            report = heal_once(args.user_id)
            print(report.summary(), flush=True)
            time.sleep(max(30, args.interval))
        return 0
    elif args.once:
        if not args.user_id:
            print("--once 需要 --user-id 或 NAS_USER_ID", file=sys.stderr)
            return 2
        payload = heal_once(args.user_id, do_backup=args.backup).as_dict()
    else:
        parser.print_help()
        return 0

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
