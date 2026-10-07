import contextlib
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from engine import (
    Engine, Error, IMAGE, IMAGE_STABLE, IMAGE_LEGACY, IMAGE_LEGACY_TAG, IMAGE_STABLE_TAG,
    NAME, LABEL, PORT, NETWORK_MODE, LAYER_ERROR_LIMIT, ZSTD_MIN_DOCKER,
    choose_image, compare_versions, container_config, confined, docker_supports_zstd,
    image_candidates, image_version_label, installed_version, layer_error,
    parse_docker_version, parse_roots, port_is_listening, pull_error_summary,
    read_ha_version, root_label, service_address, summarize_docker_error,
    VERSION, MEMORY_LIMIT, CPU_LIMIT, PORT_GRACE_SECONDS, PORT_GRACE_POLL,
)
from server import Server


def docker_stub(method, path, body=None, timeout=30):
    """Docker API 桩：容器不存在（404），其余视为成功。"""
    if path.endswith('/json'):
        return 404, b'{}'
    return 200, b'{}'


class FakeDocker:
    """有状态的 Docker API 桩：记住本插件容器与每次调用，供容器重建类用例断言。

    fail_create 表示前 N 次创建容器失败（用来验证重建失败后的回滚）。
    """

    def __init__(self, fail_create=0):
        self.calls = []
        self.container = None
        self.fail_create = fail_create

    def __call__(self, method, path, body=None, timeout=30):
        self.calls.append((method, path, body))
        if path == '/containers/' + NAME + '/json':
            if self.container is None:
                return 404, b'{}'
            return 200, json.dumps(self.container).encode()
        if path.startswith('/containers/create?name=' + NAME):
            if self.fail_create > 0:
                self.fail_create -= 1
                return 500, b'{"message":"boom"}'
            self.container = {
                'Config': {'Labels': body['Labels'], 'Env': body['Env'],
                           'Healthcheck': body['Healthcheck']},
                'State': {'Running': False},
                'HostConfig': body['HostConfig'],
                'Mounts': body['HostConfig']['Mounts'],
            }
            return 201, b'{}'
        if path == '/containers/' + NAME + '/start':
            if self.container is None:
                return 404, b'{}'
            self.container['State']['Running'] = True
            return 204, b''
        if path.startswith('/containers/' + NAME + '/stop'):
            if self.container is not None:
                self.container['State']['Running'] = False
            return 204, b''
        if path == '/containers/' + NAME and method == 'DELETE':
            self.container = None
            return 204, b''
        return 200, b'{}'

    def created_mounts(self):
        """最近一次创建容器用的 bind 源（{容器内路径: 宿主机路径}）。"""
        bodies = [body for _, path, body in self.calls if path.startswith('/containers/create')]
        assert bodies, '没有创建过容器'
        return {item['Target']: item['Source'] for item in bodies[-1]['HostConfig']['Mounts']}

    def created_bodies(self):
        return [body for _, path, body in self.calls if path.startswith('/containers/create')]

    def removed(self):
        return any(method == 'DELETE' and path == '/containers/' + NAME
                   for method, path, _ in self.calls)


@contextmanager
def nas_owned():
    """模拟「目录由普通 NAS 用户拥有」的环境。

    Windows 上 os.stat 的 st_uid/st_gid 恒为 0（初始化会被直接拒绝），os.chown
    也不存在；测试要在开发机上跑通初始化流程，所以这里临时把身份号换成 1000，
    并让私有用例里的 chown 不炸。
    """
    real_stat = Path.stat

    def stat(self, *, follow_symlinks=True):
        item = real_stat(self, follow_symlinks=follow_symlinks)
        return os.stat_result((item.st_mode, item.st_ino, item.st_dev, item.st_nlink,
                               1000, 1000, item.st_size, item.st_atime, item.st_mtime,
                               item.st_ctime))

    with patch.object(Path, 'stat', stat), patch('engine.os.chown', create=True, return_value=None):
        yield


def fresh_container_item(**overrides):
    """一个「现在的插件建的、已经跑着」的容器对象（供 start() 分支用例改造）。"""
    item = {
        'Config': {'Labels': {LABEL: 'tok'}, 'Healthcheck': {'Test': ['NONE']},
                   'Env': ['TZ=Asia/Shanghai']},
        'HostConfig': {'NetworkMode': NETWORK_MODE, 'Memory': MEMORY_LIMIT,
                       'MemorySwap': MEMORY_LIMIT, 'NanoCpus': CPU_LIMIT},
        'State': {'Running': True},
    }
    item.update(overrides)
    return item


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'root'
        self.root.mkdir()
        (self.root / 'HA').mkdir()
        self.engine = Engine(Path(self.tmp.name) / 'private', self.root)
        # snapshot() 会解析镜像、进而查 /version：给一个固定的守护进程版本，
        # 免得每条用例都去碰真的 Docker socket（本机没有 AF_UNIX）。
        patcher = patch.object(Engine, 'daemon_version', return_value='20.10.17')
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_image_and_identity(self):
        self.assertTrue(IMAGE.startswith('ghcr.io/home-assistant/home-assistant:'))
        self.assertEqual(IMAGE, 'ghcr.io/home-assistant/home-assistant:stable')
        self.assertEqual(NAME, 'xiaomi-plugin-homeassistant')
        self.assertEqual(LABEL, 'io.xiaomi-plugin.homeassistant.owner')
        # 容器用 host 网络：8123 就是宿主机自己的 Web UI / API 端口
        self.assertEqual(PORT, 8123)
        self.assertEqual(NETWORK_MODE, 'host')

    def test_confined_rejects_traversal(self):
        self.assertEqual(confined(self.root, 'HA'), self.root / 'HA')
        for name in ['../', '/etc', 'HA/..', '.hidden']:
            with self.subTest(name=name), self.assertRaises(Error):
                confined(self.root, name)

    def test_container_uses_host_network_and_no_port_publishing(self):
        cfg = container_config({
            'owner': 'tok', 'uid': 1000, 'gid': 1000,
            'config': '/nas/ha',
        })
        self.assertEqual(cfg['Image'], IMAGE)
        # Home Assistant 官方容器要求以 root 运行：这里刻意不设 User
        self.assertNotIn('User', cfg)
        self.assertEqual(cfg.get('User', ''), '')
        self.assertEqual(cfg['Labels'], {LABEL: 'tok'})
        host = cfg['HostConfig']
        self.assertEqual(host['NetworkMode'], 'host')
        # host 模式下不做端口发布：这两个键一个都不能出现
        self.assertNotIn('PortBindings', host)
        self.assertNotIn('ExposedPorts', cfg)
        # 只挂一路：配置目录 → /config；没有媒体/缓存挂载
        mounts = {m['Target']: m['Source'] for m in host['Mounts']}
        self.assertEqual(mounts, {'/config': '/nas/ha'})
        self.assertNotIn('Privileged', host)
        self.assertFalse(any('docker.sock' in s for s in mounts.values()))
        self.assertEqual(host['Memory'], MEMORY_LIMIT)
        self.assertEqual(host['MemorySwap'], MEMORY_LIMIT)      # 与 Memory 相同 = 不给 swap
        self.assertEqual(host['NanoCpus'], CPU_LIMIT)
        self.assertEqual(host['PidsLimit'], 512)
        self.assertEqual(host['RestartPolicy'], {'Name': 'no'})
        self.assertEqual(host['SecurityOpt'], ['no-new-privileges:true'])
        self.assertEqual(cfg['Env'], ['TZ=Asia/Shanghai'])

    def test_service_address_is_host_port_8123(self):
        self.assertEqual(service_address('192.168.1.30'), 'http://192.168.1.30:8123')
        self.assertEqual(service_address('nas.local'), 'http://nas.local:8123')

    def test_port_is_listening_uses_localhost_tcp(self):
        """host 模式下判断「容器的 8123 有没有真的提供出来」＝看宿主机端口在不在听。

        用真的 socket 验一次：起一个临时监听 → 判定为 True，关掉 → 判定为 False。
        """
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        free_port = listener.getsockname()[1]
        try:
            self.assertTrue(port_is_listening(port=free_port))
        finally:
            listener.close()
        self.assertFalse(port_is_listening(port=free_port))       # 没人监听了
        # 连不上（抛 OSError）也是「没监听」，不能把异常抛给调用方
        with patch('engine.socket.socket') as probe:
            probe.return_value.__enter__.return_value.connect_ex.side_effect = OSError('refused')
            self.assertFalse(port_is_listening())

    def test_snapshot_before_setup(self):
        state = self.engine.snapshot()
        self.assertFalse(state['configured'])
        self.assertFalse(state['running'])
        self.assertFalse(state['ready'])
        self.assertEqual(state['port'], 8123)
        self.assertEqual(state['config_abs'], '')
        self.assertEqual(state['clientVersion'], '')
        self.assertEqual(state['version'], installed_version())

    def test_installed_version_fallback(self):
        with patch('engine.__file__',
                   '/data/plugin/homeassistant/releases/0.1.0-1789828016-8538/engine.py'):
            self.assertEqual(installed_version(), '0.1.0')
        self.assertEqual(installed_version(), VERSION)

    def test_dev_cannot_start(self):
        self.engine.dev = True
        with self.assertRaises(Error):
            self.engine.launch('start', {})

    def test_setup_rejects_non_string_config(self):
        with patch('engine.docker_api', side_effect=docker_stub):
            with self.assertRaises(Error):
                self.engine.setup({'bad': 'type'})

    def test_setup_with_blank_config_uses_private_dir(self):
        """留空＝配置放在插件私有目录：不占用户存储，也不属于任何存储位置。"""
        with nas_owned(), patch('engine.docker_api', side_effect=docker_stub):
            self.engine.setup('')
            state = self.engine.snapshot()
        config = json.loads(self.engine.cfgfile.read_text(encoding='utf-8'))
        self.assertEqual(config['config'], str(self.engine.data / 'config'))
        self.assertEqual(config['config_relative'], '')
        self.assertEqual(config['config_root'], '')
        self.assertTrue(state['config_private'])
        self.assertTrue((self.engine.data / 'config').is_dir())

    def test_foreign_container_not_stopped(self):
        self.engine.config = {'owner': 'mine'}
        foreign = json.dumps({'Config': {'Labels': {LABEL: 'other'}}}).encode('utf-8')
        with patch('engine.docker_api', return_value=(200, foreign)) as api:
            with self.assertRaises(Error):
                self.engine.stop()
        self.assertEqual(api.call_count, 1)

    def test_stop_remembers_preference(self):
        self.engine.config = {'owner': 'mine', 'enabled': True}
        with patch.object(self.engine, 'owned', return_value={'State': {'Running': True}}), \
                patch('engine.docker_api', return_value=(204, b'')) as api:
            self.engine.stop()
        self.assertEqual(api.call_args.args[1], '/containers/' + NAME + '/stop?t=30')
        self.assertFalse(json.loads(self.engine.cfgfile.read_text())['enabled'])


