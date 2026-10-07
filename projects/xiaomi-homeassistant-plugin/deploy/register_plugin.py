#!/usr/bin/env python3
"""把「Home Assistant」注册进小米客户端的插件注册表 /data/plugin/u<user>.list。

与「控制台」/「Unifi」插件同源（算法与条目结构完全一致），只是 key / 名称 /
编号 / 端口 / 描述不同。生成的条目与商店安装器（storelib._registry_record）
写出来的结构保持兼容：registry 块 + status/install/enable + info 里的
plugin/name/id/version/port 等字段。

注意：frontend.title 里**不得出现任何 __XXX__ 占位符**——注册表是原样使用的。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

PLUGIN_KEY = 'homeassistant'


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


def plugin_record(plugin_id: int, version: str, now: int, port: str) -> dict[str, Any]:
    return {
        'status': 'running',
        'install': True,
        'enable': True,
        'icon': '/icon/homeassistant.icon?v=1',
        'frontend': {
            'title': 'Home Assistant',
            'desc': '家庭自动化',
            'type': 'url',
            'permission': ['admin'],
            'dev_type': [1, 2, 3, 4],
            'url': [
                {'dev_type': [1], 'url': '/index.html#/homeassistantHome_app'},
                {'dev_type': [2, 3, 4], 'url': '/index.html#/homeassistantHome_pc'},
            ],
            'sortid': plugin_id,
            'widget': [],
        },
        'info': {
            'plugin': PLUGIN_KEY,
            'name': 'Home Assistant',
            'id': plugin_id,
            'version': version,
            'tags': ['tool', 'home'],
            'desc': '开源家庭自动化平台；配置目录持久化，端口 8123 对局域网开放',
            'developer': 'community',
            'publisher': 'community',
            'system': False,
            'port': str(port),
            'type': 'standard',
            'ext': {'admin': True},
        },
        'timestamp': now,
        'changetime': now,
    }


def assert_placeholders_absent(record: dict[str, Any]) -> None:
    """注册表条目里不许残留 __XXX__ 占位符（商店是原样安装的）。"""
    import re

    blob = json.dumps(record, ensure_ascii=False)
    found = re.findall(r'__[A-Z0-9_]+__', blob)
    if found:
        raise RuntimeError('注册表条目里还有未替换的占位符：' + '、'.join(sorted(set(found))))


def assert_id_available(registry: dict[str, Any], plugin_id: int) -> None:
    for key, record in registry.items():
        if key == PLUGIN_KEY or not isinstance(record, dict):
            continue
        info = record.get('info')
        if isinstance(info, dict) and str(info.get('id')) == str(plugin_id):
            raise RuntimeError(f'插件编号 {plugin_id} 已被 {info.get("name") or key}（{key}）占用')


def write_registry(path: Path, registry: dict[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + '.homeassistant.tmp')
    temporary.write_text(json.dumps(registry, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.chmod(temporary, 0o644)
    temporary.replace(path)
    os.chmod(path, 0o644)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='注册 Home Assistant 插件')
    parser.add_argument('--user', required=True, help='小米用户 ID，如 u3943892')
    parser.add_argument('--plugin-id', type=int, default=11022)
    parser.add_argument('--version', default='0.1.0')
    parser.add_argument('--port', default='18200', help='插件服务端口（写进注册表 info.port）')
    parser.add_argument('--registry-root', default='/data/plugin')
    parser.add_argument('--quiet', action='store_true')
    args = parser.parse_args(argv)

    registry_path = Path(args.registry_root) / f'{args.user}.list'
    registry = load_registry(registry_path)
    assert_id_available(registry, args.plugin_id)
    record = plugin_record(args.plugin_id, args.version, int(time.time()), args.port)
    assert_placeholders_absent(record)
    registry[PLUGIN_KEY] = record
    if registry_path.exists():
        backup = registry_path.with_suffix(registry_path.suffix + '.homeassistant.bak')
        backup.write_bytes(registry_path.read_bytes())
    write_registry(registry_path, registry)
    if not args.quiet:
        print(f'已注册 {PLUGIN_KEY}（编号 {args.plugin_id}）到 {registry_path}')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:                                              # noqa: BLE001
        print(f'register-plugin 失败：{error}', file=sys.stderr)
        raise SystemExit(1)
