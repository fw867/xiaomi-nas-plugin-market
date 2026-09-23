import http.client
import json
import os
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from engine import (Engine, Error, IMAGE, NAME, LABEL, PORT, VERSION, confined,
                    container_config, installed_version)
from server import Server


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'root'
        self.root.mkdir()
        (self.root / 'MiShare').mkdir()
        self.engine = Engine(Path(self.tmp.name) / 'private', self.root)

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
        self.engine.config = {'relative': 'MiShare', 'media': str(folder),
                              'device': s.st_dev, 'inode': s.st_ino + 1}
        with self.assertRaises(Error):
            self.engine.check_directory()

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
        return {'owner': 'owner-token', 'relative': 'MiShare', 'media': str(folder),
                'uid': 1000, 'gid': 1000, 'device': stat.st_dev, 'inode': stat.st_ino,
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
        self.engine.config = {'owner': 'o', 'relative': 'MiShare', 'media': str(media),
                              'uid': 1000, 'gid': 1000, 'device': mstat.st_dev, 'inode': mstat.st_ino,
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
        self.assertEqual(self.engine.config['relative'], 'MiShare')
        self.assertEqual(self.engine.config['media'], str(self.root / 'MiShare'))
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
        self.engine.config = {'relative': 'MiShare'}
        with self.assertRaises(Error):
            self.engine.setup('MiShare')

    @unittest.skipUnless(os.name == 'posix', 'requires POSIX ownership semantics')
    def test_setup_rejects_path_outside_root(self):
        with self.assertRaises(Error):
            self.engine.setup('../etc')


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.engine = Engine(Path(self.tmp.name) / 'data', self.tmp.name, dev=True)
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


class UiTests(unittest.TestCase):
    def test_ui_calls_the_api_with_relative_paths(self):
        """插件页挂在 /plugin/<用户>/emby/ 下，接口必须用相对路径。

        回归用例：写成 fetch('/api/status') 会打到站点根，nginx 没有对应 location，
        返回 404，插件页只会显示「正在连接 / 请求失败（HTTP 404）」。
        """
        script = (Path(__file__).resolve().parents[1] / 'web' / 'app.js').read_text(encoding='utf-8')
        self.assertNotIn("'/api", script)
        self.assertNotIn('"/api', script)
        self.assertIn("assetUrl('api' + path", script)


    def test_page_version_comes_from_server(self):
        """页脚不能写死版本，必须留占位符由服务端填实际安装版本。"""
        html = (Path(__file__).resolve().parents[1] / 'web' / 'index.html').read_text(encoding='utf-8')
        self.assertIn('__PLUGIN_VERSION__', html)


if __name__ == '__main__':
    unittest.main()