class HaVersionTests(unittest.TestCase):
    """版本显示优先读用户配置目录里的 .HA_VERSION（Home Assistant 自己写的）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.root = self.base / 'pool'
        self.folder = self.root / 'ha'
        self.folder.mkdir(parents=True)
        self.engine = Engine(self.base / 'data', self.root)
        self.engine.config = {
            'owner': 'tok', 'enabled': True, 'uid': 1000, 'gid': 1000,
            'config_root': str(self.root), 'config': str(self.folder),
            'config_relative': 'ha', 'config_device': 1, 'config_inode': 1,
        }
        # 同 EngineTests：snapshot() 会解析镜像，先把守护进程版本钉住
        patcher = patch.object(Engine, 'daemon_version', return_value='20.10.17')
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_read_ha_version_strips_and_handles_missing(self):
        self.assertEqual(read_ha_version(self.folder), '')
        (self.folder / '.HA_VERSION').write_text('2025.1.2\n', encoding='utf-8')
        self.assertEqual(read_ha_version(self.folder), '2025.1.2')
        (self.folder / '.HA_VERSION').write_text('  2025.1.2  \n\n', encoding='utf-8')
        self.assertEqual(read_ha_version(self.folder), '2025.1.2')
        self.assertEqual(read_ha_version(self.folder / 'nope'), '')

    def test_read_ha_version_is_bounded(self):
        (self.folder / '.HA_VERSION').write_text('x' * 200, encoding='utf-8')
        self.assertEqual(len(read_ha_version(self.folder)), 32)

    def test_snapshot_reports_client_version_from_config_dir(self):
        (self.folder / '.HA_VERSION').write_text('2025.1.2', encoding='utf-8')
        with patch.object(self.engine, 'owned', return_value=None):
            state = self.engine.snapshot()
        self.assertEqual(state['clientVersion'], '2025.1.2')
        self.assertEqual(state['config_abs'], str(self.folder))
        self.assertFalse(state['config_private'])
        self.assertTrue(state['healthcheckOff'] is False)

    def test_snapshot_client_version_empty_while_busy(self):
        """操作进行中不去碰磁盘/容器，版本字段留空由前端退回镜像口径。"""
        (self.folder / '.HA_VERSION').write_text('2025.1.2', encoding='utf-8')
        self.engine.busy = True
        with patch.object(self.engine, 'owned', return_value=None):
            state = self.engine.snapshot()
        self.assertEqual(state['clientVersion'], '')
        self.assertTrue(state['busy'])

    def test_snapshot_reports_healthcheck_flag(self):
        self.engine.config['healthcheck_off'] = True
        with patch.object(self.engine, 'owned', return_value=None):
            self.assertTrue(self.engine.snapshot()['healthcheckOff'])
            self.engine.config['healthcheck_off'] = False
            self.assertFalse(self.engine.snapshot()['healthcheckOff'])

    def test_snapshot_ready_uses_manifest_probe(self):
        running = {'Config': {'Labels': {LABEL: 'tok'}}, 'State': {'Running': True}}
        with patch.object(self.engine, 'owned', return_value=running), \
                patch('engine.homeassistant_info', return_value={'name': 'Home Assistant'}):
            self.assertTrue(self.engine.snapshot()['ready'])
        with patch.object(self.engine, 'owned', return_value=running), \
                patch('engine.homeassistant_info', side_effect=Error('未就绪')):
            self.assertFalse(self.engine.snapshot()['ready'])


class HealthcheckTests(unittest.TestCase):
    """容器健康检查必须关掉：官方镜像每 30 秒打一次 HTTP 探针，让 Home Assistant
    重写 SQLite 的 -shm/-wal，再经 fanotify 触发系统索引写 /nas/sys（跨两块盘的
    RAID1），两块机械盘因此永远不休眠。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'root'
        self.root.mkdir()
        (self.root / 'HA').mkdir()
        self.engine = Engine(Path(self.tmp.name) / 'private', self.root)
        self.engine.config = {
            'owner': 'tok', 'enabled': True, 'uid': 1000, 'gid': 1000,
            'config_root': str(self.root), 'config': str(self.root / 'HA'),
            'config_relative': 'HA', 'config_device': 1, 'config_inode': 1,
        }

    def test_new_container_is_created_without_healthcheck(self):
        cfg = container_config(self.engine.config)
        self.assertEqual(cfg['Healthcheck'], {'Test': ['NONE']})

    def test_inherited_healthcheck_detection(self):
        cases = [
            ({'Config': {}}, False),
            ({'Config': {'Healthcheck': None}}, False),
            ({'Config': {'Healthcheck': {'Test': []}}}, False),
            ({'Config': {'Healthcheck': {'Test': ['NONE']}}}, False),
            ({'Config': {'Healthcheck': {'Test': ['none']}}}, False),
            ({'Config': {'Healthcheck': {'Test': ['CMD-SHELL', 'curl x']}}}, True),
        ]
        for item, expected in cases:
            with self.subTest(item=item):
                self.assertEqual(Engine.inherited_healthcheck(item), expected)

    def _run_start(self, item, pull=False, listening=True, owned_callable=None,
                   pull_callable=None):
        """跑一遍 start()，返回 Docker 调用序列。

        pull=False 时把「拉镜像」打桩掉（只为验证重建/启动路径）；要验证
        「容器不存在就先拉镜像」的用例传 pull=True，让它真的走 docker_api。
        listening 是 host 网络下的端口自检结果：传单个布尔值就一直用这个结果，
        传列表就按调用顺序依次给（例如 [False, True]＝重建前没监听、重建后好了）；
        容器已经跑着时根本不会查它。
        owned_callable 用来接管 owned()：例如按调用顺序依次返回两个不同的容器
        对象（起手停着、自检重建时已经起来）。不传就用 item 作固定返回值。
        pull_callable 用来接管 pull_with_fallback()（只验证重建路径时用）。
        """
        calls = []
        results = list(listening) if isinstance(listening, (list, tuple)) else [listening]
        cursor = {'index': 0}

        def fake_api(method, path, body=None, timeout=30):
            calls.append((method, path, body))
            return 200, b'{}'

        def fake_listening(*args, **kwargs):
            # 依次给出序列里的结果，用完就沿用最后一个：宽限期里会问很多次，
            # 不能因为次数比预期多就抛 StopIteration。
            index = min(cursor['index'], len(results) - 1)
            cursor['index'] += 1
            return results[index]

        owned = owned_callable if owned_callable is not None else (lambda *a, **k: item)
        patches = [patch.object(self.engine, 'owned', side_effect=owned),
                   patch.object(self.engine, 'check_directories'),
                   patch('engine.homeassistant_info', return_value={}),
                   patch('engine.port_is_listening', side_effect=fake_listening),
                   patch('engine.time.sleep'),
                   patch('engine.docker_api', side_effect=fake_api)]
        if not pull:
            stub = pull_callable if pull_callable is not None else (lambda *a, **k: IMAGE_STABLE)
            patches.append(patch.object(self.engine, 'pull_with_fallback', side_effect=stub))
        with contextlib.ExitStack() as stack:
            for item_patch in patches:
                stack.enter_context(item_patch)
            self.engine.start()
        return calls

    def test_start_recreates_container_that_inherited_healthcheck(self):
        item = fresh_container_item(
            Config={'Labels': {LABEL: 'tok'}, 'Healthcheck': {'Test': ['CMD-SHELL', 'curl x']}})
        calls = self._run_start(item)
        paths = [path for _, path, _ in calls]
        self.assertIn('/containers/' + NAME + '/stop?t=30', paths)
        self.assertIn('/containers/' + NAME, paths)                       # DELETE
        self.assertIn('/containers/create?name=' + NAME, paths)
        self.assertIn('/containers/' + NAME + '/start', paths)
        # 必须先删再建，否则 create 会撞名
        self.assertLess(paths.index('/containers/' + NAME + '/stop?t=30'),
                        paths.index('/containers/create?name=' + NAME))
        create = next(body for _, path, body in calls if path.startswith('/containers/create'))
        self.assertEqual(create['Healthcheck'], {'Test': ['NONE']})
        settings = json.loads(self.engine.cfgfile.read_text(encoding='utf-8'))
        self.assertTrue(settings['healthcheck_off'])
        self.assertTrue(settings['enabled'])

    def test_start_does_not_touch_container_without_healthcheck(self):
        calls = self._run_start(fresh_container_item())
        paths = [path for _, path, _ in calls]
        self.assertEqual(paths, [])                                       # 运行中就不该有 Docker 调用
        self.assertTrue(json.loads(self.engine.cfgfile.read_text(encoding='utf-8'))['healthcheck_off'])

    def test_start_starts_stopped_container_without_recreating(self):
        calls = self._run_start(fresh_container_item(State={'Running': False}))
        paths = [path for _, path, _ in calls]
        self.assertEqual(paths, ['/containers/' + NAME + '/start'])
        self.assertEqual(paths.count('/containers/create?name=' + NAME), 0)

    def test_network_stale_detection(self):
        """网络模式只在创建时生效：桥接 + 端口映射建的旧容器必须重建。"""
        good = {'HostConfig': {'NetworkMode': NETWORK_MODE}}
        cases = [
            (good, False),
            ({'HostConfig': {'NetworkMode': 'host'}}, False),
            ({'HostConfig': {'NetworkMode': 'bridge'}}, True),
            ({'HostConfig': {}}, True),                # 老容器：等于 Docker 的默认 bridge
            ({'HostConfig': {'NetworkMode': None}}, True),
        ]
        for item, expected in cases:
            with self.subTest(item=item):
                self.assertEqual(Engine.network_stale(item), expected)

    def test_missing_network_mode_counts_as_stale(self):
        """「老容器没有 NetworkMode 键」必须判为过期，否则桥接容器永远不会被换掉。"""
        self.assertTrue(Engine.network_stale({'HostConfig': {}}))

    def test_docker_version_parsing_and_comparison(self):
        """版本号必须按数字元组比，不能按字符串比（20.10.17 vs 23.0.0）。"""
        cases = {
            '20.10.17': (20, 10, 17),
            '22.06.0': (22, 6, 0),
            '23.0.0': (23, 0, 0),
            '24': (24,),
            '26.1': (26, 1),
            'v23.0.1': (23, 0, 1),
            '26.1.0-rc1+build.5': (26, 1, 0),
            '  20.10.17  ': (20, 10, 17),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_docker_version(text), expected)
        for text in ('', None, 'unknown', 'docker'):
            with self.subTest(text=text):
                self.assertEqual(parse_docker_version(text), ())

        self.assertLess(parse_docker_version('20.10.17'), parse_docker_version('23.0.0'))
        self.assertLess(parse_docker_version('22.06.0'), parse_docker_version('23.0.0'))
        # 字符串比较在这里会给错（'9.0' > '20.0'），数字元组比较必须给对
        self.assertLess(parse_docker_version('9.0'), parse_docker_version('20.0'))
        self.assertEqual(compare_versions(parse_docker_version('26.1'),
                                          parse_docker_version('26.1.0')), 0)
        self.assertEqual(compare_versions(parse_docker_version('24'),
                                          parse_docker_version('24.0.0')), 0)
        self.assertEqual(compare_versions(parse_docker_version('23.0.0'),
                                          parse_docker_version('23.0.0')), 0)
        self.assertGreater(compare_versions(parse_docker_version('26.1'),
                                            parse_docker_version('24')), 0)
        self.assertLess(compare_versions(parse_docker_version('22.06.0'),
                                         parse_docker_version('23.0.0')), 0)

    def test_docker_supports_zstd_guard(self):
        """zstd 层要 Docker 23+；解析不出守护进程版本时按不支持处理（宁可先钉旧 tag）。"""
        for version, expected in (('20.10.17', False), ('22.06.0', False), ('23.0.0', True),
                                  ('24', True), ('26.1', True), ('', False), ('unknown', False)):
            with self.subTest(version=version):
                self.assertEqual(docker_supports_zstd(version), expected)

    def test_choose_image_by_daemon_capability(self):
        """daemon < 23 → 固定 2026.2.0；daemon >= 23 → stable。stable 始终是首选。"""
        self.assertEqual(choose_image('20.10.17'), IMAGE_LEGACY)
        self.assertEqual(choose_image('22.06.0'), IMAGE_LEGACY)
        self.assertEqual(choose_image('23.0.0'), IMAGE_STABLE)
        self.assertEqual(choose_image('24'), IMAGE_STABLE)
        self.assertEqual(choose_image('26.1'), IMAGE_STABLE)
        self.assertEqual(choose_image(''), IMAGE_STABLE)        # 查不到就先试首选
        self.assertEqual(choose_image(None), IMAGE_STABLE)
        self.assertEqual(choose_image('unknown'), IMAGE_STABLE)
        self.assertEqual(image_candidates(''), [IMAGE_STABLE, IMAGE_LEGACY])
        self.assertEqual(IMAGE_LEGACY, 'ghcr.io/home-assistant/home-assistant:2026.2.0')
        self.assertEqual(IMAGE_STABLE, 'ghcr.io/home-assistant/home-assistant:stable')

    def test_image_candidates_order_and_fallback(self):
        """候选顺序跟着守护进程能力走，另一个作为兜底（只多试一次）。"""
        self.assertEqual(image_candidates('20.10.17'), [IMAGE_LEGACY, IMAGE_STABLE])
        self.assertEqual(image_candidates('23.0.0'), [IMAGE_STABLE, IMAGE_LEGACY])
        self.assertEqual(image_candidates(''), [IMAGE_STABLE, IMAGE_LEGACY])
        for version in ('20.10.17', '23.0.0'):
            with self.subTest(version=version):
                self.assertEqual(len(image_candidates(version)), 2)
                self.assertEqual(set(image_candidates(version)), {IMAGE_STABLE, IMAGE_LEGACY})

    def test_image_version_label(self):
        self.assertEqual(image_version_label(IMAGE_STABLE), 'stable（滚动标签）')
        self.assertIn('2026.2.0', image_version_label(IMAGE_LEGACY))
        self.assertIn('固定版本', image_version_label(IMAGE_LEGACY))

    def test_docker_error_summaries_are_bounded(self):
        """底层报错要带出来，但必须截断（不能整段塞进页面/日志）。"""
        long_text = 'failed to register layer: ' + 'x' * 5000
        summary = summarize_docker_error(long_text)
        self.assertLessEqual(len(summary), LAYER_ERROR_LIMIT + 1)
        self.assertTrue(summary.endswith('…'))
        self.assertIn('failed to register layer', summary)
        self.assertEqual(summarize_docker_error(None), '')
        self.assertEqual(summarize_docker_error(b'boom'), 'boom')
        self.assertEqual(summarize_docker_error('a\n\n b\tc'), 'a b c')

    def test_layer_error_detection(self):
        """厂商 dockerd 20.10 拉 zstd 层时的原话要能被认出来。"""
        real = ('failed to register layer: Error processing tar file(exit status 1): '
                'archive/tar: invalid tar header')
        self.assertTrue(layer_error(real))
        self.assertTrue(layer_error('unsupported media type application/vnd.oci.image.layer.v1.tar+zstd'))
        self.assertTrue(layer_error('Error processing tar file: zstd: unsupported'))
        self.assertFalse(layer_error('no such host'))
        self.assertFalse(layer_error(''))
        self.assertFalse(layer_error(None))

    def test_pull_error_summary_reads_the_stream(self):
        """Docker 把错误放在流式响应的最后一行 JSON 里。"""
        stream = (b'{"status":"Pulling fs layer"}\n'
                  b'{"status":"Downloading"}\n'
                  b'{"errorDetail":{"message":"failed to register layer: Error processing tar file'
                  b'(exit status 1): archive/tar: invalid tar header"},"error":"failed to register '
                  b'layer: Error processing tar file(exit status 1): archive/tar: invalid tar header"}\n')
        summary = pull_error_summary(stream)
        self.assertIn('failed to register layer', summary)
        self.assertIn('invalid tar header', summary)
        self.assertEqual(pull_error_summary(b'{"status":"Download complete"}\n'), '')
        self.assertEqual(pull_error_summary(b'not json at all'), '')

    def test_start_recreates_bridged_container(self):
        item = fresh_container_item(HostConfig={'NetworkMode': 'bridge', 'Memory': MEMORY_LIMIT,
                                                'MemorySwap': MEMORY_LIMIT, 'NanoCpus': CPU_LIMIT})
        calls = self._run_start(item)
        paths = [path for _, path, _ in calls]
        self.assertIn('/containers/' + NAME + '/stop?t=30', paths)
        self.assertIn('/containers/' + NAME, paths)
        create = next(body for _, path, body in calls if path.startswith('/containers/create'))
        self.assertEqual(create['HostConfig']['NetworkMode'], 'host')
        self.assertNotIn('PortBindings', create['HostConfig'])

    def test_start_repairs_container_whose_port_is_not_listening(self):
        """host 网络下容器在跑、宿主机 8123 一直没人听：等完宽限期就重建。

        用有状态的 Docker 桩，让 owned()/停/删/建/启 全部走真实路径：
        容器一开始是**停着**的（用户之前点过「停止服务」→ 重新 start 那条路），
        然后端口自检一直失败，于是必须真的停掉 → 删除 → 按 host 网络重建 → 再启动。
        端口桩模拟真实现场：重建之前一律 False，重建之后才 True。
        """
        docker = FakeDocker()
        docker.container = fresh_container_item(State={'Running': False})
        repaired = {'done': False}

        def probe(*args, **kwargs):
            return repaired['done']

        def fake_api(method, path, body=None, timeout=30):
            if path.startswith('/containers/create'):
                repaired['done'] = True          # 新容器起来后端口就有了
            return docker(method, path, body, timeout)

        with nas_owned(), patch('engine.docker_api', side_effect=fake_api), \
                patch.object(self.engine, 'check_directories'), \
                patch('engine.port_is_listening', side_effect=probe), \
                patch('engine.time.sleep'), \
                patch('engine.homeassistant_info', return_value={}):
            self.engine.start()
        paths = [path for _, path, _ in docker.calls]
        self.assertEqual(paths.count('/containers/' + NAME + '/start'), 2)
        self.assertEqual(paths.count('/containers/create?name=' + NAME), 1)
        self.assertIn('/containers/' + NAME + '/stop?t=30', paths)
        self.assertIn('/containers/' + NAME, paths)                       # DELETE
        self.assertLess(paths.index('/containers/' + NAME + '/stop?t=30'),
                        paths.index('/containers/create?name=' + NAME))
        create = next(body for _, path, body in docker.calls
                      if path.startswith('/containers/create'))
        self.assertEqual(create['HostConfig']['NetworkMode'], 'host')
        self.assertTrue(docker.container['State']['Running'])

    def test_start_reports_failure_when_port_never_listens(self):
        """重建之后 8123 还是没人听：如实报错（多半是端口被别的程序占了）。"""
        docker = FakeDocker()
        docker.container = fresh_container_item(State={'Running': False})
        with nas_owned(), patch('engine.docker_api', side_effect=docker), \
                patch.object(self.engine, 'check_directories'), \
                patch('engine.port_is_listening', return_value=False), \
                patch('engine.time.sleep'), \
                patch('engine.homeassistant_info', return_value={}):
            with self.assertRaises(Error) as caught:
                self.engine.start()
        self.assertIn('8123', str(caught.exception))
        self.assertIn('没有监听', str(caught.exception))
        self.assertTrue(docker.removed())                                 # 至少试过重建修复

    def test_start_does_not_query_port_when_container_is_already_running(self):
        """容器本来就在跑：这是「刷新/服务重启」路径，不该碰 Docker 也不该自检。"""
        calls = self._run_start(fresh_container_item())              # State.Running = True
        self.assertEqual(calls, [])
        with patch('engine.port_is_listening') as probe:
            self._run_start(fresh_container_item())
        self.assertFalse(probe.called)

    def test_start_pulls_image_when_container_is_missing(self):
        with nas_owned():
            calls = self._run_start(None, pull=True)
        paths = [path for _, path, _ in calls]
        # 镜像不在本地时先拉（/images/create），再建容器、再启动
        self.assertIn('/version', paths)                     # 先问守护进程版本再决定拉哪个
        self.assertTrue(any(path.startswith('/images/create?') for path in paths))
        self.assertIn('/containers/create?name=' + NAME, paths)
        self.assertIn('/containers/' + NAME + '/start', paths)


    def test_resources_stale_detection(self):
        """内存/CPU 上限只在创建时生效：和现在要求的不一致就得重建。"""
        good = {'HostConfig': {'Memory': MEMORY_LIMIT, 'MemorySwap': MEMORY_LIMIT,
                               'NanoCpus': CPU_LIMIT}}
        cases = [
            (good, False),
            ({'HostConfig': {'Memory': 1024 * 1024 * 1024, 'MemorySwap': 1024 * 1024 * 1024,
                             'NanoCpus': CPU_LIMIT}}, True),
            ({'HostConfig': {'Memory': MEMORY_LIMIT, 'MemorySwap': 1024 * 1024 * 1024,
                             'NanoCpus': CPU_LIMIT}}, True),          # swap 口径不一致也算
            ({'HostConfig': {'Memory': MEMORY_LIMIT, 'MemorySwap': MEMORY_LIMIT,
                             'NanoCpus': 10 ** 9}}, True),
            ({'HostConfig': {}}, True),                               # 老容器没有这些键
        ]
        for item, expected in cases:
            with self.subTest(item=item):
                self.assertEqual(Engine.resources_stale(item), expected)



    def test_start_recreates_container_with_old_memory_limit(self):
        item = fresh_container_item(
            HostConfig={'Memory': 1024 * 1024 * 1024, 'MemorySwap': 1024 * 1024 * 1024,
                        'NanoCpus': CPU_LIMIT})
        calls = self._run_start(item)
        paths = [path for _, path, _ in calls]
        self.assertIn('/containers/' + NAME + '/stop?t=30', paths)
        self.assertIn('/containers/' + NAME, paths)                   # 删掉旧容器
        self.assertIn('/containers/create?name=' + NAME, paths)       # 按新上限重建
        self.assertIn('/containers/' + NAME + '/start', paths)


    def test_stale_env_detection(self):
        """环境变量只在创建时生效：值不对、或残留已删除的键，都得重建。"""
        cases = [
            ({'Config': {}}, True),                                        # 老容器连 Env 都没有
            ({'Config': {'Env': ['TZ=Asia/Shanghai']}}, False),
            ({'Config': {'Env': ['TZ=UTC']}}, True),
            ({'Config': {'Env': ['PATH=/usr/bin', 'TZ=Asia/Shanghai']}}, False),
        ]
        for item, expected in cases:
            with self.subTest(item=item):
                self.assertEqual(Engine.stale_env(item), expected)


    def test_removed_env_keys_is_empty_for_now(self):
        """本插件从没删过环境变量键；这个空元组是给将来留的自动重建钩子。"""
        from engine import REMOVED_ENV_KEYS
        self.assertEqual(REMOVED_ENV_KEYS, ())


    def test_start_recreates_container_with_stale_timezone(self):
        item = fresh_container_item(Config={'Labels': {LABEL: 'tok'},
                                            'Healthcheck': {'Test': ['NONE']},
                                            'Env': ['TZ=UTC']})
        calls = self._run_start(item)
        paths = [path for _, path, _ in calls]
        self.assertIn('/containers/' + NAME + '/stop?t=30', paths)
        self.assertIn('/containers/create?name=' + NAME, paths)
        create = next(body for _, path, body in calls if path.startswith('/containers/create'))
        self.assertEqual(create['Env'], ['TZ=Asia/Shanghai'])


    def test_container_config_only_sets_timezone(self):
        cfg = container_config({'uid': 1, 'gid': 1, 'owner': 'tok', 'config': '/tmp/config'})
        self.assertEqual(cfg['Env'], ['TZ=Asia/Shanghai'])
        self.assertNotIn('__NAS_IP__', json.dumps(cfg))
        self.assertNotIn('PUID', json.dumps(cfg))
        self.assertNotIn('PGID', json.dumps(cfg))


    def test_fuse_mount_identity_is_refreshed_not_rejected(self):
        """厂商存储池是 FUSE：每次挂载都会换设备号/inode 号，不该据此拒绝启动。"""
        folder = self.root / 'HA'
        s = folder.stat()
        self.engine.config = {'config': str(folder), 'config_relative': 'HA',
                              'config_device': s.st_dev + 7,
                              'config_inode': s.st_ino + 7}
        with patch('engine.covering_mount', return_value=('/nas/pool0', 'fuse.cfs')), \
                patch('engine.atomic_json') as saved:
            self.engine.check_directories()
        self.assertEqual(self.engine.config['config_device'], s.st_dev)
        self.assertEqual(self.engine.config['config_inode'], s.st_ino)
        self.assertTrue(saved.called)                 # 新值要写回配置
        # 普通盘（设备号稳定）仍然按老规矩拒绝
        self.engine.config['config_device'] = s.st_dev + 7
        with patch('engine.covering_mount', return_value=('/', 'ext4')), \
                patch('engine.atomic_json'), self.assertRaises(Error):
            self.engine.check_directories()
        # 目录不在任何挂载点下（没挂盘）也要拒绝
        self.engine.config['config_device'] = s.st_dev + 7
        with patch('engine.covering_mount', return_value=(None, '')), \
                patch('engine.atomic_json'), self.assertRaises(Error):
            self.engine.check_directories()


    def test_private_config_skips_directory_identity_check(self):
        """配置在插件私有目录时不随存储挂载变化：不该去校验身份，也不该读挂载表。"""
        private = self.engine.data / 'config'
        private.mkdir(parents=True, exist_ok=True)
        self.engine.config = {'config': str(private), 'config_relative': '', 'config_root': '',
                              'config_device': 7, 'config_inode': 7}
        with patch('engine.covering_mount', side_effect=AssertionError('不该读挂载表')):
            self.engine.check_directories()
        self.assertEqual(self.engine.config['config_device'], 7)


    def test_port_grace_window_is_60_seconds(self):
        """宽限期是 60 秒（真机上 HA 首次初始化/重启后 bind 8123 可能超过 20 秒，
        给 60 秒避免一次不必要的停→删→重建）。轮询间隔不变，次数仍由 window/POLL 决定。
        """
        self.assertEqual(PORT_GRACE_SECONDS, 60)
        self.assertEqual(PORT_GRACE_POLL, 0.5)
        self.assertEqual(Engine._ensure_port.__defaults__, (False, 60))


    def test_ensure_port_reports_failure_after_repair(self):
        """重建之后端口还是没人听：如实报错，不假装成功。"""
        with patch('engine.port_is_listening', return_value=False), \
                patch('engine.time.sleep'), \
                patch.object(self.engine, '_rebuild_container') as rebuild:
            with self.assertRaises(Error) as caught:
                self.engine._ensure_port(restart=True, window=PORT_GRACE_SECONDS)
        self.assertTrue(rebuild.called)                       # 至少试过重建修复
        self.assertIn('8123', str(caught.exception))
        self.assertIn('没有监听', str(caught.exception))


    def test_ensure_port_waits_the_full_default_window(self):
        """缺省宽限期（60 秒）要真的等满 window/POLL 次才判失败。

        这条用例盯住的是「默认值本身」：把 window 显式传成 60 而不是 1，
        sleep 次数必须等于 60/0.5=120，少一次都算回归。
        """
        with patch('engine.port_is_listening', return_value=False), \
                patch('engine.time.sleep') as nap, \
                patch.object(self.engine, '_rebuild_container') as rebuild:
            with self.assertRaises(Error):
                self.engine._ensure_port(restart=True, window=PORT_GRACE_SECONDS)
        expected = int(PORT_GRACE_SECONDS / PORT_GRACE_POLL)
        self.assertEqual(expected, 120)                       # 60 秒 / 0.5 秒
        self.assertEqual(nap.call_count, expected)
        self.assertTrue(rebuild.called)


    def test_ensure_port_waits_out_the_grace_window_before_repairing(self):
        """宽限期内端口起来了就不该重建：容器只是还在启动。"""
        cursor = {'index': 0}
        results = [False, False, True]

        def probe(*args, **kwargs):
            index = min(cursor['index'], len(results) - 1)
            cursor['index'] += 1
            return results[index]

        with patch('engine.port_is_listening', side_effect=probe), \
                patch('engine.time.sleep') as nap, \
                patch.object(self.engine, '_rebuild_container') as rebuild:
            self.assertTrue(self.engine._ensure_port(restart=True, window=2))
        self.assertFalse(rebuild.called)
        self.assertTrue(nap.called)                           # 确实等过，不是立刻判定


    def test_ensure_port_succeeds_without_repair_when_listening(self):
        with patch('engine.port_is_listening', return_value=True), \
                patch.object(self.engine, '_rebuild_container') as rebuild:
            self.assertTrue(self.engine._ensure_port(restart=True, window=1))
        self.assertFalse(rebuild.called)


    def test_ensure_port_does_not_repair_fresh_container(self):
        """新建的容器（restart=False）不在这里判：HA 首次要跑几分钟才监听 8123。"""
        with patch('engine.port_is_listening', return_value=False), \
                patch.object(self.engine, '_rebuild_container') as rebuild:
            self.assertFalse(self.engine._ensure_port(restart=False, window=1))
        self.assertFalse(rebuild.called)



