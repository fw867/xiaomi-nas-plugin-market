import http.client
import json
import os
import re
import tempfile
import threading
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from engine import (Engine, Error, IMAGE, NAME, LABEL, PORT, VERSION, confined,
                    container_config, installed_version, parse_roots, root_label)
from server import Server


def docker_stub(method, path, body=None, timeout=30):
    """Docker API 桩：容器不存在（404），其余视为成功。"""
    if path.endswith('/json'):
        return 404, b'{}'
    return 200, b'{}'


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
        (self.root / 'MiShare').mkdir()
        self.engine = Engine(Path(self.tmp.name) / 'private', self.root)

    def join_worker(self):
        """等后台操作结束：launch() 是异步的（页面靠轮询状态看结果）。"""
        if self.engine.worker:
            self.engine.worker.join(30)

    def test_confined_normal(self):
        self.assertEqual(confined(self.root, 'MiShare'), self.root / 'MiShare')

    def test_confined_accepts_storage_root(self):
        self.assertEqual(confined(self.root, ''), self.root)

    def test_reject_traversal(self):
        for name in ['../', '/etc', '.', 'MiShare/..', 'MiShare//x', '.hidden', 'MiShare\\x']:
            with self.subTest(name=name), self.assertRaises(Error):
                confined(self.root, name)

    @unittest.skipUnless(os.name == 'posix', 'Windows 需要额外权限才能创建符号链接')
    def test_reject_symlink(self):
        (self.root / 'link').symlink_to(self.root / 'MiShare')
        with self.assertRaises(Error):
            confined(self.root, 'link')

    @unittest.skipUnless(os.name == 'posix', 'Windows 需要额外权限才能创建符号链接')
    def test_browse_hides_symlinks(self):
        (self.root / 'alias').symlink_to(self.root / 'MiShare')
        self.assertEqual(self.engine.browse(''), [{'name': 'MiShare', 'path': 'MiShare'}])

    def test_container_isolation(self):
        cfg = container_config({'owner': 'test', 'uid': 1000, 'gid': 1000, 'media': '/test/media',
                                'config': '/test/private/config'})
        self.assertEqual(cfg['Image'], IMAGE)
        self.assertEqual(cfg['Labels'], {LABEL: 'test'})
        host = cfg['HostConfig']
        self.assertEqual(host['PortBindings'],
                         {str(PORT) + '/tcp': [{'HostIp': '0.0.0.0', 'HostPort': str(PORT)}]})
        # 端口必须在顶层 ExposedPorts 里一起声明：只给 PortBindings 时，
        # Engine API 会静默忽略镜像 EXPOSE 里没有的端口，映射不生效。
        self.assertEqual(cfg['ExposedPorts'], {str(PORT) + '/tcp': {}})
        # 失败后自动重启但限次，避免持续 OOM 时变成无限重启循环
        self.assertEqual(host['RestartPolicy'], {'Name': 'on-failure', 'MaximumRetryCount': 3})
        # 不能提权、不能改网络模式、不挂 docker socket 或设备
        self.assertNotIn('Privileged', host)
        self.assertNotIn('NetworkMode', host)
        sources = [m['Source'] for m in host['Mounts']]
        self.assertEqual(len(sources), 2)
        self.assertIn('/test/media', sources)
        self.assertIn('/test/private/config', sources)
        self.assertFalse(any('docker.sock' in s for s in sources))
        self.assertFalse(any('/dev/dri' in s for s in sources))
        # 媒体目录必须可写：Emby 需要把元数据和字幕写回媒体文件夹
        self.assertFalse(any(m.get('ReadOnly') for m in host['Mounts']))
        # 资源限制
        # 2 GiB：1 GiB 时 Emby 扫描媒体库会被 cgroup OOM 杀掉
        self.assertEqual(host['Memory'], 2 * 1024 * 1024 * 1024)
        self.assertEqual(host['MemorySwap'], 2 * 1024 * 1024 * 1024)
        self.assertEqual(host['NanoCpus'], 2 * 10 ** 9)
        self.assertEqual(host['PidsLimit'], 512)
        self.assertEqual(host['SecurityOpt'], ['no-new-privileges:true'])

    def test_dev_cannot_start(self):
        self.engine.dev = True
        with self.assertRaises(Error):
            self.engine.launch('start', {})

    def test_installed_version_from_release_directory(self):
        """页脚要显示实际装上的包版本，不能再是源码里写死的常量。

        回归用例：发布目录是 releases/<版本>-<时间戳>-<pid>，页脚却一直显示
        代码里的 VERSION，装了 0.1.6 的包页面仍写 0.1.0。
        """
        with patch('engine.__file__',
                   '/data/plugin/emby/releases/0.1.6-1789827953-8538/engine.py'):
            self.assertEqual(installed_version(), '0.1.6')

    def test_installed_version_falls_back_in_source_tree(self):
        """源码树里跑（开发、预览）解析不出发布目录，回退到 VERSION。"""
        self.assertEqual(installed_version(), VERSION)

    def test_snapshot_reports_installed_version(self):
        self.assertEqual(self.engine.snapshot()['version'], installed_version())

    def test_snapshot_before_setup(self):
        state = self.engine.snapshot()
        self.assertFalse(state['configured'])
        self.assertFalse(state['running'])
        self.assertEqual(state['port'], PORT)

    def test_unknown_action_rejected(self):
        with self.assertRaises(Error):
            self.engine.launch('exec', {})

    def test_directory_identity_change(self):
        folder = self.root / 'MiShare'
        s = folder.stat()
        self.engine.config = {'media_relative': 'MiShare', 'media': str(folder),
                              'media_device': s.st_dev, 'media_inode': s.st_ino + 1}
        with self.assertRaises(Error):
            self.engine.check_directories()

    def test_foreign_container_not_stopped(self):
        self.engine.config = {'owner': 'mine'}
        foreign = json.dumps({'Config': {'Labels': {LABEL: 'foreign'}}}).encode('utf-8')
        with patch('engine.docker_api', return_value=(200, foreign)) as api:
            with self.assertRaises(Error):
                self.engine.stop()
        # 只读了一次容器详情就拒绝接管，没有发出任何写操作
        self.assertEqual(api.call_count, 1)
        self.assertEqual(api.call_args.args[0], 'GET')
        self.assertIn('/containers/' + NAME + '/json', api.call_args.args[1])

    def test_stop_remembers_preference(self):
        self.engine.config = {'owner': 'mine', 'enabled': True}
        with patch.object(self.engine, 'owned', return_value={'State': {'Running': True}}), \
                patch('engine.docker_api', return_value=(204, b'')) as api:
            self.engine.stop()
        self.assertEqual(api.call_count, 1)
        self.assertEqual(api.call_args.args[0], 'POST')
        self.assertEqual(api.call_args.args[1], '/containers/' + NAME + '/stop?t=30')
        self.assertFalse(json.loads(self.engine.cfgfile.read_text(encoding='utf-8'))['enabled'])

    def _engine_config(self):
        folder = self.root / 'MiShare'
        stat = folder.stat()
        cfg = self.engine.data / 'config'
        cfg.mkdir(parents=True, exist_ok=True)
        cstat = cfg.stat()
        return {'owner': 'owner-token', 'media_relative': 'MiShare', 'media': str(folder),
                'media_root': str(self.root),
                'uid': 1000, 'gid': 1000, 'media_device': stat.st_dev, 'media_inode': stat.st_ino,
                'config': str(cfg), 'config_relative': '',
                'config_device': cstat.st_dev, 'config_inode': cstat.st_ino, 'enabled': True}

    def test_start_pulls_and_runs_the_container(self):
        """回归：容器还不存在时，点「启动」必须真的 pull 镜像、再建并启动容器。

        安装插件本身不会部署 Docker（和 qB 下载一致），要等插件页完成初始化；
        这条路径此前没有覆盖，出现过「装完没有任何 Docker 操作」的疑问。
        """
        self.engine.config = self._engine_config()
        calls = []

        def fake_api(method, path, body=None, timeout=30):
            calls.append((method, path, body, timeout))
            if path == '/containers/' + NAME + '/json':
                return 404, b'{"message":"No such container"}'
            if path.startswith('/images/create'):
                return 200, b'{"status":"Downloaded newer image"}\n'
            return 201, b'{}'

        with patch('engine.docker_api', side_effect=fake_api), \
                patch('engine.emby_info', return_value={}):
            self.engine.start()

        # 顺序：查容器（404）→ pull → create → start
        self.assertEqual(calls[0][0], 'GET')
        self.assertEqual(calls[0][1], '/containers/' + NAME + '/json')

        self.assertEqual(calls[1][0], 'POST')
        self.assertEqual(calls[1][1].split('?')[0], '/images/create')
        self.assertIn('fromImage', calls[1][1])
        # 镜像很大，pull 的超时要放宽
        self.assertEqual(calls[1][3], 1800)

        self.assertEqual(calls[2][0], 'POST')
        self.assertEqual(calls[2][1], '/containers/create?name=' + NAME)
        cfg = calls[2][2]
        self.assertEqual(cfg['Image'], IMAGE)
        self.assertEqual(cfg['Labels'], {LABEL: 'owner-token'})
        self.assertIn(str(self.root / 'MiShare'),
                      [m['Source'] for m in cfg['HostConfig']['Mounts']])

        self.assertEqual(calls[3][0], 'POST')
        self.assertEqual(calls[3][1], '/containers/' + NAME + '/start')
        self.assertEqual(len(calls), 4)

    def test_start_only_starts_an_existing_container(self):
        """容器已存在时不能再建一次，只把它启动起来。"""
        self.engine.config = self._engine_config()
        with patch.object(self.engine, 'owned', return_value={'State': {'Running': False}}), \
                patch('engine.docker_api', return_value=(204, b'')) as api, \
                patch('engine.emby_info', return_value={}):
            self.engine.start()
        self.assertEqual(api.call_count, 1)
        self.assertEqual(api.call_args.args[0], 'POST')
        self.assertEqual(api.call_args.args[1], '/containers/' + NAME + '/start')

    def test_service_stop_preserves_enabled(self):
        self.engine.config = {'enabled': True}
        with patch.object(self.engine, 'owned', return_value=None):
            self.engine.stop(remember=False)
        self.assertTrue(self.engine.config['enabled'])

    def test_setup_accepts_custom_config_directory(self):
        """可以指定存储里的一个目录作为 Emby 配置目录，便于备份与迁移。"""
        if not self.root.stat().st_uid or not self.root.stat().st_gid:
            self.skipTest('requires non-root test directory owner')
        (self.root / 'EmbyConfig').mkdir()

        def fake_api(method, path, body=None, timeout=30):
            if path == '/info':
                return 200, b'{"Architecture":"aarch64"}'
            return 404, b'{"message":"No such container"}'

        with patch('engine.os.chown'), patch('engine.docker_api', side_effect=fake_api):
            self.engine.setup('MiShare', 'EmbyConfig')
        self.assertEqual(self.engine.config['config_relative'], 'EmbyConfig')
        self.assertEqual(self.engine.config['config'], str(self.root / 'EmbyConfig'))
        cfg = container_config(self.engine.config)
        mounts = {m['Target']: m['Source'] for m in cfg['HostConfig']['Mounts']}
        self.assertEqual(mounts['/config'], str(self.root / 'EmbyConfig'))
        self.assertEqual(mounts['/mnt/media'], str(self.root / 'MiShare'))

    def test_setup_rejects_config_overlapping_media(self):
        """配置目录不能与媒体目录相同或互相包含，否则两边会互相污染。"""
        if not self.root.stat().st_uid or not self.root.stat().st_gid:
            self.skipTest('requires non-root test directory owner')
        (self.root / 'MiShare' / 'cfg').mkdir()
        with self.assertRaises(Error):
            self.engine.setup('MiShare', 'MiShare')
        with self.assertRaises(Error):
            self.engine.setup('MiShare', 'MiShare/cfg')

    def _configured_engine(self, config_relative):
        folder = self.root / config_relative
        folder.mkdir(parents=True, exist_ok=True)
        stat = folder.stat()
        media = self.root / 'MiShare'
        mstat = media.stat()
        self.engine.config = {'owner': 'o', 'media_relative': 'MiShare', 'media': str(media),
                              'media_root': str(self.root),
                              'uid': 1000, 'gid': 1000, 'media_device': mstat.st_dev,
                              'media_inode': mstat.st_ino,
                              'config': str(folder), 'config_relative': config_relative,
                              'config_device': stat.st_dev, 'config_inode': stat.st_ino, 'enabled': True}
        return folder

    def test_relocate_config_migrates_and_clears_old(self):
        """已运行的实例换配置目录时，必须先停容器、复制、核对，再删旧目录。"""
        if not self.root.stat().st_uid or not self.root.stat().st_gid:
            self.skipTest('requires non-root test directory owner')
        old = self._configured_engine('OldConfig')
        (old / 'data').mkdir()
        (old / 'data' / 'library.db').write_text('x')
        new = self.root / 'NewConfig'
        new.mkdir()

        with patch.object(self.engine, 'stop') as stop, patch.object(self.engine, 'remove') as remove:
            self.engine.relocate_config('NewConfig')

        # 先停容器再复制，最后删掉旧容器以便按新挂载重建
        stop.assert_called_once()
        remove.assert_called_once()
        self.assertEqual(self.engine.config['config'], str(new))
        self.assertEqual(self.engine.config['config_relative'], 'NewConfig')
        self.assertEqual((new / 'data' / 'library.db').read_text(), 'x')
        self.assertFalse(old.exists(), '核对一致后应删除旧目录')

    def test_relocate_refuses_non_empty_target(self):
        """新目录非空时直接拒绝，防止和已有文件混在一起。"""
        if not self.root.stat().st_uid or not self.root.stat().st_gid:
            self.skipTest('requires non-root test directory owner')
        self._configured_engine('OldConfig')
        new = self.root / 'NewConfig'
        new.mkdir()
        (new / 'keep.txt').write_text('keep')
        with patch.object(self.engine, 'stop'), patch.object(self.engine, 'remove'):
            with self.assertRaises(Error):
                self.engine.relocate_config('NewConfig')
        self.assertTrue((new / 'keep.txt').exists())
        self.assertEqual(self.engine.config['config_relative'], 'OldConfig', '失败的迁移不应改动记录')

    def test_relocate_refuses_media_overlap(self):
        if not self.root.stat().st_uid or not self.root.stat().st_gid:
            self.skipTest('requires non-root test directory owner')
        self._configured_engine('OldConfig')
        (self.root / 'MiShare' / 'cfg').mkdir()
        with patch.object(self.engine, 'stop'), patch.object(self.engine, 'remove'):
            with self.assertRaises(Error):
                self.engine.relocate_config('MiShare/cfg')

    @unittest.skipUnless(os.name == 'posix', 'requires POSIX ownership semantics')
    def test_setup_keeps_media_owner_and_marks_enabled(self):
        before = (self.root / 'MiShare').stat()

        def fake_api(method, path, body=None, timeout=30):
            if path == '/info':
                return 200, b'{"Architecture":"aarch64"}'
            # 同名容器不存在 → 可以继续初始化
            return 404, b'{"message":"No such container"}'

        with patch('engine.os.chown'), patch('engine.docker_api', side_effect=fake_api):
            self.engine.setup('MiShare')
        after = (self.root / 'MiShare').stat()
        self.assertEqual((before.st_uid, before.st_gid), (after.st_uid, after.st_gid))
        self.assertEqual(self.engine.config['media_relative'], 'MiShare')
        self.assertEqual(self.engine.config['media'], str(self.root / 'MiShare'))
        self.assertEqual(self.engine.config['media_root'], str(self.root))
        self.assertTrue(self.engine.config['enabled'])
        self.assertTrue((self.engine.data / 'config').is_dir())

    @unittest.skipUnless(os.name == 'posix', 'requires POSIX ownership semantics')
    def test_setup_refuses_existing_container(self):
        def fake_api(method, path, body=None, timeout=30):
            if path == '/info':
                return 200, b'{"Architecture":"aarch64"}'
            return 200, json.dumps({'Config': {'Labels': {LABEL: 'other'}}}).encode('utf-8')

        with patch('engine.docker_api', side_effect=fake_api):
            with self.assertRaises(Error):
                self.engine.setup('MiShare')

    @unittest.skipUnless(os.name == 'posix', 'requires POSIX ownership semantics')
    def test_setup_is_one_shot(self):
        self.engine.config = {'media_relative': 'MiShare'}
        with self.assertRaises(Error):
            self.engine.setup('MiShare')

    @unittest.skipUnless(os.name == 'posix', 'requires POSIX ownership semantics')
    def test_setup_rejects_path_outside_root(self):
        with self.assertRaises(Error):
            self.engine.setup('../etc')

    def test_fuse_mount_identity_is_refreshed_not_rejected(self):
        """厂商存储池是 FUSE：每次挂载都会换设备号/inode 号，不该据此拒绝启动。"""
        folder = self.root / 'MiShare'
        s = folder.stat()
        self.engine.config = {'media_relative': 'MiShare', 'media': str(folder),
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


class HealthcheckTests(unittest.TestCase):
    """容器健康检查必须显式关掉。

    Emby 当前镜像没带 HEALTHCHECK，但一旦上游加上，它就会定时打 HTTP 接口，
    让 Emby 反复读写 SQLite 的 -shm/-wal；媒体库与配置目录在机械盘上，
    硬盘就再也进不了休眠（Jellyfin 已经踩过这个坑）。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'root'
        self.root.mkdir()
        (self.root / 'MiShare').mkdir()
        (self.root / 'Cfg').mkdir()
        self.engine = Engine(Path(self.tmp.name) / 'private', self.root)
        self.engine.config = {
            'owner': 'tok', 'enabled': True, 'uid': 1000, 'gid': 1000,
            'media_relative': 'MiShare', 'media': str(self.root / 'MiShare'),
            'config': str(self.root / 'Cfg'), 'config_relative': 'Cfg',
        }

    def test_container_is_created_without_healthcheck(self):
        cfg = container_config({'owner': 'test', 'uid': 1000, 'gid': 1000,
                                'media': '/test/media', 'config': '/test/config'})
        self.assertEqual(cfg['Healthcheck'], {'Test': ['NONE']})

    def test_inherited_healthcheck_detection(self):
        cases = [
            ({'Config': {}}, False),
            ({'Config': {'Healthcheck': None}}, False),
            ({'Config': {'Healthcheck': {'Test': []}}}, False),
            ({'Config': {'Healthcheck': {'Test': ['NONE']}}}, False),
            ({'Config': {'Healthcheck': {'Test': ['none']}}}, False),
            ({'Config': {'Healthcheck': {'Test': ['CMD', 'curl x']}}}, True),
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
                patch('engine.emby_info', return_value={}), \
                patch('engine.docker_api', side_effect=fake_api):
            self.engine.start()
        return calls

    def test_start_recreates_container_that_inherited_healthcheck(self):
        item = {
            'Config': {'Labels': {LABEL: 'tok'}, 'Healthcheck': {'Test': ['CMD', 'curl x']}},
            'State': {'Running': True},
        }
        calls = self._run_start(item)
        paths = [path for _, path, _ in calls]
        self.assertIn('/containers/' + NAME + '/stop?t=30', paths)
        self.assertIn('/containers/' + NAME, paths)                        # DELETE
        self.assertIn('/containers/create?name=' + NAME, paths)
        self.assertIn('/containers/' + NAME + '/start', paths)
        self.assertLess(paths.index('/containers/' + NAME + '/stop?t=30'),
                        paths.index('/containers/create?name=' + NAME))
        create = next(body for _, path, body in calls if path.startswith('/containers/create'))
        self.assertEqual(create['Healthcheck'], {'Test': ['NONE']})
        settings = json.loads(self.engine.cfgfile.read_text(encoding='utf-8'))
        self.assertTrue(settings['healthcheck_off'])
        self.assertTrue(settings['enabled'])

    def test_start_does_not_touch_container_without_healthcheck(self):
        item = {
            'Config': {'Labels': {LABEL: 'tok'}, 'Healthcheck': {'Test': ['NONE']}},
            'State': {'Running': True},
        }
        calls = self._run_start(item)
        self.assertEqual([path for _, path, _ in calls], [])
        self.assertTrue(json.loads(self.engine.cfgfile.read_text(encoding='utf-8'))['healthcheck_off'])

    def test_snapshot_reports_healthcheck_flag(self):
        self.engine.config['healthcheck_off'] = True
        with patch.object(self.engine, 'owned', return_value=None):
            self.assertTrue(self.engine.snapshot()['healthcheckOff'])
            self.engine.config['healthcheck_off'] = False
            self.assertFalse(self.engine.snapshot()['healthcheckOff'])


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

    def join_worker(self):
        if self.engine.worker:
            self.engine.worker.join(30)

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
            ('/data/plugin/emby/media', 'media'),
        ]
        for path, expected in cases:
            with self.subTest(path=path):
                self.assertEqual(root_label(path), expected)

    def test_parse_roots_ignores_empty_entries(self):
        self.assertEqual(parse_roots(''), [])
        self.assertEqual(parse_roots('   '), [])
        self.assertEqual(parse_roots(None), [])

    @unittest.skipIf(os.name == 'nt', 'Windows 盘符里的冒号与 LOCAL_ROOTS 的分隔符冲突（NAS 上是 Linux）')
    def test_parse_roots_keeps_order_and_dedupes(self):
        self.assertEqual(parse_roots(str(self.usb)), [str(self.usb)])
        self.assertEqual(parse_roots(':'.join(['', str(self.pool), str(self.usb), str(self.pool)])),
                         [str(self.pool), str(self.usb)])
        self.assertEqual(parse_roots('relative:' + str(self.pool)), [str(self.pool)])

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
        with nas_owned(), patch('engine.docker_api', side_effect=docker_stub):
            self.engine.setup(str(folder))
            state = self.engine.snapshot()
        config = json.loads(self.engine.cfgfile.read_text(encoding='utf-8'))
        self.assertEqual(config['media_root'], str(self.usb))
        self.assertEqual(config['media_relative'], '照片')
        self.assertEqual(config['media'], str(folder))
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

    def test_setup_rejects_path_outside_roots(self):
        outside = self.base / 'outside'
        outside.mkdir()
        with nas_owned(), patch('engine.docker_api', side_effect=docker_stub):
            with self.assertRaises(Error) as caught:
                self.engine.setup(str(outside))
        self.assertIn('所选目录必须位于已挂载的存储位置内', str(caught.exception))
        self.assertFalse(self.engine.cfgfile.exists())

    def test_setup_accepts_relative_path_on_the_first_root(self):
        """相对路径提交仍然可用（旧语义），根记成第 0 个位置。"""
        with nas_owned(), patch('engine.docker_api', side_effect=docker_stub):
            self.engine.setup('下载', '仅池内')
            state = self.engine.snapshot()
        config = json.loads(self.engine.cfgfile.read_text(encoding='utf-8'))
        self.assertEqual(config['media_root'], str(self.pool))
        self.assertEqual(config['media_relative'], '下载')
        self.assertEqual(config['config_root'], str(self.pool))
        self.assertEqual(state['config_abs'], str(self.pool / '仅池内'))
        self.assertFalse(state['config_private'])

    def _legacy_config(self, folder, **extra):
        stat = folder.stat()
        config = {
            'owner': 'tok', 'uid': 1000, 'gid': 1000,
            'media_relative': '照片', 'media': str(folder),
            'media_device': stat.st_dev, 'media_inode': stat.st_ino,
            'config': '', 'config_relative': '', 'config_device': 0, 'config_inode': 0,
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
        engine.config = self._legacy_config(folder, media_root=str(self.usb))
        engine.check_directories()
        self.assertEqual(engine.config['media_root'], str(self.usb))


class ReconfigureTests(unittest.TestCase):
    """「修改目录」：换位置但保留数据，容器必须按新宿主路径重建。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.pool = self.base / 'pool'
        self.usb = self.base / 'usb'
        (self.pool / 'Media').mkdir(parents=True)
        (self.pool / 'NewMedia').mkdir()
        (self.pool / 'Cfg').mkdir()
        (self.usb / 'Media2').mkdir(parents=True)
        (self.usb / 'Cfg2').mkdir()
        # 用户数据：换目录/重新初始化都不许碰
        (self.pool / 'Media' / 'movie.mkv').write_text('movie')
        (self.pool / 'Cfg' / 'library.db').write_text('db')
        self.engine = Engine(self.base / 'private', self.pool, roots=[self.pool, self.usb])
        self.owner = nas_owned()
        self.owner.__enter__()
        self.addCleanup(self.owner.__exit__, None, None, None)

    def join_worker(self):
        if self.engine.worker:
            self.engine.worker.join(30)

    def _prepare(self, media_relative='Media', config_relative='Cfg'):
        media = self.pool / media_relative
        mstat = media.stat()
        cfg = self.pool / config_relative
        cstat = cfg.stat()
        self.engine.config = {'owner': 'tok', 'uid': 1000, 'gid': 1000, 'enabled': True,
                              'media_root': str(self.pool), 'media_relative': media_relative,
                              'media': str(media), 'media_device': mstat.st_dev,
                              'media_inode': mstat.st_ino,
                              'config_root': str(self.pool), 'config_relative': config_relative,
                              'config': str(cfg), 'config_device': cstat.st_dev,
                              'config_inode': cstat.st_ino}
        atomic = self.engine.cfgfile
        atomic.write_text(json.dumps(self.engine.config), encoding='utf-8')

    def _stub(self, calls, container_missing=True):
        """Docker 桩：容器不存在（或已在），其余调用都当成功。"""
        def fake_api(method, path, body=None, timeout=30):
            calls.append((method, path, body))
            if path == '/containers/' + NAME + '/json':
                if container_missing:
                    return 404, b'{"message":"No such container"}'
                return 200, json.dumps({'Config': {'Labels': {LABEL: 'tok'}},
                                        'State': {'Running': True}}).encode()
            if path.startswith('/containers/create'):
                return 201, b'{}'
            if path.startswith('/images/create'):
                return 200, b'{"status":"ok"}\n'
            return 200, b'{}'
        return fake_api

    def test_reconfigure_switches_directory_and_rebuilds_container(self):
        self._prepare()
        calls = []
        with patch('engine.docker_api', side_effect=self._stub(calls)), \
                patch('engine.emby_info', return_value={}):
            self.engine.reconfigure(str(self.usb / 'Media2'), str(self.usb / 'Cfg2'))
            state = self.engine.snapshot()
        creates = [body for _, path, body in calls if path.startswith('/containers/create')]
        self.assertEqual(len(creates), 1)
        mounts = {m['Target']: m['Source'] for m in creates[0]['HostConfig']['Mounts']}
        self.assertEqual(mounts['/mnt/media'], str(self.usb / 'Media2'))
        self.assertEqual(mounts['/config'], str(self.usb / 'Cfg2'))
        self.assertEqual(self.engine.config['media_root'], str(self.usb))
        self.assertEqual(self.engine.config['config_root'], str(self.usb))
        self.assertEqual(state['media_abs'], str(self.usb / 'Media2'))
        self.assertEqual(state['config_abs'], str(self.usb / 'Cfg2'))
        # 数据不动
        self.assertEqual((self.pool / 'Media' / 'movie.mkv').read_text(), 'movie')
        self.assertEqual((self.pool / 'Cfg' / 'library.db').read_text(), 'db')
        # 新配置要落盘，并且留有备份
        settings = json.loads(self.engine.cfgfile.read_text(encoding='utf-8'))
        self.assertEqual(settings['media'], str(self.usb / 'Media2'))
        backups = list(self.engine.data.glob('settings.json.bak-*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text(encoding='utf-8'))['media'],
                         str(self.pool / 'Media'))

    def test_reconfigure_stops_old_container_before_creating(self):
        self._prepare()
        calls = []
        with patch('engine.docker_api',
                   side_effect=self._stub(calls, container_missing=False)), \
                patch('engine.emby_info', return_value={}):
            self.engine.reconfigure(str(self.pool / 'NewMedia'), 'Cfg')
        paths = [path for _, path, _ in calls]
        self.assertIn('/containers/' + NAME + '/stop?t=30', paths)
        self.assertIn('/containers/' + NAME, paths)               # DELETE
        self.assertLess(paths.index('/containers/' + NAME + '/stop?t=30'),
                        paths.index('/containers/create?name=' + NAME))
        create = next(body for _, path, body in calls if path.startswith('/containers/create'))
        mounts = {m['Target']: m['Source'] for m in create['HostConfig']['Mounts']}
        self.assertEqual(mounts['/mnt/media'], str(self.pool / 'NewMedia'))

    def test_reconfigure_failure_rolls_back_config_and_container(self):
        """重建失败必须回滚：配置回到旧路径，旧容器按旧挂载重建。"""
        self._prepare()
        calls = []
        seen = []

        def fake_api(method, path, body=None, timeout=30):
            calls.append((method, path, body))
            if path == '/containers/' + NAME + '/json':
                return 404, b'{"message":"No such container"}'
            if path.startswith('/containers/create'):
                media = body['HostConfig']['Mounts'][1]['Source']
                seen.append(media)
                if media == str(self.usb / 'Media2'):
                    # 新位置不可用：Docker 拒绝建容器
                    raise Error('Docker 操作失败，请检查镜像网络、端口 ' + str(PORT) + ' 和可用资源；未修改其他容器')
                return 201, b'{}'
            return 200, b'{}'

        with patch('engine.docker_api', side_effect=fake_api):
            with self.assertRaises(Error) as caught:
                self.engine.reconfigure(str(self.usb / 'Media2'), str(self.usb / 'Cfg2'))
        self.assertIn('已回滚到原目录', str(caught.exception))
        self.assertEqual(self.engine.config['media'], str(self.pool / 'Media'))
        self.assertEqual(self.engine.config['media_root'], str(self.pool))
        settings = json.loads(self.engine.cfgfile.read_text(encoding='utf-8'))
        self.assertEqual(settings['media'], str(self.pool / 'Media'))
        # 过程：按新挂载建容器（失败）→ 按旧挂载重建回来
        self.assertEqual(seen, [str(self.usb / 'Media2'), str(self.pool / 'Media')])
        self.assertEqual((self.usb / 'Media2').is_dir(), True, '新目录本身不能被删')

    def test_reconfigure_refuses_when_busy(self):
        self._prepare()
        self.engine.busy = True
        with self.assertRaises(Error) as caught:
            self.engine.launch('reconfigure', {'path': str(self.usb / 'Media2')})
        self.assertIn('当前有操作正在进行，请稍后再试', str(caught.exception))

    def test_reconfigure_requires_setup(self):
        with self.assertRaises(Error):
            self.engine.reconfigure(str(self.usb / 'Media2'))

    def test_reconfigure_rejects_unchanged_directories(self):
        self._prepare()
        with self.assertRaises(Error) as caught:
            self.engine.reconfigure('Media', 'Cfg')
        self.assertIn('没有变化', str(caught.exception))

    def test_reconfigure_rejects_path_outside_roots(self):
        self._prepare()
        outside = self.base / 'outside'
        outside.mkdir()
        with patch('engine.docker_api', side_effect=docker_stub):
            with self.assertRaises(Error) as caught:
                self.engine.reconfigure(str(outside))
        self.assertIn('所选目录必须位于已挂载的存储位置内', str(caught.exception))
        self.assertEqual(self.engine.config['media'], str(self.pool / 'Media'))

    def test_reconfigure_rejects_overlapping_config_directory(self):
        self._prepare()
        (self.pool / 'NewMedia' / 'cfg').mkdir()
        with patch('engine.docker_api', side_effect=docker_stub):
            with self.assertRaises(Error):
                self.engine.reconfigure('NewMedia', 'NewMedia/cfg')

    def test_reconfigure_accepts_relative_paths(self):
        """相对路径提交仍然可用（旧语义）：走第 0 个位置。"""
        self._prepare()
        calls = []
        with patch('engine.docker_api', side_effect=self._stub(calls)), \
                patch('engine.emby_info', return_value={}):
            self.engine.reconfigure('NewMedia')
        self.assertEqual(self.engine.config['media'], str(self.pool / 'NewMedia'))
        self.assertEqual(self.engine.config['media_relative'], 'NewMedia')
        self.assertEqual(self.engine.config['media_root'], str(self.pool))

    def test_reconfigure_action_reports_snapshot(self):
        """走 launch('reconfigure') 这条前端实际路径：成功后状态就是新目录。"""
        self._prepare()
        with patch('engine.docker_api', side_effect=self._stub([], container_missing=False)), \
                patch('engine.emby_info', return_value={}), \
                patch.object(self.engine, 'owned',
                             return_value={'Config': {'Labels': {LABEL: 'tok'}},
                                           'State': {'Running': True}}):
            self.engine.launch('reconfigure', {'path': str(self.usb / 'Media2'),
                                               'configPath': str(self.usb / 'Cfg2')})
            self.join_worker()
            state = self.engine.snapshot()
        self.assertFalse(state['busy'])
        self.assertEqual(state['error'], '')
        self.assertEqual(state['media_abs'], str(self.usb / 'Media2'))
        self.assertTrue(state['configured'])


class ResetTests(unittest.TestCase):
    """「重新初始化」：清空插件配置、移除容器，绝不动用户数据目录。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.pool = self.base / 'pool'
        (self.pool / 'Media').mkdir(parents=True)
        (self.pool / 'Cfg').mkdir()
        (self.pool / 'Media' / 'movie.mkv').write_text('movie')
        (self.pool / 'Cfg' / 'library.db').write_text('db')
        self.engine = Engine(self.base / 'private', self.pool)
        folder = self.pool / 'Media'
        cfg = self.pool / 'Cfg'
        stat, cstat = folder.stat(), cfg.stat()
        self.engine.config = {'owner': 'tok', 'uid': 1000, 'gid': 1000, 'enabled': True,
                              'media_root': str(self.pool), 'media_relative': 'Media',
                              'media': str(folder), 'media_device': stat.st_dev,
                              'media_inode': stat.st_ino,
                              'config_root': str(self.pool), 'config_relative': 'Cfg',
                              'config': str(cfg), 'config_device': cstat.st_dev,
                              'config_inode': cstat.st_ino}
        self.engine.cfgfile.write_text(json.dumps(self.engine.config), encoding='utf-8')

    def join_worker(self):
        if self.engine.worker:
            self.engine.worker.join(30)

    def test_reset_requires_confirmation(self):
        with self.assertRaises(Error) as caught:
            self.engine.reset()
        self.assertIn('确认', str(caught.exception))
        self.assertTrue(self.engine.cfgfile.exists(), '未确认时配置不能被清掉')
        self.assertTrue(self.engine.config)

    def test_reset_requires_confirmation_through_launch(self):
        with self.assertRaises(Error) as caught:
            self.engine.launch('reset', {})
        self.assertIn('请确认', str(caught.exception))
        self.assertTrue(self.engine.cfgfile.exists())

    def test_reset_removes_container_and_clears_config_without_touching_data(self):
        with patch.object(self.engine, 'remove') as remove:
            self.engine.reset(True)
        remove.assert_called_once()                                  # 容器被移除
        self.assertFalse(self.engine.cfgfile.exists(), '配置应被移走 → 页面回到初始化表单')
        self.assertIsNone(self.engine.config)
        self.assertFalse(self.engine.snapshot()['configured'])
        # 用户数据一个字节都不能动
        self.assertEqual((self.pool / 'Media' / 'movie.mkv').read_text(), 'movie')
        self.assertEqual((self.pool / 'Cfg' / 'library.db').read_text(), 'db')
        backups = list(self.engine.data.glob('settings.json.bak-*'))
        self.assertEqual(len(backups), 1)

    def test_reset_keeps_going_when_container_removal_fails(self):
        with patch.object(self.engine, 'owned', side_effect=Error('同名容器不属于本插件，拒绝接管')):
            self.engine.reset(True)
        self.assertIsNone(self.engine.config)
        self.assertFalse(self.engine.cfgfile.exists())
        self.assertEqual((self.pool / 'Media' / 'movie.mkv').read_text(), 'movie')

    def test_reset_refuses_when_busy(self):
        self.engine.busy = True
        with self.assertRaises(Error) as caught:
            self.engine.launch('reset', {'confirm': True})
        self.assertIn('当前有操作正在进行，请稍后再试', str(caught.exception))

    @staticmethod
    def _stub(calls):
        def fake_api(method, path, body=None, timeout=30):
            calls.append((method, path, body))
            return 200, json.dumps({'Config': {'Labels': {LABEL: 'tok'}},
                                    'State': {'Running': True}}).encode()
        return fake_api


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name) / 'root'
        root.mkdir()
        (root / 'MiShare').mkdir()
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
        self.token = re.search(r'name="emby-session" content="([^"]+)"', html.decode())[1]
        self.csrf = re.search(r'name="csrf-token" content="([^"]+)"', html.decode())[1]

    def request(self, method, route, data=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port)
        try:
            conn.request(method, route, json.dumps(data) if data is not None else None, headers or {})
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def auth(self):
        return {'X-Emby-Session': self.token, 'X-CSRF-Token': self.csrf, 'Content-Type': 'application/json'}

    def test_unauthenticated_reads_denied(self):
        self.assertEqual(self.request('GET', '/api/status')[0], 401)

    def test_csrf_required(self):
        self.assertEqual(self.request('POST', '/api/service/start', {}, {'X-Emby-Session': self.token})[0], 403)

    def test_status_no_secrets(self):
        code, body = self.request('GET', '/api/status', headers=self.auth())
        self.assertEqual(code, 200)
        self.assertNotIn(self.server.key.hex(), body.decode())

    def test_malformed_session(self):
        self.assertEqual(self.request('GET', '/api/status', headers={'X-Emby-Session': 'garbage'})[0], 401)

    def test_no_arbitrary_file_serving(self):
        for route in ['/engine.py', '/server.py', '/../engine.py', '/assets/../engine.py']:
            with self.subTest(route=route):
                self.assertEqual(self.request('GET', route, headers=self.auth())[0], 404)

    def test_no_generic_api(self):
        for route in ['/api/docker', '/api/emby/System/Info', '/api/exec']:
            with self.subTest(route=route):
                self.assertEqual(self.request('POST', route, {}, self.auth())[0], 404)

    def test_preview_write_blocked(self):
        code, _ = self.request('POST', '/api/service/start', {}, self.auth())
        self.assertEqual(code, 400)

    def test_setup_validates_path_type(self):
        code, _ = self.request('POST', '/api/setup', {'path': ['x']}, self.auth())
        self.assertEqual(code, 400)

    def test_address_prefers_local_network_ip(self):
        """地址要用 NAS 自己的局域网 IP。

        小米客户端是经客户端隧道访问 NAS 的，请求到了插件这里 Host 已经是
        127.0.0.1，用它拼出来的地址在电视/手机上打不开。
        """
        with patch('server.lan_ip', return_value='192.168.1.15'):
            code, body = self.request('GET', '/api/status',
                                      headers={**self.auth(), 'Host': '127.0.0.1:18150'})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)['address'], '192.168.1.15:' + str(PORT))

    def test_address_falls_back_to_client_host(self):
        """取不到本机网卡地址时，退回请求里的 Host。"""
        with patch('server.lan_ip', return_value=''):
            code, body = self.request('GET', '/api/status',
                                      headers={**self.auth(), 'Host': 'nas.123456.local:8443'})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)['address'], 'nas.123456.local:' + str(PORT))

    def test_address_ignores_garbage_host(self):
        """本机地址取不到、Host 又不合法时，宁可为空也不要给出错的地址。"""
        with patch('server.lan_ip', return_value=''):
            code, body = self.request('GET', '/api/status',
                                      headers={**self.auth(), 'Host': 'evil/path'})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)['address'], '')

    def test_wrong_device_cert_gets_no_session(self):
        self.server.dev = False
        code, body = self.request('GET', '/', headers={
            'X-Xiaomi-Client-Verify': 'SUCCESS',
            'X-Xiaomi-Client-DN': 'CN=nas.999999.test.2',
            'X-Real-IP': '192.168.1.2'})
        self.assertIn(b'name="emby-session" content=""', body)

    def test_owner_cert_gets_session(self):
        self.server.dev = False
        _, body = self.request('GET', '/', headers={
            'X-Xiaomi-Client-Verify': 'SUCCESS',
            'X-Xiaomi-Client-DN': 'CN=nas.123456.test.2'})
        self.assertNotIn(b'name="emby-session" content=""', body)

    def test_page_shows_installed_version(self):
        """页脚版本由服务端注入，源码里不再留写死的字符串。"""
        _, body = self.request('GET', '/')
        text = body.decode()
        self.assertNotIn('__PLUGIN_VERSION__', text)
        self.assertIn('Emby · ' + installed_version(), text)

    def test_status_lists_storage_locations(self):
        code, body = self.request('GET', '/api/status', headers=self.auth())
        self.assertEqual(code, 200)
        roots = json.loads(body)['roots']
        self.assertEqual([item['index'] for item in roots], [0, 1])
        self.assertEqual(roots[1]['path'], str(self.usb))
        self.assertEqual(roots[1]['label'], 'usb')

    def test_browse_accepts_root_parameter(self):
        code, body = self.request('GET', '/api/browse?root=1&path=', headers=self.auth())
        self.assertEqual(code, 200)
        self.assertEqual([item['name'] for item in json.loads(body)['items']], ['照片'])
        code, body = self.request('GET', '/api/browse', headers=self.auth())   # 缺省 root=0
        self.assertEqual(code, 200)
        self.assertEqual([item['name'] for item in json.loads(body)['items']], ['MiShare'])

    def test_browse_rejects_unknown_root(self):
        code, body = self.request('GET', '/api/browse?root=9', headers=self.auth())
        self.assertEqual(code, 400)
        self.assertIn('存储位置无效', json.loads(body)['error'])

    def test_service_reset_needs_confirmation(self):
        code, body = self.request('POST', '/api/service/reset', {}, self.auth())
        self.assertEqual(code, 400)
        self.assertIn('确认', json.loads(body)['error'])

    def test_service_reset_requires_csrf(self):
        code, _ = self.request('POST', '/api/service/reset', {'confirm': True},
                               {'X-Emby-Session': self.token})
        self.assertEqual(code, 403)


class UiTests(unittest.TestCase):
    def setUp(self):
        self.web = Path(__file__).resolve().parents[1] / 'web'

    def test_ui_calls_the_api_with_relative_paths(self):
        """插件页挂在 /plugin/<用户>/emby/ 下，接口必须用相对路径。

        回归用例：写成 fetch('/api/status') 会打到站点根，nginx 没有对应 location，
        返回 404，插件页只会显示「正在连接 / 请求失败（HTTP 404）」。
        """
        script = (self.web / 'app.js').read_text(encoding='utf-8')
        self.assertNotIn("'/api", script)
        self.assertNotIn('"/api', script)
        self.assertIn("assetUrl('api' + path", script)

    def test_page_version_comes_from_server(self):
        """页脚不能写死版本，必须留占位符由服务端填实际安装版本。"""
        html = (self.web / 'index.html').read_text(encoding='utf-8')
        self.assertIn('__PLUGIN_VERSION__', html)

    def test_ui_has_storage_location_switcher(self):
        html = (self.web / 'index.html').read_text(encoding='utf-8')
        self.assertIn('id="roots"', html)
        script = (self.web / 'app.js').read_text(encoding='utf-8')
        self.assertIn('state.roots', script)                     # 位置列表来自状态接口
        self.assertIn("'/browse?root='", script)                 # 浏览接口带上位置序号

    def test_ui_shows_absolute_paths(self):
        script = (self.web / 'app.js').read_text(encoding='utf-8')
        self.assertIn('state.media_abs', script)
        self.assertIn('state.config_abs', script)
        html = (self.web / 'index.html').read_text(encoding='utf-8')
        # 绝对路径可能很长：用可换行的 textarea，而不是单行 input
        self.assertIn('<textarea name="path"', html)
        self.assertIn('id="directory"', html)

    def test_ui_has_reconfigure_and_reset_entry_points(self):
        """配置完成后要能改目录 / 重新初始化，两者都带后果说明的确认框。"""
        html = (self.web / 'index.html').read_text(encoding='utf-8')
        self.assertIn('id="reconfigure"', html)
        self.assertIn('id="reset"', html)
        self.assertIn('id="directorySettings"', html)
        script = (self.web / 'app.js').read_text(encoding='utf-8')
        self.assertIn("'/service/reconfigure'", script)
        self.assertIn("'/service/reset'", script)
        self.assertIn('confirm(', script)
        self.assertIn('用户数据', script)                         # 明确说明数据不受影响
        self.assertIn('重建容器', script)


if __name__ == '__main__':
    unittest.main()
