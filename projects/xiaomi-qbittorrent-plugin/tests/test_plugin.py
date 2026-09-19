import base64
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import unittest
from unittest.mock import patch

from engine import (Engine, Error, IMAGE, NAME, LABEL, PORT, BT_PORT, VERSION, confined, mutation,
                    password_hash, container_config, installed_version)
from server import Server, accepted


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'root'
        self.root.mkdir()
        (self.root / 'MiShare').mkdir()
        self.engine = Engine(Path(self.tmp.name) / 'private', self.root)

    def test_password_hash_matches_qb(self):
        salt, key = (base64.b64decode(v) for v in password_hash('Example123!').split(':'))
        self.assertEqual(len(salt), 16)
        self.assertEqual(key, hashlib.pbkdf2_hmac('sha512', b'Example123!', salt, 100000, 64))

    def test_password_policy(self):
        for value in ['abc', '12345678', 'abcdefgh', 'ABCD123\n', None]:
            with self.subTest(value=value), self.assertRaises(Error):
                password_hash(value)

    def test_password_salt_random(self):
        self.assertNotEqual(password_hash('Example123'), password_hash('Example123'))

    def test_confined_normal(self):
        self.assertEqual(confined(self.root, 'MiShare'), self.root / 'MiShare')

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

    def test_task_hash_not_all(self):
        for value in ['all', 'x', 'a' * 40 + '|all', None]:
            with self.assertRaises(Error):
                mutation('stop', {'hash': value})

    def test_remove_always_keeps_files(self):
        route, params = mutation('remove', {'hash': 'a' * 40, 'deleteFiles': True})
        self.assertEqual(route, 'torrents/delete')
        self.assertEqual(params['deleteFiles'], 'false')

    def test_only_whitelisted_mutations(self):
        with self.assertRaises(Error):
            mutation('setLocation', {'location': '/config'})

    def test_limits_bounds(self):
        for value in [-1, 11, True, '2']:
            with self.assertRaises(Error):
                mutation('limits', {'download': 0, 'upload': 0, 'active': value})

    def test_limits_units(self):
        route, params = mutation('limits', {'download': 100, 'upload': 200, 'active': 2})
        prefs = json.loads(params['json'])
        self.assertEqual(prefs['dl_limit'], 102400)
        self.assertTrue(prefs['queueing_enabled'])

    def test_magnet_fixed_directory(self):
        route, params = mutation('magnet', {'url': 'magnet:?xt=urn:btih:' + 'a' * 40, 'savepath': '/config'})
        self.assertEqual(params['savepath'], '/downloads')

    def test_non_magnets_rejected(self):
        for url in ['http://example.org/file', 'magnet:?foo=bar', 'magnet:?xt=urn:btih:' + 'a'*40 + '\nhttp://example.org']:
            with self.assertRaises(Error):
                mutation('magnet', {'url': url})

    def test_container_isolation(self):
        cfg = container_config({'owner': 'test', 'uid': 1000, 'gid': 1000, 'download': '/test/downloads'},
                               Path('/test/private'))
        self.assertEqual(cfg['Image'], IMAGE)
        self.assertEqual(cfg['Labels'], {LABEL: 'test'})
        host = cfg['HostConfig']
        self.assertEqual(host['RestartPolicy'], {'Name': 'no'})
        # WebUI 对局域网开放；BT 入站端口也要映射出去，否则别人连不进来
        self.assertEqual(host['PortBindings'], {
            str(PORT) + '/tcp': [{'HostIp': '0.0.0.0', 'HostPort': str(PORT)}],
            str(BT_PORT) + '/tcp': [{'HostIp': '0.0.0.0', 'HostPort': str(BT_PORT)}],
            str(BT_PORT) + '/udp': [{'HostIp': '0.0.0.0', 'HostPort': str(BT_PORT)}],
        })
        # 端口必须在顶层 ExposedPorts 里一起声明：只给 PortBindings 时，
        # Engine API 会静默忽略镜像 EXPOSE 里没有的端口，映射不生效。
        self.assertEqual(cfg['ExposedPorts'],
                         {str(PORT) + '/tcp': {}, str(BT_PORT) + '/tcp': {}, str(BT_PORT) + '/udp': {}})
        # 不能提权、不能改网络模式、不挂 docker socket
        self.assertNotIn('Privileged', host)
        self.assertNotIn('NetworkMode', host)
        sources = [m['Source'] for m in host['Mounts']]
        self.assertEqual(len(sources), 2)
        self.assertEqual([m['Target'] for m in host['Mounts']], ['/config', '/downloads'])
        self.assertIn('/test/downloads', sources)
        self.assertIn(str(Path('/test/private') / 'config'), sources)
        self.assertFalse(any('docker.sock' in s for s in sources))
        # 资源限制
        self.assertEqual(host['Memory'], 512 * 1024 * 1024)
        self.assertEqual(host['MemorySwap'], 512 * 1024 * 1024)
        self.assertEqual(host['NanoCpus'], 1500000000)
        self.assertEqual(host['PidsLimit'], 128)
        self.assertEqual(host['SecurityOpt'], ['no-new-privileges:true'])

    def test_dev_cannot_start(self):
        self.engine.dev = True
        with self.assertRaises(Error):
            self.engine.launch('start', {})

    def _engine_config(self):
        folder = self.root / 'MiShare'
        stat = folder.stat()
        return {'owner': 'owner-token', 'relative': 'MiShare', 'download': str(folder),
                'uid': 1000, 'gid': 1000, 'device': stat.st_dev, 'inode': stat.st_ino, 'enabled': True}

    def test_installed_version_from_release_directory(self):
        """页脚要显示实际装上的包版本，不能再是源码里写死的常量。

        回归用例：发布目录是 releases/<版本>-<时间戳>-<pid>，页脚一度只显示
        代码里写死的 VERSION，跟实际装上的包版本毫无关系。
        """
        with patch('engine.__file__',
                   '/data/plugin/qbittorrent/releases/0.1.2-rc1-1789828016-8538/engine.py'):
            self.assertEqual(installed_version(), '0.1.2-rc1')
        with patch('engine.__file__',
                   '/data/plugin/qbittorrent/releases/0.1.6-1789827953-8538/engine.py'):
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
        self.assertFalse(state['ready'])

    def test_start_pulls_and_runs_the_container(self):
        """回归：容器还不存在时，点「启动」必须真的 pull 镜像、再建并启动容器。

        NAS 上没有 docker 命令行，所有容器操作都走 Docker Engine API；这条路径
        此前没有覆盖，出现过「点启动只报 Docker 不可用」。
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
                patch('engine.qb_request', return_value=(200, b'5.2.3', '')):
            self.engine.start()

        # 顺序：查容器（404）→ pull → create → start
        self.assertEqual(calls[0][0], 'GET')
        self.assertEqual(calls[0][1], '/containers/' + NAME + '/json')

        self.assertEqual(calls[1][0], 'POST')
        self.assertEqual(calls[1][1].split('?')[0], '/images/create')
        self.assertIn('fromImage', calls[1][1])
        # 镜像较大，pull 的超时要放宽
        self.assertEqual(calls[1][3], 900)

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
                patch('engine.qb_request', return_value=(200, b'5.2.3', '')):
            self.engine.start()
        self.assertEqual(api.call_count, 1)
        self.assertEqual(api.call_args.args[0], 'POST')
        self.assertEqual(api.call_args.args[1], '/containers/' + NAME + '/start')

    def test_directory_identity_change(self):
        folder = self.root / 'MiShare'
        s = folder.stat()
        self.engine.config = {'relative': 'MiShare', 'download': str(folder), 'device': s.st_dev, 'inode': s.st_ino + 1}
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
        self.assertEqual(api.call_args.args[1], '/containers/' + NAME + '/stop?t=15')
        self.assertFalse(json.loads(self.engine.cfgfile.read_text())['enabled'])

    def test_service_stop_preserves_enabled(self):
        self.engine.config = {'enabled': True}
        with patch.object(self.engine, 'owned', return_value=None):
            self.engine.stop(remember=False)
        self.assertTrue(self.engine.config['enabled'])

    def test_setup_uses_selected_directory_directly(self):
        """所选目录直接作为下载目录，不再自动嵌套一层 qBDownloads。"""
        # This test runs on a normal non-root Mac user and mocks only chown/Docker.
        if not self.root.stat().st_uid or not self.root.stat().st_gid:
            self.skipTest('requires non-root test directory owner')

        def fake_api(method, path, body=None, timeout=30):
            if path == '/info':
                return 200, b'{"Architecture":"aarch64"}'
            # 同名容器不存在 → 可以继续初始化
            return 404, b'{"message":"No such container"}'

        before = (self.root / 'MiShare').stat()
        with patch('engine.os.chown'), patch('engine.docker_api', side_effect=fake_api):
            self.engine.setup('MiShare', 'Example123')
        after = (self.root / 'MiShare').stat()
        # 不改动所选目录的属主，也不在其中新建子目录
        self.assertEqual((before.st_uid, before.st_gid), (after.st_uid, after.st_gid))
        self.assertEqual(self.engine.config['relative'], 'MiShare')
        self.assertEqual(self.engine.config['download'], str(self.root / 'MiShare'))
        self.assertFalse((self.root / 'MiShare/qBDownloads').exists())
        conf = (self.engine.data / 'config/qBittorrent/qBittorrent.conf').read_text()
        self.assertNotIn('Example123', conf)
        self.assertIn('PortForwardingEnabled=false', conf)
        self.assertIn('CSRFProtection=true', conf)

    def test_setup_refuses_existing_container(self):
        if not self.root.stat().st_uid or not self.root.stat().st_gid:
            self.skipTest('requires non-root test directory owner')

        def fake_api(method, path, body=None, timeout=30):
            if path == '/info':
                return 200, b'{"Architecture":"aarch64"}'
            return 200, json.dumps({'Config': {'Labels': {LABEL: 'other'}}}).encode('utf-8')

        with patch('engine.docker_api', side_effect=fake_api), self.assertRaises(Error):
            self.engine.setup('MiShare', 'Example123')


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.engine = Engine(Path(self.tmp.name) / 'data', self.tmp.name, dev=True)
        self.server = Server(('127.0.0.1', 0), self.engine, 'u123456', dev=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        def close():
            self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.addCleanup(close)
        _, html = self.request('GET', '/')
        self.token = re.search(r'name="qb-session" content="([^"]+)"', html.decode())[1]
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
        return {'X-QB-Session': self.token, 'X-CSRF-Token': self.csrf, 'Content-Type': 'application/json'}

    def test_unauthenticated_reads_denied(self):
        self.assertEqual(self.request('GET', '/api/status')[0], 401)

    def test_csrf_required(self):
        self.assertEqual(self.request('POST', '/api/remove', {}, {'X-QB-Session': self.token})[0], 403)

    def test_status_no_secrets(self):
        code, body = self.request('GET', '/api/status', headers=self.auth())
        self.assertEqual(code, 200)
        self.assertNotIn(self.server.key.hex(), body.decode())

    def test_malformed_session(self):
        self.assertEqual(self.request('GET', '/api/status', headers={'X-QB-Session':'garbage'})[0], 401)

    def test_no_arbitrary_file_serving(self):
        self.assertEqual(self.request('GET', '/engine.py', headers=self.auth())[0], 404)

    def test_no_generic_api(self):
        self.assertEqual(self.request('POST', '/api/app/setPreferences', {}, self.auth())[0], 400)

    def test_preview_write_blocked(self):
        code, body = self.request('POST', '/api/service/start', {}, self.auth())
        self.assertEqual(code, 400)

    def test_qb_login_required(self):
        code, body = self.request('GET', '/api/torrents', headers=self.auth())
        self.assertEqual(code, 400)

    def test_login_password_not_stored(self):
        """只保存 qB 发回的会话 cookie，密码本身不落盘。"""
        with patch('server.qb_request', return_value=(200, b'Ok.', 'SID=' + 'a'*32 + '; HttpOnly')):
            code, _ = self.request('POST', '/api/login', {'password':'secret123'}, self.auth())
        self.assertEqual(code, 200)
        self.assertNotIn('secret123', self.server.qb_session_file.read_text(encoding='utf-8'))

    def test_login_persists_qb_session(self):
        """登录成功后会话落盘：下次进下载列表不必重输密码。"""
        header = 'QBT_SID_18123=' + 'b' * 32 + '; HttpOnly; path=/'
        with patch('server.qb_request', return_value=(204, b'', header)):
            code, _ = self.request('POST', '/api/login', {'password': 'secret123'}, self.auth())
        self.assertEqual(code, 200)
        self.assertEqual(self.server.qb_session(), 'QBT_SID_18123=' + 'b' * 32)
        saved = json.loads(self.server.qb_session_file.read_text(encoding='utf-8'))
        self.assertEqual(saved['cookie'], 'QBT_SID_18123=' + 'b' * 32)
        self.assertGreater(saved['expiry'], 0)

    def test_logout_clears_qb_session(self):
        self.server.save_qb_session('SID=abc')
        self.assertTrue(self.server.qb_session())
        code, _ = self.request('POST', '/api/logout', {}, self.auth())
        self.assertEqual(code, 200)
        self.assertEqual(self.server.qb_session(), '')
        self.assertFalse(self.server.qb_session_file.exists())

    def test_status_reports_lan_address(self):
        """状态卡显示的是局域网地址：客户端隧道里 Host 已被改写成 127.0.0.1。"""
        with patch('server.lan_ip', return_value='192.168.1.15'):
            code, body = self.request('GET', '/api/status', headers=self.auth())
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)['address'], 'http://192.168.1.15:' + str(PORT))

    def test_login_accepts_qb5_response(self):
        """qB 5.x 登录成功返回 204 + 空 body + QBT_SID_<端口> cookie。

        回归用例：照 4.x 的 200 + "Ok." + SID 去校验，密码正确也会被判成
        「登录失败」——qB 日志里记的却是 login success。
        """
        header = 'QBT_SID_18123=' + 'b' * 32 + '; HttpOnly; path=/'
        with patch('server.qb_request', return_value=(204, b'', header)):
            code, _ = self.request('POST', '/api/login', {'password': 'secret123'}, self.auth())
        self.assertEqual(code, 200)
        self.assertTrue(self.server.qb_session().startswith('QBT_SID_18123='))

    def test_login_still_accepts_qb4_response(self):
        with patch('server.qb_request',
                   return_value=(200, b'Ok.', 'SID=' + 'c' * 32 + '; HttpOnly')):
            code, _ = self.request('POST', '/api/login', {'password': 'secret123'}, self.auth())
        self.assertEqual(code, 200)
        self.assertEqual(self.server.qb_session(), 'SID=' + 'c' * 32)

    def test_login_rejects_wrong_password(self):
        with patch('server.qb_request', return_value=(401, b'Unauthorized', '')):
            code, _ = self.request('POST', '/api/login', {'password': 'wrong'}, self.auth())
        self.assertEqual(code, 400)
        self.assertEqual(self.server.qb_session(), '')

    def test_add_response_recognises_both_formats(self):
        """qB 4.x 的 torrents/add 返回 "Ok."，5.x 返回 added_torrent_ids。"""
        self.assertTrue(accepted(b'Ok.'))
        self.assertTrue(accepted(b'{"added_torrent_ids":["abc"]}'))
        self.assertFalse(accepted(b'{"added_torrent_ids":[]}'))
        self.assertFalse(accepted(b'Fails.'))
        self.assertFalse(accepted(b''))

    def test_expired_qb_cookie_cleared(self):
        """qB 说会话失效（401/403）时，插件要把本地会话一并清掉。"""
        self.server.save_qb_session('SID=stale')
        with patch('server.qb_request', return_value=(403, b'', '')):
            self.request('GET', '/api/torrents', headers=self.auth())
        self.assertEqual(self.server.qb_session(), '')

    def test_wrong_device_cert_gets_no_session(self):
        self.server.dev = False
        code, body = self.request('GET', '/', headers={'X-Xiaomi-Client-Verify':'SUCCESS','X-Xiaomi-Client-DN':'CN=nas.999999.test.2','X-Real-IP':'192.168.1.2'})
        self.assertIn(b'name="qb-session" content=""', body)

    def test_owner_cert_gets_session(self):
        self.server.dev = False
        _, body = self.request('GET', '/', headers={'X-Xiaomi-Client-Verify':'SUCCESS','X-Xiaomi-Client-DN':'CN=nas.123456.test.2'})
        self.assertNotIn(b'name="qb-session" content=""', body)

    def test_page_shows_installed_version(self):
        """页脚版本由服务端注入，源码里不再留写死的字符串。"""
        _, body = self.request('GET', '/')
        text = body.decode()
        self.assertNotIn('__PLUGIN_VERSION__', text)
        self.assertIn('qB 下载 · ' + installed_version(), text)


class UiTests(unittest.TestCase):
    """插件页是随包下发的静态文件，用静态检查补上浏览器之外的回归。"""

    def setUp(self):
        self.web = Path(__file__).resolve().parents[1] / 'web'
        self.icons = {p.stem for p in (self.web / 'assets').glob('*.png')}

    def test_ui_calls_the_api_with_relative_paths(self):
        """插件页挂在 /plugin/<用户>/qbittorrent/ 下，接口必须用相对路径。

        回归用例：写成 fetch('/api/status') 会打到站点根，nginx 没有对应 location，
        返回 404，插件页只会显示「正在连接 / 请求失败」。
        """
        script = (self.web / 'app.js').read_text(encoding='utf-8')
        self.assertNotIn("'/api", script)
        self.assertNotIn('"/api', script)
        self.assertIn("fetch('api/' + route", script)

    def test_html_icon_references_have_assets(self):
        """页面引用的图标名必须有对应 PNG，否则会渲染成空白图标。"""
        html = (self.web / 'index.html').read_text(encoding='utf-8')
        used = set(re.findall(r'data-icon="([a-z0-9]+)"', html))
        self.assertTrue(used)
        self.assertEqual(used - self.icons, set())

    def test_page_version_comes_from_server(self):
        """页脚不能写死版本，必须留占位符由服务端填实际安装版本。"""
        html = (self.web / 'index.html').read_text(encoding='utf-8')
        self.assertIn('__PLUGIN_VERSION__', html)

    def test_html_references_existing_files(self):
        html = (self.web / 'index.html').read_text(encoding='utf-8')
        referenced = re.findall(r'(?:href|src)="([^"]+\.(?:css|js))(?:\?[^"]*)?"', html)
        self.assertTrue(referenced)
        for name in referenced:
            self.assertTrue((self.web / name).is_file(), name)

    def test_bundle_is_built_from_source(self):
        """页面加载的是 app.bundle.js；改了 app.js 忘了重建，改动就等于没生效。"""
        script = (self.web / 'app.js').read_text(encoding='utf-8')
        icons = {p.stem: 'data:image/png;base64,' + base64.b64encode(p.read_bytes()).decode()
                 for p in sorted((self.web / 'assets').glob('*.png'))}
        self.assertEqual((self.web / 'app.bundle.js').read_text(encoding='utf-8'),
                         script.replace('__ICON_ASSETS__', json.dumps(icons)))


if __name__ == '__main__':
    unittest.main()