class ImageSelectionTests(unittest.TestCase):
    """守护进程能力 → 镜像选择 → 拉取回退 → 底层报错透传。

    真机实测（2026-10）：厂商 dockerd 20.10.17 解不开 HA arm64 镜像从 2026.3.0 起的
    zstd 层（`failed to register layer: … archive/tar: invalid tar header`），
    2026.2.0 是最后一个 gzip 层版本。所以老 Docker 上必须自动钉到 2026.2.0，
    升到 23+ 后自动回到 stable。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'root'
        self.root.mkdir()
        (self.root / 'HA').mkdir()
        self.engine = Engine(Path(self.tmp.name) / 'private', self.root)
        self.engine.config = {
            'owner': 'tok', 'enabled': True, 'uid': 1000, 'gid': 1000,
            'config_root': str(self.root), 'config': str(self.root / 'HA'),
            'config_relative': 'HA', 'config_device': 1, 'config_inode': 1,
        }

    def _api(self, version):
        """把 /version 返回给定版本、其余请求成功。"""
        body = json.dumps({'Version': version, 'ApiVersion': '1.41'}).encode()

        def fake_api(method, path, data=None, timeout=30):
            if path == '/version':
                return 200, body
            return 200, b'{}'

        return fake_api

    def test_daemon_version_is_read_once_and_cached(self):
        calls = []

        def fake_api(method, path, body=None, timeout=30):
            calls.append(path)
            return 200, json.dumps({'Version': '20.10.17'}).encode()

        with patch('engine.docker_api', side_effect=fake_api):
            self.assertEqual(self.engine.daemon_version(), '20.10.17')
            self.assertEqual(self.engine.daemon_version(), '20.10.17')
        self.assertEqual(calls.count('/version'), 1)          # 只查一次

    def test_daemon_version_empty_when_unavailable(self):
        with patch('engine.docker_api', side_effect=Error('Docker 不可用或操作超时')):
            self.assertEqual(self.engine.daemon_version(), '')
        with patch('engine.docker_api', return_value=(500, b'boom')):
            fresh = Engine(Path(self.tmp.name) / 'p2', self.root)
            self.assertEqual(fresh.daemon_version(), '')
        with patch('engine.docker_api', return_value=(200, b'not json')):
            fresh = Engine(Path(self.tmp.name) / 'p3', self.root)
            self.assertEqual(fresh.daemon_version(), '')

    def test_engine_image_follows_the_daemon(self):
        """daemon < 23 → 固定 tag；>= 23 → stable（与 choose_image 一致）。"""
        with patch('engine.docker_api', side_effect=self._api('20.10.17')):
            self.assertEqual(self.engine.image(), IMAGE_LEGACY)
        with patch('engine.docker_api', side_effect=self._api('23.0.0')):
            fresh = Engine(Path(self.tmp.name) / 'p4', self.root)
            fresh.config = dict(self.engine.config)
            self.assertEqual(fresh.image(), IMAGE_STABLE)

    def test_recorded_image_wins_after_a_successful_pull(self):
        """上次成功拉下来的镜像记在配置里：重启后不再重复试错。"""
        self.engine.config['image'] = IMAGE_LEGACY
        with patch('engine.docker_api', side_effect=self._api('26.1')):
            self.assertEqual(self.engine.image(), IMAGE_LEGACY)
            self.assertIn('上次实际拉下来', self.engine.image_reason())

    def test_image_reason_explains_the_choice(self):
        with patch('engine.docker_api', side_effect=self._api('20.10.17')):
            reason = self.engine.image_reason()
        self.assertIn('20.10.17', reason)
        self.assertIn('zstd', reason)
        self.assertIn(IMAGE_LEGACY_TAG, reason)
        with patch('engine.docker_api', side_effect=self._api('24.0.1')):
            fresh = Engine(Path(self.tmp.name) / 'p5', self.root)
            fresh.config = dict(self.engine.config)
            self.assertIn('stable', fresh.image_reason())
        fresh = Engine(Path(self.tmp.name) / 'p6', self.root)
        fresh.config = dict(self.engine.config)
        fresh._daemon_version = ''
        self.assertIn(IMAGE_LEGACY_TAG, fresh.image_reason())

    def test_container_config_uses_the_chosen_image(self):
        cfg = container_config(self.engine.config, image=IMAGE_LEGACY)
        self.assertEqual(cfg['Image'], IMAGE_LEGACY)
        self.assertEqual(container_config(self.engine.config)['Image'], IMAGE_STABLE)
        with patch('engine.docker_api', side_effect=self._api('20.10.17')):
            self.assertEqual(self.engine.container_config()['Image'], IMAGE_LEGACY)

    def test_pull_reports_the_raw_docker_error(self):
        """拉取失败必须把 Docker 原话带出来（这次真机排查就是被笼统提示耽误的）。"""
        real = (b'{"status":"Downloading"}\n'
                b'{"errorDetail":{"message":"failed to register layer: Error processing tar file'
                b'(exit status 1): archive/tar: invalid tar header"}}\n')

        with patch.object(self.engine, '_call', return_value=real):
            with self.assertRaises(Error) as caught:
                self.engine.pull(IMAGE_STABLE)
        message = str(caught.exception)
        self.assertIn(IMAGE_STABLE, message)
        self.assertIn('failed to register layer', message)
        self.assertIn('invalid tar header', message)
        self.assertNotIn('请检查镜像网络', message)               # 不能再是那句笼统提示

    def test_pull_error_is_truncated(self):
        stream = json.dumps({'error': 'register layer: ' + 'x' * 5000}).encode()
        with patch.object(self.engine, '_call', return_value=stream):
            with self.assertRaises(Error) as caught:
                self.engine.pull(IMAGE_STABLE)
        self.assertLessEqual(len(str(caught.exception)), LAYER_ERROR_LIMIT + 200)

    def test_daemon_below_23_pins_the_legacy_tag_and_pulls_it(self):
        """①daemon <23 → 选固定 tag（而且真的去拉它）。"""
        pulled = []

        def fake_call(method, path, body=None, timeout=30, ok=(200, 201, 204)):
            pulled.append(path)
            return b'{"status":"Download complete"}\n'

        with patch('engine.docker_api', side_effect=self._api('20.10.17')), \
                patch.object(self.engine, '_call', side_effect=fake_call):
            image = self.engine.pull_with_fallback()
        self.assertEqual(image, IMAGE_LEGACY)
        self.assertEqual(len(pulled), 1)                      # 首选成功就不试第二个
        self.assertIn('2026.2.0', urllib.parse.unquote(pulled[0]))
        self.assertEqual(self.engine.config['image'], IMAGE_LEGACY)

    def test_daemon_23_or_newer_pulls_stable(self):
        """②daemon >=23 → 选 stable。"""
        pulled = []

        def fake_call(method, path, body=None, timeout=30, ok=(200, 201, 204)):
            pulled.append(path)
            return b'{"status":"Download complete"}\n'

        with patch('engine.docker_api', side_effect=self._api('24.0.1')), \
                patch.object(self.engine, '_call', side_effect=fake_call):
            image = self.engine.pull_with_fallback()
        self.assertEqual(image, IMAGE_STABLE)
        self.assertEqual(len(pulled), 1)
        self.assertIn('stable', urllib.parse.unquote(pulled[0]))

    def test_layer_failure_falls_back_to_the_other_candidate(self):
        """④首选失败（zstd 层）→ 换成另一个候选，且只重试一次。"""
        pulled = []
        broken = (b'{"errorDetail":{"message":"failed to register layer: Error processing tar file'
                  b'(exit status 1): archive/tar: invalid tar header"}}\n')

        def fake_call(method, path, body=None, timeout=30, ok=(200, 201, 204)):
            pulled.append(urllib.parse.unquote(path))
            if 'stable' in pulled[-1]:
                return broken
            return b'{"status":"Download complete"}\n'

        # 守护进程报 24（会先选 stable），stable 挂 → 回退 2026.2.0
        with patch('engine.docker_api', side_effect=self._api('24.0.1')), \
                patch.object(self.engine, '_call', side_effect=fake_call):
            image = self.engine.pull_with_fallback()
        self.assertEqual(image, IMAGE_LEGACY)
        self.assertEqual(len(pulled), 2)                      # 只多试一次
        self.assertIn('stable', pulled[0])
        self.assertIn('2026.2.0', pulled[1])
        self.assertEqual(self.engine.config['image'], IMAGE_LEGACY)

    def test_legacy_failure_falls_back_to_stable(self):
        """反方向也要兜底：固定 tag 失败 → 试 stable。"""
        pulled = []
        broken = b'{"errorDetail":{"message":"manifest unknown"}}\n'

        def fake_call(method, path, body=None, timeout=30, ok=(200, 201, 204)):
            pulled.append(urllib.parse.unquote(path))
            if '2026.2.0' in pulled[-1]:
                return broken
            return b'{"status":"Download complete"}\n'

        with patch('engine.docker_api', side_effect=self._api('20.10.17')), \
                patch.object(self.engine, '_call', side_effect=fake_call):
            image = self.engine.pull_with_fallback()
        self.assertEqual(image, IMAGE_STABLE)
        self.assertEqual(len(pulled), 2)
        self.assertIn('2026.2.0', pulled[0])
        self.assertIn('stable', pulled[1])

    def test_both_candidates_failing_reports_both_reasons(self):
        """⑥两个都失败：报错里要同时保留各自的原始原因，且只剩两条请求。"""
        pulled = []

        def fake_call(method, path, body=None, timeout=30, ok=(200, 201, 204)):
            pulled.append(urllib.parse.unquote(path))
            if '2026.2.0' in pulled[-1]:
                return b'{"errorDetail":{"message":"manifest unknown"}}\n'
            return (b'{"errorDetail":{"message":"failed to register layer: Error processing tar '
                    b'file(exit status 1): archive/tar: invalid tar header"}}\n')

        with patch('engine.docker_api', side_effect=self._api('24.0.1')), \
                patch.object(self.engine, '_call', side_effect=fake_call):
            with self.assertRaises(Error) as caught:
                self.engine.pull_with_fallback()
        message = str(caught.exception)
        self.assertEqual(len(pulled), 2)                      # 不无限重试
        self.assertIn('invalid tar header', message)
        self.assertIn('manifest unknown', message)
        self.assertIn(IMAGE_STABLE, message)
        self.assertIn(IMAGE_LEGACY, message)
        self.assertIn('都拉不下来', message)

    def test_snapshot_exposes_image_and_reason(self):
        """③⑤ snapshot 里要有实际镜像、版本标签、原因与守护进程版本。"""
        with patch.object(self.engine, 'owned', return_value=None), \
                patch('engine.docker_api', side_effect=self._api('20.10.17')):
            state = self.engine.snapshot()
        self.assertEqual(state['image'], IMAGE_LEGACY)
        self.assertIn('2026.2.0', state['imageVersion'])
        self.assertEqual(state['dockerVersion'], '20.10.17')
        self.assertIn('zstd', state['imageReason'])
        self.assertIn(IMAGE_LEGACY_TAG, state['imageReason'])

    def test_start_error_keeps_the_layer_reason(self):
        """⑤ 端到端：容器不存在 → 两个候选都因 zstd 层失败 → 页面 error 里有原话。"""
        docker = FakeDocker()

        def fake_api(method, path, body=None, timeout=30):
            if path == '/version':
                return 200, json.dumps({'Version': '20.10.17'}).encode()
            if path.startswith('/images/create'):
                return 200, (b'{"errorDetail":{"message":"failed to register layer: Error '
                             b'processing tar file(exit status 1): archive/tar: invalid tar '
                             b'header"}}\n')
            return docker(method, path, body, timeout)

        with nas_owned(), patch('engine.docker_api', side_effect=fake_api), \
                patch.object(self.engine, 'check_directories'), \
                patch('engine.time.sleep'), \
                patch('engine.homeassistant_info', return_value={}):
            with self.assertRaises(Error) as caught:
                self.engine.start()
        self.assertIn('invalid tar header', str(caught.exception))
        self.assertIn('都拉不下来', str(caught.exception))

    def test_pull_failure_reaches_snapshot_error(self):
        """⑤ 拉取失败的原因要经 launch() 落进 snapshot 的 error 字段。"""
        def fake_api(method, path, body=None, timeout=30):
            if path == '/version':
                return 200, json.dumps({'Version': '20.10.17'}).encode()
            if path.startswith('/images/create'):
                return 200, (b'{"errorDetail":{"message":"failed to register layer: Error '
                             b'processing tar file(exit status 1): archive/tar: invalid tar '
                             b'header"}}\n')
            if path.endswith('/json'):
                return 404, b'{}'
            return 200, b'{}'

        with nas_owned(), patch('engine.docker_api', side_effect=fake_api), \
                patch.object(self.engine, 'check_directories'), \
                patch('engine.time.sleep'):
            self.engine.launch('start', {})
            self.engine.worker.join(60)
            state = self.engine.snapshot()
        self.assertIn('invalid tar header', state['error'])
        self.assertIn('failed to register layer', state['error'])
        self.assertFalse(state['running'])

class MultiRootTests(unittest.TestCase):
    """多个「存储位置」：内置存储池（FUSE）+ 外接设备（U 盘）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.pool = self.base / 'pool'
        self.usb = self.base / 'usb'
        (self.pool / '下载').mkdir(parents=True)
        (self.pool / '仅池内').mkdir()
        (self.usb / '照片').mkdir(parents=True)
        (self.usb / '照片' / '2024').mkdir()
        self.engine = Engine(self.base / 'private', self.pool, roots=[self.pool, self.usb])

    def test_roots_deduplicated_and_first_is_default(self):
        engine = Engine(self.base / 'data2', self.pool,
                        roots=[self.pool, self.usb, str(self.pool), self.usb])
        self.assertEqual(engine.roots, [self.pool, self.usb])
        self.assertEqual(engine.root, self.pool)
        single = Engine(self.base / 'data3', self.usb)          # 未给 roots 时退化为单根
        self.assertEqual(single.roots, [self.usb])
        self.assertEqual(single.root, self.usb)

    def test_locations_report_index_label_path_and_exists(self):
        items = self.engine.locations()
        self.assertEqual([item['index'] for item in items], [0, 1])
        self.assertEqual([item['path'] for item in items], [str(self.pool), str(self.usb)])
        self.assertEqual([item['label'] for item in items], ['pool', 'usb'])
        self.assertTrue(all(item['exists'] for item in items))
        missing = Engine(self.base / 'data4', self.pool,
                         roots=[self.pool, self.base / '未挂载'])
        self.assertEqual([item['exists'] for item in missing.locations()], [True, False])

    def test_root_label_rules(self):
        cases = [
            ('/nas/pool0/u3943892/data', '存储池'),
            ('/nas/pool0', '存储池'),
            ('/nas/mnt/usb', '外接设备'),
            ('/nas/mnt/usb/下载/MT', '外接设备'),
            ('/mnt/usb-1a2b3c', '外接设备'),
            ('/mnt/usb-1a2b3c/DCIM', '外接设备'),
            ('/data/plugin/homeassistant/config', 'config'),
        ]
        for path, expected in cases:
            with self.subTest(path=path):
                self.assertEqual(root_label(path), expected)

    def test_parse_roots_ignores_empty_entries(self):
        self.assertEqual(parse_roots(''), [])
        self.assertEqual(parse_roots('   '), [])
        self.assertEqual(parse_roots(None), [])
        self.assertEqual(parse_roots('relative/path'), [])

    def test_parse_roots_keeps_order_and_dedupes(self):
        self.assertEqual(parse_roots(str(self.usb)), [str(self.usb)])
        self.assertEqual(parse_roots(':'.join(['', str(self.pool), str(self.usb), str(self.pool)])),
                         [str(self.pool), str(self.usb)])
        self.assertEqual(parse_roots(':'.join(['relative', str(self.pool)])), [str(self.pool)])

    def test_browse_uses_selected_root(self):
        self.assertEqual([item['name'] for item in self.engine.browse('')],
                         sorted(['下载', '仅池内']))
        self.assertEqual([item['name'] for item in self.engine.browse('', 1)], ['照片'])
        self.assertEqual([item['path'] for item in self.engine.browse('照片', 1)], ['照片/2024'])
        with self.assertRaises(Error):
            self.engine.browse('', 2)                            # 位置序号越界
        with self.assertRaises(Error):
            self.engine.browse('', 'root')                       # 位置序号非法

    def test_browse_hides_hidden_and_symlinks(self):
        (self.pool / '.hidden').mkdir()
        link = self.pool / 'link'
        try:
            link.symlink_to(self.usb)
        except (OSError, NotImplementedError):
            # Windows 非开发者模式没有创建符号链接的权限：这条断言在其它用例里
            # （confined 拒绝符号链接）仍然有覆盖，这里就只验证隐藏目录。
            link = None
        names = [item['name'] for item in self.engine.browse('')]
        self.assertEqual(names, sorted(['下载', '仅池内']))
        if link is not None:
            self.assertNotIn('link', names)

    def test_locate_absolute_and_longest_prefix(self):
        nested = Engine(self.base / 'data5', self.pool, roots=[self.pool, self.usb / '照片'])
        self.assertEqual(nested.locate(str(self.usb / '照片' / '2024')), (1, '2024'))
        self.assertEqual(nested.locate(str(self.usb / '照片')), (1, ''))     # 位置根部
        self.assertEqual(nested.locate(str(self.pool / '下载')), (0, '下载'))
        # 相对路径保持旧语义：用第 0 个位置，也可显式指定序号
        self.assertEqual(nested.locate('下载'), (0, '下载'))
        self.assertEqual(nested.locate('下载', 1), (1, '下载'))
        with self.assertRaises(Error) as caught:
            nested.locate(str(self.base / 'outside'))
        self.assertIn('所选目录必须位于已挂载的存储位置内', str(caught.exception))
        with self.assertRaises(Error):
            nested.locate(str(self.usb))                         # 只是根的上层目录，不算命中
        with self.assertRaises(Error):
            nested.locate(str(self.pool) + 'x')                  # 名字前缀相同但不是同一个目录

    def test_setup_with_absolute_path_records_root_and_identity(self):
        folder = self.usb / '照片'
        stat = folder.stat()
        with nas_owned(), patch('engine.docker_api', side_effect=docker_stub):
            self.engine.setup(str(folder))
            state = self.engine.snapshot()
        config = json.loads(self.engine.cfgfile.read_text(encoding='utf-8'))
        self.assertEqual(config['config_root'], str(self.usb))
        self.assertEqual(config['config_relative'], '照片')
        self.assertEqual(config['config'], str(folder))
        self.assertEqual(config['config_device'], stat.st_dev)
        self.assertEqual(config['config_inode'], stat.st_ino)
        self.assertEqual(state['config_abs'], str(folder))
        self.assertFalse(state['config_private'])
        self.assertEqual([item['path'] for item in state['roots']],
                         [str(self.pool), str(self.usb)])

    def test_setup_picks_the_longest_matching_root(self):
        nested_root = self.usb / '照片'
        engine = Engine(self.base / 'data6', self.pool, roots=[self.pool, nested_root])
        with nas_owned(), patch('engine.docker_api', side_effect=docker_stub):
            engine.setup(str(nested_root / '2024'))
        config = json.loads(engine.cfgfile.read_text(encoding='utf-8'))
        self.assertEqual(config['config_root'], str(nested_root))
        self.assertEqual(config['config_relative'], '2024')
        self.assertEqual(config['config'], str(nested_root / '2024'))

    def test_setup_rejects_path_outside_roots(self):
        outside = self.base / 'outside'
        outside.mkdir()
        with nas_owned(), patch('engine.docker_api', side_effect=docker_stub):
            with self.assertRaises(Error) as caught:
                self.engine.setup(str(outside))
        self.assertIn('所选目录必须位于已挂载的存储位置内', str(caught.exception))
        self.assertFalse(self.engine.cfgfile.exists())

    def test_setup_accepts_relative_path_on_the_first_root(self):
        with nas_owned(), patch('engine.docker_api', side_effect=docker_stub):
            self.engine.setup('仅池内')
            state = self.engine.snapshot()
        config = json.loads(self.engine.cfgfile.read_text(encoding='utf-8'))
        self.assertEqual(config['config_root'], str(self.pool))
        self.assertEqual(config['config_relative'], '仅池内')
        self.assertEqual(config['config'], str(self.pool / '仅池内'))
        self.assertEqual(state['config_abs'], str(self.pool / '仅池内'))
        self.assertFalse(state['config_private'])

    def test_setup_rejects_configured_twice(self):
        with nas_owned(), patch('engine.docker_api', side_effect=docker_stub):
            self.engine.setup('下载')
            with self.assertRaises(Error) as caught:
                self.engine.setup('仅池内')
        self.assertIn('已完成初始化', str(caught.exception))
        self.assertEqual(self.engine.config['config_relative'], '下载')

    def test_setup_rejects_comma_in_path(self):
        """Docker 的 bind 语法用逗号分隔字段，带逗号的路径会让挂载参数错位。"""
        (self.pool / 'a,b').mkdir()
        with nas_owned(), patch('engine.docker_api', side_effect=docker_stub):
            with self.assertRaises(Error) as caught:
                self.engine.setup('a,b')
        self.assertIn('不能包含逗号', str(caught.exception))

    def test_setup_rejects_container_name_taken(self):
        """同名容器已存在时拒绝覆盖（哪怕归属看起来是自己的）。"""
        docker = FakeDocker()
        docker.container = fresh_container_item()

        with nas_owned(), patch('engine.docker_api', side_effect=docker):
            with self.assertRaises(Error) as caught:
                self.engine.setup('下载')
        self.assertIn('同名容器已存在', str(caught.exception))

    def _legacy_config(self, folder, absolute=None, **extra):
        """旧版配置（没有 config_root）：相对路径取 folder 的目录名，绝对路径可另给。

        注意相对路径必须是 folder 的真实目录名——它决定 check_directories()
        会去哪个根里找目录；absolute 用来故意制造「解析出来的目录 ≠ 记录的绝对
        路径」，那才是身份变化。
        """
        folder = Path(folder)
        stat = folder.stat()
        config = {
            'owner': 'tok', 'uid': 1000, 'gid': 1000,
            'config_relative': folder.name, 'config': str(absolute or folder),
            'config_device': stat.st_dev, 'config_inode': stat.st_ino,
        }
        config.update(extra)
        return config

    def test_legacy_config_without_root_field_still_passes(self):
        """旧配置没有 config_root：按绝对路径前缀反查所属位置（回归保证）。

        '照片' 只存在于第 1 个位置（U 盘）下，所以这条用例同时证明校验没有
        退回第 0 个根（否则会报「目录不存在」）。
        """
        self.engine.config = self._legacy_config(self.usb / '照片')
        self.engine.check_directories()
        self.assertNotIn('config_root', self.engine.config)

    def test_legacy_config_fuse_identity_refresh_still_works(self):
        folder = self.usb / '照片'
        stat = folder.stat()
        self.engine.config = self._legacy_config(folder, config_device=stat.st_dev + 7,
                                                 config_inode=stat.st_ino + 7)
        with patch('engine.covering_mount', return_value=('/nas/mnt/usb', 'fuse.cfs')), \
                patch('engine.atomic_json') as saved:
            self.engine.check_directories()
        self.assertEqual(self.engine.config['config_device'], stat.st_dev)
        self.assertEqual(self.engine.config['config_inode'], stat.st_ino)
        self.assertTrue(saved.called)                            # 新身份号要写回配置

    def test_recorded_root_wins_over_the_current_root_list(self):
        """配置里记着根时按它校验：LOCAL_ROOTS 变了也不该无谓拒绝启动。"""
        engine = Engine(self.base / 'data7', self.pool, roots=[self.pool])
        folder = self.usb / '照片'
        engine.config = self._legacy_config(folder, config_root=str(self.usb))
        engine.check_directories()
        self.assertEqual(engine.config['config_root'], str(self.usb))

    def test_check_directories_rejects_missing_folder(self):
        """挂载点没挂上时，相对路径会落到一个不存在的目录上：拒绝启动并给出指向。"""
        folder = self.usb / '临目录'
        folder.mkdir()
        self.engine.config = self._legacy_config(folder)
        shutil.rmtree(folder)
        with self.assertRaises(Error) as caught:
            self.engine.check_directories()
        self.assertIn('配置目录不可用', str(caught.exception))

    def test_check_directories_rejects_swapped_folder(self):
        """记录的绝对路径与解析出来的目录不是同一个目录：拒绝启动。

        厂商存储盘挂载变化时会出现这种组合（.HA_VERSION 会写错地方，
        所以绝不能放行）。
        """
        folder = self.usb / '另一个目录'
        folder.mkdir()
        nested = self.pool / 'cfg' / 'nested'
        nested.mkdir(parents=True)
        self.engine.config = self._legacy_config(folder, absolute=nested)
        with self.assertRaises(Error) as caught:
            self.engine.check_directories()
        self.assertIn('配置目录', str(caught.exception))
        self.assertIn('拒绝启动', str(caught.exception))

    def test_check_directories_rejects_same_root_swapped_folder(self):
        """同一个存储位置里换成了另一个目录：走身份变化分支。"""
        folder = self.usb / '甲目录'
        folder.mkdir()
        other = self.usb / '乙目录'
        other.mkdir()
        self.engine.config = self._legacy_config(folder, absolute=other)
        with self.assertRaises(Error) as caught:
            self.engine.check_directories()
        self.assertIn('身份已变化', str(caught.exception))


