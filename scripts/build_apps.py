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
        "version": "0.1.0-rc1",
        "summary": "磁力与种子下载、暂停继续和限速；首次启动需拉取独立 Docker 镜像",
        "description": "磁力/种子下载、进度、暂停继续、限速。开发候选版，需要 Docker。",
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
            "icon": "/icon/qbittorrent.icon?v=0.1.0-rc1",
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
]


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
        (root / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
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

    # 自动递增版本号（基于现有 apps.json）
    existing_versions = load_existing_versions()
    if existing_versions and not args.no_bump:
        for spec in PACKAGE_SPECS:
            old = existing_versions.get(spec["id"])
            if old:
                spec["version"] = bump_patch(old)
                if spec["version"] != old:
                    print(f"版本递增: {spec['id']}  {old} → {spec['version']}")

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

    # 清理旧版本 zip（只保留本次构建的）
    keep_names: set[str] = set()
    for spec in specs:
        keep_names.add(f"{spec['id']}-{spec['version']}.zip")
    for old_zip in APPS.glob("*.zip"):
        if old_zip.name not in keep_names:
            old_zip.unlink()
            print(f"清理旧包: {old_zip.name}")

    apps: list[dict[str, Any]] = []
    skipped_ids: list[str] = []
    for spec in specs:
        print(f"Building {spec['id']} {spec['version']} ...")
        try:
            entry = build_bundle(spec)
        except SystemExit as error:
            if args.skip_missing:
                print(f"  ⚠ skipped: {error}")
                skipped_ids.append(spec["id"])
                continue
            raise
        apps.append(entry)
        print(f"  → {entry['bundle']}  sha256={entry['sha256'][:16]}…  {entry['size']//1024} KiB")

    if not apps:
        raise SystemExit("No apps were built")

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
    APPS_JSON.write_text(json.dumps(apps_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\nWrote {APPS_JSON.relative_to(ROOT)} with {len(apps)} apps")
    if skipped_ids:
        print(f"Skipped: {', '.join(skipped_ids)}")
    print(f"Repository: {repo}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
