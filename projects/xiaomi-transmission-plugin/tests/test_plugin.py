import http.client
import json
import os
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from engine import (
    Engine, Error, IMAGE, NAME, LABEL, PORT, BT_PORT, VERSION, IMAGE_VERSION,
    confined, container_config, installed_version, settings_document,
    webui_password, webui_username,
)
from server import Server


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'root'
        self.root.mkdir()
        for name in ('Downloads', 'Config', 'Watch'):
            (self.root / name).mkdir()
        self.engine = Engine(Path(self.tmp.name) / 'private', self.root)

    def test_username_policy(self):
        self.assertEqual(webui_username('admin'), 'admin')
        for value in ['', 'a' * 65, 'bad:name', 'sp ace', None]:
            with self.subTest(value=value), self.assertRaises(Error):
                webui_username(value)

    def test_password_policy(self):
        for value in ['abc', '12345678', 'abcdefgh', 'ABCD123\n', None]:
            with self.subTest(value=value), self.assertRaises(Error):
                webui_password(value)

    def test_confined_normal(self):
        self.assertEqual(confined(self.root, 'Downloads'), self.root / 'Downloads')

    def test_reject_traversal(self):
        for name in ['../', '/etc', '.', 'Downloads/..', 'Downloads//x', '.hidden', 'Downloads\\x']:
            with self.subTest(name=name), self.assertRaises(Error):
                confined(self.root, name)

    def test_settings_omits_webui_credentials(self):
        doc = settings_document()
        text = json.dumps(doc)
        self.assertNotIn('rpc-username', doc)
        self.assertNotIn('rpc-password', doc)
        self.assertEqual(doc['download-dir'], '/downloads')
        self.assertEqual(doc['watch-dir'], '/watch')
        self.assertTrue(doc['watch-dir-enabled'])
        self.assertEqual(doc['rpc-port'], PORT)
        self.assertEqual(doc['peer-port'], BT_PORT)
        self.assertIn(str(BT_PORT), text)

    def test_container_isolation_and_ports(self):
        config = {
            'owner': 'test',
            'uid': 1000,
            'gid': 1000,
            'username': 'admin',
            'download': '/nas/Downloads',
            'config': '/nas/Config',
            'watch': '/nas/Watch',
        }
        cfg = container_config(config, Path('/tmp/trdata'), 'Example123!')
        self.assertEqual(cfg['Image'], IMAGE)
        self.assertEqual(cfg['Labels'], {LABEL: 'test'})
        env = cfg['Env']
        self.assertIn('PUID=1000', env)
        self.assertIn('PGID=1000', env)
        self.assertIn('USER=admin', env)
        self.assertIn('PASS=Example123!', env)
        self.assertIn('PEERPORT=' + str(BT_PORT), env)
        host = cfg['HostConfig']
        self.assertEqual(host['RestartPolicy'], {'Name': 'no'})
        self.assertEqual(host['PortBindings'], {
            str(PORT) + '/tcp': [{'HostIp': '0.0.0.0', 'HostPort': str(PORT)}],
            str(BT_PORT) + '/tcp': [{'HostIp': '0.0.0.0', 'HostPort': str(BT_PORT)}],
            str(BT_PORT) + '/udp': [{'HostIp': '0.0.0.0', 'HostPort': str(BT_PORT)}],
        })
        self.assertEqual(cfg['ExposedPorts'], {
            str(PORT) + '/tcp': {},
            str(BT_PORT) + '/tcp': {},
            str(BT_PORT) + '/udp': {},
        })
        self.assertNotIn('Privileged', host)
        self.assertNotIn('NetworkMode', host)
        mounts = {m['Target']: m['Source'] for m in host['Mounts']}
        self.assertEqual(mounts, {
            '/config': '/nas/Config',
            '/downloads': '/nas/Downloads',
            '/watch': '/nas/Watch',
        })
        self.assertFalse(any('docker.sock' in s for s in mounts.values()))
        self.assertEqual(host['Memory'], 512 * 1024 * 1024)
        self.assertEqual(host['SecurityOpt'], ['no-new-privileges:true'])

    def test_legacy_native_settings_ignored(self):
        """旧版原生插件的 settings.json 不能被当成 Docker 版已初始化配置。"""
        cfgfile = self.engine.cfgfile
        cfgfile.write_text(json.dumps({'downloadDir': '/somewhere', 'limits': {}}), encoding='utf-8')
        engine = Engine(self.engine.data, self.root)
        self.assertIsNone(engine.config)

    def test_snapshot_before_setup(self):
        state = self.engine.snapshot()
        self.assertFalse(state['configured'])
        self.assertFalse(state['running'])
        self.assertFalse(state['ready'])
        self.assertEqual(state['version'], installed_version())
        self.assertEqual(state['imageVersion'], IMAGE_VERSION)

    def test_installed_version_from_release_directory(self):
        with patch('engine.__file__',
                   '/data/plugin/transmission/releases/0.2.0-1789828016-8538/engine.py'):
            self.assertEqual(installed_version(), '0.2.0')
        self.assertEqual(installed_version(), VERSION)

    def test_dev_cannot_start(self):
        self.engine.dev = True
        with self.assertRaises(Error):
            self.engine.launch('start', {})

    def test_setup_requires_three_paths(self):
        if not (self.root / 'Downloads').stat().st_uid:
            self.skipTest('requires non-root test directory owner')

        def fake_api(method, path, body=None, timeout=30):
            if path == '/info':
                return 200, b'{"Architecture":"aarch64"}'
            return 404, b'{"message":"No such container"}'

        with patch('engine.os.chown'), patch('engine.docker_api', side_effect=fake_api):
            with self.assertRaises(Error):
                self.engine.setup({'download': 'Downloads'}, 'admin', 'Example123!')
            with self.assertRaises(Error):
                self.engine.setup(
                    {'download': 'Downloads', 'config': 'Downloads', 'watch': 'Watch'},
                    'admin', 'Example123!')

    def test_setup_writes_plugin_config_and_settings(self):
        if not (self.root / 'Downloads').stat().st_uid:
            self.skipTest('requires non-root test directory owner')

        def fake_api(method, path, body=None, timeout=30):
            if path == '/info':
                return 200, b'{"Architecture":"aarch64"}'
            return 404, b'{"message":"No such container"}'

        with patch('engine.os.chown'), patch('engine.docker_api', side_effect=fake_api):
            self.engine.setup(
                {'download': 'Downloads', 'config': 'Config', 'watch': 'Watch'},
                'admin', 'Example123!')
        self.assertEqual(self.engine.config['download'], str(self.root / 'Downloads'))
        self.assertEqual(self.engine.config['config'], str(self.root / 'Config'))
        self.assertEqual(self.engine.config['watch'], str(self.root / 'Watch'))
        self.assertEqual(self.engine.config['username'], 'admin')
        self.assertEqual(self.engine.saved_password(), 'Example123!')
        conf = json.loads((self.root / 'Config/transmission-daemon/settings.json').read_text(encoding='utf-8'))
        self.assertEqual(conf['download-dir'], '/downloads')
        self.assertEqual(conf['watch-dir'], '/watch')
        self.assertNotIn('Example123', json.dumps(conf))
        self.assertNotIn('rpc-password', conf)

    def test_setup_refuses_same_owner_mismatch_paths(self):
        if not (self.root / 'Downloads').stat().st_uid:
            self.skipTest('requires non-root test directory owner')

        def fake_api(method, path, body=None, timeout=30):
            return 404, b'{}'

        with patch('engine.os.chown'), patch('engine.docker_api', side_effect=fake_api):
            with self.assertRaises(Error):
                self.engine.setup(
                    {'download': 'Downloads', 'config': 'Config', 'watch': 'Config'},
                    'admin', 'Example123!')

    def test_directory_identity_change(self):
        folder = self.root / 'Downloads'
        s = folder.stat()
        self.engine.config = {
            'download': str(folder), 'download_relative': 'Downloads',
            'download_device': s.st_dev, 'download_inode': s.st_ino + 1,
            'config': str(self.root / 'Config'), 'config_relative': 'Config',
            'config_device': s.st_dev, 'config_inode': s.st_ino,
            'watch': str(self.root / 'Watch'), 'watch_relative': 'Watch',
            'watch_device': s.st_dev, 'watch_inode': s.st_ino,
        }
        with self.assertRaises(Error):
            self.engine.check_directories()

    def test_foreign_container_not_stopped(self):
        self.engine.config = {'owner': 'mine'}
        foreign = json.dumps({'Config': {'Labels': {LABEL: 'foreign'}}}).encode('utf-8')
        with patch('engine.docker_api', return_value=(200, foreign)) as api:
            with self.assertRaises(Error):
                self.engine.stop()
        self.assertEqual(api.call_count, 1)
        self.assertEqual(api.call_args.args[0], 'GET')

    def test_stop_remembers_preference(self):
        self.engine.config = {'owner': 'mine', 'enabled': True}
        with patch.object(self.engine, 'owned', return_value={'State': {'Running': True}}), \
                patch('engine.docker_api', return_value=(204, b'')) as api:
            self.engine.stop()
        self.assertEqual(api.call_args.args[1], '/containers/' + NAME + '/stop?t=15')
        self.assertFalse(json.loads(self.engine.cfgfile.read_text())['enabled'])

    def test_start_creates_container_with_three_mounts(self):
        def identity(relative):
            folder = self.root / relative
            s = folder.stat()
            return str(folder), relative, s.st_dev, s.st_ino

        download = identity('Downloads')
        config = identity('Config')
        watch = identity('Watch')
        self.engine.config = {
            'owner': 'owner-token', 'uid': 1000, 'gid': 1000, 'username': 'admin',
            'download': download[0], 'download_relative': download[1],
            'download_device': download[2], 'download_inode': download[3],
            'config': config[0], 'config_relative': config[1],
            'config_device': config[2], 'config_inode': config[3],
            'watch': watch[0], 'watch_relative': watch[1],
            'watch_device': watch[2], 'watch_inode': watch[3],
            'enabled': False,
        }
        self.engine.credentialfile.write_text(json.dumps({'password': 'Example123!'}), encoding='utf-8')
        calls = []

        def fake_api(method, path, body=None, timeout=30):
            calls.append((method, path, body, timeout))
            if path == '/containers/' + NAME + '/json':
                return 404, b'{"message":"No such container"}'
            if path.startswith('/images/create'):
                return 200, b'{"status":"Downloaded newer image"}\n'
            return 201, b'{}'

        with patch('engine.docker_api', side_effect=fake_api), \
                patch('engine.tr_rpc_probe', return_value=True):
            self.engine.start()
        create = next(c for c in calls if c[1].startswith('/containers/create'))
        mounts = create[2]['HostConfig']['Mounts']
        self.assertEqual([m['Target'] for m in mounts], ['/config', '/downloads', '/watch'])
        self.assertIn('USER=admin', create[2]['Env'])
        self.assertIn('PASS=Example123!', create[2]['Env'])


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name) / 'root'
        root.mkdir()
        self.engine = Engine(Path(self.tmp.name) / 'data', root, dev=True)
        self.server = Server(('127.0.0.1', 0), self.engine, 'u123456', dev=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

        def close():
            self.server.shutdown()
            self.server.server_close()
            self.thread.join()

        self.addCleanup(close)
        _, html = self.request('GET', '/')
        self.token = re.search(r'name="tr-session" content="([^"]+)"', html.decode())[1]
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
            'X-TR-Session': self.token,
            'X-CSRF-Token': self.csrf,
            'Content-Type': 'application/json',
        }

    def test_unauthenticated_reads_denied(self):
        self.assertEqual(self.request('GET', '/api/status')[0], 401)

    def test_csrf_required(self):
        self.assertEqual(self.request('POST', '/api/service/stop', {}, {'X-TR-Session': self.token})[0], 403)

    def test_status_no_secrets(self):
        code, body = self.request('GET', '/api/status', headers=self.auth())
        self.assertEqual(code, 200)
        text = body.decode()
        self.assertNotIn(self.server.key.hex(), text)
        self.assertNotIn('Example123', text)

    def test_no_arbitrary_file_serving(self):
        self.assertEqual(self.request('GET', '/engine.py', headers=self.auth())[0], 404)

    def test_preview_write_blocked(self):
        code, body = self.request('POST', '/api/service/start', {}, self.auth())
        self.assertEqual(code, 400)

    def test_page_shows_installed_version(self):
        _, body = self.request('GET', '/')
        text = body.decode()
        self.assertNotIn('__PLUGIN_VERSION__', text)
        self.assertIn('Transmission 下载 · ' + installed_version(), text)

    def test_status_reports_lan_address(self):
        with patch('server.lan_ip', return_value='192.168.1.15'):
            code, body = self.request('GET', '/api/status', headers=self.auth())
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)['address'], 'http://192.168.1.15:' + str(PORT))