class AdminActionTests(unittest.TestCase):
    """「修改目录」(reconfigure) 与「重新初始化」(reset)。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.pool = self.base / 'pool'
        self.usb = self.base / 'usb'
        self.cfgdir = self.pool / 'cfg'
        self.cfgdir.mkdir(parents=True)
        (self.usb / '照片').mkdir(parents=True)
        # 用户数据哨兵：这两个文件在任何操作之后都必须原样还在
        self.sentinel = self.cfgdir / 'configuration.yaml'
        self.sentinel.write_text('default_config:\n', encoding='utf-8')
        self.db_sentinel = self.cfgdir / 'home-assistant_v2.db'
        self.db_sentinel.write_text('db', encoding='utf-8')
        self.engine = Engine(self.base / 'private', self.pool, roots=[self.pool, self.usb])

    def _configure(self, docker):
        """走一遍初始化并造出容器（模拟「已经配好、容器在跑」的现场）。"""
        with nas_owned(), patch('engine.docker_api', side_effect=docker), \
                patch('engine.homeassistant_info', return_value={}):
            self.engine.setup(str(self.cfgdir))
            self.engine.start()
        return json.loads(self.engine.cfgfile.read_text(encoding='utf-8'))

    def _backups(self):
        return sorted(self.engine.data.glob('settings.json.bak-*'))

    def test_reconfigure_switches_directory_and_rebuilds_container(self):
        docker = FakeDocker()
        old = self._configure(docker)
        self.assertEqual(docker.created_mounts()['/config'], str(self.cfgdir))
        folder = self.usb / '照片'
        stat = folder.stat()
        with nas_owned(), patch('engine.docker_api', side_effect=docker), \
                patch('engine.homeassistant_info', return_value={}):
            state = self.engine.reconfigure(str(folder))
        config = json.loads(self.engine.cfgfile.read_text(encoding='utf-8'))
        self.assertEqual(config['config'], str(folder))                # 绝对路径
        self.assertEqual(config['config_relative'], '照片')             # 相对路径
        self.assertEqual(config['config_root'], str(self.usb))         # 所属存储位置
        self.assertEqual((config['config_device'], config['config_inode']),
                         (stat.st_dev, stat.st_ino))                   # 身份号
        self.assertEqual(config['owner'], old['owner'])                # 容器归属不变
        self.assertEqual(self.engine.config['config'], str(folder))
        # 旧容器被删掉，新容器按新 bind 创建并启动
        self.assertTrue(docker.removed())
        self.assertEqual(docker.created_mounts()['/config'], str(folder))
        self.assertTrue(docker.container['State']['Running'])
        self.assertEqual(state['config_abs'], str(folder))
        self.assertTrue(state['configured'])
        self.assertEqual(len(self._backups()), 1)                      # 先备份了旧配置
        self.assertEqual(self.sentinel.read_text(encoding='utf-8'), 'default_config:\n')
        self.assertEqual(self.db_sentinel.read_text(encoding='utf-8'), 'db')

    def test_reconfigure_without_choice_keeps_current_directory(self):
        """不带 configPath（None）＝配置目录不动，只按当前挂载重建一次。"""
        docker = FakeDocker()
        old = self._configure(docker)
        with nas_owned(), patch('engine.docker_api', side_effect=docker), \
                patch('engine.homeassistant_info', return_value={}):
            state = self.engine.reconfigure(None)
        config = json.loads(self.engine.cfgfile.read_text(encoding='utf-8'))
        self.assertEqual(config['config'], old['config'])
        self.assertEqual(config['config_root'], old['config_root'])
        self.assertEqual(config['owner'], old['owner'])
        self.assertEqual(docker.created_mounts()['/config'], str(self.cfgdir))
        self.assertTrue(docker.container['State']['Running'])
        self.assertEqual(state['config_abs'], str(self.cfgdir))

    def test_reconfigure_rolls_back_when_rebuild_fails(self):
        docker = FakeDocker()
        old = self._configure(docker)
        docker.fail_create = 1                                        # 按新 bind 创建失败
        with nas_owned(), patch('engine.docker_api', side_effect=docker):
            with self.assertRaises(Error) as caught:
                self.engine.reconfigure(str(self.usb / '照片'))
        self.assertIn('Docker 操作失败', str(caught.exception))        # 原因如实上报
        config = json.loads(self.engine.cfgfile.read_text(encoding='utf-8'))
        self.assertEqual(config['config'], str(self.cfgdir))          # 配置回滚
        self.assertEqual(config['config_root'], str(self.pool))
        self.assertEqual(self.engine.config['config'], str(self.cfgdir))
        # 原容器按旧配置恢复：最后一次创建用的是旧 bind，并且已经启动
        self.assertEqual(docker.created_mounts()['/config'], str(self.cfgdir))
        self.assertTrue(docker.container['State']['Running'])
        self.assertEqual(docker.container['Config']['Labels'][LABEL], old['owner'])
        self.assertEqual(self.sentinel.read_text(encoding='utf-8'), 'default_config:\n')

    def test_reconfigure_can_move_config_dir_to_private(self):
        """显式传空串＝配置目录改回插件私有目录；原配置目录里的文件同样不动。"""
        docker = FakeDocker()
        self._configure(docker)
        with nas_owned(), patch('engine.docker_api', side_effect=docker), \
                patch('engine.homeassistant_info', return_value={}):
            state = self.engine.reconfigure('')
        config = json.loads(self.engine.cfgfile.read_text(encoding='utf-8'))
        private = str(self.engine.data / 'config')
        self.assertEqual(config['config'], private)
        self.assertEqual(config['config_root'], '')
        self.assertTrue(state['config_private'])
        self.assertEqual(docker.created_mounts()['/config'], private)
        self.assertEqual(self.sentinel.read_text(encoding='utf-8'), 'default_config:\n')
        self.assertEqual(self.db_sentinel.read_text(encoding='utf-8'), 'db')

    def test_reconfigure_can_move_from_private_to_user_storage(self):
        docker = FakeDocker()
        with nas_owned(), patch('engine.docker_api', side_effect=docker), \
                patch('engine.homeassistant_info', return_value={}):
            self.engine.setup('')
            self.engine.start()
            self.assertTrue(self.engine.snapshot()['config_private'])
            state = self.engine.reconfigure(str(self.usb / '照片'))
        config = json.loads(self.engine.cfgfile.read_text(encoding='utf-8'))
        self.assertEqual(config['config'], str(self.usb / '照片'))
        self.assertEqual(config['config_root'], str(self.usb))
        self.assertFalse(state['config_private'])
        self.assertEqual(docker.created_mounts()['/config'], str(self.usb / '照片'))

    def test_reconfigure_validates_before_touching_anything(self):
        """目录不合法时：配置不动、容器不动、也不产生备份。"""
        docker = FakeDocker()
        self._configure(docker)
        outside = self.base / 'outside'
        outside.mkdir()
        with nas_owned(), patch('engine.docker_api', side_effect=docker):
            with self.assertRaises(Error) as caught:
                self.engine.reconfigure(str(outside))
        self.assertIn('所选目录必须位于已挂载的存储位置内', str(caught.exception))
        self.assertEqual(self.engine.config['config'], str(self.cfgdir))
        self.assertFalse(docker.removed())
        self.assertEqual(self._backups(), [])

    def test_reconfigure_rejects_comma_path_before_touching_anything(self):
        docker = FakeDocker()
        self._configure(docker)
        (self.pool / 'a,b').mkdir()
        with nas_owned(), patch('engine.docker_api', side_effect=docker):
            with self.assertRaises(Error) as caught:
                self.engine.reconfigure('a,b')
        self.assertIn('不能包含逗号', str(caught.exception))
        self.assertFalse(docker.removed())
        self.assertEqual(self._backups(), [])

    def test_reconfigure_rejected_before_setup(self):
        with self.assertRaises(Error) as caught:
            self.engine.reconfigure(str(self.usb / '照片'))
        self.assertIn('请先初始化', str(caught.exception))

    def test_reconfigure_rejected_while_busy(self):
        docker = FakeDocker()
        self._configure(docker)
        self.engine.busy = True
        try:
            with self.assertRaises(Error) as caught:
                self.engine.reconfigure(str(self.usb / '照片'))
        finally:
            self.engine.busy = False
        self.assertIn('当前有操作正在进行，请稍后再试', str(caught.exception))
        self.assertEqual(self.engine.config['config'], str(self.cfgdir))

    def test_reset_requires_confirmation(self):
        docker = FakeDocker()
        self._configure(docker)
        with self.assertRaises(Error) as caught:
            self.engine.reset()
        self.assertIn('请确认重新初始化', str(caught.exception))
        self.assertTrue(self.engine.cfgfile.exists())                 # 什么都没动
        self.assertIsNotNone(self.engine.config)
        self.assertFalse(docker.removed())

    def test_reset_clears_config_and_keeps_user_files(self):
        docker = FakeDocker()
        self._configure(docker)
        with patch('engine.docker_api', side_effect=docker):
            state = self.engine.reset(confirm=True)
        self.assertFalse(state['configured'])
        self.assertEqual(state['config_abs'], '')
        self.assertIsNone(self.engine.config)
        self.assertFalse(self.engine.cfgfile.exists())                # 配置已挪走
        backups = self._backups()
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text(encoding='utf-8'))['config'],
                         str(self.cfgdir))
        self.assertTrue(docker.removed())                             # 容器已移除
        self.assertIsNone(docker.container)
        # 用户数据一个文件都没动
        self.assertEqual(sys_listing(self.cfgdir), ['configuration.yaml', 'home-assistant_v2.db'])
        self.assertEqual(self.sentinel.read_text(encoding='utf-8'), 'default_config:\n')
        self.assertEqual(self.db_sentinel.read_text(encoding='utf-8'), 'db')

    def test_reset_rejected_while_busy(self):
        docker = FakeDocker()
        self._configure(docker)
        self.engine.busy = True
        try:
            with self.assertRaises(Error) as caught:
                self.engine.reset(confirm=True)
        finally:
            self.engine.busy = False
        self.assertIn('当前有操作正在进行，请稍后再试', str(caught.exception))
        self.assertIsNotNone(self.engine.config)
        self.assertTrue(self.engine.cfgfile.exists())

    def test_backup_names_do_not_collide(self):
        docker = FakeDocker()
        self._configure(docker)
        with nas_owned(), patch('engine.docker_api', side_effect=docker), \
                patch('engine.homeassistant_info', return_value={}):
            self.engine.reconfigure(str(self.usb / '照片'))
            self.engine.reconfigure(str(self.cfgdir))
        names = [path.name for path in self._backups()]
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(len(names), 2)

    def test_start_after_setup_starts_container(self):
        docker = FakeDocker()
        with nas_owned(), patch('engine.docker_api', side_effect=docker), \
                patch('engine.homeassistant_info', return_value={}):
            self.engine.setup(str(self.cfgdir))
            self.engine.start()
        self.assertTrue(docker.container['State']['Running'])
        self.assertEqual(docker.created_mounts()['/config'], str(self.cfgdir))


def sys_listing(folder):
    """目录里的文件名（排序），用于断言「一个文件都没动」。"""
    return sorted(item.name for item in Path(folder).iterdir())


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name) / 'root'
        root.mkdir()
        (root / '配置').mkdir()
        self.usb = Path(self.tmp.name) / 'usb'
        (self.usb / '照片').mkdir(parents=True)
        self.engine = Engine(Path(self.tmp.name) / 'data', root, dev=True, roots=[root, self.usb])
        self.server = Server(('127.0.0.1', 0), self.engine, 'u123456', dev=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

        def close():
            self.server.shutdown()
            self.server.server_close()
            self.thread.join()

        self.addCleanup(close)
        _, html = self.request('GET', '/')
        self.token = re.search(r'name="homeassistant-session" content="([^"]+)"', html.decode())[1]
        self.csrf = re.search(r'name="csrf-token" content="([^"]+)"', html.decode())[1]

    def request(self, method, route, data=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port)
        try:
            conn.request(method, route, json.dumps(data) if data is not None else None, headers or {})
            r = conn.getresponse()
            return r.status, r.read()
        finally:
            conn.close()

    def auth(self):
        return {
            'X-Homeassistant-Session': self.token,
            'X-CSRF-Token': self.csrf,
            'Content-Type': 'application/json',
        }

    def test_healthz_is_open(self):
        code, body = self.request('GET', '/healthz')
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)['version'], installed_version())

    def test_unauthenticated_denied(self):
        self.assertEqual(self.request('GET', '/api/status')[0], 401)

    def test_unknown_session_denied(self):
        headers = dict(self.auth(), **{'X-Homeassistant-Session': 'dead.beef'})
        self.assertEqual(self.request('GET', '/api/status', headers=headers)[0], 401)

    def test_csrf_required(self):
        self.assertEqual(
            self.request('POST', '/api/service/start', {}, {'X-Homeassistant-Session': self.token})[0], 403)

    def test_status_no_secrets(self):
        code, body = self.request('GET', '/api/status', headers=self.auth())
        self.assertEqual(code, 200)
        self.assertNotIn(self.server.key.hex(), body.decode())

    def test_preview_write_blocked(self):
        code, _ = self.request('POST', '/api/service/start', {}, self.auth())
        self.assertEqual(code, 400)

    def test_static_assets_served(self):
        for route, mime in (('/app.js', 'application/javascript'),
                            ('/styles.css', 'text/css'),
                            ('/assets/homeassistant.png', 'image/png')):
            with self.subTest(route=route):
                code, body = self.request('GET', route)
                self.assertEqual(code, 200)
                self.assertTrue(body)

    def test_unknown_api_route_is_404(self):
        self.assertEqual(self.request('GET', '/api/nope', headers=self.auth())[0], 404)

    def test_page_shows_installed_version(self):
        _, body = self.request('GET', '/')
        text = body.decode()
        self.assertNotIn('__PLUGIN_VERSION__', text)
        self.assertIn('Home Assistant · ' + installed_version(), text)

    def test_page_embeds_session_and_csrf(self):
        """每次打开页面都会新签一个会话令牌，所以要从同一次响应里取。"""
        _, body = self.request('GET', '/')
        text = body.decode()
        token = re.search(r'name="homeassistant-session" content="([^"]+)"', text)[1]
        csrf = re.search(r'name="csrf-token" content="([^"]+)"', text)[1]
        self.assertTrue(token)
        self.assertTrue(csrf)
        headers = {'X-Homeassistant-Session': token, 'X-CSRF-Token': csrf,
                   'Content-Type': 'application/json'}
        self.assertEqual(self.request('GET', '/api/status', headers=headers)[0], 200)

    def test_status_reports_lan_address_on_8123(self):
        """host 网络：入口地址就是宿主机地址 + 8123（容器没有自己的端口映射）。"""
        with patch('server.lan_ip', return_value='192.168.1.30'):
            code, body = self.request('GET', '/api/status', headers=self.auth())
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)['address'], 'http://192.168.1.30:8123')

    def test_status_lists_storage_locations(self):
        code, body = self.request('GET', '/api/status', headers=self.auth())
        self.assertEqual(code, 200)
        roots = json.loads(body)['roots']
        self.assertEqual([item['index'] for item in roots], [0, 1])
        self.assertEqual(roots[1]['path'], str(self.usb))

    def test_browse_accepts_root_parameter(self):
        code, body = self.request('GET', '/api/browse?root=1&path=', headers=self.auth())
        self.assertEqual(code, 200)
        self.assertEqual([item['name'] for item in json.loads(body)['items']], ['照片'])
        code, body = self.request('GET', '/api/browse', headers=self.auth())   # 缺省 root=0
        self.assertEqual(code, 200)
        self.assertEqual([item['name'] for item in json.loads(body)['items']], ['配置'])

    def test_browse_rejects_unknown_root(self):
        code, body = self.request('GET', '/api/browse?root=9', headers=self.auth())
        self.assertEqual(code, 400)
        self.assertIn('存储位置无效', json.loads(body)['error'])

    def test_browse_rejects_traversal(self):
        code, body = self.request('GET', '/api/browse?root=0&path=../', headers=self.auth())
        self.assertEqual(code, 400)
        self.assertIn('目录路径无效', json.loads(body)['error'])

    def test_post_rejects_non_json_body(self):
        headers = {'X-Homeassistant-Session': self.token, 'X-CSRF-Token': self.csrf,
                   'Content-Type': 'text/plain'}
        self.assertEqual(self.request('POST', '/api/service/start', {'a': 1}, headers)[0], 400)

    def test_unsupported_action_is_400(self):
        self.assertEqual(self.request('POST', '/api/service/nope', {}, self.auth())[0], 400)


class AdminHTTPTests(unittest.TestCase):
    """非预览模式 + Docker 打桩，用真实 HTTP 走一遍修改目录与重新初始化。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.pool = base / 'pool'
        self.usb = base / 'usb'
        self.cfgdir = self.pool / 'cfg'
        self.cfgdir.mkdir(parents=True)
        (self.usb / '照片').mkdir(parents=True)
        self.sentinel = self.cfgdir / 'configuration.yaml'
        self.sentinel.write_text('default_config:\n', encoding='utf-8')
        self.docker = FakeDocker()
        self.engine = Engine(base / 'data', self.pool, roots=[self.pool, self.usb])
        self.server = Server(('127.0.0.1', 0), self.engine, 'u123456')
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._close)
        _, html = self.request('GET', '/')
        self.token = re.search(r'name="homeassistant-session" content="([^"]+)"', html.decode())[1]
        self.csrf = re.search(r'name="csrf-token" content="([^"]+)"', html.decode())[1]

    def _close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, method, route, data=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port)
        try:
            conn.request(method, route, json.dumps(data) if data is not None else None,
                         headers or {'X-Real-IP': '127.0.0.1'})
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def auth(self):
        return {
            'X-Homeassistant-Session': self.token,
            'X-CSRF-Token': self.csrf,
            'Content-Type': 'application/json',
            'X-Real-IP': '127.0.0.1',
        }

    def _patch(self):
        return patch('engine.docker_api', side_effect=self.docker)

    def _setup(self):
        code, _ = self.request('POST', '/api/setup', {'path': str(self.cfgdir)}, self.auth())
        self.assertEqual(code, 202)                      # setup 仍是异步的
        self.engine.worker.join(30)

    def test_reconfigure_over_http_reports_new_state(self):
        with nas_owned(), self._patch(), patch('engine.homeassistant_info', return_value={}):
            self._setup()
            code, body = self.request('POST', '/api/service/reconfigure',
                                      {'configPath': str(self.usb / '照片')}, self.auth())
        state = json.loads(body)
        self.assertEqual(code, 200)                          # 操作完成即回最新状态
        self.assertTrue(state['ok'])
        self.assertEqual(state['config_abs'], str(self.usb / '照片'))
        self.assertEqual(self.docker.created_mounts()['/config'], str(self.usb / '照片'))
        self.assertTrue(self.docker.removed())

    def test_reconfigure_over_http_without_config_path_keeps_it(self):
        """只提交 {}（不带 configPath）＝保持当前配置目录，不能掉回插件私有目录。"""
        with nas_owned(), self._patch(), patch('engine.homeassistant_info', return_value={}):
            self._setup()
            code, body = self.request('POST', '/api/service/reconfigure', {}, self.auth())
        state = json.loads(body)
        self.assertEqual(code, 200)
        self.assertEqual(state['config_abs'], str(self.cfgdir))
        self.assertFalse(state['config_private'])
        self.assertEqual(self.docker.created_mounts()['/config'], str(self.cfgdir))

    def test_setup_over_http_accepts_blank_path(self):
        """配置目录留空＝插件私有目录；初始化的请求体可以只有 path=''。"""
        with nas_owned(), self._patch(), patch('engine.homeassistant_info', return_value={}):
            code, _ = self.request('POST', '/api/setup', {'path': ''}, self.auth())
            self.assertEqual(code, 202)
            self.engine.worker.join(30)
            state = self.engine.snapshot()
        self.assertTrue(state['configured'])
        self.assertTrue(state['config_private'])
        self.assertEqual(self.docker.created_mounts()['/config'], str(self.engine.data / 'config'))

    def test_setup_over_http_rejects_bad_path_type(self):
        code, body = self.request('POST', '/api/setup', {'path': 5}, self.auth())
        self.assertEqual(code, 400)
        self.assertIn('目录路径无效', json.loads(body)['error'])

    def test_reset_over_http_needs_confirm_and_keeps_files(self):
        with nas_owned(), self._patch(), patch('engine.homeassistant_info', return_value={}):
            self._setup()
            code, body = self.request('POST', '/api/service/reset', {}, self.auth())
            self.assertEqual(code, 400)
            self.assertIn('请确认重新初始化', json.loads(body)['error'])
            code, body = self.request('POST', '/api/service/reset', {'confirm': True}, self.auth())
        state = json.loads(body)
        self.assertEqual(code, 200)
        self.assertFalse(state['configured'])                # 页面回到初始化表单
        self.assertFalse(self.engine.cfgfile.exists())
        self.assertTrue(list(self.engine.data.glob('settings.json.bak-*')))
        self.assertIsNone(self.docker.container)
        self.assertEqual(sorted(p.name for p in self.cfgdir.iterdir()), ['configuration.yaml'])
        self.assertEqual(self.sentinel.read_text(encoding='utf-8'), 'default_config:\n')

    def test_service_action_rejected_while_busy(self):
        self.engine.busy = True
        try:
            code, body = self.request('POST', '/api/service/reconfigure',
                                      {'configPath': str(self.usb / '照片')}, self.auth())
        finally:
            self.engine.busy = False
        self.assertEqual(code, 400)
        self.assertIn('当前有操作正在进行，请稍后再试', json.loads(body)['error'])


