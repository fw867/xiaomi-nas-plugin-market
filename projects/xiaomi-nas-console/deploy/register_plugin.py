#!/usr/bin/env python3
"""只把「控制台」注册进小米用户插件注册表，不动其它条目。

为什么要写这个文件：小米客户端的「全部应用」读的是 /data/plugin/<用户>.list
（一个 JSON 文件）。缺失条目时插件服务照跑，但客户端里看不到入口。
写入前会备份原注册表，插件编号冲突时拒绝执行而不是覆盖别人的条目。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

PLUGIN_KEY = 'nasconsole'


def load_registry(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f'无法读取插件注册表：{error}') from error
    if not isinstance(value, dict):
        raise RuntimeError('插件注册表必须是 JSON 对象')
    return value


def plugin_record(plugin_id: int, version: str, now: int) -> dict[str, Any]:
    return {
        'status': 'running',
        'install': True,
        'enable': True,
        'icon': '/icon/nas-console.icon?v=1',
        'frontend': {
            'title': '控制台',
            'desc': '硬件状态、文件、容器与服务',
            'type': 'url',
            'permission': ['admin'],
            'dev_type': [1, 2, 3, 4],
            'url': [
                {'dev_type': [1], 'url': '/index.html#/nasConsole_app'},
                {'dev_type': [2, 3, 4], 'url': '/index.html#/nasConsole_pc'},
            ],
            'sortid': plugin_id,
            'widget': [],
        },
        'info': {
            'plugin': PLUGIN_KEY,
            'name': '控制台',
            'id': plugin_id,
            'version': version,
            'tags': ['tool', 'monitoring'],
            'desc': '硬件状态、硬盘、容器与服务；可浏览和整理文件（删除进回收站）',
            'developer': 'Kingwell Community',
            'publisher': 'community',
            'system': False,
            'port': '18100',
            'type': 'standard',
            'ext': {'admin': True},
        },
        'timestamp': now,
        'changetime': now,
    }


def assert_id_available(registry: dict[str, Any], plugin_id: int) -> None:
    for key, record in registry.items():
        if key == PLUGIN_KEY or not isinstance(record, dict):
            continue
        info = record.get('info')
        if isinstance(info, dict) and str(info.get('id')) == str(plugin_id):
            raise RuntimeError(f'插件编号 {plugin_id} 已被 {info.get("name") or key}（{key}）占用')


def write_registry(path: Path, registry: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + '.nasconsole.tmp')
    temporary.write_text(json.dumps(registry, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.chmod(temporary, 0o644)
    temporary.replace(path)
    os.chmod(path, 0o644)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--user-id', required=True)
    parser.add_argument('--plugin-id', type=int, default=11020)
    parser.add_argument('--version', default='0.1.0')
    parser.add_argument('--registry', type=Path)
    arguments = parser.parse_args()
    if arguments.plugin_id < 1:
        raise RuntimeError('插件编号必须是正整数')
    registry_path = arguments.registry or Path(f'/data/plugin/{arguments.user_id}.list')
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry = load_registry(registry_path)
    assert_id_available(registry, arguments.plugin_id)
    if registry_path.exists():
        backup = registry_path.with_name(
            f'{registry_path.name}.before-nasconsole-{int(time.time())}.bak')
        backup.write_bytes(registry_path.read_bytes())
        os.chmod(backup, 0o600)
    registry[PLUGIN_KEY] = plugin_record(arguments.plugin_id, arguments.version, int(time.time()))
    write_registry(registry_path, registry)
    print(f'已注册 {PLUGIN_KEY}（编号 {arguments.plugin_id}）到 {registry_path}')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except RuntimeError as error:
        print(f'控制台注册被拒绝：{error}', file=sys.stderr)
        raise SystemExit(1)
