#!/usr/bin/env python3
"""取回 transmission-web-control 的**构建产物**并放进 web/twc/。

上游仓库（github.com/ronggang/transmission-web-control）的 `src/` 目录本身就是
可用的构建产物——它自己的安装脚本就是下载源码包后把 `src/.` 整体拷进
transmission 的 web 目录，所以这里同样直接取 `src/`。

关键约束（别改目录层级）：transmission-web-control 的 RPC 地址写死为相对页面的
`../rpc`（见 src/tr-web-control/script/transmission.js 的 `rpcpath: "../rpc"`）。
把 `src/` 的内容放在 web/twc/ 下，页面就是
`<base>/twc/index.html`，`../rpc` 正好落到 `<base>/rpc`，
由 server.py 与 deploy/xiaomi-transmission.nginx.conf 一起代理给 daemon 的
`/transmission/rpc`。如果把 index.html 再放到子目录里就会 404。

用法：

    python3 scripts/fetch_web_control.py            # 下载并解包
    python3 scripts/fetch_web_control.py --check    # 只按 PROVENANCE.json 校验
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import tarfile
import urllib.error
import urllib.request
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
TARGET = PROJECT / "web" / "twc"
PROVENANCE = TARGET / "PROVENANCE.json"

REPOSITORY = "ronggang/transmission-web-control"
# 最新 release：tag v1.6.1-update1（2020-09-13），对应提交。
REF = "v1.6.1-update1"
COMMIT = "c26a0761e3a8fe3cff2480735ec363dc253c5105"
ARCHIVE_URL = f"https://github.com/{REPOSITORY}/archive/refs/tags/{REF}.tar.gz"
USER_AGENT = "xiaomi-transmission-plugin-vendor/1.0"


def log(message: str) -> None:
    print(message, flush=True)


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            if response.status != 200:
                raise SystemExit(f"下载 {url} 返回 HTTP {response.status}")
            return response.read()
    except urllib.error.URLError as error:
        raise SystemExit(f"无法下载 {url}：{error}") from error


def source_root(archive: tarfile.TarFile) -> str:
    """GitHub 的源码包顶层目录名会随 tag 变化，动态探测而不是硬编码。"""
    roots: set[str] = set()
    for member in archive.getmembers():
        head = member.name.split("/", 1)[0]
        if head:
            roots.add(head)
    if len(roots) != 1:
        raise SystemExit(f"源码包顶层目录不唯一：{sorted(roots)}")
    return roots.pop()


def command_fetch(arguments: argparse.Namespace) -> int:
    log(f"下载 {ARCHIVE_URL}")
    payload = download(ARCHIVE_URL)
    archive_sha = sha256_bytes(payload)
    if arguments.sha256 and archive_sha != arguments.sha256.lower():
        raise SystemExit(f"源码包摘要不符：期望 {arguments.sha256} / 实际 {archive_sha}")

    files: list[dict] = []
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        root = source_root(archive)
        src_prefix = f"{root}/src/"
        members = [
            member for member in archive.getmembers()
            if member.name.startswith(src_prefix) and member.isfile()
        ]
        if not any(member.name == f"{src_prefix}index.html" for member in members):
            raise SystemExit("源码包里没有 src/index.html，上游结构可能已变")
        if TARGET.is_dir():
            shutil.rmtree(TARGET)
        TARGET.mkdir(parents=True, exist_ok=True)
        for member in members:
            relative = member.name[len(src_prefix):]
            if not relative or ".." in Path(relative).parts:
                continue
            destination = TARGET / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            stream = archive.extractfile(member)
            if stream is None:
                continue
            content = stream.read()
            destination.write_bytes(content)
            files.append(
                {
                    "path": f"web/twc/{relative}",
                    "size": len(content),
                    "sha256": sha256_bytes(content),
                }
            )

    files.sort(key=lambda item: item["path"])
    provenance = {
        "schemaVersion": 1,
        "generatedBy": "projects/xiaomi-transmission-plugin/scripts/fetch_web_control.py",
        "repository": f"https://github.com/{REPOSITORY}",
        "ref": REF,
        "commit": COMMIT,
        "archiveUrl": ARCHIVE_URL,
        "archiveSha256": archive_sha,
        "sourcePath": "src/",
        "note": (
            "上游把构建产物直接提交在 src/；页面用相对路径 ../rpc 访问 RPC，"
            "因此必须整目录放在 <base>/twc/ 之下。"
        ),
        "fileCount": len(files),
        "files": files,
    }
    PROVENANCE.write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    log(f"解出 {len(files)} 个文件到 web/twc/")
    log(f"源码包 SHA-256：{archive_sha}")
    return 0


def command_check(arguments: argparse.Namespace) -> int:
    if not PROVENANCE.is_file():
        raise SystemExit(f"缺少 {PROVENANCE}，请先运行 fetch_web_control.py")
    provenance = json.loads(PROVENANCE.read_text(encoding="utf-8"))
    problems = 0
    for item in provenance["files"]:
        path = PROJECT / item["path"]
        if not path.is_file():
            log(f"  ! 缺失 {item['path']}")
            problems += 1
            continue
        if sha256_bytes(path.read_bytes()) != item["sha256"]:
            log(f"  ! 摘要不符 {item['path']}")
            problems += 1
    if problems:
        log(f"{problems} 个文件校验失败")
        return 1
    log(f"{len(provenance['files'])} 个文件全部匹配 {PROVENANCE.name}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="只校验已有文件，不联网")
    parser.add_argument("--sha256", default="", help="可选的源码包摘要固定值")
    arguments = parser.parse_args()
    return command_check(arguments) if arguments.check else command_fetch(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
