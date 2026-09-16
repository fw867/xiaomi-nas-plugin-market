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


def load_apps_catalog(
    *,
    local_apps_json: Path | None = None,
    remote_url: str | None = None,
    cache_path: Path | None = None,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Load apps.json: local file first, then GitHub, then last successful cache."""
    candidates: list[Path] = []
    if local_apps_json and local_apps_json.is_file():
        candidates.append(local_apps_json)
    if cache_path and cache_path.is_file():
        candidates.append(cache_path)

    if not force_refresh:
        for path in candidates:
            try:
                document = validate_apps_catalog(json.loads(path.read_text(encoding="utf-8")))
                document["_source"] = str(path)
                return document
            except (OSError, json.JSONDecodeError, StoreError):
                continue

    url = remote_url or _raw_url("apps.json")
    document = validate_apps_catalog(fetch_url_json(url))
    document["_source"] = url
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
        """从 GitHub 拉取 apps.json（带本地缓存）。"""
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
                _copy_path(staging_root / "ui", ui_target)
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

                registry[manifest["id"]] = self._registry_record(manifest)
                _write_json_atomic(registry_path, registry)
                state = {
                    "managed": True,
                    "installedAt": int(time.time()),
                    "manifest": manifest,
                    "release": str(release),
                }
                _write_json_atomic(self.state_dir / f"{manifest['id']}.json", state, 0o600)
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
            return {"ok": True, "id": manifest["id"], "version": manifest["version"]}
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

    def installed(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for path in self.state_dir.glob("*.json"):
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
                manifest = validate_manifest(state.get("manifest"))
                result[manifest["id"]] = manifest["version"]
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
        for plugin_id, version in self.installed().items():
            result[plugin_id] = {"version": version, "managed": True}
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
        # catalog 可选（新版可能没有）
        if (project_dir / "catalog").is_dir():
            shutil.copytree(project_dir / "catalog", release_dir / "catalog")
        # deploy 可选
        if (project_dir / "deploy").is_dir():
            shutil.copytree(project_dir / "deploy", release_dir / "deploy")

        # 更新 current 符号链接
        current_link = store_path / "current"
        temp_link = store_path / "current.self-update.tmp"
        if temp_link.exists() or temp_link.is_symlink():
            temp_link.unlink()
        temp_link.symlink_to(release_dir)
        temp_link.replace(current_link)

        # 重启服务（systemctl 会用新代码重新启动进程）
        result = subprocess.run(
            ["systemctl", "restart", "xiaomi-community-store.service"],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            # 回滚符号链接
            temp_link2 = store_path / "current.rollback.tmp"
            if temp_link2.exists() or temp_link2.is_symlink():
                temp_link2.unlink()
            # 尝试恢复到之前的 release（通过 /proc 或直接读旧链接）
            # 简单回滚：不改链接，只报错
            detail = (result.stderr or result.stdout or "restart failed").strip()
            raise StoreError(f"服务重启失败：{detail}")

        return {"ok": True, "version": target_version, "updated": True}
    finally:
        shutil.rmtree(staging, ignore_errors=True)
