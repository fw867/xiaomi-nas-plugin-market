import http.client
import json
import os
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from engine import Engine, Error, IMAGE, NAME, LABEL, PORT, confined, container_config
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
        cfg = container_config({'owner': 'test', 'uid': 1000, 'gid': 1000, 'media': '/test/media'},
                               Path('/test/private'))
        self.assertEqual(cfg['Image'], IMAGE)
        self.assertEqual(cfg['Labels'], {LABEL: 'test'})
        host = cfg['HostConfig']
        self.assertEqual(host['PortBindings'],
                         {str(PORT) + '/tcp': [{'HostIp': '0.0.0.0', 'HostPort': str(PORT)}]})
        self.assertEqual(host['RestartPolicy'], {'Name': 'no'})
        # 不能提权、不能改网络模式、不挂 docker socket 或设备
        self.assertNotIn('Privileged', host)
        self.assertNotIn('NetworkMode', host)
        sources = [m['Source'] for m in host['Mounts']]
        self.assertEqual(len(sources), 2)
        self.assertIn('/test/media', sources)
        self.assertIn(str(Path('/test/private') / 'config'), sources)
        self.assertFalse(any('docker.sock' in s for s in sources))
        self.assertFalse(any('/dev/dri' in s for s in sources))
        # 媒体目录必须可写：Emby 需要把元数据和字幕写回媒体文件夹
        self.assertFalse(any(m.get('ReadOnly') for m in host['Mounts']))
        # 资源限制
        self.assertEqual(host['Memory'], 1024 * 1024 * 1024)
        self.assertEqual(host['MemorySwap'], 1024 * 1024 * 1024)
        self.assertEqual(host['NanoCpus'], 2 * 10 ** 9)
        self.assertEqual(host['PidsLimit'], 512)
        self.assertEqual(host['SecurityOpt'], ['no-new-privileges:true'])

    def test_dev_cannot_start(self):
        self.engine.dev = True
        with self.assertRaises(Error):
            self.engine.launch('start', {})

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
        stat = (self.root / 'MiShare').stat()
        return {'owner': 'owner-token', 'relative': 'MiShare',
                'media': str(self.root / 'MiShare'), 'uid': 1000, 'gid': 1000,
                'device': stat.st_dev, 'inode': stat.st_ino, 'enabled': True}

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

    def test_address_uses_client_host(self):
        code, body = self.request('GET', '/api/status', headers={**self.auth(), 'Host': 'nas.123456.local:8443'})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)['address'], 'nas.123456.local:' + str(PORT))

    def test_address_ignores_garbage_host(self):
        code, body = self.request('GET', '/api/status', headers={**self.auth(), 'Host': 'evil/path'})
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


class UiTests(unittest.TestCase):
    def test_ui_calls_the_api_with_relative_paths(self):
        """插件页挂在 /plugin/<用户>/emby/ 下，接口必须用相对路径。

        回归用例：写成 fetch('/api/status') 会打到站点根，nginx 没有对应 location，
        返回 404，插件页只会显示「正在连接 / 请求失败（HTTP 404）」。
        """
        script = (Path(__file__).resolve().parents[1] / 'web' / 'app.js').read_text(encoding='utf-8')
        self.assertNotIn("'/api", script)
        self.assertNotIn('"/api', script)
        self.assertIn("fetch('api' + path", script)


if __name__ == '__main__':
    unittest.main()
