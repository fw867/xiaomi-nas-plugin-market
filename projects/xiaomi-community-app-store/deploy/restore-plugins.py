#!/usr/bin/env python3
"""恢复被 plugincenter 强制卸载的社区插件与商店自身。

背景：plugin.boot 会执行 `plugincenter boot --system`，它对本机每个
「已安装」插件运行 /usr/bin/plugin.sh verify（小米签名校验）。第三方插件
没有小米签名，校验必然失败，于是被判定
「can't use forever, force uninstall」：
  - 删除 /home/<用户>/plugin/<key>/
  - 从 /data/plugin/<用户>.list 移除条目
结果就是服务仍在运行（由开机钩子拉起），但客户端应用列表里插件全部消失。

本脚本在 plugincenter 之后写回注册表条目并复制回 UI 文件，幂等。
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import time
import zipfile
from pathlib import Path
from typing import Any


STORE_ROOT = Path(os.environ.get("STORE_ROOT", "/data/plugin/community-store"))
PLUGIN_DIR = Path(os.environ.get("PLUGIN_DIR", "/data/plugin"))
STATE_DIR = STORE_ROOT / "state"
STORE_REGISTRATION = STATE_DIR / "store.json"
SKIP_STATE_FILES = {"apps-cache.json", "apps.json", "store.json"}

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import native_layout
except ImportError:  # pragma: no cover - 部署缺失时降级
    native_layout = None


def fix_layout(home_dir: Path, meta: dict, *, user: str, service: str) -> bool:
    """补齐小米插件规范的目录与 INFO（含重算 abstract）。"""
    if native_layout is None or not home_dir.is_dir():
        return False
    try:
        native_layout.ensure_layout(home_dir, meta, user=user, service=service)
        return True
    except (SystemExit, OSError) as error:
        log(f"  ! 补齐 {home_dir.name} 结构失败: {error}")
        return False


def log(message: str) -> None:
    if "--quiet" not in sys.argv:
        print(message, flush=True)


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def ui_from_bundle(plugin_id: str, version: str) -> Path | None:
    """从商店的下载缓存里提取插件的 UI。

    兼容没有 <release>/ui 的旧安装：安装包 ZIP 一直缓存在
    state/downloads/ 下，可以从中还原 ui/ 目录。
    提取结果缓存到 state/ui-cache/<id>/，避免每次都重新解压。
    """
    cache = STATE_DIR / "ui-cache" / plugin_id
    if (cache / "index.html").is_file():
        return cache

    downloads = STATE_DIR / "downloads"
    if not downloads.is_dir():
        return None

    for candidate in sorted(downloads.iterdir()):
        if not candidate.is_file() or candidate.name.endswith(".ok"):
            continue
        try:
            with zipfile.ZipFile(candidate) as archive:
                names = archive.namelist()
                if "manifest.json" not in names:
                    continue
                manifest = json.loads(archive.read("manifest.json"))
                if str(manifest.get("id")) != plugin_id:
                    continue
                if version and str(manifest.get("version")) != version:
                    continue
                if "ui/index.html" not in names:
                    continue
                if cache.exists():
                    shutil.rmtree(cache, ignore_errors=True)
                cache.mkdir(parents=True, exist_ok=True)
                for member in names:
                    if not member.startswith("ui/") or member.endswith("/"):
                        continue
                    destination = cache / member[len("ui/"):]
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(archive.read(member))
                return cache
        except (zipfile.BadZipFile, KeyError, json.JSONDecodeError, OSError):
            continue
    return None


def resolve_user() -> str:
    user = os.environ.get("NAS_USER_ID", "").strip()
    if user:
        return user
    return str(read_json(STORE_REGISTRATION).get("user", "")).strip()


VENDOR_BASELINE = os.environ.get(
    "VENDOR_BASELINE", "/data/backup/plugin-registry/vendor-entries.json"
)


def vendor_baseline() -> dict[str, Any]:
    """厂商条目基线（安装后从设备自身快照留存）。

    `plugincenter` 对账会清空整个注册表，而这些厂商条目（影视 / 中枢功能 / 监控存储 /
    百度网盘 / 迅雷）**我们无法凭空重建** —— 缺了它们，App 里的应用就会"少几个"。
    所以留一份原样快照在 /data，重建时只增补缺的。
    """
    data = read_json(Path(VENDOR_BASELINE))
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict) and isinstance(v.get("info"), dict)}


def _vendor_record_for(key: str, record: dict[str, Any]) -> dict[str, Any]:
    """按磁盘实际状态校正 `install/status/enable`。

    `plugincenter` 开机会对"注册表说已装、但 `/data/plugin/<key>` 无文件"的插件
    执行 `verify failed → force uninstall`。若我们坚持写 `install=true`，
    每次开机都会拉锯一次。所以：目录在 ⇒ 照抄；目录不在 ⇒ `install=false / free`。
    其余字段（system/type/port/id/version/frontend/resource…）仍逐字照抄。
    """
    if (PLUGIN_DIR / key).is_dir():
        return record
    adjusted = copy.deepcopy(record)
    adjusted["install"] = False
    adjusted["status"] = "free"
    adjusted["enable"] = False
    return adjusted


def sync_vendor_reality(registry: dict[str, Any]) -> list[str]:
    """把**已存在**的厂商条目按磁盘实际状态校正 `install/status/enable`。

    这是"不覆盖已有厂商条目"规则的**唯一例外**，且只动这三个字段、只在
    "注册表说已装但目录不存在"时动：
    `plugincenter` 开机会对这类条目做 `verify failed → force uninstall`，
    我们要么先校正、要么每次开机都被它改一遍（拉锯 + 日志刷屏）。
    """
    touched: list[str] = []
    for key in vendor_baseline():
        record = registry.get(key)
        if not isinstance(record, dict) or (PLUGIN_DIR / key).is_dir():
            continue
        if record.get("install") is False and record.get("status") == "free":
            continue
        record["install"] = False
        record["status"] = "free"
        record["enable"] = False
        touched.append(key)
    return touched


def merge_vendor_entries(registry: dict[str, Any]) -> list[str]:
    """把基线里**缺失**的厂商条目补回注册表，返回补了哪些 key。

    铁律：
      * 已存在的厂商条目**一字不改**（哪怕版本看起来旧）；
      * **不删除**任何非我方条目；
      * 只增"基线里有、注册表里没有"的；
      * 幂等：再跑一次不会重复补；
      * `install/status/enable` 按磁盘实际状态校正。
    """
    added: list[str] = []
    for key, record in vendor_baseline().items():
        if key in registry:
            continue
        if not isinstance(record.get("info"), dict):
            continue
        registry[key] = _vendor_record_for(key, record)
        added.append(key)
    return added


def build_record(template: dict[str, Any], plugin_id: int, key: str, name: str,
                 version: str, port: str, icon: str) -> dict[str, Any]:
    """从 bundle 的 registry 模板生成注册表记录（与安装时保持一致）。"""
    record = copy.deepcopy(template) if template else {}
    record.update({"status": "running", "install": True, "enable": True})
    if icon and not record.get("icon"):
        record["icon"] = icon
    info = record.setdefault("info", {})
    info.update({
        "plugin": key,
        "name": name,
        "id": plugin_id,
        "version": version,
        "port": str(port),
        "system": False,
        "type": "standard",
    })
    now = int(time.time())
    record["timestamp"] = now
    record["changetime"] = now
    return record


def restore_ui(source: Path, target: Path) -> bool:
    """把 UI 复制到 /home 下的插件入口；已存在且完整则跳过。"""
    if (target / "index.html").is_file():
        return False
    if not (source / "index.html").is_file():
        log(f"  ! UI 源缺失，跳过: {source}")
        return False
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)
    return True


def main() -> int:
    user = resolve_user()
    if not user:
        log("无法确定 NAS_USER_ID，跳过恢复")
        return 1

    registry_path = PLUGIN_DIR / f"{user}.list"
    registry = read_json(registry_path)
    home_plugin = Path(f"/home/{user}/plugin")

    changes: list[str] = []

    # ---- 商店自身 ----
    registration = read_json(STORE_REGISTRATION)
    if registration:
        key = str(registration.get("uiKey", "communitystore"))
        if restore_ui(STORE_ROOT / "current" / "web", home_plugin / key / "src" / "ui"):
            changes.append(f"{key}:ui")
        if not registry.get(key, {}).get("frontend"):
            record = registration.get("record")
            if isinstance(record, dict) and record:
                registry[key] = build_record(
                    record,
                    int(registration.get("pluginId", 11002)),
                    key,
                    str(record.get("info", {}).get("name", "插件市场")),
                    str(record.get("info", {}).get("version", "")),
                    str(record.get("info", {}).get("port", "18119")),
                    "/icon/community-store-v4.icon",
                )
                changes.append(f"{key}:registry")
                # 注册表被抹说明 plugincenter 强卸过：补齐原生结构，
                # 让下次开机的 verify 能通过（abstract 会按当前文件重算）
                if fix_layout(
                    home_plugin / key,
                    {
                        "plugin": key,
                        "name": str(record.get("info", {}).get("name", "插件市场")),
                        "id": int(registration.get("pluginId", 11002)),
                        "version": str(record.get("info", {}).get("version", "")),
                        "desc": str(record.get("info", {}).get("desc", "")),
                        "tags": list(record.get("info", {}).get("tags", ["store"])),
                        "developer": "community",
                        "publisher": "community",
                        "system": False,
                        "type": "standard",
                        "size": 0,
                        "ext": {},
                        "forceupgrade": False,
                    },
                    user=user,
                    service="xiaomi-community-store.service",
                ):
                    changes.append(f"{key}:layout")

    # ---- 各托管插件 ----
    for state_file in sorted(STATE_DIR.glob("*.json")):
        if state_file.name in SKIP_STATE_FILES:
            continue
        state = read_json(state_file)
        manifest = state.get("manifest")
        release = state.get("release")
        if not isinstance(manifest, dict) or not release:
            continue
        key = str(manifest.get("id", ""))
        if not key:
            continue
        paths = manifest.get("paths", {}) if isinstance(manifest.get("paths"), dict) else {}
        ui_key = str(paths.get("uiKey", key))

        ui_source = Path(release) / "ui"
        if not (ui_source / "index.html").is_file():
            fallback = ui_from_bundle(key, str(manifest.get("version", "")))
            if fallback is not None:
                ui_source = fallback

        if restore_ui(ui_source, home_plugin / ui_key / "src" / "ui"):
            changes.append(f"{key}:ui")

        if not registry.get(key, {}).get("frontend"):
            template = manifest.get("registry")
            registry[key] = build_record(
                template if isinstance(template, dict) else {},
                int(manifest.get("pluginId", 0)) or 11000,
                key,
                str(manifest.get("name", key)),
                str(manifest.get("version", "")),
                str(manifest.get("port", "")),
                str(paths.get("iconName", "")),
            )
            changes.append(f"{key}:registry")
            # 同上：补齐原生结构，使下次开机的 plugin.sh verify 通过
            if fix_layout(
                home_plugin / ui_key,
                {
                    "plugin": key,
                    "name": str(manifest.get("name", key)),
                    "id": int(manifest.get("pluginId", 0)) or 11000,
                    "version": str(manifest.get("version", "")),
                    "desc": str(manifest.get("summary", "")),
                    "tags": ["tool"],
                    "developer": "community",
                    "publisher": "community",
                    "system": False,
                    "type": "standard",
                    "size": 0,
                    "ext": {},
                    "forceupgrade": False,
                },
                user=user,
                service=str(manifest.get("service", "")),
            ):
                changes.append(f"{key}:layout")

    # ★ 厂商条目兜底：`plugincenter` 对账会把整个注册表清空，而我们没有厂商条目的
    #   重建来源，结果就是 App 里"应用少了几个"。这里从 /data 的基线里**只增**补回
    #   缺失的厂商条目 —— **绝不覆盖已存在的、绝不删除任何非我方条目**。
    for vendor_key in merge_vendor_entries(registry):
        changes.append(f"{vendor_key}:vendor")

    # 已存在的厂商条目：只校正 install/status/enable（磁盘没文件就别写"已安装"）
    for vendor_key in sync_vendor_reality(registry):
        changes.append(f"{vendor_key}:vendor-state")

    if not changes:
        return 0

    try:
        if registry_path.exists():
            backup = registry_path.with_name(
                f"{registry_path.name}.before-restore-{int(time.time())}.bak"
            )
            shutil.copy2(registry_path, backup)
        temporary = registry_path.with_suffix(registry_path.suffix + ".restore.tmp")
        temporary.write_text(json.dumps(registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o644)
        temporary.replace(registry_path)
        os.chmod(registry_path, 0o644)
    except OSError as error:
        log(f"写入注册表失败: {error}")
        return 1

    log(f"已恢复 {user}: {' '.join(changes)}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:  # noqa: BLE001
        print(f"restore-plugins 失败: {error}", file=sys.stderr)
        raise SystemExit(1)
