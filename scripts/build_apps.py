#!/usr/bin/env python3
"""Package plugin projects into apps/ and generate apps.json.

Usage:
  python3 scripts/build_apps.py
  python3 scripts/build_apps.py --signing-key ~/.local/share/xiaomi-community-store/signing-key.pem

Output (committed to git):
  apps/<id>-<version>.zip
  apps/icons/<id>.png
  apps.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PROJECTS = ROOT / "projects"
APPS = ROOT / "apps"
APPS_JSON = ROOT / "apps.json"

# ---------------------------------------------------------------------------
# Package specs — one entry per publishable plugin.
# Keys match the existing build_repository.py so bundles stay compatible.
# ---------------------------------------------------------------------------
PACKAGE_SPECS: list[dict[str, Any]] = [
    {
        "id": "devicemanager",
        "name": "设备管家",
        "version": "0.4.1",
        "summary": "2 秒实时监控系统、网络、磁盘健康与 Docker 容器资源",
        "description": "只读查看 CPU、内存、温度、网络、磁盘 SMART 与 Docker 容器资源占用，页面可见时每 2 秒刷新。",
        "project": "xiaomi-device-manager-prototype",
        "pluginId": 11001,
        "port": 18080,
        "releaseRoot": "/data/plugin/xiaomi-device-manager",
        "uiKey": "devicemanager",
        "iconSource": "assets/xiaomi-device-manager-v2.png",
        "iconName": "xiaomi-device-manager-v2.icon",
        "runtime": {
            "scripts/nas_status_server.py": "server.py",
            "dist/client": "public",
        },
        "ui": "dist/client",
        "serviceSource": "deploy/xiaomi-device-manager.service",
        "service": "xiaomi-device-manager.service",
        "nginxSource": "deploy/xiaomi-device-manager.nginx.conf",
        "nginx": "xiaomi-device-manager.conf",
        "healthPath": "/healthz",
        "tags": ["tool", "monitoring"],
        "author": "Kingwell",
        "registry": {
            "icon": "/icon/xiaomi-device-manager-v2.icon?v=2",
            "frontend": {
                "title": "设备管家",
                "desc": "设备状态管家",
                "type": "url",
                "permission": ["admin"],
                "dev_type": [1, 2, 3, 4],
                "url": [
                    {"dev_type": [1], "url": "/index.html#/deviceManager_app"},
                    {"dev_type": [2, 3, 4], "url": "/index.html#/deviceManager_pc"},
                ],
                "sortid": 11001,
                "widget": [],
            },
            "info": {
                "tags": ["tool", "monitoring"],
                "desc": "监控 NAS 系统、网络、硬盘健康与 Docker 容器资源",
                "developer": "Kingwell Community",
                "publisher": "community",
                "ext": {"admin": True},
            },
        },
    },
    {
        "id": "115sync",
        "name": "115 云备份",
        "version": "0.1.0",
        "summary": "使用 115 官方 OpenAPI 同步与备份 NAS 文件",
        "description": "需要自己获批的 115 App ID，扫码授权后同步与备份 NAS 文件。",
        "project": "xiaomi-115-sync-plugin",
        "pluginId": 1000,
        "port": 18115,
        "releaseRoot": "/data/plugin/115-sync",
        "uiKey": "115sync",
        "iconSource": "web/assets/115-sync-icon.png",
        "iconName": "115-sync.icon",
        "runtime": {"server.py": "server.py", "requirements.txt": "requirements.txt"},
        "requirements": "runtime/requirements.txt",
        "ui": "web",
        "serviceSource": "deploy/xiaomi-115-sync.service",
        "service": "xiaomi-115-sync.service",
        "nginxSource": "deploy/xiaomi-115-sync.nginx.conf",
        "nginx": "xiaomi-115-sync.conf",
        "healthPath": "/api/status",
        "tags": ["cloud", "backup"],
        "author": "community",
        "registry": {
            "icon": "/icon/115-sync.icon?v=115life-38.2.0",
            "frontend": {
                "title": "115 云备份",
                "type": "url",
                "permission": ["admin"],
                "dev_type": [1, 2, 3, 4],
                "url": [
                    {"dev_type": [1], "url": "/index.html#/115Sync_app"},
                    {"dev_type": [2, 3, 4], "url": "/index.html#/115Sync_pc"},
                ],
                "sortid": 1000,
                "widget": [],
            },
            "info": {"tags": ["cloud", "backup"], "publisher": "community", "ext": {"admin": True}},
        },
    },
    {
        "id": "aliyundrivesync",
        "name": "阿里云盘备份",
        "version": "0.1.0",
        "summary": "扫码连接阿里云盘并同步 NAS 文件",
        "description": "需要自己获批的阿里云盘应用，扫码授权后同步 NAS 文件。",
        "project": "xiaomi-aliyundrive-sync-plugin",
        "pluginId": 1002,
        "port": 18117,
        "releaseRoot": "/data/plugin/aliyundrive-sync",
        "uiKey": "aliyundrivesync",
        "iconSource": "web/assets/aliyundrive-icon.png",
        "iconName": "aliyundrive-sync.icon",
        "runtime": {"server.py": "server.py"},
        "ui": "web",
        "serviceSource": "deploy/xiaomi-aliyundrive-sync.service",
        "service": "xiaomi-aliyundrive-sync.service",
        "nginxSource": "deploy/xiaomi-aliyundrive-sync.nginx.conf",
        "nginx": "xiaomi-aliyundrive-sync.conf",
        "healthPath": "/api/status",
        "tags": ["cloud", "backup"],
        "author": "community",
        "registry": {
            "icon": "/icon/aliyundrive-sync.icon?v=0.1.0",
            "frontend": {
                "title": "阿里云盘备份",
                "type": "url",
                "permission": ["admin"],
                "dev_type": [1, 2, 3, 4],
                "url": [
                    {"dev_type": [1], "url": "/index.html#/aliyunDriveSync_app"},
                    {"dev_type": [2, 3, 4], "url": "/index.html#/aliyunDriveSync_pc"},
                ],
                "sortid": 1002,
                "widget": [],
            },
            "info": {"tags": ["cloud", "backup"], "publisher": "community", "ext": {"admin": True}},
        },
    },
    {
        "id": "webdav",
        "name": "WebDAV 文件桥",
        "version": "0.2.0-rc5",
        "summary": "NAS HTTPS 文件共享，以及远程 WebDAV 上传、下载与定时备份",
        "description": "多账号、多目录授权、远程单向备份、目录新建与原位改名。测试版。",
        "project": "xiaomi-webdav-plugin",
        "pluginId": 11003,
        "port": 18120,
        "releaseRoot": "/data/plugin/webdav",
        "uiKey": "webdav",
        "iconSource": "web/assets/webdav.png",
        "iconName": "webdav-file-bridge.icon",
        "runtime": {
            "server.py": "server.py",
            "engine.py": "engine.py",
            "access.py": "access.py",
            "multi_dav.py": "multi_dav.py",
            "compatibility.py": "compatibility.py",
            "vendor": "vendor",
            "vendor-integrity.json": "vendor-integrity.json",
            "scripts/check_after_upgrade.py": "scripts/check_after_upgrade.py",
            "requirements-dav.txt": "requirements-dav.txt",
            "bin": "bin",
            "web": "web",
            "licenses": "licenses",
            "README.md": "README.md",
        },
        "ui": "web",
        "serviceSource": "deploy/xiaomi-webdav.service",
        "service": "xiaomi-webdav.service",
        "nginxSource": "deploy/xiaomi-webdav.nginx.conf",
        "nginx": "xiaomi-webdav.conf",
        "healthPath": "/healthz",
        "tags": ["files", "backup"],
        "author": "community",
        "registry": {
            "icon": "/icon/webdav-file-bridge.icon?v=0.1.0",
            "frontend": {
                "title": "WebDAV 文件桥",
                "desc": "文件共享与备份",
                "type": "url",
                "permission": ["admin"],
                "dev_type": [1, 2, 3, 4],
                "url": [
                    {"dev_type": [1], "url": "/index.html#/webDavBridge_app"},
                    {"dev_type": [2, 3, 4], "url": "/index.html#/webDavBridge_pc"},
                ],
                "sortid": 11003,
                "widget": [],
            },
            "info": {"tags": ["files", "backup"], "publisher": "community", "ext": {"admin": True}},
        },
    },
    {
        "id": "qbittorrent",
        "name": "qB 下载",
        "version": "0.1.2",
        "summary": "磁力与种子下载、暂停继续和限速；首次启动需拉取独立 Docker 镜像",
        "description": "磁力/种子下载、进度、暂停继续、限速。需要 Docker。",
        "project": "xiaomi-qbittorrent-plugin",
        "pluginId": 11004,
        "port": 18122,
        "releaseRoot": "/data/plugin/qbittorrent",
        "uiKey": "qbittorrent",
        "iconSource": "web/assets/qb.png",
        "iconName": "qbittorrent.icon",
        "runtime": {
            "server.py": "server.py",
            "engine.py": "engine.py",
            "web": "web",
            "licenses": "licenses",
            "README.md": "README.md",
        },
        "ui": "web",
        "serviceSource": "deploy/xiaomi-qbittorrent.service",
        "service": "xiaomi-qbittorrent.service",
        "nginxSource": "deploy/xiaomi-qbittorrent.nginx.conf",
        "nginx": "xiaomi-qbittorrent.conf",
        "healthPath": "/healthz",
        "tags": ["download"],
        "author": "community",
        "registry": {
            "icon": "/icon/qbittorrent.icon?v=0.1.2",
            "frontend": {
                "title": "qB 下载",
                "desc": "下载任务管理",
                "type": "url",
                "permission": ["admin"],
                "dev_type": [1, 2, 3, 4],
                "url": [
                    {"dev_type": [1], "url": "/index.html#/qbDownloads_app"},
                    {"dev_type": [2, 3, 4], "url": "/index.html#/qbDownloads_pc"},
                ],
                "sortid": 11004,
                "widget": [],
            },
            "info": {"tags": ["download"], "publisher": "community", "ext": {"admin": True}},
        },
    },
    {
        "id": "sshcontrol",
        "name": "SSH 开关",
        "version": "0.1.0",
        "summary": "启停 SSH 远程登录，并设置开机自动保持运行",
        "description": "在客户端内启停 SSH，支持开机自启。用于绕过 boot_check.sh 每次开机强制关闭 SSH 的行为。",
        "project": "xiaomi-ssh-control-plugin",
        "pluginId": 11005,
        "port": 18130,
        "releaseRoot": "/data/plugin/ssh-control",
        "uiKey": "sshcontrol",
        "iconSource": "web/assets/ssh-control-icon.png",
        "iconName": "ssh-control.icon",
        "runtime": {
            "server.py": "server.py",
            "keepalive.sh": "keepalive.sh",
            "web": "web",
            "README.md": "README.md",
        },
        "ui": "web",
        "serviceSource": "deploy/xiaomi-ssh-control.service",
        "service": "xiaomi-ssh-control.service",
        "nginxSource": "deploy/xiaomi-ssh-control.nginx.conf",
        "nginx": "xiaomi-ssh-control.conf",
        "healthPath": "/api/status",
        "tags": ["tool", "system"],
        "author": "community",
        "registry": {
            "icon": "/icon/ssh-control.icon?v=1",
            "frontend": {
                "title": "SSH 开关",
                "desc": "远程登录控制",
                "type": "url",
                "permission": ["admin"],
                "dev_type": [1, 2, 3, 4],
                "url": [
                    {"dev_type": [1], "url": "/index.html#/sshControl_app"},
                    {"dev_type": [2, 3, 4], "url": "/index.html#/sshControl_pc"},
                ],
                "sortid": 11005,
                "widget": [],
            },
            "info": {"tags": ["tool", "system"], "publisher": "community", "ext": {"admin": True}},
        },
    },
    {
        "id": "transmission",
        "name": "Transmission 下载",
        "version": "0.1.0",
        "summary": "BT 下载，可设置下载目录、并发数、连接数与上传下载限速",
        "description": "随包携带 aarch64 的 transmission-daemon（从 Entware 提取），不需要 Docker 或 Entware；内置 transmission-web-control 界面。",
        "project": "xiaomi-transmission-plugin",
        "pluginId": 11006,
        "port": 18140,
        "releaseRoot": "/data/plugin/transmission",
        "uiKey": "transmission",
        "iconSource": "web/assets/transmission.png",
        "iconName": "transmission.icon",
        # bin/ lib/ licenses/ runtime-manifest.json 由 scripts/fetch_runtime.py 从
        # Entware 提取；web/twc/ 由 scripts/fetch_web_control.py 取回；
        # web/assets/transmission.png 是 Transmission 官方图标（MIT），随仓库存放。
        # 三者都是打包输入，缺失时 build_bundle 会直接报 “Missing package source”。
        "runtime": {
            "server.py": "server.py",
            "bin": "bin",
            "lib": "lib",
            "runtime-manifest.json": "runtime-manifest.json",
            "licenses": "licenses",
            "scripts": "scripts",
            "web": "web",
            "README.md": "README.md",
        },
        "ui": "web",
        "serviceSource": "deploy/xiaomi-transmission.service",
        "service": "xiaomi-transmission.service",
        "nginxSource": "deploy/xiaomi-transmission.nginx.conf",
        "nginx": "xiaomi-transmission.conf",
        "healthPath": "/healthz",
        "tags": ["download"],
        "author": "community",
        "registry": {
            "icon": "/icon/transmission.icon?v=1",
            "frontend": {
                "title": "Transmission 下载",
                "desc": "下载任务管理",
                "type": "url",
                "permission": ["admin"],
                "dev_type": [1, 2, 3, 4],
                "url": [
                    {"dev_type": [1], "url": "/index.html#/transmissionDownload_app"},
                    {"dev_type": [2, 3, 4], "url": "/index.html#/transmissionDownload_pc"},
                ],
                "sortid": 11006,
                "widget": [],
            },
            "info": {"tags": ["download"], "publisher": "community", "ext": {"admin": True}},
        },
    },
    {
        "id": "emby",
        "name": "Emby 媒体服务器",
        "version": "0.1.0",
        "summary": "在 NAS 上运行 Emby，向局域网设备串流个人影音",
        "description": "官方 Emby 镜像的受限容器；媒体目录读写挂载以便写回元数据，配置保存在插件私有目录，Emby 默认端口 8096 对局域网开放。",
        "project": "xiaomi-emby-plugin",
        "pluginId": 11007,
        "port": 18150,
        "releaseRoot": "/data/plugin/emby",
        "uiKey": "emby",
        "iconSource": "web/assets/emby.png",
        "iconName": "emby.icon",
        "runtime": {
            "server.py": "server.py",
            "engine.py": "engine.py",
            "web": "web",
            "README.md": "README.md",
        },
        "ui": "web",
        "serviceSource": "deploy/xiaomi-emby.service",
        "service": "xiaomi-emby.service",
        "nginxSource": "deploy/xiaomi-emby.nginx.conf",
        "nginx": "xiaomi-emby.conf",
        "healthPath": "/healthz",
        "tags": ["media"],
        "author": "community",
        "registry": {
            "icon": "/icon/emby.icon?v=0.1.0",
            "frontend": {
                "title": "Emby 媒体服务器",
                "desc": "影音串流",
                "type": "url",
                "permission": ["admin"],
                "dev_type": [1, 2, 3, 4],
                "url": [
                    {"dev_type": [1], "url": "/index.html#/embyMedia_app"},
                    {"dev_type": [2, 3, 4], "url": "/index.html#/embyMedia_pc"},
                ],
                "sortid": 11007,
                "widget": [],
            },
            "info": {"tags": ["media"], "publisher": "community", "ext": {"admin": True}},
        },
    },
]


def _attach_plugin_elements() -> None:
    """把各插件自带的小米插件规范要素纳入打包内容。

    每个插件可在自己的 deploy/ 下提供：
      plugin-meta.json  —— INFO 的元数据（名称、描述、标签等）
      control           —— plugincenter boot 时执行的 <plugin>/scripts/control
    安装时会复制到插件目录，使 plugin.sh verify 通过。
    """
    elements = (
        ("deploy/plugin-meta.json", "plugin-meta.json"),
        ("deploy/control", "control"),
    )
    for spec in PACKAGE_SPECS:
        project = PROJECTS / spec["project"]
        for source, destination in elements:
            if (project / source).is_file():
                spec["runtime"].setdefault(source, destination)


_attach_plugin_elements()


def copy_required(source: Path, target: Path) -> None:
    if not source.exists():
        raise SystemExit(f"Missing package source: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, target, dirs_exist_ok=True)
    else:
        shutil.copy2(source, target)


def write_deterministic_zip(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(item for item in source.rglob("*") if item.is_file()):
            relative = path.relative_to(source).as_posix()
            info = zipfile.ZipInfo(relative, date_time=(2026, 9, 2, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o100644 & 0xFFFF) << 16
            archive.writestr(info, path.read_bytes())


def build_bundle(spec: dict[str, Any]) -> dict[str, Any]:
    project = PROJECTS / spec["project"]
    if not project.is_dir():
        raise SystemExit(f"Project directory not found: {project}")

    with tempfile.TemporaryDirectory(prefix=f"apps-{spec['id']}-") as temporary:
        root = Path(temporary)
        for source, destination in spec["runtime"].items():
            copy_required(project / source, root / "runtime" / destination)
        copy_required(project / spec["ui"], root / "ui")
        copy_required(project / spec["iconSource"], root / "icon")
        copy_required(project / spec["serviceSource"], root / "config" / spec["service"])
        copy_required(project / spec["nginxSource"], root / "config" / spec["nginx"])
        manifest = {
            "schemaVersion": 1,
            "id": spec["id"],
            "name": spec["name"],
            "version": spec["version"],
            "pluginId": spec["pluginId"],
            "port": spec["port"],
            "paths": {
                "releaseRoot": spec["releaseRoot"],
                "uiKey": spec["uiKey"],
                "iconName": spec["iconName"],
            },
            "service": spec["service"],
            "nginx": spec["nginx"],
            "healthPath": spec["healthPath"],
            "registry": spec["registry"],
        }
        if spec.get("requirements"):
            manifest["requirements"] = spec["requirements"]
        # 显式 LF：这个文件进 zip，Windows 上写成 CRLF 会让同一个包算出不同的
        # sha256，CI 就会把「内容没变」误判成「内容有变」而凭空抬一个版本。
        with (root / "manifest.json").open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        bundle_name = f"{spec['id']}-{spec['version']}.zip"
        bundle_path = APPS / bundle_name
        write_deterministic_zip(root, bundle_path)

    icon_name = f"{spec['id']}.png"
    copy_required(project / spec["iconSource"], APPS / "icons" / icon_name)
    digest = hashlib.sha256(bundle_path.read_bytes()).hexdigest()
    size = bundle_path.stat().st_size
    channel = "candidate" if "-rc" in spec["version"] or "-beta" in spec["version"] else "stable"
    return {
        "id": spec["id"],
        "name": spec["name"],
        "version": spec["version"],
        "summary": spec["summary"],
        "description": spec.get("description", spec["summary"]),
        "bundle": f"apps/{bundle_name}",
        "sha256": digest,
        "size": size,
        "icon": f"apps/icons/{icon_name}",
        "channel": channel,
        "tags": spec.get("tags", []),
        "author": spec.get("author", "community"),
        "pluginId": spec["pluginId"],
        "port": spec["port"],
    }


def detect_repo_slug() -> str:
    """Best-effort owner/name from git remote; falls back to a placeholder."""
    try:
        url = subprocess.check_output(
            ["git", "remote", "get-url", "origin"],
            cwd=ROOT,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "fw867/xiaomi-nas-plugin-market"
    for prefix in ("git@github.com:", "https://github.com/"):
        if url.startswith(prefix):
            slug = url[len(prefix):].removesuffix(".git").strip("/")
            if slug.count("/") == 1:
                return slug
    return "fw867/xiaomi-nas-plugin-market"


def read_store_version() -> str:
    release_script = PROJECTS / "xiaomi-community-app-store" / "scripts" / "build_release.py"
    if not release_script.is_file():
        return "0.2.0"
    for line in release_script.read_text(encoding="utf-8").splitlines():
        if line.startswith("VERSION"):
            return line.split("=", 1)[1].strip().strip("\"'")
    return "0.2.0"


def load_existing_versions() -> dict[str, str]:
    """从现有 apps.json 读取各插件的当前版本号。"""
    if not APPS_JSON.is_file():
        return {}
    try:
        data = json.loads(APPS_JSON.read_text(encoding="utf-8"))
        return {a["id"]: a["version"] for a in data.get("apps", []) if "id" in a and "version" in a}
    except (json.JSONDecodeError, OSError):
        return {}


def load_existing_hashes() -> dict[str, str]:
    """从现有 apps.json 读取各插件已发布包的 SHA-256，用于判断内容是否变化。"""
    if not APPS_JSON.is_file():
        return {}
    try:
        data = json.loads(APPS_JSON.read_text(encoding="utf-8"))
        return {
            a["id"]: a["sha256"]
            for a in data.get("apps", [])
            if "id" in a and isinstance(a.get("sha256"), str)
        }
    except (json.JSONDecodeError, OSError):
        return {}


def load_existing_apps() -> list[dict[str, Any]]:
    """读取现有 apps.json 中的应用条目（用于 --only 时保留其它条目）。"""
    if not APPS_JSON.is_file():
        return []
    try:
        data = json.loads(APPS_JSON.read_text(encoding="utf-8"))
        apps = data.get("apps", [])
        return [a for a in apps if isinstance(a, dict) and "id" in a]
    except (json.JSONDecodeError, OSError):
        return []


def bump_patch(version: str) -> str:
    """递增 patch 版本号：0.4.1 → 0.4.2；0.2.0-rc5 → 0.2.1-rc5。"""
    import re as _re
    m = _re.match(r"^(\d+)\.(\d+)\.(\d+)(.*)$", version)
    if not m:
        return version
    major, minor, patch, suffix = m.groups()
    return f"{major}.{minor}.{int(patch) + 1}{suffix}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Build apps/ bundles and apps.json")
    parser.add_argument("--only", help="Build a single app id")
    parser.add_argument("--include-candidates", action="store_true", help="Include rc/beta packages")
    parser.add_argument("--skip-missing", action="store_true", help="Skip apps with missing sources instead of failing")
    parser.add_argument("--no-bump", action="store_true", help="Do not auto-increment patch version")
    args = parser.parse_args()

    # 版本号策略：默认沿用 apps.json 里已发布的版本，只有打包内容真的
    # 变了才递增 patch。否则每次 CI 构建都会把全部插件抬一个版本，
    # 客户端会对所有已安装插件显示「可更新」。
    existing_versions = load_existing_versions()
    existing_hashes = load_existing_hashes()
    if existing_versions and not args.no_bump:
        for spec in PACKAGE_SPECS:
            old = existing_versions.get(spec["id"])
            if old:
                spec["version"] = old

    specs = PACKAGE_SPECS
    if args.only:
        specs = [s for s in specs if s["id"] == args.only]
        if not specs:
            raise SystemExit(f"Unknown app id: {args.only}")
    if not args.include_candidates:
        skipped = [s["id"] for s in specs if "-rc" in s["version"] or "-beta" in s["version"]]
        if skipped:
            print(f"Skipping candidate packages (use --include-candidates): {', '.join(skipped)}")
        specs = [s for s in specs if "-rc" not in s["version"] and "-beta" not in s["version"]]

    APPS.mkdir(parents=True, exist_ok=True)
    (APPS / "icons").mkdir(parents=True, exist_ok=True)

    apps: list[dict[str, Any]] = []
    skipped_ids: list[str] = []
    for spec in specs:
        published = existing_versions.get(spec["id"])
        print(f"Building {spec['id']} {spec['version']} ...")
        try:
            entry = build_bundle(spec)
        except SystemExit as error:
            if args.skip_missing:
                print(f"  ⚠ skipped: {error}")
                skipped_ids.append(spec["id"])
                continue
            raise

        # 内容与已发布包一致 → 保持版本号，客户端不会提示更新。
        # 只有内容真的变了才递增 patch 并重新打包。
        if published and not args.no_bump:
            if existing_hashes.get(spec["id"]) == entry["sha256"]:
                print("  内容未变，保持版本")
            else:
                bumped = bump_patch(published)
                spec["version"] = bumped
                print(f"  内容有变，版本递增 {published} → {bumped}")
                entry = build_bundle(spec)

        apps.append(entry)
        print(f"  → {entry['bundle']}  sha256={entry['sha256'][:16]}…  {entry['size']//1024} KiB")

    if not apps:
        raise SystemExit("No apps were built")

    # 清理被重建应用的旧版本 zip（按最终版本号判断，保留其它应用的包）
    keep_names = {Path(entry["bundle"]).name for entry in apps}
    built_ids = {entry["id"] for entry in apps}
    for old_zip in APPS.glob("*.zip"):
        if old_zip.name in keep_names:
            continue
        if any(old_zip.name.startswith(f"{app_id}-") for app_id in built_ids):
            old_zip.unlink()
            print(f"清理旧包: {old_zip.name}")

    # 使用 --only 时保留未重建应用的既有条目，避免清单被清空。
    # 顺序按 PACKAGE_SPECS 而不是 id 排序：全量构建就是这个顺序，两者不一致
    # 会让清单在每次构建之间来回重排。
    if args.only:
        rebuilt = {entry["id"]: entry for entry in apps}
        published = {app["id"]: app for app in load_existing_apps()}
        apps = [rebuilt.get(spec["id"]) or published.get(spec["id"]) for spec in PACKAGE_SPECS]
        apps = [entry for entry in apps if entry]

    repo = detect_repo_slug()
    branch = "main"
    apps_doc = {
        "schemaVersion": 2,
        "name": "小米智能存储应用商店",
        "generatedAt": int(time.time()),
        "repository": repo,
        "branch": branch,
        "store": {
            "version": read_store_version(),
            "releasesUrl": f"https://github.com/{repo}/releases",
            "latestReleaseApi": f"https://api.github.com/repos/{repo}/releases/latest",
        },
        "apps": apps,
    }
    # 显式写 LF：.gitattributes 用 `* -text` 关掉了行尾归一化，Windows 上默认
    # 写出的 CRLF 会让整份清单在 diff 里全量变化，也和 CI/Linux 的产物不一致。
    with APPS_JSON.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(apps_doc, ensure_ascii=False, indent=2) + "\n")
    print(f"\nWrote {APPS_JSON.relative_to(ROOT)} with {len(apps)} apps")
    if skipped_ids:
        print(f"Skipped: {', '.join(skipped_ids)}")
    print(f"Repository: {repo}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
