#!/usr/bin/env python3
"""为社区插件补齐小米插件规范要求的目录结构与 INFO。

背景：plugin.boot 每次开机执行 `plugincenter boot --system`，对它认定的
每个「已安装」插件运行 `/usr/bin/plugin.sh verify`。

plugin_verify() 并不是厂商签名校验，而是**摘要自校验**：

    [ ! -d "$PLUG_SRC_DIR" ] && return 1
    [ ! -f "$PLUG_HOME_DIR/INFO" ] && return 1
    abstract = sha256( 按 LC_COLLATE=C 排序的、src/ 下每个文件的 sha256 列表 )
    PLUG_STATUS=unverified 时：比对 INFO.abstract 与当前计算值

目录或 INFO 缺失时 verify 直接返回 1，plugincenter 会判定
"can't use forever, force uninstall"：删除 /home/<用户>/plugin/<key>/
并从注册表移除条目——表现为服务还在跑但客户端应用列表里插件消失。

本脚本按同一算法补齐结构，使 verify 通过，插件被当作正常安装保留。
必须在 UI 文件就位后最后执行：abstract 覆盖 src/ 下所有文件，
之后任何改动都会让校验失败。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path


SUBDIRS = ("etc", "var", "tmp", "scripts")


def log(message: str) -> None:
    print(message, flush=True)


def compute_abstract(src_dir: Path) -> str:
    """复刻 plugin.sh 的算法：文件摘要按序拼接后再取一次 sha256。

    plugin.sh 用 `find ... | LC_COLLATE=C sort` 枚举，再逐个
    `sha256sum | cut -d' ' -f1 >> digest`，因此摘要文件内容是
    「每行一个 64 位十六进制摘要 + 换行」。排序用字节序等价复现。
    """
    files = sorted(
        (p for p in src_dir.rglob("*") if p.is_file()),
        key=lambda p: str(p).encode("utf-8"),
    )
    lines: list[str] = []
    for path in files:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        lines.append(digest.hexdigest())
    combined = hashlib.sha256("".join(f"{line}\n" for line in lines).encode("ascii"))
    return combined.hexdigest()


def write_control(scripts_dir: Path, service: str) -> bool:
    """写 scripts/control。plugincenter boot 会执行 `<control> enable`。"""
    scripts_dir.mkdir(parents=True, exist_ok=True)
    target = scripts_dir / "control"
    body = f"""#!/bin/sh
# 由 install 生成：给 plugincenter 一个成功的应答，
# 实际的服务启停交给 systemd（{service}）。
log() {{ logger -t "plugin" "[$(basename "$(dirname "$(dirname "$0")")")] control: $*"; }}
log "do $1"
case "$1" in
enable)  systemctl start {service} 2>/dev/null ;;
disable) systemctl stop  {service} 2>/dev/null ;;
esac
exit 0
"""
    changed = True
    if target.is_file() and target.read_text(encoding="utf-8") == body:
        changed = False
    target.write_text(body, encoding="utf-8")
    target.chmod(0o755)

    hotplug = scripts_dir / "hotplug"
    if not hotplug.is_file():
        hotplug.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        hotplug.chmod(0o755)
    return changed


def ensure_layout(home_dir: Path, meta: dict, *, user: str, service: str) -> dict:
    src_dir = home_dir / "src"
    if not src_dir.is_dir():
        raise SystemExit(f"缺少 UI 目录: {src_dir}")

    for name in SUBDIRS:
        (home_dir / name).mkdir(parents=True, exist_ok=True)

    write_control(home_dir / "scripts", service)

    # abstract 必须最后算：它覆盖 src/ 下全部文件
    abstract = compute_abstract(src_dir)
    info_path = home_dir / "INFO"
    info = {}
    if info_path.is_file():
        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            info = {}
    info.update(meta)
    info["abstract"] = abstract
    info_path.write_text(json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    info_path.chmod(0o644)

    # 属主改为该用户：verify 以该用户身份执行，文件不可读会失败
    try:
        subprocess.run(["chown", "-R", f"{user}:{user}", str(home_dir)], check=False,
                       capture_output=True)
    except OSError as error:
        log(f"  ! chown 失败: {error}")

    return {"abstract": abstract, "info": str(info_path)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user", required=True, help="小米用户 ID，如 u3943892")
    parser.add_argument("--name", required=True, help="插件 key，如 sshcontrol")
    parser.add_argument("--service", required=True, help="对应的 systemd 服务名")
    parser.add_argument("--title", default="", help="显示名称")
    parser.add_argument("--plugin-id", type=int, default=0)
    parser.add_argument("--version", default="")
    parser.add_argument("--desc", default="")
    parser.add_argument("--tags", default="tool")
    parser.add_argument("--home", default="", help="覆盖插件目录")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    home_dir = Path(args.home) if args.home else Path(f"/home/{args.user}/plugin/{args.name}")
    if not home_dir.is_dir():
        raise SystemExit(f"插件目录不存在: {home_dir}")

    meta = {
        "plugin": args.name,
        "name": args.title or args.name,
        "id": args.plugin_id,
        "version": args.version,
        "desc": args.desc,
        "tags": [t for t in args.tags.split(",") if t],
        "developer": "community",
        "publisher": "community",
        "system": False,
        "type": "standard",
        "size": 0,
        "ext": {},
        "forceupgrade": False,
        "timestamp": int(time.time()),
    }
    result = ensure_layout(home_dir, meta, user=args.user, service=args.service)
    if not args.quiet:
        log(f"已补齐 {home_dir}  abstract={result['abstract'][:16]}…")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as error:  # noqa: BLE001
        print(f"native-layout 失败: {error}", file=sys.stderr)
        raise SystemExit(1)
