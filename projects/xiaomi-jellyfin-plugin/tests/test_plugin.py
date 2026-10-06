import http.client
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from engine import (
    Engine, Error, IMAGE, NAME, LABEL, PORT,
    CONTAINER_HTTP, CONTAINER_HTTPS, CONTAINER_DLNA,
    container_config, confined, installed_version, parse_roots, root_label, VERSION,
    MEMORY_LIMIT, CPU_LIMIT,
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

    def removed(self):
        return any(method == 'DELETE' and path == '/containers/' + NAME
                   for method, path, _ in self.calls)


@contextmanager
def nas_owned():
    """模拟「目录由普通 NAS 用户拥有」的环境。

    Windows 上 os.stat 的 st_uid/st_gid 恒为 0（初始化会被直接拒绝），os.chown
    也不存在；测试要在开发机上跑通初始化流程，所以这里临时把身份号换成 1000。
    """
    real_stat = Path.stat

    def stat(self, *, follow_symlinks=True):
        item = real_stat(self, follow_symlinks=follow_symlinks)
        return os.stat_result((item.st_mode, item.st_ino, item.st_dev, item.st_nlink,
                               1000, 1000, item.st_size, item.st_atime, item.st_mtime,
                               item.st_ctime))

    with patch.object(Path, 'stat', stat), patch('engine.os.chown', create=True):
        yield


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'root'
        self.root.mkdir()
        (self.root / 'Media').mkdir()
        self.engine = Engine(Path(self.tmp.name) / 'private', self.root)

    def test_image_and_identity(self):
        self.assertIn('jellyfin/jellyfin@sha256:', IMAGE.replace(':latest', ''))
        self.assertTrue(IMAGE.startswith('jellyfin/jellyfin:latest@sha256:'))
        self.assertEqual(NAME, 'xiaomi-plugin-jellyfin')
        self.assertEqual(LABEL, 'io.xiaomi-plugin.jellyfin.owner')
        self.assertEqual(PORT, 8097)
        self.assertEqual(CONTAINER_HTTP, 8096)

    def test_confined_rejects_traversal(self):
        self.assertEqual(confined(self.root, 'Media'), self.root / 'Media')
        for name in ['../', '/etc', 'Media/..', '.hidden']:
            with self.subTest(name=name), self.assertRaises(Error):
                confined(self.root, name)

    def test_container_ports_and_persistence(self):
        cfg = container_config({
            'owner': 'tok', 'uid': 1000, 'gid': 1000,
            'media': '/nas/Media', 'config': '/nas/Cfg', 'cache': '/data/cache',
        })
        self.assertEqual(cfg['Image'], IMAGE)
        self.assertEqual(cfg['User'], '1000:1000')
        self.assertEqual(cfg['Labels'], {LABEL: 'tok'})
        host = cfg['HostConfig']
        self.assertEqual(host['PortBindings'], {
            str(CONTAINER_HTTP) + '/tcp': [{'HostIp': '0.0.0.0', 'HostPort': str(PORT)}],
            str(CONTAINER_HTTPS) + '/tcp': [{'HostIp': '0.0.0.0', 'HostPort': str(CONTAINER_HTTPS)}],
            str(CONTAINER_DLNA) + '/udp': [{'HostIp': '0.0.0.0', 'HostPort': str(CONTAINER_DLNA)}],
        })
        self.assertEqual(cfg['ExposedPorts'], {
            str(CONTAINER_HTTP) + '/tcp': {},
            str(CONTAINER_HTTPS) + '/tcp': {},
            str(CONTAINER_DLNA) + '/udp': {},
        })
        mounts = {m['Target']: m['Source'] for m in host['Mounts']}
        self.assertEqual(mounts, {
            '/config': '/nas/Cfg',
            '/cache': '/data/cache',
            '/media': '/nas/Media',
        })
        self.assertNotIn('Privileged', host)
        self.assertNotIn('NetworkMode', host)
        self.assertFalse(any('docker.sock' in s for s in mounts.values()))
        self.assertEqual(host['Memory'], MEMORY_LIMIT)
        self.assertEqual(host['MemorySwap'], MEMORY_LIMIT)      # 与 Memory 相同 = 不给 swap
        self.assertEqual(host['NanoCpus'], CPU_LIMIT)
        self.assertEqual(host['SecurityOpt'], ['no-new-privileges:true'])

    def test_snapshot_before_setup(self):
        state = self.engine.snapshot()
        self.assertFalse(state['configured'])
        self.assertFalse(state['running'])
        self.assertEqual(state['version'], installed_version())

    def test_installed_version_fallback(self):
        with patch('engine.__file__',
                   '/data/plugin/jellyfin/releases/0.1.0-1789828016-8538/engine.py'):
            self.assertEqual(installed_version(), '0.1.0')
        self.assertEqual(installed_version(), VERSION)

    def test_dev_cannot_start(self):
        self.engine.dev = True
        with self.assertRaises(Error):
            self.engine.launch('start', {})

    def test_setup_requires_media_dir(self):
        def fake_api(method, path, body=None, timeout=30):
            return 404, b'{}' if path.endswith('/json') else (200, b'{}')

        with patch('engine.docker_api', side_effect=fake_api):
            with self.assertRaises(Error):
                self.engine.setup('')

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


class HealthcheckTests(unittest.TestCase):
    """容器健康检查必须关掉：它每 30 秒打一次 /health，让 Jellyfin 重写 SQLite
    的 -shm/-wal，再经 fanotify 触发系统索引写 /nas/sys（跨两块盘的 RAID1），
    两块机械盘因此永远不休眠。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'root'
        self.root.mkdir()
        (self.root / 'Media').mkdir()
        (self.root / 'Cfg').mkdir()
        self.engine = Engine(Path(self.tmp.name) / 'private', self.root)
        self.engine.config = {
            'owner': 'tok', 'enabled': True, 'uid': 1000, 'gid': 1000,
            'media': str(self.root / 'Media'), 'media_relative': 'Media',
            'config': str(self.root / 'Cfg'), 'config_relative': 'Cfg',
            'cache': str(self.root / 'Cache'),
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

    def _run_start(self, item):
        calls = []

        def fake_api(method, path, body=None, timeout=30):
            calls.append((method, path, body))
            return 200, b'{}'

        with patch.object(self.engine, 'owned', return_value=item), \
                patch.object(self.engine, 'check_directories'), \
                patch.object(self.engine, 'pull'), \
                patch('engine.jellyfin_info', return_value={}), \
                patch('engine.docker_api', side_effect=fake_api):
            self.engine.start()
        return calls

    def test_start_recreates_container_that_inherited_healthcheck(self):
        item = {
            'Config': {'Labels': {LABEL: 'tok'}, 'Healthcheck': {'Test': ['CMD-SHELL', 'curl x']}},
            'State': {'Running': True},
        }
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
        item = {
            'Config': {'Labels': {LABEL: 'tok'}, 'Healthcheck': {'Test': ['NONE']},
                       'Env': ['TZ=Asia/Shanghai']},
            'HostConfig': {'Memory': MEMORY_LIMIT, 'MemorySwap': MEMORY_LIMIT,
                           'NanoCpus': CPU_LIMIT},
            'State': {'Running': True},
        }
        calls = self._run_start(item)
        paths = [path for _, path, _ in calls]
        self.assertEqual(paths, [])                                       # 运行中就不该有 Docker 调用
        self.assertTrue(json.loads(self.engine.cfgfile.read_text(encoding='utf-8'))['healthcheck_off'])

    def test_start_starts_stopped_container_without_recreating(self):
        item = {
            'Config': {'Labels': {LABEL: 'tok'}, 'Healthcheck': {'Test': ['NONE']},
                       'Env': ['TZ=Asia/Shanghai']},
            'HostConfig': {'Memory': MEMORY_LIMIT, 'MemorySwap': MEMORY_LIMIT,
                           'NanoCpus': CPU_LIMIT},
            'State': {'Running': False},
        }
        calls = self._run_start(item)
        paths = [path for _, path, _ in calls]
        self.assertEqual(paths, ['/containers/' + NAME + '/start'])

    def test_resources_stale_detection(self):
        """内存/CPU 上限只在创建时生效：和现在要求的不一致就得重建。

        回归用例：Jellyfin 扫描媒体库时单个 ffprobe 能吃到 780 MB 匿名内存，
        1 GiB 上限下被 memcg OOM kill（宿主机当时还有 2.3 GB 可用），所以提到 2 GiB；
        已经装好的容器必须能自动换成新上限，而不是等用户卸载重装。
        """
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
        item = {
            'Config': {'Labels': {LABEL: 'tok'}, 'Healthcheck': {'Test': ['NONE']},
                       'Env': ['TZ=Asia/Shanghai']},
            'HostConfig': {'Memory': 1024 * 1024 * 1024, 'MemorySwap': 1024 * 1024 * 1024,
                           'NanoCpus': CPU_LIMIT},
            'State': {'Running': True},
        }
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
            # 历史上写死的假地址（占位符从未被替换）——Jellyfin 会把它当对外地址
            ({'Config': {'Env': ['TZ=Asia/Shanghai',
                                 'JELLYFIN_PublishedServerUrl=http://__NAS_IP__:8097']}}, True),
        ]
        for item, expected in cases:
            with self.subTest(item=item):
                self.assertEqual(Engine.stale_env(item), expected)

    def test_start_recreates_container_with_stale_published_url(self):
        item = {
            'Config': {'Labels': {LABEL: 'tok'}, 'Healthcheck': {'Test': ['NONE']},
                       'Env': ['TZ=Asia/Shanghai',
                               'JELLYFIN_PublishedServerUrl=http://__NAS_IP__:8097']},
            'State': {'Running': True},
        }
        calls = self._run_start(item)
        paths = [path for _, path, _ in calls]
        self.assertIn('/containers/' + NAME + '/stop?t=30', paths)
        self.assertIn('/containers/create?name=' + NAME, paths)
        create = next(body for _, path, body in calls if path.startswith('/containers/create'))
        self.assertEqual(create['Env'], ['TZ=Asia/Shanghai'])
        self.assertFalse([e for e in create['Env'] if 'PublishedServerUrl' in e])

    def test_container_config_no_longer_sets_a_published_url(self):
        """写死的 http://__NAS_IP__:8097 会被 Jellyfin 当成对外地址（见 engine 注释）。"""
        cfg = container_config({'uid': 1, 'gid': 1, 'owner': 'tok', 'config': '/tmp/config',
                                'cache': '/tmp/cache', 'media': '/tmp/media'})
        self.assertFalse([e for e in cfg['Env'] if 'PublishedServerUrl' in e])
        self.assertIn('TZ=Asia/Shanghai', cfg['Env'])
        self.assertNotIn('__NAS_IP__', json.dumps(cfg))

    def test_snapshot_reports_healthcheck_flag(self):
        self.engine.config['healthcheck_off'] = True
        with patch.object(self.engine, 'owned', return_value=None):
            self.assertTrue(self.engine.snapshot()['healthcheckOff'])
            self.engine.config['healthcheck_off'] = False
            self.assertFalse(self.engine.snapshot()['healthcheckOff'])

    def test_fuse_mount_identity_is_refreshed_not_rejected(self):
        """厂商存储池是 FUSE：每次挂载都会换设备号/inode 号，不该据此拒绝启动。"""
        folder = self.root / 'MT'
        folder.mkdir(exist_ok=True)
        s = folder.stat()
        self.engine.config = {'media': str(folder), 'media_relative': 'MT',
                              'media_device': s.st_dev + 7, 'media_inode': s.st_ino + 7}
        with patch('engine.covering_mount', return_value=('/nas/pool0', 'fuse.cfs')), \
                patch('engine.atomic_json') as saved:
            self.engine.check_directories()
        self.assertEqual(self.engine.config['media_device'], s.st_dev)
        self.assertEqual(self.engine.config['media_inode'], s.st_ino)
        self.assertTrue(saved.called)                 # 新值要写回配置
        # 普通盘（设备号稳定）仍然按老规矩拒绝
        self.engine.config['media_device'] = s.st_dev + 7
        with patch('engine.covering_mount', return_value=('/', 'ext4')), \
                patch('engine.atomic_json'), self.assertRaises(Error):
            self.engine.check_directories()
        # 目录不在任何挂载点下（没挂盘）也要拒绝
        self.engine.config['media_device'] = s.st_dev + 7
        with patch('engine.covering_mount', return_value=(None, '')), \
                patch('engine.atomic_json'), self.assertRaises(Error):
            self.engine.check_directories()


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
            ('/data/plugin/jellyfin/media', 'media'),
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
        self.assertEqual(config['media_root'], str(self.usb))
        self.assertEqual(config['media_relative'], '照片')
        self.assertEqual(config['media'], str(folder))
        self.assertEqual(config['media_device'], stat.st_dev)
        self.assertEqual(config['media_inode'], stat.st_ino)
        self.assertEqual(state['media_abs'], str(folder))
        self.assertTrue(state['config_private'])                 # 没选配置目录：在插件私有目录
        self.assertEqual([item['path'] for item in state['roots']],
                         [str(self.pool), str(self.usb)])

    def test_setup_picks_the_longest_matching_root(self):
        nested_root = self.usb / '照片'
        engine = Engine(self.base / 'data6', self.pool, roots=[self.pool, nested_root])
        with nas_owned(), patch('engine.docker_api', side_effect=docker_stub):
            engine.setup(str(nested_root / '2024'))
        config = json.loads(engine.cfgfile.read_text(encoding='utf-8'))
        self.assertEqual(config['media_root'], str(nested_root))
        self.assertEqual(config['media_relative'], '2024')
        self.assertEqual(config['media'], str(nested_root / '2024'))

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
            self.engine.setup('下载', '仅池内')
            state = self.engine.snapshot()
        config = json.loads(self.engine.cfgfile.read_text(encoding='utf-8'))
        self.assertEqual(config['media_root'], str(self.pool))
        self.assertEqual(config['media_relative'], '下载')
        self.assertEqual(config['media'], str(self.pool / '下载'))
        self.assertEqual(config['config_root'], str(self.pool))
        self.assertEqual(config['config_relative'], '仅池内')
        self.assertEqual(state['config_abs'], str(self.pool / '仅池内'))
        self.assertFalse(state['config_private'])

    def _legacy_config(self, folder, **extra):
        stat = folder.stat()
        config = {
            'owner': 'tok', 'uid': 1000, 'gid': 1000,
            'media_relative': '照片', 'media': str(folder),
            'media_device': stat.st_dev, 'media_inode': stat.st_ino,
            'config': '', 'config_relative': '',
            'cache': str(self.base / 'cache'),
        }
        config.update(extra)
        return config

    def test_legacy_config_without_root_field_still_passes(self):
        """旧配置没有 *_root：按绝对路径前缀反查所属位置（回归保证）。

        '照片' 只存在于第 1 个位置（U 盘）下，所以这条用例同时证明校验没有
        退回第 0 个根（否则会报「目录不存在」）。
        """
        self.engine.config = self._legacy_config(self.usb / '照片')
        self.engine.check_directories()
        self.assertNotIn('media_root', self.engine.config)

    def test_legacy_config_fuse_identity_refresh_still_works(self):
        folder = self.usb / '照片'
        stat = folder.stat()
        self.engine.config = self._legacy_config(folder, media_device=stat.st_dev + 7,
                                                 media_inode=stat.st_ino + 7)
        with patch('engine.covering_mount', return_value=('/nas/mnt/usb', 'fuse.cfs')), \
                patch('engine.atomic_json') as saved:
            self.engine.check_directories()
        self.assertEqual(self.engine.config['media_device'], stat.st_dev)
        self.assertEqual(self.engine.config['media_inode'], stat.st_ino)
        self.assertTrue(saved.called)                            # 新身份号要写回配置

    def test_recorded_root_wins_over_the_current_root_list(self):
        """配置里记着根时按它校验：LOCAL_ROOTS 变了也不该无谓拒绝启动。"""
        engine = Engine(self.base / 'data7', self.pool, roots=[self.pool])
        folder = self.usb / '照片'
        stat = folder.stat()
        engine.config = self._legacy_config(folder, media_root=str(self.usb))
        engine.check_directories()
        self.assertEqual(engine.config['media_root'], str(self.usb))


class AdminActionTests(unittest.TestCase):
    """「修改目录」(reconfigure) 与「重新初始化」(reset)。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.pool = self.base / 'pool'
        self.usb = self.base / 'usb'
        self.media = self.pool / '下载'
        self.cfgdir = self.pool / 'cfg'
        self.media.mkdir(parents=True)
        self.cfgdir.mkdir()
        (self.usb / '照片').mkdir(parents=True)
        # 用户数据哨兵：这两个文件在任何操作之后都必须原样还在
        self.sentinel = self.media / 'sentinel.txt'
        self.sentinel.write_text('keep me', encoding='utf-8')
        self.cfg_sentinel = self.cfgdir / 'jellyfin.db'
        self.cfg_sentinel.write_text('db', encoding='utf-8')
        self.engine = Engine(self.base / 'private', self.pool, roots=[self.pool, self.usb])

    def _configure(self, docker):
        """走一遍初始化并造出容器（模拟「已经配好、容器在跑」的现场）。"""
        with nas_owned(), patch('engine.docker_api', side_effect=docker), \
                patch('engine.jellyfin_info', return_value={}):
            self.engine.setup(str(self.media), str(self.cfgdir))
            self.engine.start()
        return json.loads(self.engine.cfgfile.read_text(encoding='utf-8'))

    def _backups(self):
        return sorted(self.engine.data.glob('settings.json.bak-*'))

    def test_reconfigure_switches_directory_and_rebuilds_container(self):
        docker = FakeDocker()
        old = self._configure(docker)
        self.assertEqual(docker.created_mounts()['/media'], str(self.media))
        folder = self.usb / '照片'
        stat = folder.stat()
        with nas_owned(), patch('engine.docker_api', side_effect=docker), \
                patch('engine.jellyfin_info', return_value={}):
            state = self.engine.reconfigure(str(folder))
        config = json.loads(self.engine.cfgfile.read_text(encoding='utf-8'))
        self.assertEqual(config['media'], str(folder))                # 绝对路径
        self.assertEqual(config['media_relative'], '照片')             # 相对路径
        self.assertEqual(config['media_root'], str(self.usb))         # 所属存储位置
        self.assertEqual((config['media_device'], config['media_inode']),
                         (stat.st_dev, stat.st_ino))                  # 身份号
        self.assertEqual(config['owner'], old['owner'])               # 容器归属不变
        self.assertEqual(config['cache'], old['cache'])               # /data 上的挂载不动
        self.assertEqual(self.engine.config['media'], str(folder))
        # 旧容器被删掉，新容器按新 bind 创建并启动
        self.assertTrue(docker.removed())
        self.assertEqual(docker.created_mounts()['/media'], str(folder))
        self.assertEqual(docker.created_mounts()['/config'], str(self.cfgdir))
        self.assertEqual(docker.created_mounts()['/cache'], old['cache'])
        self.assertTrue(docker.container['State']['Running'])
        self.assertEqual(state['media_abs'], str(folder))
        self.assertTrue(state['configured'])
        self.assertEqual(len(self._backups()), 1)                     # 先备份了旧配置
        self.assertEqual(self.sentinel.read_text(encoding='utf-8'), 'keep me')

    def test_reconfigure_rolls_back_when_rebuild_fails(self):
        docker = FakeDocker()
        old = self._configure(docker)
        docker.fail_create = 1                                        # 按新 bind 创建失败
        with nas_owned(), patch('engine.docker_api', side_effect=docker):
            with self.assertRaises(Error) as caught:
                self.engine.reconfigure(str(self.usb / '照片'))
        self.assertIn('Docker 操作失败', str(caught.exception))        # 原因如实上报
        config = json.loads(self.engine.cfgfile.read_text(encoding='utf-8'))
        self.assertEqual(config['media'], str(self.media))            # 配置回滚
        self.assertEqual(config['media_root'], str(self.pool))
        self.assertEqual(self.engine.config['media'], str(self.media))
        # 原容器按旧配置恢复：最后一次创建用的是旧 bind，并且已经启动
        self.assertEqual(docker.created_mounts()['/media'], str(self.media))
        self.assertTrue(docker.container['State']['Running'])
        self.assertEqual(docker.container['Config']['Labels'][LABEL], old['owner'])

    def test_reconfigure_can_move_config_dir_to_private(self):
        """显式传空串＝配置目录改回插件私有目录；原配置目录里的文件同样不动。"""
        docker = FakeDocker()
        self._configure(docker)
        with nas_owned(), patch('engine.docker_api', side_effect=docker), \
                patch('engine.jellyfin_info', return_value={}):
            state = self.engine.reconfigure(str(self.usb / '照片'), '')
        config = json.loads(self.engine.cfgfile.read_text(encoding='utf-8'))
        private = str(self.engine.data / 'config')
        self.assertEqual(config['config'], private)
        self.assertEqual(config['config_root'], '')
        self.assertTrue(state['config_private'])
        self.assertEqual(docker.created_mounts()['/config'], private)
        self.assertEqual(self.cfg_sentinel.read_text(encoding='utf-8'), 'db')

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
        self.assertEqual(self.engine.config['media'], str(self.media))
        self.assertFalse(docker.removed())
        self.assertEqual(self._backups(), [])

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
        self.assertEqual(self.engine.config['media'], str(self.media))

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
        self.assertEqual(state['media_abs'], '')
        self.assertIsNone(self.engine.config)
        self.assertFalse(self.engine.cfgfile.exists())                # 配置已挪走
        backups = self._backups()
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text(encoding='utf-8'))['media'],
                         str(self.media))
        self.assertTrue(docker.removed())                             # 容器已移除
        self.assertIsNone(docker.container)
        # 用户数据一个文件都没动
        self.assertEqual(self.sentinel.read_text(encoding='utf-8'), 'keep me')
        self.assertEqual(self.cfg_sentinel.read_text(encoding='utf-8'), 'db')

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


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name) / 'root'
        root.mkdir()
        (root / 'Media').mkdir()
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
        self.token = re.search(r'name="jellyfin-session" content="([^"]+)"', html.decode())[1]
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
            'X-Jellyfin-Session': self.token,
            'X-CSRF-Token': self.csrf,
            'Content-Type': 'application/json',
        }

    def test_unauthenticated_denied(self):
        self.assertEqual(self.request('GET', '/api/status')[0], 401)

    def test_csrf_required(self):
        self.assertEqual(
            self.request('POST', '/api/service/start', {}, {'X-Jellyfin-Session': self.token})[0], 403)

    def test_status_no_secrets(self):
        code, body = self.request('GET', '/api/status', headers=self.auth())
        self.assertEqual(code, 200)
        self.assertNotIn(self.server.key.hex(), body.decode())

    def test_preview_write_blocked(self):
        code, _ = self.request('POST', '/api/service/start', {}, self.auth())
        self.assertEqual(code, 400)

    def test_page_shows_installed_version(self):
        _, body = self.request('GET', '/')
        text = body.decode()
        self.assertNotIn('__PLUGIN_VERSION__', text)
        self.assertIn('Jellyfin · ' + installed_version(), text)

    def test_status_reports_lan_address_on_8097(self):
        with patch('server.lan_ip', return_value='192.168.1.30'):
            code, body = self.request('GET', '/api/status', headers=self.auth())
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)['address'], 'http://192.168.1.30:8097')

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
        self.assertEqual([item['name'] for item in json.loads(body)['items']], ['Media'])

    def test_browse_rejects_unknown_root(self):
        code, body = self.request('GET', '/api/browse?root=9', headers=self.auth())
        self.assertEqual(code, 400)
        self.assertIn('存储位置无效', json.loads(body)['error'])


class AdminHTTPTests(unittest.TestCase):
    """非预览模式 + Docker 打桩，用真实 HTTP 走一遍修改目录与重新初始化。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.pool = base / 'pool'
        self.usb = base / 'usb'
        self.media = self.pool / '下载'
        self.cfgdir = self.pool / 'cfg'
        self.media.mkdir(parents=True)
        self.cfgdir.mkdir()
        (self.usb / '照片').mkdir(parents=True)
        self.sentinel = self.media / 'sentinel.txt'
        self.sentinel.write_text('keep me', encoding='utf-8')
        self.docker = FakeDocker()
        self.engine = Engine(base / 'data', self.pool, roots=[self.pool, self.usb])
        self.server = Server(('127.0.0.1', 0), self.engine, 'u123456')
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._close)
        _, html = self.request('GET', '/')
        self.token = re.search(r'name="jellyfin-session" content="([^"]+)"', html.decode())[1]
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
            'X-Jellyfin-Session': self.token,
            'X-CSRF-Token': self.csrf,
            'Content-Type': 'application/json',
            'X-Real-IP': '127.0.0.1',
        }

    def _patch(self):
        return patch('engine.docker_api', side_effect=self.docker)

    def test_reconfigure_over_http_reports_new_state(self):
        with nas_owned(), self._patch(), patch('engine.jellyfin_info', return_value={}):
            code, _ = self.request('POST', '/api/setup',
                                   {'path': str(self.media), 'configPath': str(self.cfgdir)},
                                   self.auth())
            self.assertEqual(code, 202)                      # setup 仍是异步的
            self.engine.worker.join(30)
            code, body = self.request('POST', '/api/service/reconfigure',
                                      {'path': str(self.usb / '照片'),
                                       'configPath': str(self.cfgdir)}, self.auth())
        state = json.loads(body)
        self.assertEqual(code, 200)                          # 操作完成即回最新状态
        self.assertTrue(state['ok'])
        self.assertEqual(state['media_abs'], str(self.usb / '照片'))
        self.assertEqual(self.docker.created_mounts()['/media'], str(self.usb / '照片'))
        self.assertTrue(self.docker.removed())

    def test_reconfigure_over_http_without_config_path_keeps_it(self):
        """只提交 path（不带 configPath）＝保持当前配置目录，不能掉回插件私有目录。"""
        with nas_owned(), self._patch(), patch('engine.jellyfin_info', return_value={}):
            self.request('POST', '/api/setup',
                         {'path': str(self.media), 'configPath': str(self.cfgdir)}, self.auth())
            self.engine.worker.join(30)
            code, body = self.request('POST', '/api/service/reconfigure',
                                      {'path': str(self.usb / '照片')}, self.auth())
        state = json.loads(body)
        self.assertEqual(code, 200)
        self.assertEqual(state['config_abs'], str(self.cfgdir))
        self.assertFalse(state['config_private'])
        self.assertEqual(self.docker.created_mounts()['/config'], str(self.cfgdir))

    def test_reset_over_http_needs_confirm_and_keeps_files(self):
        with nas_owned(), self._patch(), patch('engine.jellyfin_info', return_value={}):
            self.request('POST', '/api/setup',
                         {'path': str(self.media), 'configPath': str(self.cfgdir)}, self.auth())
            self.engine.worker.join(30)
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
        self.assertEqual(self.sentinel.read_text(encoding='utf-8'), 'keep me')

    def test_service_action_rejected_while_busy(self):
        self.engine.busy = True
        try:
            code, body = self.request('POST', '/api/service/reconfigure',
                                      {'path': str(self.usb / '照片')}, self.auth())
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
        self.assertIn('assets/jellyfin.png', html)
        self.assertIn('name="path"', html)
        self.assertIn('name="configPath"', html)
        self.assertIn('8097', html)
        self.assertIn('__PLUGIN_VERSION__', html)
        self.assertTrue((self.web / 'assets' / 'jellyfin.png').is_file())

    def test_healthcheck_note_lives_in_the_notes_card(self):
        """「健康检查已关」是说明性文案，放「说明与限制」里，状态栏只显示运行信息。"""
        html = (self.web / 'index.html').read_text(encoding='utf-8')
        self.assertIn('容器健康检查是关闭的', html)
        script = (self.web / 'app.js').read_text(encoding='utf-8')
        self.assertNotIn('健康检查已关', script)
        self.assertIn("info.push('镜像 ' + s.imageVersion)", script)

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
        self.assertIn('s.media_abs', script)
        self.assertIn('s.config_abs', script)
        html = (self.web / 'index.html').read_text(encoding='utf-8')
        # 绝对路径可能很长：用可换行的 textarea，而不是单行 input
        self.assertIn('<textarea name="path"', html)
        self.assertIn('<textarea name="configPath"', html)

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

    def test_ui_reconfigure_dialog_covers_both_directories(self):
        """「修改目录」弹窗要能同时改媒体目录与配置目录（含「用私有目录」）。"""
        html = (self.web / 'index.html').read_text(encoding='utf-8')
        for name in ('id="reconfigureDialog"', 'id="reconfigureMediaChoose"',
                     'id="reconfigureConfigChoose"', 'id="reconfigureConfigPrivate"',
                     'id="reconfigureSubmit"'):
            self.assertIn(name, html)
        script = (self.web / 'app.js').read_text(encoding='utf-8')
        # 配置目录三种语义：省略 configPath（保持）/ 空串（私有）/ 路径（新位置）
        self.assertIn("if (choice.mode === 'private') payload.configPath = '';", script)
        self.assertIn("if (choice.mode === 'path') payload.configPath = choice.path;", script)
        self.assertIn('function reconfigureQuestion(', script)   # 确认文案按选择动态生成
        self.assertIn("(state && state.media_abs) || ''", script)  # 媒体目录默认保持当前


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


if __name__ == '__main__':
    unittest.main()
