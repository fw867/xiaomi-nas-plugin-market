import http.client
import json
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from engine import (
    Engine, Error, IMAGE, NAME, LABEL, PORT,
    CONTAINER_HTTP, CONTAINER_HTTPS, CONTAINER_DLNA,
    container_config, confined, installed_version, VERSION,
)
from server import Server


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
        self.assertEqual(host['Memory'], 1024 * 1024 * 1024)
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
            'Config': {'Labels': {LABEL: 'tok'}, 'Healthcheck': {'Test': ['NONE']}},
            'State': {'Running': True},
        }
        calls = self._run_start(item)
        paths = [path for _, path, _ in calls]
        self.assertEqual(paths, [])                                       # 运行中就不该有 Docker 调用
        self.assertTrue(json.loads(self.engine.cfgfile.read_text(encoding='utf-8'))['healthcheck_off'])

    def test_start_starts_stopped_container_without_recreating(self):
        item = {
            'Config': {'Labels': {LABEL: 'tok'}, 'Healthcheck': {'Test': ['NONE']}},
            'State': {'Running': False},
        }
        calls = self._run_start(item)
        paths = [path for _, path, _ in calls]
        self.assertEqual(paths, ['/containers/' + NAME + '/start'])

    def test_snapshot_reports_healthcheck_flag(self):
        self.engine.config['healthcheck_off'] = True
        with patch.object(self.engine, 'owned', return_value=None):
            self.assertTrue(self.engine.snapshot()['healthcheckOff'])
            self.engine.config['healthcheck_off'] = False
            self.assertFalse(self.engine.snapshot()['healthcheckOff'])


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

    def test_html_references_existing_files(self):
        html = (self.web / 'index.html').read_text(encoding='utf-8')
        referenced = re.findall(r'(?:href|src)="([^"]+\.(?:css|js|png))(?:\?[^"]*)?"', html)
        self.assertTrue(referenced)
        for name in referenced:
            self.assertTrue((self.web / name).is_file(), name)


if __name__ == '__main__':
    unittest.main()