class UiTests(unittest.TestCase):
    def setUp(self):
        self.web = Path(__file__).resolve().parents[1] / 'web'

    def test_ui_uses_relative_api(self):
        script = (self.web / 'app.js').read_text(encoding='utf-8')
        self.assertNotIn("'/api", script)
        self.assertIn("assetUrl('api' + path", script)

    def test_html_has_icon_and_setup_fields(self):
        html = (self.web / 'index.html').read_text(encoding='utf-8')
        self.assertIn('assets/homeassistant.png', html)
        self.assertIn('name="path"', html)
        self.assertIn('8123', html)
        self.assertIn('__PLUGIN_VERSION__', html)
        self.assertTrue((self.web / 'assets' / 'homeassistant.png').is_file())

    def test_html_has_no_media_directory_leftovers(self):
        """Home Assistant 只需要配置目录：页面上不该再出现「媒体目录」这类字段。"""
        html = (self.web / 'index.html').read_text(encoding='utf-8')
        self.assertNotIn('name="configPath"', html)          # 初始化只有一个 path 字段
        self.assertNotIn('id="reconfigureMedia"', html)
        self.assertNotIn('media_abs', html)
        script = (self.web / 'app.js').read_text(encoding='utf-8')
        self.assertNotIn('media_abs', script)
        self.assertNotIn('mediaDirectory', script)

    def test_healthcheck_note_lives_in_the_notes_card(self):
        """「健康检查已关」是说明性文案，放「说明与限制」里，状态栏只显示运行信息。"""
        html = (self.web / 'index.html').read_text(encoding='utf-8')
        self.assertIn('容器健康检查是关闭的', html)
        script = (self.web / 'app.js').read_text(encoding='utf-8')
        self.assertNotIn('健康检查已关', script)
        self.assertIn("info.push('镜像 ' + s.imageVersion)", script)

    def test_ui_documents_host_network(self):
        """host 网络是刻意的：页面上必须写清楚它直接用宿主机的 8123，没有端口映射。"""
        html = (self.web / 'index.html').read_text(encoding='utf-8')
        self.assertIn('host</code> 网络', html)
        self.assertIn('不做端口映射', html)
        self.assertIn('网络模式是 host', html)
        self.assertIn('8123', html)
        self.assertIn('18200', html)                     # 插件自己的服务端口也写清楚
        self.assertNotIn('8123→8123', html)              # 不能再留端口映射的旧说法
        self.assertNotIn('端口映射：宿主机', html)

    def test_root_running_note_is_documented_in_the_ui(self):
        """容器以 root 运行是刻意的，必须在页面上说清楚，否则会被当成配置错误。"""
        html = (self.web / 'index.html').read_text(encoding='utf-8')
        self.assertIn('官方容器以 root 运行', html)

    def test_html_references_existing_files(self):
        html = (self.web / 'index.html').read_text(encoding='utf-8')
        referenced = re.findall(r'(?:href|src)="([^"]+\.(?:css|js|png))(?:\?[^"]*)?"', html)
        self.assertTrue(referenced)
        for name in referenced:
            self.assertTrue((self.web / name).is_file(), name)

    def test_ui_has_storage_location_switcher(self):
        html = (self.web / 'index.html').read_text(encoding='utf-8')
        self.assertIn('id="roots"', html)
        script = (self.web / 'app.js').read_text(encoding='utf-8')
        self.assertIn('state.roots', script)                     # 位置列表来自状态接口
        self.assertIn("'/browse?root='", script)                 # 浏览接口带上位置序号

    def test_ui_shows_absolute_paths(self):
        script = (self.web / 'app.js').read_text(encoding='utf-8')
        self.assertIn('s.config_abs', script)
        html = (self.web / 'index.html').read_text(encoding='utf-8')
        # 绝对路径可能很长：用可换行的 textarea，而不是单行 input
        self.assertIn('<textarea name="path"', html)
        self.assertIn('<textarea id="reconfigureConfig"', html)

    def test_ui_shows_client_version_from_ha_version_file(self):
        """状态栏优先显示 .HA_VERSION 里的版本，读不到才退回镜像口径。"""
        script = (self.web / 'app.js').read_text(encoding='utf-8')
        self.assertIn("info.push('Home Assistant ' + s.clientVersion)", script)
        self.assertIn('s.imageVersion', script)

    def test_ui_shows_which_image_is_used_and_why(self):
        """页面要显示「当前使用镜像 + 原因」：老 Docker 上固定到 2026.2.0 时用户能看懂。"""
        html = (self.web / 'index.html').read_text(encoding='utf-8')
        script = (self.web / 'app.js').read_text(encoding='utf-8')
        self.assertIn('id="imageInfo"', html)
        self.assertIn("'当前使用镜像：' + s.image", script)
        self.assertIn('s.imageReason', script)
        self.assertIn("$('imageInfo').hidden", script)
        # 说明与限制里要讲清楚 zstd 层这回事与「升级 Docker 后自动回到 stable」
        self.assertIn('不支持 zstd 压缩层', html)
        self.assertIn('2026.2.0', html)
        self.assertIn('自动回到最新的', html)

    def test_ui_has_directory_settings_block(self):
        """改目录 / 重新初始化入口，两个动作都要确认并写清后果。"""
        html = (self.web / 'index.html').read_text(encoding='utf-8')
        self.assertIn('id="directorySettings"', html)
        self.assertIn('id="reconfigure"', html)
        self.assertIn('id="reset"', html)
        self.assertIn('btn danger', html)
        script = (self.web / 'app.js').read_text(encoding='utf-8')
        self.assertIn("call('/service/reconfigure'", script)
        self.assertIn("call('/service/reset'", script)
        self.assertIn('window.confirm(', script)
        # 后果说明：原数据不会被删除 / 用户数据目录不受影响
        self.assertIn('不会被删除', script)
        self.assertIn('文件不受影响', script)

    def test_ui_reconfigure_dialog_covers_config_directory(self):
        """「修改目录」弹窗只改配置目录（含「用私有目录」）。"""
        html = (self.web / 'index.html').read_text(encoding='utf-8')
        for name in ('id="reconfigureDialog"', 'id="reconfigureConfigChoose"',
                     'id="reconfigureConfigPrivate"', 'id="reconfigureSubmit"'):
            self.assertIn(name, html)
        script = (self.web / 'app.js').read_text(encoding='utf-8')
        # 配置目录三种语义：省略 configPath（保持）/ 空串（私有）/ 路径（新位置）
        self.assertIn("if (choice.mode === 'private') return { configPath: '' };", script)
        self.assertIn("if (choice.mode === 'path') return { configPath: choice.path };", script)
        self.assertIn('function reconfigureQuestion(', script)   # 确认文案按选择动态生成

    def test_ui_session_header_matches_server(self):
        script = (self.web / 'app.js').read_text(encoding='utf-8')
        html = (self.web / 'index.html').read_text(encoding='utf-8')
        self.assertIn("'X-Homeassistant-Session': session", script)
        self.assertIn('name="homeassistant-session"', html)
        self.assertIn('name="csrf-token"', html)

    def test_css_matches_the_app_container_id(self):
        html = (self.web / 'index.html').read_text(encoding='utf-8')
        css = (self.web / 'styles.css').read_text(encoding='utf-8')
        self.assertIn('id="homeassistant-app"', html)
        self.assertIn('#homeassistant-app', css)
        self.assertIn("$('homeassistant-app').isConnected", (self.web / 'app.js').read_text(encoding='utf-8'))


