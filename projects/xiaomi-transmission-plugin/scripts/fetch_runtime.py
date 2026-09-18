#!/usr/bin/env python3
"""从 Entware 的 aarch64 仓库提取 transmission 运行时，并记录来源与校验值。

为什么要自己提取：小米智能存储的根文件系统是只读 erofs，设备上**没有** Entware，
也不能在安装时联网装包。所以 transmission-daemon 及其全部 .so 依赖必须在打包前
就随插件一起带上，运行时用 LD_LIBRARY_PATH 指向随包的 lib/ 目录。

产物（都会被提交到 git，并被 scripts/build_apps.py 打进 bundle）：

    bin/transmission-daemon, bin/transmission-remote, bin/transmission-cli
    lib/*.so*                     递归解析出的全部共享库依赖
    licenses/*                    transmission 的 GPL-2 文本、transmission-web-control 的 MIT
    runtime-manifest.json         每个文件的来源 .ipk、成员路径与 SHA-256

用法：

    python3 scripts/fetch_runtime.py              # 下载、校验、解包、写清单
    python3 scripts/fetch_runtime.py --check      # 只按清单校验已有文件，不联网
    python3 scripts/fetch_runtime.py --print-table  # 打印可粘进 README 的 .ipk 表格

本脚本只用标准库，不需要 Entware 环境。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import tarfile
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
INDEX_URL = "https://bin.entware.net/aarch64-k3.10/Packages"
REPO_URL = "https://bin.entware.net/aarch64-k3.10"

BIN_DIR = PROJECT / "bin"
LIB_DIR = PROJECT / "lib"
LICENSE_DIR = PROJECT / "licenses"
MANIFEST = PROJECT / "runtime-manifest.json"

# 要装的 Entware 包；其余依赖由索引递归解析。
ROOTS = ["transmission-daemon", "transmission-remote", "transmission-cli"]

# Entware 二进制的 ELF 解释器是 /opt/lib/ld-linux-aarch64.so.1（Entware 自带
# 的加载器），设备上的系统 glibc 在 /lib 下，路径对不上。因此必须把 Entware
# 自己的 libc 一并打包，运行时用随包的加载器显式调用：
#   lib/ld-linux-aarch64.so.1 --library-path lib bin/transmission-daemon
# libc/libpthread/librt/libssp 在 Entware 里是真实存在的包；libm/libdl 等在
# glibc 2.27 里由 libc 包内提供，索引里没有独立包，列在这里仅用于跳过。
SYSTEM_PROVIDERS = {
    "libm", "libdl", "libcrypt", "libnsl", "libresolv", "libutil",
    "libmvec", "libgcc-s",
}

# 需要一起归档的许可证文本（sha256 会写进清单）。
LICENSE_SOURCES = [
    {
        "name": "TRANSMISSION-COPYING",
        "url": "https://raw.githubusercontent.com/transmission/transmission/4.0.6/COPYING",
        "note": "Transmission 4.0.6，GPL-2.0-or-later",
    },
    {
        "name": "TRANSMISSION-WEB-CONTROL-LICENSE",
        "url": (
            "https://raw.githubusercontent.com/ronggang/transmission-web-control/"
            "c26a0761e3a8fe3cff2480735ec363dc253c5105/LICENSE"
        ),
        "note": "transmission-web-control v1.6.1-update1，MIT",
    },
]

USER_AGENT = "xiaomi-transmission-plugin-vendor/1.0"


def log(message: str) -> None:
    print(message, flush=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch(url: str, *, retries: int = 3, timeout: float = 60.0) -> bytes:
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                if response.status != 200:
                    raise urllib.error.HTTPError(
                        url, response.status, "unexpected status", response.headers, None
                    )
                return response.read()
        except Exception as error:  # noqa: BLE001 - 重试任何网络故障
            last = error
            log(f"  ! 第 {attempt} 次下载失败：{error}")
    raise SystemExit(f"无法下载 {url}：{last}")


def download_to(url: str, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".part")
    temporary.write_bytes(fetch(url))
    temporary.replace(target)


# ---------------------------------------------------------------------------
# 解析 Entware 的 Packages 索引
# ---------------------------------------------------------------------------

def parse_index(text: str) -> dict[str, dict]:
    packages: dict[str, dict] = {}
    for block in text.split("\n\n"):
        entry: dict[str, str] = {}
        for line in block.splitlines():
            if not line.strip() or line.startswith(" "):
                continue
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            entry[key.strip()] = value.strip()
        name = entry.get("Package")
        if not name:
            continue
        entry["Depends"] = entry.get("Depends", "")
        packages[name] = entry
    return packages


def build_providers(packages: dict[str, dict]) -> dict[str, str]:
    providers: dict[str, str] = {}
    for name, entry in packages.items():
        providers.setdefault(name, name)
        for provided in entry.get("Provides", "").split(","):
            provided = provided.strip()
            if provided:
                providers.setdefault(provided, name)
    return providers


def resolve(packages: dict[str, dict], providers: dict[str, str]) -> list[str]:
    """递归解析 ROOTS 的全部依赖，返回需要打包的包名列表（排序后）。"""
    needed: dict[str, str] = {}
    pending = list(ROOTS)
    while pending:
        wanted = pending.pop()
        name = providers.get(wanted)
        if name is None:
            raise SystemExit(f"索引里找不到 {wanted} 的提供者")
        if name in needed or name in SYSTEM_PROVIDERS:
            continue
        entry = packages.get(name)
        if entry is None:
            raise SystemExit(f"索引里找不到包 {name}")
        needed[name] = entry.get("Depends", "")
        for dependency in entry.get("Depends", "").split(","):
            dependency = dependency.strip()
            if dependency and dependency not in SYSTEM_PROVIDERS:
                pending.append(dependency)
    return sorted(needed)


# ---------------------------------------------------------------------------
# 解包 .ipk
# ---------------------------------------------------------------------------

def extract_ipk(ipk: Path, destination: Path) -> Path:
    """把 .ipk 里的 data.tar.* 解开到 destination，返回解出的目录。

    Entware 的 .ipk 是 gzip 包着的 ar 归档，成员名为 `./data.tar.gz`
    （带 `./` 前缀），所以匹配前要先归一化。
    """
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(ipk, "r:*") as outer:
        data_member = None
        for member in outer.getmembers():
            name = member.name[2:] if member.name.startswith("./") else member.name
            if name.startswith("data.tar"):
                data_member = member
                break
        if data_member is None:
            raise SystemExit(f"{ipk.name} 里没有 data.tar.*")
        stream = outer.extractfile(data_member)
        if stream is None:
            raise SystemExit(f"{ipk.name} 的 data.tar.* 无法读取")
        with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as temporary:
            shutil.copyfileobj(stream, temporary)
            data_path = Path(temporary.name)
    try:
        with tarfile.open(data_path, "r:*") as inner:
            for member in inner.getmembers():
                normalized = os.path.normpath(member.name).lstrip("./")
                if normalized.startswith("..") or os.path.isabs(normalized):
                    raise SystemExit(f"{ipk.name} 含可疑路径：{member.name}")
            try:
                # filter="tar" 允许包内的相对符号链接，同时拦住绝对路径与逃逸。
                inner.extractall(destination, filter="tar")
            except TypeError:  # Python < 3.11.4 没有 filter 参数
                inner.extractall(destination)
    finally:
        data_path.unlink(missing_ok=True)
    return destination


def package_files(tree: Path, package: str) -> list[tuple[str, Path, str]]:
    """收集该包里属于 bin/ 与 lib/ 的文件（symlink 已解析为真实文件）。

    `safe_extract_bundle` 会拒绝 zip 里的符号链接成员，而打 zip 时所有文件都会被
    写成普通文件，所以这里必须先把 symlink 解析成真实文件再复制。
    """
    collected: list[tuple[str, Path, str]] = []
    for kind, target in (("bin", "bin"), ("sbin", "bin"), ("lib", "lib")):
        root = tree / "opt" / kind
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_dir() and not path.is_symlink():
                continue
            if not path.is_file():
                # 断链或指向包外的东西，直接跳过。
                continue
            if target == "lib" and not re.search(r"\.so(\.|$)", path.name):
                continue
            collected.append((package, path, target))
    return collected


def stage(files: list[tuple[str, Path, str]], records: list[dict]) -> None:
    for package, path, target in files:
        target_dir = BIN_DIR if target == "bin" else LIB_DIR
        mode = 0o755 if target == "bin" else 0o644
        target_dir.mkdir(parents=True, exist_ok=True)
        destination = target_dir / path.name
        payload = path.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        if destination.exists():
            if sha256_file(destination) != digest:
                raise SystemExit(
                    f"文件冲突：{destination.name} 来自多个包且内容不同（{package}）"
                )
        else:
            destination.write_bytes(payload)
            os.chmod(destination, mode)
        records.append(
            {
                "path": destination.relative_to(PROJECT).as_posix(),
                "sourcePackage": package,
                "size": len(payload),
                "sha256": digest,
            }
        )


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------

def load_index(cache: Path) -> dict[str, dict]:
    index_file = cache / "Packages"
    if not index_file.is_file():
        log(f"拉取索引 {INDEX_URL}")
        download_to(INDEX_URL, index_file)
    return parse_index(index_file.read_text(encoding="utf-8", errors="replace"))


def command_fetch(arguments: argparse.Namespace) -> int:
    cache = Path(arguments.cache_dir)
    packages = load_index(cache)
    providers = build_providers(packages)
    selected = resolve(packages, providers)
    log(f"依赖闭包（不含系统 glibc）：{len(selected)} 个包")
    for name in selected:
        log(f"  - {packages[name]['Filename']}")

    records: list[dict] = []
    package_records: list[dict] = []
    staging = Path(tempfile.mkdtemp(prefix="entware-ipk-"))
    # 先清空上一次的产物，避免版本升级后旧 .so 残留。
    for directory in (BIN_DIR, LIB_DIR):
        if directory.is_dir():
            shutil.rmtree(directory)
    # licenses/ 里除了这里下载的上游许可，还有随仓库维护的文件（如图标许可），
    # 所以只清掉自己管理的那几个，不要整个目录一起删。
    for source in LICENSE_SOURCES:
        (LICENSE_DIR / source["name"]).unlink(missing_ok=True)
    try:
        for name in selected:
            entry = packages[name]
            filename = entry["Filename"]
            url = f"{REPO_URL}/{filename}"
            ipk = cache / filename
            if not ipk.is_file():
                log(f"下载 {filename}")
                download_to(url, ipk)
            expected = entry.get("SHA256sum", "").lower()
            actual = sha256_file(ipk)
            if expected and actual != expected:
                raise SystemExit(f"{filename} 校验失败：索引 {expected} / 实际 {actual}")
            if entry.get("Size") and int(entry["Size"]) != ipk.stat().st_size:
                raise SystemExit(f"{filename} 大小与索引不符")
            tree = extract_ipk(ipk, staging / name)
            files = package_files(tree, name)
            if name in ROOTS and not any(target == "bin" for _, _, target in files):
                raise SystemExit(f"{filename} 里没有可执行文件")
            stage(files, records)
            package_records.append(
                {
                    "name": name,
                    "version": entry.get("Version", ""),
                    "filename": filename,
                    "url": url,
                    "size": int(entry.get("Size", ipk.stat().st_size)),
                    "sha256": actual,
                    "license": entry.get("License", ""),
                    "depends": entry.get("Depends", ""),
                    "installedSize": int(entry.get("Installed-Size", 0) or 0),
                }
            )
            shutil.rmtree(tree, ignore_errors=True)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    license_records = []
    for source in LICENSE_SOURCES:
        target = LICENSE_DIR / source["name"]
        log(f"下载许可证 {source['name']}")
        download_to(source["url"], target)
        license_records.append(
            {
                "path": target.relative_to(PROJECT).as_posix(),
                "url": source["url"],
                "note": source["note"],
                "size": target.stat().st_size,
                "sha256": sha256_file(target),
            }
        )

    manifest = {
        "schemaVersion": 1,
        "generatedBy": "projects/xiaomi-transmission-plugin/scripts/fetch_runtime.py",
        "architecture": "aarch64-3.10",
        "indexUrl": INDEX_URL,
        "repository": REPO_URL,
        "roots": ROOTS,
        "excludedSystemProviders": sorted(SYSTEM_PROVIDERS),
        "packages": sorted(package_records, key=lambda item: item["name"]),
        "files": sorted(records, key=lambda item: item["path"]),
        "licenses": license_records,
    }
    MANIFEST.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    total_bin = sum(item["size"] for item in records if item["path"].startswith("bin/"))
    total_lib = sum(item["size"] for item in records if item["path"].startswith("lib/"))
    log("")
    log(f"bin/  {total_bin / 1024 / 1024:.2f} MiB")
    log(f"lib/  {total_lib / 1024 / 1024:.2f} MiB")
    log(f"清单已写入 {MANIFEST.relative_to(PROJECT)}（{len(records)} 个文件）")
    return 0


def command_check(arguments: argparse.Namespace) -> int:
    if not MANIFEST.is_file():
        raise SystemExit(f"缺少 {MANIFEST}，请先运行 fetch_runtime.py")
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    problems = 0
    for item in manifest["files"]:
        path = PROJECT / item["path"]
        if not path.is_file():
            log(f"  ! 缺失 {item['path']}")
            problems += 1
            continue
        actual = sha256_file(path)
        if actual != item["sha256"]:
            log(f"  ! 摘要不符 {item['path']}")
            problems += 1
    for item in manifest.get("licenses", []):
        path = PROJECT / item["path"]
        if not path.is_file() or sha256_file(path) != item["sha256"]:
            log(f"  ! 许可证不符 {item['path']}")
            problems += 1
    if problems:
        log(f"{problems} 个文件校验失败")
        return 1
    log(f"{len(manifest['files'])} 个运行时文件全部匹配 {MANIFEST.name}")
    return 0


def command_print_table(arguments: argparse.Namespace) -> int:
    if not MANIFEST.is_file():
        raise SystemExit(f"缺少 {MANIFEST}，请先运行 fetch_runtime.py")
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    log("| 包 | 版本 | .ipk | 大小 | SHA-256 |")
    log("| --- | --- | --- | --- | --- |")
    for item in manifest["packages"]:
        log(
            f"| `{item['name']}` | {item['version']} | `{item['filename']}` | "
            f"{item['size']} B | `{item['sha256']}` |"
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="只校验已有文件，不联网")
    parser.add_argument("--print-table", action="store_true", help="打印 README 用的包表格")
    parser.add_argument(
        "--cache-dir",
        default=str(Path(tempfile.gettempdir()) / "xiaomi-transmission-entware"),
        help=".ipk 与 Packages 索引的缓存目录",
    )
    arguments = parser.parse_args()
    if arguments.check:
        return command_check(arguments)
    if arguments.print_table:
        return command_print_table(arguments)
    return command_fetch(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
