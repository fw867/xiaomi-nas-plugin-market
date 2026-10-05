#!/usr/bin/env python3
"""Verified, declarative installer for Xiaomi NAS community plugins."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any


ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{2,39}$")
VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")
SERVICE_PATTERN = re.compile(r"^[a-z0-9-]+\.service$")
NGINX_PATTERN = re.compile(r"^[a-z0-9-]+\.conf$")
MAX_BUNDLE_BYTES = 64 * 1024 * 1024
MAX_EXPANDED_BYTES = 192 * 1024 * 1024
MAX_FILES = 5000
DOWNLOAD_TIMEOUT = 30
DOWNLOAD_CHUNK = 1024 * 1024
ALLOWED_DOWNLOAD_HOSTS = frozenset(
    {
        "github.com",
        "api.github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
        "raw.githubusercontent.com",
    }
)
_URL_SCHEME = re.compile(r"^https?://", re.I)

# 本商店自身的插件 key：既不允许回滚，也不参与旧版本清理（它有自己的 self-update 目录规则）
STORE_PLUGIN_ID = "communitystore"
# install() 生成的版本目录名：<版本>-<unix 时间戳>-<pid>
RELEASE_DIR_PATTERN = re.compile(r"^(?P<version>.+)-(?P<stamp>\d{9,})-(?P<pid>\d+)$")
# 每个插件默认保留「当前版本 + 上一版本」
DEFAULT_KEEP_RELEASES = 2

# Default GitHub source for apps.json and app bundles.
# Override via environment: XIAOMI_STORE_REPO=owner/name  XIAOMI_STORE_BRANCH=main
DEFAULT_REPO = os.environ.get("XIAOMI_STORE_REPO", "fw867/xiaomi-nas-plugin-market")
DEFAULT_BRANCH = os.environ.get("XIAOMI_STORE_BRANCH", "main")
RAW_BASE = f"https://raw.githubusercontent.com/{DEFAULT_REPO}/{DEFAULT_BRANCH}"
GITHUB_API_LATEST = f"https://api.github.com/repos/{DEFAULT_REPO}/releases/latest"


def _is_remote_reference(value: str) -> bool:
    return bool(_URL_SCHEME.match(value or ""))


def _raw_url(relative: str) -> str:
    return f"{RAW_BASE}/{relative.lstrip('/')}"


class StoreError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_detached_signature(payload: Path, signature: Path, public_key: Path) -> None:
    result = subprocess.run(
        [
            "openssl",
            "dgst",
            "-sha256",
            "-verify",
            str(public_key),
            "-signature",
            str(signature),
            str(payload),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "signature mismatch").strip()
        raise StoreError(f"Signature verification failed: {detail}")


def _safe_member_path(member_name: str) -> PurePosixPath:
    path = PurePosixPath(member_name)
    if path.is_absolute() or not path.parts or any(part in ("", ".", "..") for part in path.parts):
        raise StoreError(f"Unsafe bundle path: {member_name!r}")
    return path


def _validate_download_url(url: str, *, field: str, package_id: str) -> urllib.parse.SplitResult:
    try:
        parsed = urllib.parse.urlsplit(url)
    except ValueError as error:
        raise StoreError(f"Invalid {field} URL for {package_id}: {error}") from error
    if parsed.scheme not in {"http", "https"}:
        raise StoreError(f"Only http/https {field} URLs are allowed for {package_id}")
    hostname = (parsed.hostname or "").lower()
    if hostname not in ALLOWED_DOWNLOAD_HOSTS:
        raise StoreError(f"Disallowed {field} host for {package_id}: {hostname or '(empty)'}")
    if parsed.username or parsed.password:
        raise StoreError(f"Credentials in {field} URL are not allowed for {package_id}")
    if not parsed.path or parsed.path.endswith("/"):
        raise StoreError(f"Invalid {field} path for {package_id}")
    return parsed


def _assert_public_ip(hostname: str) -> None:
    """Reject loopback / link-local / private targets (SSRF / DNS rebinding)."""
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as error:
        raise StoreError(f"Cannot resolve download host {hostname}: {error}") from error
    for info in infos:
        address = info[4][0]
        try:
            ip = ipaddress.ip_address(address)
        except ValueError as error:
            raise StoreError(f"Unexpected address for {hostname}: {address}") from error
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise StoreError(f"Download host {hostname} resolves to a non-public address: {address}")


def _download_to_file(url: str, destination: Path, *, expected_sha256: str | None = None) -> None:
    parsed = _validate_download_url(url, field="download", package_id=destination.name)
    _assert_public_ip(parsed.hostname or "")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".part")
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "xiaomi-community-store/0.1"},
        method="GET",
    )
    opener = urllib.request.build_opener(_NoRedirectToPrivateHandler())
    digest = hashlib.sha256()
    total = 0
    try:
        with opener.open(request, timeout=DOWNLOAD_TIMEOUT) as response:
            status = getattr(response, "status", 200)
            if status != 200:
                raise StoreError(f"Download failed with HTTP {status}: {url}")
            with temporary.open("wb") as handle:
                while True:
                    chunk = response.read(DOWNLOAD_CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_BUNDLE_BYTES:
                        raise StoreError("Downloaded artifact exceeds the 64 MiB limit")
                    digest.update(chunk)
                    handle.write(chunk)
        if total == 0:
            raise StoreError("Downloaded artifact is empty")
        actual = digest.hexdigest()
        if expected_sha256 and actual != expected_sha256:
            raise StoreError("Downloaded artifact checksum mismatch")
        temporary.replace(destination)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as error:
        temporary.unlink(missing_ok=True)
        if isinstance(error, StoreError):
            raise
        raise StoreError(f"Download failed: {error}") from error
    except StoreError:
        temporary.unlink(missing_ok=True)
        raise


class _NoRedirectToPrivateHandler(urllib.request.HTTPRedirectHandler):
    """Allow redirects only to whitelisted public hosts."""

    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        parsed = _validate_download_url(newurl, field="redirect", package_id=req.full_url)
        _assert_public_ip(parsed.hostname or "")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def safe_extract_bundle(bundle: Path, destination: Path) -> None:
    if bundle.stat().st_size > MAX_BUNDLE_BYTES:
        raise StoreError("Bundle exceeds the 64 MiB compressed size limit")
    expanded = 0
    with zipfile.ZipFile(bundle) as archive:
        members = archive.infolist()
        if len(members) > MAX_FILES:
            raise StoreError("Bundle contains too many files")
        for member in members:
            _safe_member_path(member.filename)
            expanded += member.file_size
            if expanded > MAX_EXPANDED_BYTES:
                raise StoreError("Bundle exceeds the 192 MiB expanded size limit")
            mode = member.external_attr >> 16
            if stat.S_ISLNK(mode) or stat.S_ISCHR(mode) or stat.S_ISBLK(mode) or stat.S_ISFIFO(mode):
                raise StoreError(f"Unsupported special file in bundle: {member.filename}")
        archive.extractall(destination)


def validate_manifest(manifest: Any) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        raise StoreError("manifest.json must be an object")
    required = {
        "schemaVersion",
        "id",
        "name",
        "version",
        "pluginId",
        "port",
        "paths",
        "service",
        "nginx",
        "healthPath",
        "registry",
    }
    unknown = set(manifest) - (required | {"requirements"})
    missing = required - set(manifest)
    if missing or unknown:
        raise StoreError(f"Invalid manifest fields; missing={sorted(missing)}, unknown={sorted(unknown)}")
    if manifest["schemaVersion"] != 1:
        raise StoreError("Unsupported manifest schema")
    if not isinstance(manifest["id"], str) or not ID_PATTERN.fullmatch(manifest["id"]):
        raise StoreError("Invalid plugin id")
    if not isinstance(manifest["name"], str) or not 1 <= len(manifest["name"]) <= 40:
        raise StoreError("Invalid plugin name")
    if not isinstance(manifest["version"], str) or not VERSION_PATTERN.fullmatch(manifest["version"]):
        raise StoreError("Invalid plugin version")
    if not isinstance(manifest["pluginId"], int) or manifest["pluginId"] < 1:
        raise StoreError("Invalid numeric plugin ID")
    if not isinstance(manifest["port"], int) or not 1024 <= manifest["port"] <= 65535:
        raise StoreError("Invalid service port")
    if not isinstance(manifest["service"], str) or not SERVICE_PATTERN.fullmatch(manifest["service"]):
        raise StoreError("Invalid systemd service name")
    if not isinstance(manifest["nginx"], str) or not NGINX_PATTERN.fullmatch(manifest["nginx"]):
        raise StoreError("Invalid Nginx config name")
    if not isinstance(manifest["healthPath"], str) or not manifest["healthPath"].startswith("/"):
        raise StoreError("Invalid health path")
    paths = manifest["paths"]
    if not isinstance(paths, dict) or set(paths) != {"releaseRoot", "uiKey", "iconName"}:
        raise StoreError("Invalid paths object")
    if not re.fullmatch(r"/data/plugin/[a-z0-9-]+", str(paths["releaseRoot"])):
        raise StoreError("Invalid release root")
    if not ID_PATTERN.fullmatch(str(paths["uiKey"])):
        raise StoreError("Invalid UI key")
    if not re.fullmatch(r"[A-Za-z0-9._-]+\.icon", str(paths["iconName"])):
        raise StoreError("Invalid icon name")
    if "requirements" in manifest and not re.fullmatch(r"runtime/[A-Za-z0-9._/-]+", str(manifest["requirements"])):
        raise StoreError("Invalid requirements path")
    if not isinstance(manifest["registry"], dict):
        raise StoreError("Invalid registry record")
    return manifest


def load_verified_catalog(catalog_dir: Path, public_key: Path) -> dict[str, Any]:
    catalog_path = catalog_dir / "catalog.json"
    verify_detached_signature(catalog_path, catalog_dir / "catalog.json.sig", public_key)
    try:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StoreError(f"Cannot read catalog: {error}") from error
    if not isinstance(catalog, dict) or catalog.get("schemaVersion") != 1:
        raise StoreError("Unsupported catalog schema")
    packages = catalog.get("packages")
    if not isinstance(packages, list):
        raise StoreError("Catalog packages must be an array")
    seen: set[str] = set()
    for package in packages:
        if not isinstance(package, dict) or not ID_PATTERN.fullmatch(str(package.get("id", ""))):
            raise StoreError("Catalog contains an invalid package")
        if package["id"] in seen:
            raise StoreError(f"Duplicate package id: {package['id']}")
        seen.add(package["id"])
        if not re.fullmatch(r"[0-9a-f]{64}", str(package.get("sha256", ""))):
            raise StoreError(f"Invalid SHA-256 for {package['id']}")
        for field in ("bundle", "signature"):
            value = str(package.get(field, ""))
            if _is_remote_reference(value):
                _validate_download_url(value, field=field, package_id=package["id"])
            else:
                _safe_member_path(value)
        _safe_member_path(str(package.get("icon", "")))
    return catalog


# ---------------------------------------------------------------------------
# apps.json (schemaVersion 2) — GitHub-sourced catalog
# ---------------------------------------------------------------------------

def validate_apps_catalog(document: Any) -> dict[str, Any]:
    if not isinstance(document, dict):
        raise StoreError("apps.json must be an object")
    if document.get("schemaVersion") != 2:
        raise StoreError("Unsupported apps.json schema")
    apps = document.get("apps")
    if not isinstance(apps, list) or not apps:
        raise StoreError("apps.json apps must be a non-empty array")
    seen: set[str] = set()
    for app in apps:
        if not isinstance(app, dict):
            raise StoreError("Each app entry must be an object")
        app_id = str(app.get("id", ""))
        if not ID_PATTERN.fullmatch(app_id):
            raise StoreError(f"Invalid app id: {app_id!r}")
        if app_id in seen:
            raise StoreError(f"Duplicate app id: {app_id}")
        seen.add(app_id)
        if not isinstance(app.get("name"), str) or not 1 <= len(app["name"]) <= 40:
            raise StoreError(f"Invalid app name for {app_id}")
        version = str(app.get("version", ""))
        if not VERSION_PATTERN.fullmatch(version):
            raise StoreError(f"Invalid version for {app_id}")
        if not re.fullmatch(r"[0-9a-f]{64}", str(app.get("sha256", ""))):
            raise StoreError(f"Invalid SHA-256 for {app_id}")
        bundle = str(app.get("bundle", ""))
        if not bundle:
            raise StoreError(f"Missing bundle path for {app_id}")
        if _is_remote_reference(bundle):
            _validate_download_url(bundle, field="bundle", package_id=app_id)
        else:
            _safe_member_path(bundle)
        icon = str(app.get("icon", ""))
        if icon and not _is_remote_reference(icon):
            _safe_member_path(icon)
    return document


def fetch_url_json(url: str, *, timeout: float = 20) -> dict[str, Any]:
    _validate_download_url(url, field="apps-catalog", package_id="apps.json")
    parsed = urllib.parse.urlsplit(url)
    _assert_public_ip(parsed.hostname or "")
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "xiaomi-community-store/0.2", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            if status != 200:
                raise StoreError(f"Fetching apps.json failed with HTTP {status}")
            raw = response.read(4 * 1024 * 1024)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as error:
        raise StoreError(f"Cannot fetch apps.json: {error}") from error
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise StoreError(f"apps.json is not valid JSON: {error}") from error


CATALOG_CACHE_TTL = float(os.environ.get("CATALOG_CACHE_TTL", "180"))


def _github_headers() -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "xiaomi-community-store/0.2",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_branch_sha(repo: str | None = None, branch: str | None = None, *, timeout: float = 15) -> str:
    """解析分支当前指向的 commit SHA。

    raw.githubusercontent.com 会缓存分支路径（<repo>/main/...），
    内容更新后仍可能长时间返回旧版本；按 commit SHA 取则不受影响。
    """
    repo = repo or DEFAULT_REPO
    branch = branch or DEFAULT_BRANCH
    url = f"https://api.github.com/repos/{repo}/git/refs/heads/{branch}"
    host = urllib.parse.urlsplit(url).hostname or ""
    if host != "api.github.com":
        raise StoreError(f"Disallowed host: {host}")
    _assert_public_ip(host)
    request = urllib.request.Request(url, headers=_github_headers())
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read(256 * 1024).decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, json.JSONDecodeError) as error:
        raise StoreError(f"Cannot resolve branch {branch}: {error}") from error
    sha = str(payload.get("object", {}).get("sha", ""))
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise StoreError("GitHub returned an invalid branch SHA")
    return sha


def fetch_apps_json(
    *,
    repo: str | None = None,
    branch: str | None = None,
    timeout: float = 20,
) -> dict[str, Any]:
    """按 commit SHA 精确拉取 apps.json，避开分支路径的 CDN 缓存。"""
    repo = repo or DEFAULT_REPO
    branch = branch or DEFAULT_BRANCH
    sha = fetch_branch_sha(repo, branch, timeout=timeout)
    url = f"https://raw.githubusercontent.com/{repo}/{sha}/apps.json"
    return fetch_url_json(url, timeout=timeout)


def _read_local_catalog(paths: list[Path]) -> dict[str, Any] | None:
    for path in paths:
        try:
            document = validate_apps_catalog(json.loads(path.read_text(encoding="utf-8")))
            document["_source"] = str(path)
            return document
        except (OSError, json.JSONDecodeError, StoreError):
            continue
    return None


def load_apps_catalog(
    *,
    local_apps_json: Path | None = None,
    remote_url: str | None = None,
    cache_path: Path | None = None,
    force_refresh: bool = False,
    ttl: float | None = None,
) -> dict[str, Any]:
    """加载 apps.json：优先远程（带短 TTL 缓存），失败时回退本地副本。

    apps.json 是插件列表的唯一来源，必须反映仓库最新状态，因此远程优先：
    先解析 HEAD SHA 再按 SHA 拉取，避免分支路径被 CDN 缓存住。
    缓存只用于降低请求频率和断网兜底。
    """
    fallback: list[Path] = []
    if local_apps_json and local_apps_json.is_file():
        fallback.append(local_apps_json)

    ttl = CATALOG_CACHE_TTL if ttl is None else ttl

    if cache_path and cache_path.is_file():
        try:
            fresh = (time.time() - cache_path.stat().st_mtime) < ttl
        except OSError:
            fresh = False
        if fresh and not force_refresh:
            cached = _read_local_catalog([cache_path])
            if cached is not None:
                return cached
        fallback.append(cache_path)

    document: dict[str, Any] | None = None
    errors: list[str] = []
    if remote_url:
        try:
            document = validate_apps_catalog(fetch_url_json(remote_url))
            document["_source"] = remote_url
        except StoreError as error:
            errors.append(str(error))
    else:
        try:
            document = validate_apps_catalog(fetch_apps_json())
            document["_source"] = "github-sha"
        except StoreError as error:
            errors.append(str(error))
        if document is None:
            # 退一步用分支路径（可能被 CDN 缓存，但聊胜于无）
            try:
                branch_url = _raw_url("apps.json")
                document = validate_apps_catalog(fetch_url_json(branch_url))
                document["_source"] = branch_url
            except StoreError as error:
                errors.append(str(error))

    if document is None:
        offline = _read_local_catalog(fallback)
        if offline is not None:
            offline["_stale"] = True
            return offline
        raise StoreError("; ".join(errors) or "Cannot load apps.json")

    if cache_path:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps({k: v for k, v in document.items() if not k.startswith("_")}, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass
    return document


def fetch_store_latest_version(api_url: str | None = None) -> dict[str, Any]:
    """Query GitHub Releases for the latest store version."""
    url = api_url or GITHUB_API_LATEST
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise StoreError("store-update URL must be http/https")
    hostname = (parsed.hostname or "").lower()
    if hostname != "api.github.com":
        raise StoreError(f"Disallowed store-update host: {hostname}")
    _assert_public_ip(hostname)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "xiaomi-community-store/0.2", "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read(512 * 1024).decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, json.JSONDecodeError) as error:
        raise StoreError(f"Cannot check store updates: {error}") from error
    tag = str(payload.get("tag_name") or "").lstrip("vV")
    html_url = str(payload.get("html_url") or "")
    return {"version": tag, "url": html_url, "name": payload.get("name") or tag}


def version_tuple(version: str) -> tuple[int, ...]:
    core = re.split(r"[-+]", version, maxsplit=1)[0]
    parts: list[int] = []
    for piece in core.split("."):
        try:
            parts.append(int(piece))
        except ValueError:
            parts.append(0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:3])


def is_newer_version(candidate: str, current: str) -> bool:
    return version_tuple(candidate) > version_tuple(current)


def _copy_path(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() or target.is_file():
        target.unlink()
    elif target.is_dir():
        shutil.rmtree(target)
    if source.is_symlink():
        target.symlink_to(os.readlink(source))
    elif source.is_dir():
        shutil.copytree(source, target)
    else:
        shutil.copy2(source, target)


def _write_json_atomic(path: Path, value: Any, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".community-store.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, mode)
    temporary.replace(path)
    os.chmod(path, mode)


def _log(message: str) -> None:
    """清理类操作的日志：只打印，不抛异常（不能影响安装结果）。"""
    print(f"[community-store] {message}", flush=True)


def release_name_parts(directory_name: str) -> tuple[str, int, int] | None:
    """解析 install() 生成的版本目录名。

    目录名形如 `<版本>-<unix 时间戳>-<pid>`，返回 (版本, 时间戳, pid)；
    不符合该规则的历史遗留目录返回 None（调用方只跳过、不删）。
    """
    match = RELEASE_DIR_PATTERN.fullmatch(directory_name)
    if match is None:
        return None
    version = match.group("version")
    if not VERSION_PATTERN.fullmatch(version):
        return None
    return version, int(match.group("stamp")), int(match.group("pid"))


def release_version(directory_name: str) -> str | None:
    """版本目录名里的版本号；不是本商店创建的目录返回 None。"""
    parts = release_name_parts(directory_name)
    return parts[0] if parts else None


def _release_sort_key(release_dir: Path) -> tuple[int, float, str]:
    """最新优先的排序键：目录名里的时间戳 > mtime > 名字（都是越大越新）。"""
    parts = release_name_parts(release_dir.name)
    stamp = parts[1] if parts else 0
    try:
        modified = release_dir.stat().st_mtime
    except OSError:
        modified = 0.0
    return (stamp, modified, release_dir.name)


def current_release_target(release_root: Path) -> Path | None:
    """读 `<releaseRoot>/current` 软链指向的版本目录；不是软链时返回 None。"""
    current = release_root / "current"
    try:
        if not current.is_symlink():
            return None
        target = Path(os.readlink(current))
    except OSError:
        return None
    if not target.is_absolute():
        target = current.parent / target
    try:
        return target.resolve()
    except OSError:
        return target


def _directory_size(path: Path) -> int:
    """目录占用的字节数；读不到的条目按 0 计。"""
    total = 0
    for root, _directories, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).lstat().st_size
            except OSError:
                continue
    return total


class InstallManager:
    def __init__(
        self,
        catalog_dir: Path,
        public_key: Path | None,
        user_id: str,
        root: Path = Path("/"),
        execute_system: bool = True,
        apps_json: Path | None = None,
        apps_root: Path | None = None,
        remote_apps: bool = True,
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]+", user_id):
            raise StoreError("Invalid Xiaomi user id")
        self.catalog_dir = catalog_dir.resolve() if catalog_dir else None
        self.public_key = public_key.resolve() if public_key else None
        self.user_id = user_id
        self.root = root.resolve()
        self.execute_system = execute_system
        self.apps_json = apps_json.resolve() if apps_json else None
        self.apps_root = apps_root.resolve() if apps_root else None
        self.remote_apps = remote_apps
        self.state_dir = self._host("/data/plugin/community-store/state")
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def _host(self, absolute: str) -> Path:
        if not absolute.startswith("/"):
            raise StoreError(f"Expected absolute target path: {absolute}")
        return self.root / absolute.lstrip("/")

    def _load_catalog(self) -> dict[str, Any]:
        """插件清单：显式配置了本地 apps.json 时优先用它，否则从 GitHub 拉取。

        server.py 部署时不传 apps_json，走远程；只有本地/测试场景会显式传，
        此时本地清单就是调用方指定的来源（读坏了才退回远程）。
        """
        if self.apps_json and self.apps_json.is_file():
            try:
                document = validate_apps_catalog(
                    json.loads(self.apps_json.read_text(encoding="utf-8"))
                )
                document["_source"] = str(self.apps_json)
                return document
            except (OSError, json.JSONDecodeError, StoreError) as error:
                _log(f"本地 apps.json 不可用（{error}），改用远程清单")
        cache = self.state_dir / "apps-cache.json"
        return load_apps_catalog(cache_path=cache)

    def _package_entry(self, package_id: str) -> dict[str, Any]:
        catalog = self._load_catalog()
        packages = catalog.get("apps") or catalog.get("packages") or []
        for package in packages:
            if package.get("id") == package_id:
                return package
        raise StoreError(f"Package not found: {package_id}")

    def _apps_local_file(self, relative: str) -> Path | None:
        """Try apps/ directory next to apps.json, then repo-root apps/."""
        candidates: list[Path] = []
        if self.apps_root:
            candidates.append(self.apps_root / relative)
        if self.apps_json:
            candidates.append(self.apps_json.parent / relative)
        if self.catalog_dir:
            candidates.append(self.catalog_dir.parent / relative)
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
                if resolved.is_file():
                    return resolved
            except OSError:
                continue
        return None

    def _catalog_file(self, relative: str) -> Path:
        # Prefer apps/ layout
        local = self._apps_local_file(relative)
        if local is not None:
            return local
        if self.catalog_dir:
            candidate = (self.catalog_dir / relative).resolve()
            try:
                candidate.relative_to(self.catalog_dir)
            except ValueError as error:
                raise StoreError("Catalog path escaped its repository") from error
            if candidate.is_file():
                return candidate
        raise StoreError(f"Catalog artifact is missing: {relative}")

    def _download_dir(self) -> Path:
        directory = self.state_dir / "downloads"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _resolve_artifact(self, reference: str, *, expected_sha256: str | None = None) -> Path:
        """Local file first; fall back to cached GitHub download."""
        if not _is_remote_reference(reference):
            try:
                return self._catalog_file(reference)
            except StoreError:
                if not self.remote_apps:
                    raise
                # Fall through to remote download via raw.githubusercontent.com
                reference = _raw_url(reference)

        _validate_download_url(reference, field="artifact", package_id=reference)
        cache_key = hashlib.sha256(reference.encode("utf-8")).hexdigest()[:24]
        cached = self._download_dir() / cache_key
        marker = cached.with_suffix(cached.suffix + ".ok")

        if cached.is_file() and marker.is_file():
            try:
                recorded = marker.read_text(encoding="utf-8").strip()
                if (expected_sha256 is None or recorded == expected_sha256) and (
                    expected_sha256 is None or sha256_file(cached) == expected_sha256
                ):
                    return cached
            except OSError:
                pass
            cached.unlink(missing_ok=True)
            marker.unlink(missing_ok=True)

        _download_to_file(reference, cached, expected_sha256=expected_sha256)
        marker.write_text(expected_sha256 or sha256_file(cached), encoding="utf-8")
        return cached

    def _registry_path(self) -> Path:
        return self._host(f"/data/plugin/{self.user_id}.list")

    def _load_registry(self) -> dict[str, Any]:
        path = self._registry_path()
        if not path.exists():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise StoreError(f"Cannot read Xiaomi plugin registry: {error}") from error
        if not isinstance(value, dict):
            raise StoreError("Xiaomi plugin registry must be an object")
        return value

    def _registry_record(self, manifest: dict[str, Any]) -> dict[str, Any]:
        now = int(time.time())
        record = json.loads(json.dumps(manifest["registry"], ensure_ascii=False))
        record.update({"status": "running", "install": True, "enable": True})
        info = record.setdefault("info", {})
        info.update(
            {
                "plugin": manifest["id"],
                "name": manifest["name"],
                "id": manifest["pluginId"],
                "version": manifest["version"],
                "port": str(manifest["port"]),
                "system": False,
                "type": "standard",
            }
        )
        record["timestamp"] = now
        record["changetime"] = now
        return record

    def _check_registry_conflicts(self, registry: dict[str, Any], manifest: dict[str, Any]) -> None:
        for key, record in registry.items():
            if key == manifest["id"] or not isinstance(record, dict):
                continue
            info = record.get("info")
            if isinstance(info, dict) and str(info.get("id")) == str(manifest["pluginId"]):
                raise StoreError(f"Plugin ID {manifest['pluginId']} is already used by {key}")
            if isinstance(info, dict) and str(info.get("port")) == str(manifest["port"]):
                raise StoreError(f"Port {manifest['port']} is already declared by {key}")

    def _run(self, command: list[str]) -> None:
        if not self.execute_system:
            return
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "command failed").strip()
            raise StoreError(f"{command[0]} failed: {detail}")

    def _apply_native_layout(self, plugin_home: Path, manifest: dict[str, Any],
                             release: Path | None = None) -> None:
        """补齐小米插件规范的目录结构，使 plugincenter 的 verify 通过。

        plugin.sh 的 plugin_verify() 是摘要自校验：要求插件目录下有
        etc/var/tmp/scripts/src 和含 abstract 的 INFO，否则返回 1，
        plugincenter 会判定「force uninstall」并删除该插件。
        必须在 UI 文件就位后执行：abstract 覆盖 src/ 下全部文件。
        """
        if not self.execute_system:
            return
        script = Path(__file__).resolve().parent / "deploy" / "native_layout.py"
        if not script.is_file() or not plugin_home.is_dir():
            return
        registry_info = manifest.get("registry", {}).get("info", {})
        command = [
            "python3", str(script),
            "--user", self.user_id,
            "--name", str(manifest["paths"]["uiKey"]),
            "--service", str(manifest["service"]),
            "--title", str(manifest.get("name", "")),
            "--plugin-id", str(manifest.get("pluginId", 0)),
            "--version", str(manifest.get("version", "")),
            "--desc", str(registry_info.get("desc", "")),
        ]
        # 插件自带的要素（bundle 的 runtime 里）
        if release is not None:
            meta = release / "plugin-meta.json"
            control = release / "control"
            if meta.is_file():
                command += ["--meta", str(meta)]
            if control.is_file():
                command += ["--control", str(control)]
        command.append("--quiet")
        subprocess.run(command, capture_output=True, check=False)

    def _install_requirements(self, extracted: Path, manifest: dict[str, Any], release: Path) -> None:
        requirements = manifest.get("requirements")
        if not requirements:
            return
        requirements_path = extracted / requirements
        if not requirements_path.is_file():
            raise StoreError("Declared requirements file is missing")
        if not self.execute_system:
            return
        pip_check = subprocess.run(
            ["/usr/bin/python3", "-m", "pip", "--version"],
            capture_output=True,
            text=True,
            check=False,
        )
        if pip_check.returncode != 0:
            self._run(["/usr/bin/python3", "-m", "ensurepip", "--upgrade"])
        indexes = [
            "https://pypi.org/simple",
            "https://pypi.tuna.tsinghua.edu.cn/simple",
            "https://mirrors.aliyun.com/pypi/simple",
        ]
        fastest = indexes[0]
        best = float("inf")
        for index in indexes:
            started = time.monotonic()
            try:
                with urllib.request.urlopen(index, timeout=4) as response:
                    if response.status < 400 and time.monotonic() - started < best:
                        best = time.monotonic() - started
                        fastest = index
            except Exception:
                continue
        self._run(
            [
                "/usr/bin/python3",
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-warn-script-location",
                "--index-url",
                fastest,
                "--target",
                str(release / "lib"),
                "-r",
                str(requirements_path),
            ]
        )

    def _wait_for_health(self, url: str, timeout: float = 20) -> None:
        deadline = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=2) as response:
                    if response.status < 400:
                        return
                    last_error = StoreError(f"Health check returned HTTP {response.status}")
            except Exception as error:
                last_error = error
            time.sleep(0.4)
        raise StoreError(f"Health check failed after {timeout:g}s: {last_error}")

    def _activate_release(
        self,
        manifest: dict[str, Any],
        release: Path,
        *,
        ui_target: Path,
        release_root: Path,
        registry_path: Path,
        registry: dict[str, Any] | None = None,
    ) -> None:
        """切换 current 并执行安装成功后的收尾步骤。

        install() 与 rollback() 共用同一套步骤：铺 UI → 补齐小米插件规范结构 →
        原子切换 current 软链 → reload/restart → 写注册表与状态文件。
        用户数据目录（/data/plugin/<plugin>/data 之类）不在这里，永远不被动。
        """
        # release 里另存了一份 UI（/home 下的副本会被 plugincenter 强制卸载清掉）
        if (release / "ui").is_dir():
            _copy_path(release / "ui", ui_target)
        # UI 就位后补齐小米插件规范结构（abstract 覆盖 src/ 下全部文件）
        self._apply_native_layout(ui_target.parents[1], manifest, release)

        current = release_root / "current"
        current.parent.mkdir(parents=True, exist_ok=True)
        temporary_link = current.with_name("current.community-store.tmp")
        if temporary_link.exists() or temporary_link.is_symlink():
            temporary_link.unlink()
        temporary_link.symlink_to(release)
        if current.exists() and not current.is_symlink() and current.is_dir():
            shutil.rmtree(current)
        temporary_link.replace(current)

        self._run(["nginx", "-t"])
        self._run(["systemctl", "daemon-reload"])
        self._run(["systemctl", "enable", manifest["service"]])
        self._run(["systemctl", "restart", manifest["service"]])
        self._run(["systemctl", "reload", "nginx"])
        if self.execute_system:
            health = f"http://127.0.0.1:{manifest['port']}{manifest['healthPath']}"
            self._wait_for_health(health)

        if registry is None:
            registry = self._load_registry()
        registry[manifest["id"]] = self._registry_record(manifest)
        _write_json_atomic(registry_path, registry)
        state = {
            "managed": True,
            "installedAt": int(time.time()),
            "manifest": manifest,
            "release": str(release),
        }
        _write_json_atomic(self.state_dir / f"{manifest['id']}.json", state, 0o600)

    def install(self, package_id: str) -> dict[str, Any]:
        package = self._package_entry(package_id)
        bundle = self._resolve_artifact(package["bundle"], expected_sha256=package["sha256"])
        if sha256_file(bundle) != package["sha256"]:
            raise StoreError("Bundle checksum mismatch")
        # Optional ECDSA signature (legacy catalog.json mode)
        signature_ref = package.get("signature")
        if signature_ref and self.public_key:
            signature = self._resolve_artifact(signature_ref)
            verify_detached_signature(bundle, signature, self.public_key)

        operation = f"{int(time.time())}-{os.getpid()}"
        staging_root = self._host(f"/data/plugin/community-store/staging/{operation}")
        backup_root = self._host(f"/data/plugin/community-store/backups/{operation}")
        staging_root.mkdir(parents=True, exist_ok=False)
        backup_root.mkdir(parents=True, exist_ok=False)
        try:
            safe_extract_bundle(bundle, staging_root)
            manifest_path = staging_root / "manifest.json"
            try:
                manifest = validate_manifest(json.loads(manifest_path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError) as error:
                raise StoreError(f"Cannot read bundle manifest: {error}") from error
            if manifest["id"] != package["id"] or manifest["version"] != package["version"]:
                raise StoreError("Catalog and bundle identity do not match")

            for required in ("runtime", "ui", "icon", f"config/{manifest['service']}", f"config/{manifest['nginx']}"):
                if not (staging_root / required).exists():
                    raise StoreError(f"Bundle payload is missing: {required}")

            registry = self._load_registry()
            self._check_registry_conflicts(registry, manifest)
            release_root = self._host(manifest["paths"]["releaseRoot"])
            release = release_root / "releases" / f"{manifest['version']}-{operation}"
            ui_target = self._host(f"/home/{self.user_id}/plugin/{manifest['paths']['uiKey']}/src/ui")
            icon_target = self._host(f"/data/plugin/www/icon/{manifest['paths']['iconName']}")
            service_target = self._host(f"/etc/systemd/system/{manifest['service']}")
            nginx_target = self._host(f"/etc/nginx/conf.d/luci/{manifest['nginx']}")
            registry_path = self._registry_path()

            tracked = {
                "ui": ui_target,
                "icon": icon_target,
                "service": service_target,
                "nginx": nginx_target,
                "registry": registry_path,
                "current": release_root / "current",
            }
            existing: dict[str, bool] = {}
            for key, target in tracked.items():
                existing[key] = target.exists() or target.is_symlink()
                if existing[key]:
                    _copy_path(target, backup_root / key)

            try:
                _copy_path(staging_root / "runtime", release)
                self._install_requirements(staging_root, manifest, release)
                # 另存一份 UI 到 release 目录：/home 下的副本会被
                # plugincenter 的强制卸载清掉，恢复时需要从这里重新拷贝
                _copy_path(staging_root / "ui", release / "ui")
                _copy_path(staging_root / "icon", icon_target)
                service_text = (staging_root / "config" / manifest["service"]).read_text(encoding="utf-8")
                service_text = service_text.replace("__NAS_USER_ID__", self.user_id)
                nginx_text = (staging_root / "config" / manifest["nginx"]).read_text(encoding="utf-8")
                nginx_text = nginx_text.replace("__NAS_USER_ID__", self.user_id)
                service_target.parent.mkdir(parents=True, exist_ok=True)
                nginx_target.parent.mkdir(parents=True, exist_ok=True)
                service_target.write_text(service_text, encoding="utf-8")
                nginx_target.write_text(nginx_text, encoding="utf-8")
                os.chmod(service_target, 0o644)
                os.chmod(nginx_target, 0o644)
                # 切换 current + 收尾（铺 UI / 补原生结构 / 起服务 / 写注册表与状态），
                # 与 rollback() 共用同一套步骤
                self._activate_release(
                    manifest,
                    release,
                    ui_target=ui_target,
                    release_root=release_root,
                    registry_path=registry_path,
                    registry=registry,
                )
            except Exception:
                for key, target in tracked.items():
                    if target.exists() or target.is_symlink():
                        if target.is_dir() and not target.is_symlink():
                            shutil.rmtree(target)
                        else:
                            target.unlink()
                    if existing[key]:
                        _copy_path(backup_root / key, target)
                if release.exists():
                    shutil.rmtree(release)
                if self.execute_system:
                    subprocess.run(["systemctl", "daemon-reload"], check=False)
                    subprocess.run(["nginx", "-t"], check=False)
                    subprocess.run(["systemctl", "restart", manifest["service"]], check=False)
                    subprocess.run(["systemctl", "reload", "nginx"], check=False)
                raise
            # 安装成功、current 已切换后才清理旧版本；清理失败只记日志，不影响安装结果
            return {
                "ok": True,
                "id": manifest["id"],
                "version": manifest["version"],
                "prune": self._prune_after_install(manifest),
            }
        finally:
            shutil.rmtree(staging_root, ignore_errors=True)

    def uninstall(self, package_id: str) -> dict[str, Any]:
        state_path = self.state_dir / f"{package_id}.json"
        if not state_path.is_file():
            raise StoreError("Only plugins installed by this store can be uninstalled here")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        manifest = validate_manifest(state.get("manifest"))
        release_root = self._host(manifest["paths"]["releaseRoot"])
        release = Path(str(state.get("release", ""))).resolve()
        releases_root = (release_root / "releases").resolve()
        try:
            release.relative_to(releases_root)
        except ValueError as error:
            raise StoreError("Managed release path is outside the plugin release root") from error
        self._run(["systemctl", "disable", "--now", manifest["service"]])
        targets = [
            self._host(f"/etc/nginx/conf.d/luci/{manifest['nginx']}"),
            self._host(f"/etc/systemd/system/{manifest['service']}"),
            self._host(f"/home/{self.user_id}/plugin/{manifest['paths']['uiKey']}/src/ui"),
            self._host(f"/data/plugin/www/icon/{manifest['paths']['iconName']}"),
        ]
        for target in targets:
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            elif target.exists() or target.is_symlink():
                target.unlink()
        current = release_root / "current"
        if current.is_symlink() and current.resolve() == release:
            current.unlink()
        if release.is_dir():
            shutil.rmtree(release)
        registry = self._load_registry()
        registry.pop(manifest["id"], None)
        _write_json_atomic(self._registry_path(), registry)
        state_path.unlink()
        self._run(["systemctl", "daemon-reload"])
        self._run(["nginx", "-t"])
        self._run(["systemctl", "reload", "nginx"])
        return {"ok": True, "id": package_id, "dataPreserved": True}

    # ------------------------------------------------------------------
    # 旧版本清理 / 回滚
    # ------------------------------------------------------------------

    def _managed_release_root(self, package_id: str, *, action: str = "管理") -> tuple[dict[str, Any], Path, Path]:
        """本商店安装的插件的 manifest、release 根目录与 releases 目录。"""
        state_path = self.state_dir / f"{package_id}.json"
        if not state_path.is_file():
            raise StoreError(f"该插件不是由本商店安装的，无法{action}")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        manifest = validate_manifest(state.get("manifest"))
        release_root = self._host(manifest["paths"]["releaseRoot"])
        releases_root = (release_root / "releases").resolve()
        try:
            releases_root.relative_to(release_root.resolve())
        except ValueError as error:
            raise StoreError("Managed release path is outside the plugin release root") from error
        return manifest, release_root, releases_root

    def _release_candidates(self, releases_root: Path) -> list[Path]:
        """releases/ 下由本商店创建的版本目录，最新优先。

        只认名字符合 install() 规则（`<版本>-<时间戳>-<pid>`）的真实目录；
        历史遗留目录、软链、普通文件都不算，由调用方跳过。
        """
        if not releases_root.is_dir():
            return []
        candidates: list[Path] = []
        for entry in releases_root.iterdir():
            if entry.is_symlink() or not entry.is_dir():
                continue
            if release_name_parts(entry.name) is None:
                continue
            candidates.append(entry)
        candidates.sort(key=_release_sort_key, reverse=True)
        return candidates

    def rollback_info(self, release_root: Path, package_id: str = "") -> dict[str, Any]:
        """上一版版本号与是否可回滚（只读，不修改任何东西）。"""
        if package_id == STORE_PLUGIN_ID:
            return {"previousVersion": None, "canRollback": False}
        release_root = Path(release_root)
        releases_root = (release_root / "releases").resolve()
        current = current_release_target(release_root)
        current_resolved = current.resolve() if current is not None else None
        for entry in self._release_candidates(releases_root):
            if current_resolved is not None and entry.resolve() == current_resolved:
                continue
            return {"previousVersion": release_version(entry.name), "canRollback": True}
        return {"previousVersion": None, "canRollback": False}

    def _prune_after_install(self, manifest: dict[str, Any]) -> dict[str, Any]:
        """安装成功后的自动清理：无论出什么问题都不改变安装结果。"""
        try:
            return self.prune_releases(manifest["id"], keep=DEFAULT_KEEP_RELEASES)
        except Exception as error:  # noqa: BLE001 清理失败不能影响安装结果
            _log(f"清理 {manifest['id']} 旧版本失败：{error}")
            return {
                "packageId": manifest["id"],
                "removed": [],
                "kept": [],
                "skipped": [],
                "errors": [str(error)],
                "freedBytes": 0,
            }

    def prune_releases(self, package_id: str, keep: int = DEFAULT_KEEP_RELEASES) -> dict[str, Any]:
        """清理旧版本目录：永远保留 current 指向的版本，再保留最新 keep-1 个。

        只在 `<releaseRoot>/releases/` 内操作，沿用 uninstall() 那套
        `relative_to(releases_root)` 包含校验；只删本商店按
        `<版本>-<时间戳>-<pid>` 规则创建的目录，名字不符合的遗留目录跳过并记日志。
        单个目录删除失败只记日志并继续；除入参/状态错误外不向外抛异常。
        """
        if keep < 1:
            raise StoreError("keep 至少要保留 1 个版本")
        result: dict[str, Any] = {
            "packageId": package_id,
            "removed": [],
            "kept": [],
            "skipped": [],
            "errors": [],
            "freedBytes": 0,
        }
        _, release_root, releases_root = self._managed_release_root(package_id, action="清理")
        if not releases_root.is_dir():
            return result

        candidates: list[Path] = []
        for entry in sorted(releases_root.iterdir(), key=lambda item: item.name):
            if entry.is_symlink() or not entry.is_dir():
                result["skipped"].append(entry.name)
                _log(f"跳过不是目录的条目：{entry}")
                continue
            if release_name_parts(entry.name) is None:
                result["skipped"].append(entry.name)
                _log(f"跳过非本商店创建的目录（历史遗留，不动它）：{entry}")
                continue
            candidates.append(entry)
        candidates.sort(key=_release_sort_key, reverse=True)

        current = current_release_target(release_root)
        current_resolved = current.resolve() if current is not None else None
        protected: set[Path] = set()
        for entry in candidates:
            if current_resolved is not None and entry.resolve() == current_resolved:
                protected.add(entry)
                break
        remaining = keep - 1
        for entry in candidates:
            if entry in protected:
                continue
            if remaining <= 0:
                break
            protected.add(entry)
            remaining -= 1

        for entry in candidates:
            if entry in protected:
                result["kept"].append(entry.name)
                continue
            # 包含校验：只允许删 releases/ 内的目录（沿用 uninstall() 的写法）
            resolved = entry.resolve()
            try:
                resolved.relative_to(releases_root)
            except ValueError:
                result["errors"].append(f"{entry.name}: 路径超出版本目录")
                _log(f"拒绝删除 releases 目录之外的路径：{resolved}")
                continue
            size = _directory_size(entry)
            try:
                shutil.rmtree(entry)
            except OSError as error:
                result["errors"].append(f"{entry.name}: {error}")
                _log(f"清理旧版本失败 {entry}：{error}")
                continue
            result["removed"].append(entry.name)
            result["freedBytes"] += size
            _log(f"已清理旧版本：{entry.name}")
        return result

    def prune_all(self, keep: int = DEFAULT_KEEP_RELEASES) -> dict[str, Any]:
        """对所有由本商店安装的插件各跑一次 prune_releases(keep)。"""
        summary: dict[str, Any] = {
            "ok": True,
            "removedCount": 0,
            "freedBytes": 0,
            "errors": [],
            "results": {},
        }
        for package_id in sorted(self.installed()):
            try:
                result = self.prune_releases(package_id, keep=keep)
            except Exception as error:  # noqa: BLE001 一个插件失败不影响其余
                summary["errors"].append(f"{package_id}: {error}")
                _log(f"清理 {package_id} 旧版本失败：{error}")
                continue
            summary["results"][package_id] = result
            summary["removedCount"] += len(result["removed"])
            summary["freedBytes"] += result["freedBytes"]
            summary["errors"].extend(f"{package_id}: {item}" for item in result["errors"])
        return summary

    def rollback(self, package_id: str) -> dict[str, Any]:
        """一键回滚到上一版本：只切版本目录 + 与安装相同的收尾步骤。

        不动用户数据（与 install() 一致）；包未安装、没有上一版、回滚商店
        自身都会报中文错误。
        """
        if package_id == STORE_PLUGIN_ID:
            raise StoreError("插件市场自身不能回滚到旧版本")
        manifest, release_root, releases_root = self._managed_release_root(package_id, action="回滚")
        current = current_release_target(release_root)
        current_resolved = current.resolve() if current is not None else None

        target: Path | None = None
        for entry in self._release_candidates(releases_root):
            if current_resolved is not None and entry.resolve() == current_resolved:
                continue
            target = entry
            break
        if target is None:
            raise StoreError("没有可回滚的上一版本")
        version = release_version(target.name)
        if version is None:
            raise StoreError("没有可回滚的上一版本")

        # 注册表、状态文件与 INFO 都要写成回滚后的版本
        rolled_manifest = json.loads(json.dumps(manifest, ensure_ascii=False))
        rolled_manifest["version"] = version
        ui_target = self._host(f"/home/{self.user_id}/plugin/{manifest['paths']['uiKey']}/src/ui")
        self._activate_release(
            rolled_manifest,
            target,
            ui_target=ui_target,
            release_root=release_root,
            registry_path=self._registry_path(),
        )
        return {
            "ok": True,
            "packageId": package_id,
            "version": version,
            "previous": str(manifest.get("version", "")),
        }

    def installed(self) -> dict[str, dict[str, Any]]:
        """已安装插件：当前版本 + 上一版信息（供页面判断能否回滚）。"""
        result: dict[str, dict[str, Any]] = {}
        for path in self.state_dir.glob("*.json"):
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
                manifest = validate_manifest(state.get("manifest"))
                release_root = self._host(manifest["paths"]["releaseRoot"])
                info = self.rollback_info(release_root, manifest["id"])
                result[manifest["id"]] = {
                    "version": manifest["version"],
                    "previousVersion": info["previousVersion"],
                    "canRollback": info["canRollback"],
                }
            except Exception:
                continue
        return result

    def inventory(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for plugin_id, record in self._load_registry().items():
            if not isinstance(record, dict):
                continue
            info = record.get("info")
            if not isinstance(info, dict):
                continue
            version = info.get("version")
            if isinstance(version, str) and VERSION_PATTERN.fullmatch(version):
                result[plugin_id] = {"version": version, "managed": False}
        for plugin_id, record in self.installed().items():
            result[plugin_id] = {"version": record["version"], "managed": True}
        return result


# ---------------------------------------------------------------------------
# 商店自更新
# ---------------------------------------------------------------------------

def self_update_store(
    current_version: str,
    *,
    store_root: str = "/data/plugin/community-store",
    repo: str | None = None,
) -> dict[str, Any]:
    """下载最新 Release，替换当前商店文件并重启服务。

    必须以 root 在 NAS 上运行。返回 {"ok": True, "version": ...}。
    """
    repo = repo or DEFAULT_REPO
    api_url = f"https://api.github.com/repos/{repo}/releases/latest"
    latest = fetch_store_latest_version(api_url)
    target_version = latest["version"]
    if not is_newer_version(target_version, current_version):
        return {"ok": True, "version": current_version, "updated": False, "message": "已是最新版本"}

    zip_name = f"xiaomi-plugin-market-{target_version}.zip"
    release_json = fetch_url_json(api_url)

    zip_url = sha_url = ""
    for asset in release_json.get("assets", []):
        name = asset.get("name", "")
        if name == zip_name:
            zip_url = asset.get("browser_download_url", "")
        elif name == "SHA256SUMS.txt":
            sha_url = asset.get("browser_download_url", "")
    if not zip_url:
        raise StoreError(f"Release 中未找到 {zip_name}")

    store_path = Path(store_root)
    staging = store_path / "staging" / f"self-update-{int(time.time())}"
    staging.mkdir(parents=True, exist_ok=False)

    try:
        # 下载
        zip_path = staging / zip_name
        _download_to_file(zip_url, zip_path)

        # SHA-256 校验
        if sha_url:
            sha_path = staging / "SHA256SUMS.txt"
            _download_to_file(sha_url, sha_path)
            expected = ""
            for line in sha_path.read_text(encoding="utf-8").splitlines():
                if zip_name in line:
                    expected = line.split()[0]
                    break
            actual = sha256_file(zip_path)
            if expected and expected != actual:
                raise StoreError("商店更新包 SHA-256 校验失败")

        # 解压
        extract_dir = staging / "extracted"
        extract_dir.mkdir()
        with zipfile.ZipFile(zip_path) as archive:
            for member in archive.infolist():
                _safe_member_path(member.filename)
            archive.extractall(extract_dir)

        # 定位项目目录
        project_dir = None
        for candidate in [extract_dir] + list(extract_dir.iterdir()):
            if candidate.is_dir() and (candidate / "server.py").is_file():
                project_dir = candidate
                break
        if project_dir is None:
            raise StoreError("更新包中未找到 server.py")

        # 校验必要文件
        for required in ("server.py", "storelib.py", "web/index.html"):
            if not (project_dir / required).is_file():
                raise StoreError(f"更新包缺少文件：{required}")

        # 创建新 release 目录
        release_id = f"{target_version}-self-{int(time.time())}"
        release_dir = store_path / "releases" / release_id
        release_dir.mkdir(parents=True, exist_ok=False)

        # 复制文件
        shutil.copy2(project_dir / "server.py", release_dir / "server.py")
        shutil.copy2(project_dir / "storelib.py", release_dir / "storelib.py")
        if (project_dir / "web").is_dir():
            shutil.copytree(project_dir / "web", release_dir / "web")
        if (project_dir / "catalog").is_dir():
            shutil.copytree(project_dir / "catalog", release_dir / "catalog")
        if (project_dir / "deploy").is_dir():
            shutil.copytree(project_dir / "deploy", release_dir / "deploy")
        if (project_dir / "scripts").is_dir():
            shutil.copytree(project_dir / "scripts", release_dir / "scripts",
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        # VERSION 文件（server.py 启动时读取）
        version_src = project_dir / "VERSION"
        if version_src.is_file():
            shutil.copy2(version_src, release_dir / "VERSION")
        else:
            (release_dir / "VERSION").write_text(target_version + "\n", encoding="ascii")

        # 更新 current 符号链接
        current_link = store_path / "current"
        temp_link = store_path / "current.self-update.tmp"
        if temp_link.exists() or temp_link.is_symlink():
            temp_link.unlink()
        temp_link.symlink_to(release_dir)
        temp_link.replace(current_link)

        # 延迟重启：先让 HTTP 响应发出去，再由子进程执行 restart
        # 直接 restart 会杀掉当前进程，客户端收到 HTML 错误页
        subprocess.Popen(
            ["sh", "-c", "sleep 2 && systemctl restart xiaomi-community-store.service"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

        return {"ok": True, "version": target_version, "updated": True}
    finally:
        shutil.rmtree(staging, ignore_errors=True)
