#!/usr/bin/env python3
"""把「路由器软件中心」注册进小米客户端的插件注册表 /data/plugin/u<user>.list。

与「控制台」插件同源，只是 key / 名称 / 编号 / 描述不同。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

PLUGIN_KEY = 'rtrcenter'


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
        'icon': '/icon/router-center.icon?v=1',
        'frontend': {
            'title': '路由器软件中心',
            'desc': '远程管理 UniFi 软件中心',
            'type': 'url',
            'permission': ['admin'],
            'dev_type': [1, 2, 3, 4],
            'url': [
                {'dev_type': [1], 'url': '/index.html#/routerCenter_app'},
                {'dev_type': [2, 3, 4], 'url': '/index.html#/routerCenter_pc'},
            ],
            'sortid': plugin_id,
            'widget': [],
        },
        'info': {
            'plugin': PLUGIN_KEY,
            'name': '路由器软件中心',
            'id': plugin_id,
            'version': version,
            'tags': ['tool', 'network'],
            'desc': '把局域网里 UniFi SoftCenter 搬进小米存储，手机 App 远程管理路由器插件',
            'developer': 'Kingwell Community',
            'publisher': 'community',
            'system': False,
            'port': '18101',
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
    temporary = path.with_suffix(path.suffix + '.rtrcenter.tmp')
    temporary.write_text(json.dumps(registry, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.chmod(temporary, 0o644)
    temporary.replace(path)
    os.chmod(path, 0o644)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='注册路由器软件中心插件')
    parser.add_argument('--user', required=True, help='小米用户 ID，如 u3943892')
    parser.add_argument('--plugin-id', type=int, default=11021)
    parser.add_argument('--version', default='0.1.0')
    parser.add_argument('--registry-root', default='/data/plugin')
    parser.add_argument('--quiet', action='store_true')
    args = parser.parse_args(argv)

    registry_path = Path(args.registry_root) / f'{args.user}.list'
    registry = load_registry(registry_path)
    assert_id_available(registry, args.plugin_id)
    registry[PLUGIN_KEY] = plugin_record(args.plugin_id, args.version, int(__import__('time').time()))
    if registry_path.exists():
        backup = registry_path.with_suffix(registry_path.suffix + '.rtrcenter.bak')
        backup.write_bytes(registry_path.read_bytes())
    write_registry(registry_path, registry)
    if not args.quiet:
        print(f'已注册 {PLUGIN_KEY}（编号 {args.plugin_id}）到 {registry_path}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