class UiTests(unittest.TestCase):
    def setUp(self):
        self.web = Path(__file__).resolve().parents[1] / 'web'
        self.icons = {p.stem for p in (self.web / 'assets').glob('*.png')}

    def test_ui_calls_the_api_with_relative_paths(self):
        script = (self.web / 'app.js').read_text(encoding='utf-8')
        self.assertNotIn("'/api", script)
        self.assertNotIn('"/api', script)
        self.assertIn("fetch('api/' + route", script)

    def test_html_icon_references_have_assets(self):
        html = (self.web / 'index.html').read_text(encoding='utf-8')
        used = set(re.findall(r'data-icon="([a-z0-9]+)"', html))
        self.assertTrue(used)
        self.assertEqual(used - self.icons, set())

    def test_setup_fields_present(self):
        html = (self.web / 'index.html').read_text(encoding='utf-8')
        for name in ('download', 'config', 'watch', 'username', 'password'):
            self.assertIn('name="' + name + '"', html)

    def test_page_version_comes_from_server(self):
        html = (self.web / 'index.html').read_text(encoding='utf-8')
        self.assertIn('__PLUGIN_VERSION__', html)

    def test_html_references_existing_files(self):
        html = (self.web / 'index.html').read_text(encoding='utf-8')
        referenced = re.findall(r'(?:href|src)="([^"]+\.(?:css|js))(?:\?[^"]*)?"', html)
        self.assertTrue(referenced)
        for name in referenced:
            self.assertTrue((self.web / name).is_file(), name)

    def test_bundle_is_built_from_source(self):
        import base64
        script = (self.web / 'app.js').read_text(encoding='utf-8')
        icons = {p.stem: 'data:image/png;base64,' + base64.b64encode(p.read_bytes()).decode()
                 for p in sorted((self.web / 'assets').glob('*.png'))}
        self.assertEqual(
            (self.web / 'app.bundle.js').read_text(encoding='utf-8'),
            script.replace('__ICON_ASSETS__', json.dumps(icons)))


if __name__ == '__main__':
    unittest.main()