class FrontendBehaviourTests(unittest.TestCase):
    """跑 tests/ui_harness.cjs（Node + 最小 DOM 桩）验证前端真实行为。

    覆盖多位置选择器、绝对路径显示，以及「修改目录」的三种配置目录语义
    （省略 configPath＝保持当前、空串＝插件私有目录、路径＝新位置）与动态确认文案。
    本机没有 node 时跳过：插件本身不依赖 node，跑在 NAS 上时这条用例不参与。
    """

    @unittest.skipIf(shutil.which('node') is None, '本机没有 node，跳过前端行为校验')
    def test_ui_harness_passes(self):
        plugin = Path(__file__).resolve().parents[1]
        node = shutil.which('node')
        result = subprocess.run([node, str(plugin / 'tests' / 'ui_harness.cjs'), str(plugin)],
                                cwd=plugin, timeout=180)
        self.assertEqual(result.returncode, 0, '前端行为校验失败（上面应能看到 FAIL 行）')


class DeployTests(unittest.TestCase):
    """首次安装靠 deploy/install-on-nas.sh（商店之外唯一的入口），这里盯住它。

    与 router-center 的同类用例同源：重点是「一份都不能少的文件」「商店/客户端原样
    使用的文件里不能有占位符」「安装脚本确实会渲染单元文件」「版本号三处一致」。
    """

    def setUp(self):
        self.plugin = Path(__file__).resolve().parents[1]
        self.deploy = self.plugin / 'deploy'
        self.install = (self.deploy / 'install-on-nas.sh').read_text(encoding='utf-8')
        self.uninstall = (self.deploy / 'uninstall-on-nas.sh').read_text(encoding='utf-8')

    def _load_helper(self, name):
        """按文件路径加载 deploy 下的辅助脚本。

        不能用 sys.path + import：其它插件的测试也会 import 同名的
        register_plugin / native_layout，会撞上已经缓存的那个模块。
        """
        import importlib.util

        path = self.deploy / name
        spec = importlib.util.spec_from_file_location('ha_deploy_' + name[:-3], path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_install_assets_exist(self):
        for name in ('install-on-nas.sh', 'uninstall-on-nas.sh', 'register_plugin.py',
                     'native_layout.py', 'control', 'plugin-meta.json',
                     'xiaomi-homeassistant.service', 'xiaomi-homeassistant.nginx.conf'):
            with self.subTest(name=name):
                self.assertTrue((self.deploy / name).is_file(), name + ' 不能缺')

    def test_assets_used_verbatim_have_no_placeholder(self):
        """商店与客户端是原样使用这些文件的：一个占位符都不能有。

        踩过：nginx 配置里残留 __PLUGIN_PORT__ 会让 nginx -t 直接失败、插件更新报错。
        """
        for name in ('xiaomi-homeassistant.nginx.conf', 'plugin-meta.json', 'control'):
            with self.subTest(name=name):
                text = (self.deploy / name).read_text(encoding='utf-8')
                self.assertIsNone(re.search(r'__[A-Z0-9_]+__', text), name + ' 里不能有占位符')

    def test_service_unit_is_rendered_by_the_installer(self):
        unit = (self.deploy / 'xiaomi-homeassistant.service').read_text(encoding='utf-8')
        self.assertIn('NAS_USER_ID=__NAS_USER_ID__', unit)
        self.assertIn('__NAS_USER_ID__', self.install)          # 安装脚本确实会替换它
        self.assertIn('DATA_DIR=/data/plugin/homeassistant/data', unit)
        self.assertIn('LOCAL_ROOT=/nas/pool0/__NAS_USER_ID__/data', unit)
        self.assertIn('LOCAL_ROOTS=/nas/pool0/__NAS_USER_ID__/data:/nas/mnt/usb', unit)
        self.assertIn('Environment=PORT=18200', unit)

    def test_install_renders_and_validates_placeholders(self):
        self.assertIn('__[A-Z0-9_]+__', self.install)           # 渲染后残留占位符的硬校验
        self.assertIn('nginx -t', self.install)
        self.assertIn('systemctl daemon-reload', self.install)
        self.assertIn('systemctl reload nginx', self.install)
        self.assertIn('systemctl enable', self.install)
        self.assertIn('systemctl restart', self.install)

    def test_placeholder_check_skips_runtime_page_templates(self):
        """占位符检查只盯部署产物，**不能**扫 src/ui 下的页面文件。

        真机踩过：index.html 里的 __SESSION_TOKEN__ / __CSRF_TOKEN__ /
        __PLUGIN_VERSION__ 是插件服务端在返回页面时替换的运行时占位符（与
        jellyfin / transmission / 网络邻居一致），必须留在文件里；把它们当成
        "部署残留"会让安装脚本误报并异常退出。
        """
        self.assertNotIn("grep -rlE '__[A-Z0-9_]+__' '${REMOTE_UI_SRC}'", self.install)
        # 部署产物仍然要零残留：逐个点名检查
        for target in ('/etc/nginx/conf.d/luci/xiaomi-homeassistant.conf',
                       '${REMOTE_RELEASE}/deploy/xiaomi-homeassistant.nginx.conf',
                       '${REMOTE_RELEASE}/deploy/plugin-meta.json',
                       '${REMOTE_RELEASE}/control',
                       '${REMOTE_UI_HOME}/scripts/control',
                       '${REMOTE_UI_HOME}/INFO'):
            with self.subTest(target=target):
                self.assertIn(target, self.install)

    def test_placeholder_check_demands_runtime_tokens_in_the_page(self):
        """反向断言：页面模板**必须**留着服务端要替换的令牌，误删了要红。"""
        self.assertIn("'__SESSION_TOKEN__' '${REMOTE_UI_SRC}/index.html'", self.install)
        self.assertIn("'__CSRF_TOKEN__' '${REMOTE_UI_SRC}/index.html'", self.install)
        self.assertIn("'__PLUGIN_VERSION__' '${REMOTE_UI_SRC}/index.html'", self.install)
        html = (self.plugin / 'web' / 'index.html').read_text(encoding='utf-8')
        for token in ('__SESSION_TOKEN__', '__CSRF_TOKEN__', '__PLUGIN_VERSION__'):
            with self.subTest(token=token):
                self.assertIn(token, html)
        # app.js 读的是页面里的 meta 标签，不需要自己的占位符
        self.assertNotIn('__SESSION_TOKEN__', (self.plugin / 'web' / 'app.js').read_text(encoding='utf-8'))

    def test_failed_self_check_never_claims_success(self):
        """自检失败要汇总失败项并非零退出，且**绝不**打印「安装完成」。"""
        failure_line = self.install.index('安装未通过自检，失败项')
        done_line = self.install.index('自检全部通过，安装完成。')
        self.assertLess(failure_line, done_line)                # 失败分支写在成功之前
        self.assertIn('exit 1', self.install[failure_line:done_line])
        self.assertIn('fail_check', self.install)
        # 失败项名字要收集并汇总，方便定位
        self.assertIn('FAILURES="${FAILURES}${FAILURES:+, }$1"', self.install)
        # 硬检查（占位符）就地退出，同样不打印"安装完成"
        self.assertIn('终止安装（不打印"安装完成"）。', self.install)
        # 成功文案带「自检全部通过」，和失败路径彻底分开
        self.assertIn('自检全部通过，安装完成。', self.install)

    def test_install_lands_the_layout_the_plugin_expects(self):
        """目录布局必须与商店安装一致，否则 404 或开机被强制卸载。"""
        self.assertIn('/data/plugin/homeassistant/releases/', self.install)
        self.assertIn('/data/plugin/homeassistant/current', self.install)
        self.assertIn("plugin/${PLUGIN_KEY}", self.install)     # /home/<用户>/plugin/homeassistant/…
        self.assertIn('/src/ui', self.install)                  # 客户端 UI 落在 src/ui 下
        self.assertIn('/data/plugin/${NAS_USER_ID}.list', self.install)
        self.assertIn('register_plugin.py', self.install)
        self.assertIn('native_layout.py', self.install)
        self.assertIn('/data/plugin/www/icon/${PLUGIN_KEY}.icon', self.install)
        # abstract 覆盖 src/ 下全部文件，所以补齐结构必须是最后一步
        self.assertLess(self.install.index('native_layout.py'),
                        self.install.index('7/8 启动服务'))

    def test_install_self_checks(self):
        """自检要覆盖：健康检查、服务状态、两个端口、注册表、插件结构、占位符残留。"""
        for probe in ('/healthz', 'systemctl is-active', 'ss -ltn', '已经有监听',
                      '插件服务端口', 'Home Assistant 端口', '注册表', '插件结构',
                      '部署产物无占位符', '单元无占位符', '页面模板保留会话'):
            with self.subTest(probe=probe):
                self.assertIn(probe, self.install)

    def test_install_defaults_match_the_registry_entry(self):
        """安装脚本里的默认编号/端口必须与 plugin-meta.json 一致。

        插件**未上架**（`scripts/build_apps.py` 的 `PACKAGE_SPECS` 里没有 homeassistant
        条目，原因见 README「未上架」一节），所以这里只与插件自带的 plugin-meta.json
        对齐；将来把清单条目加回来时，编号/端口也要沿用这两个值。
        """
        meta = json.loads((self.deploy / 'plugin-meta.json').read_text(encoding='utf-8'))
        self.assertEqual(meta['plugin'], 'homeassistant')
        self.assertEqual(meta['id'], 11022)
        self.assertEqual(meta['name'], 'Home Assistant')
        self.assertEqual(meta['service'], 'xiaomi-homeassistant.service')
        self.assertEqual(meta['uiKey'], 'homeassistant')
        self.assertIn('PLUGIN_ID="${PLUGIN_ID:-11022}"', self.install)
        self.assertIn('PLUGIN_PORT="${PLUGIN_PORT:-18200}"', self.install)
        self.assertIn('HA_PORT="${HA_PORT:-8123}"', self.install)

    def test_install_uses_release_dir_name_the_plugin_can_parse(self):
        """发布目录名必须是 <版本>-<时间戳>-<pid>，否则 installed_version() 解析不出来。"""
        self.assertIn('RELEASE_ID="${VERSION}-$(date +%Y%m%d%H%M%S)-$$"', self.install)
        with patch('engine.__file__',
                   '/data/plugin/homeassistant/releases/0.1.0-1790960335-238127/engine.py'):
            self.assertEqual(installed_version(), '0.1.0')

    def test_install_fails_loudly(self):
        """失败要非零退出且把原因写出来，不能静默成功。"""
        self.assertIn('set -euo pipefail', self.install)
        self.assertGreaterEqual(self.install.count('exit 1'), 2)
        self.assertGreaterEqual(self.install.count('exit 2'), 3)
        for name in ('register_plugin.py', 'native_layout.py'):
            with self.subTest(name=name):
                text = (self.deploy / name).read_text(encoding='utf-8')
                self.assertIn('SystemExit(1)', text)

    def test_uninstall_keeps_user_data_by_default(self):
        self.assertIn("PURGE:-0", self.uninstall)
        self.assertIn("DROP_CONTAINER:-0", self.uninstall)
        self.assertIn('systemctl disable --now', self.uninstall)
        self.assertIn('/data/plugin/www/icon/homeassistant.icon', self.uninstall)
        self.assertNotIn('rm -rf /data/plugin/homeassistant/data', self.uninstall)
        self.assertIn('用户存储里的 Home Assistant 配置目录', self.uninstall)

    def test_version_file_matches_the_engine(self):
        version = (self.plugin / 'VERSION').read_text(encoding='utf-8').strip()
        self.assertRegex(version, r'^\d+\.\d+\.\d+$')
        self.assertEqual(version, VERSION)

    def test_register_helper_writes_a_record_without_placeholders(self):
        """注册表条目结构与商店安装器一致，且 frontend.title 干净。"""
        helper = self._load_helper('register_plugin.py')
        record = helper.plugin_record(11022, '0.1.0', 1700000000, '18200')
        self.assertEqual(record['frontend']['title'], 'Home Assistant')
        self.assertIsNone(re.search(r'__[A-Z0-9_]+__', json.dumps(record, ensure_ascii=False)))
        self.assertEqual(record['info']['id'], 11022)
        self.assertEqual(record['info']['plugin'], 'homeassistant')
        self.assertEqual(record['info']['port'], '18200')
        self.assertIs(record['status'], 'running')
        helper.assert_placeholders_absent(record)                 # 不该抛
        with self.assertRaises(RuntimeError):
            helper.assert_placeholders_absent({'frontend': {'title': '__NAS_IP__'}})
        with self.assertRaises(RuntimeError):
            helper.assert_id_available({'emby': {'info': {'id': 11022, 'name': 'Emby'}}}, 11022)
        helper.assert_id_available({'emby': {'info': {'id': 11007}}}, 11022)   # 不同编号放行

    def test_register_helper_writes_the_registry_file(self):
        helper = self._load_helper('register_plugin.py')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertEqual(helper.main(['--user', 'u123456', '--registry-root',
                                          str(root), '--quiet']), 0)
            path = root / 'u123456.list'
            data = json.loads(path.read_text(encoding='utf-8'))
            self.assertIn('homeassistant', data)
            self.assertEqual(data['homeassistant']['info']['version'], '0.1.0')
            # 再跑一次要能覆盖自己，且先备份
            self.assertEqual(helper.main(['--user', 'u123456', '--registry-root',
                                          str(root), '--version', '0.1.1', '--quiet']), 0)
            self.assertTrue((root / 'u123456.list.homeassistant.bak').is_file())
            data = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(data['homeassistant']['info']['version'], '0.1.1')

    def test_register_helper_refuses_a_taken_plugin_id(self):
        helper = self._load_helper('register_plugin.py')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'u123456.list').write_text(
                json.dumps({'emby': {'info': {'id': 11022, 'name': 'Emby'}}}), encoding='utf-8')
            # main() 直接抛 RuntimeError；命令行入口 __main__ 把它转成 exit 1
            with self.assertRaises(RuntimeError) as caught:
                helper.main(['--user', 'u123456', '--registry-root', str(root), '--quiet'])
            self.assertIn('已被 Emby', str(caught.exception))
            data = json.loads((root / 'u123456.list').read_text(encoding='utf-8'))
            self.assertNotIn('homeassistant', data)                # 冲突时不写坏原文件

    def test_register_helper_exits_nonzero_on_conflict(self):
        """命令行入口：编号被占时以非零码退出（不是静默成功）。"""
        checker = sys.executable
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'u123456.list').write_text(
                json.dumps({'emby': {'info': {'id': 11022, 'name': 'Emby'}}}), encoding='utf-8')
            result = subprocess.run(
                [checker, str(self.deploy / 'register_plugin.py'), '--user', 'u123456',
                 '--registry-root', str(root), '--quiet'],
                capture_output=True, text=True, timeout=60)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('已被 Emby', (result.stdout or '') + (result.stderr or ''))

    @staticmethod
    def _shell(checker):
        """找一个真正能 `-n` 解析的 shell。

        Windows 上 PATH 里的 bash.exe 往往是 WSL 启动器（在 -n 下直接以 127 失败），
        所以先试 Git for Windows 自带的 sh/bash，最后才退回 PATH。
        """
        candidates = [r'C:\Program Files\Git\usr\bin\%s.exe' % checker,
                      r'C:\Program Files\Git\bin\%s.exe' % checker,
                      '/bin/%s' % checker, '/usr/bin/%s' % checker]
        found = shutil.which(checker)
        if found:
            candidates.append(found)
        for candidate in candidates:
            if Path(candidate).is_file():
                return candidate
        return None

    def test_install_script_is_valid_shell(self):
        """`sh -n`（任务验收要求）与 `bash -n` 都要过——这两个脚本是 sh 兼容写法。"""
        checkers = [name for name in (self._shell('sh'), self._shell('bash')) if name]
        if not checkers:
            self.skipTest('本机没有可用的 sh/bash，跳过 shell 语法检查')
        targets = ['install-on-nas.sh', 'uninstall-on-nas.sh', 'control']
        for checker in checkers:
            for name in targets:
                with self.subTest(checker=Path(checker).name, name=name):
                    result = subprocess.run([checker, '-n', str(self.deploy / name)],
                                            capture_output=True, text=True, timeout=60)
                    self.assertEqual(result.returncode, 0, result.stderr)

    def test_control_script_is_valid_shell(self):
        checker = self._shell('sh')
        if not checker:
            self.skipTest('本机没有可用的 sh，跳过 shell 语法检查')
        result = subprocess.run([checker, '-n', str(self.deploy / 'control')],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
