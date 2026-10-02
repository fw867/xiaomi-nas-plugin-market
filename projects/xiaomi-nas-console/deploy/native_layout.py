#!/usr/bin/env python3
"""为「控制台」补齐小米插件规范要求的目录结构与 INFO（否则开机会被强制卸载）。

背景：plugin.boot 每次开机执行 `plugincenter boot --system`，对每个「已安装」
插件运行 `/usr/bin/plugin.sh verify`。它并不是签名校验，而是**摘要自校验**：

    [ ! -d "$PLUG_SRC_DIR" ] && return 1        # 缺少 src/
    [ ! -f "$PLUG_HOME_DIR/INFO" ] && return 1  # 缺少 INFO
    abstract = sha256(按 LC_COLLATE=C 排序的 src/ 下每个文件 sha256 列表)

目录或 INFO 缺失时 verify 直接失败，plugincenter 判定
"force uninstall"：删掉 /home/<用户>/plugin/<key>/ 并从注册表移除条目——
表现就是服务还在跑、客户端应用列表里插件消失。

本脚本按同一算法补齐结构。**必须在 UI 文件全部就位后最后执行**：
abstract 覆盖 src/ 下所有文件，之后任何改动都会让校验失败。

（算法与目录约定来自同仓库的 community-store 安装器，保持一致。）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

SUBDIRS = ('etc', 'var', 'tmp', 'scripts')


def compute_abstract(src_dir: Path) -> str:
    """复刻 plugin.sh：逐个文件 sha256 → 每行一个摘要 → 再整体 sha256。"""
    files = sorted((path for path in src_dir.rglob('*') if path.is_file()),
                   key=lambda path: str(path).encode('utf-8'))
    lines: list[str] = []
    for path in files:
        digest = hashlib.sha256()
        with path.open('rb') as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(block)
        lines.append(digest.hexdigest())
    combined = hashlib.sha256(''.join(f'{line}\n' for line in lines).encode('ascii'))
    return combined.hexdigest()


def write_control(scripts_dir: Path, service: str, source: Path | None) -> None:
    scripts_dir.mkdir(parents=True, exist_ok=True)
    target = scripts_dir / 'control'
    if source is not None and source.is_file():
        body = source.read_text(encoding='utf-8')
    else:
        body = (f'#!/bin/sh\n'
                f'log() {{ logger -t "plugin" "[nasconsole] control: $*"; }}\n'
                f'log "do $1"\n'
                f'case "$1" in\n'
                f'enable)  systemctl start {service} 2>/dev/null ;;\n'
                f'disable) systemctl stop  {service} 2>/dev/null ;;\n'
                f'esac\n'
                f'exit 0\n')
    if not (target.is_file() and target.read_text(encoding='utf-8') == body):
        target.write_text(body, encoding='utf-8')
    target.chmod(0o755)
    hotplug = scripts_dir / 'hotplug'
    if not hotplug.is_file():
        hotplug.write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
        hotplug.chmod(0o755)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--user', required=True, help='小米用户 ID，如 u3943892')
    parser.add_argument('--name', default='nasconsole', help='插件 key')
    parser.add_argument('--service', default='xiaomi-nas-console.service')
    parser.add_argument('--title', default='控制台')
    parser.add_argument('--plugin-id', type=int, default=11020)
    parser.add_argument('--version', default='0.1.0')
    parser.add_argument('--desc', default='硬件状态、硬盘、容器与服务；可浏览和整理文件（删除进回收站）')
    parser.add_argument('--tags', default='tool,monitoring')
    parser.add_argument('--control', default='')
    parser.add_argument('--home', default='')
    parser.add_argument('--quiet', action='store_true')
    args = parser.parse_args()

    home_dir = Path(args.home) if args.home else Path(f'/home/{args.user}/plugin/{args.name}')
    src_dir = home_dir / 'src'
    if not src_dir.is_dir():
        raise SystemExit(f'缺少 UI 目录：{src_dir}')
    for name in SUBDIRS:
        (home_dir / name).mkdir(parents=True, exist_ok=True)
    write_control(home_dir / 'scripts', args.service, Path(args.control) if args.control else None)

    abstract = compute_abstract(src_dir)
    info_path = home_dir / 'INFO'
    info = {
        'plugin': args.name,
        'name': args.title,
        'id': args.plugin_id,
        'version': args.version,
        'desc': args.desc,
        'tags': [tag for tag in args.tags.split(',') if tag],
        'developer': 'Kingwell Community',
        'publisher': 'community',
        'system': False,
        'type': 'standard',
        'size': 0,
        'ext': {},
        'forceupgrade': False,
        'timestamp': int(time.time()),
        'abstract': abstract,
    }
    info_path.write_text(json.dumps(info, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    info_path.chmod(0o644)

    # verify 以该用户身份执行，属主不对会读不到文件
    try:
        subprocess.run(['chown', '-R', f'{args.user}:{args.user}', str(home_dir)],
                       check=False, capture_output=True)
    except OSError as error:
        print(f'  ! chown 失败：{error}', file=sys.stderr)

    if not args.quiet:
        print(f'已补齐 {home_dir}  abstract={abstract[:16]}…')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as error:                                              # noqa: BLE001
        print(f'native-layout 失败：{error}', file=sys.stderr)
        raise SystemExit(1)
