import http.client
import json
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from engine import (
    Engine, Error, IMAGE, NAME, LABEL, PORT, CONTAINER_PORT,
    container_config, confined, installed_version, VERSION,
)
from server import Server


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'root'
        self.root.mkdir()
        (self.root / 'MiShare').mkdir()
        self.engine = Engine(Path(self.tmp.name) / 'private', self.root)

    def test_image_is_lite_with_digest(self):
        self.assertIn('dpanel/dpanel:lite@sha256:', IMAGE)
        self.assertEqual(NAME, 'xiaomi-plugin-dpanel')
        self.assertEqual(LABEL, 'io.xiaomi-plugin.dpanel.owner')

    def test_confined_rejects_traversal(self):
        self.assertEqual(confined(self.root, 'MiShare'), self.root / 'MiShare')
        for name in ['../', '/etc', 'MiShare/..', '.hidden']:
            with self.subTest(name=name), self.assertRaises(Error):
                confined(self.root, name)

    def test_container_mounts_docker_sock_and_config(self):
        cfg = container_config({'owner': 'tok', 'config': '/nas/dpanel-cfg'})
        self.assertEqual(cfg['Image'], IMAGE)
        self.assertEqual(cfg['Labels'], {LABEL: 'tok'})
        self.assertIn('APP_NAME=' + NAME, cfg['Env'])
        host = cfg['HostConfig']
        mounts = {m['Target']: m['Source'] for m in host['Mounts']}
        self.assertEqual(mounts['/var/run/docker.sock'], '/var/run/docker.sock')
        self.assertEqual(mounts['/dpanel'], '/nas/dpanel-cfg')
        self.assertEqual(host['PortBindings'], {
            str(CONTAINER_PORT) + '/tcp': [{'HostIp': '0.0.0.0', 'HostPort': str(PORT)}],
        })
        self.assertEqual(cfg['ExposedPorts'], {str(CONTAINER_PORT) + '/tcp': {}})
        self.assertNotIn('Privileged', host)
        self.assertNotIn('NetworkMode', host)
        self.assertEqual(host['RestartPolicy'], {'Name': 'no'})
        self.assertEqual(host['Memory'], 512 * 1024 * 1024)

    def test_snapshot_before_setup(self):
        state = self.engine.snapshot()
        self.assertFalse(state['configured'])
        self.assertFalse(state['running'])
        self.assertEqual(state['version'], installed_version())

    def test_installed_version_fallback(self):
        with patch('engine.__file__',
                   '/data/plugin/dpanel/releases/0.1.0-1789828016-8538/engine.py'):
            self.assertEqual(installed_version(), '0.1.0')
        self.assertEqual(installed_version(), VERSION)

    def test_dev_cannot_start(self):
        self.engine.dev = True
        with self.assertRaises(Error):
            self.engine.launch('start', {})

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
        self.assertEqual(api.call_args.args[1], '/containers/' + NAME + '/stop?t=20')
        self.assertFalse(json.loads(self.engine.cfgfile.read_text())['enabled'])


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
        self.token = re.search(r'name="dpanel-session" content="([^"]+)"', html.decode())[1]
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
            'X-DPanel-Session': self.token,
            'X-CSRF-Token': self.csrf,
            'Content-Type': 'application/json',
        }

    def test_unauthenticated_denied(self):
        self.assertEqual(self.request('GET', '/api/status')[0], 401)

    def test_csrf_required(self):
        self.assertEqual(self.request('POST', '/api/service/start', {}, {'X-DPanel-Session': self.token})[0], 403)

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
        self.assertIn('DPanel 容器管理 · ' + installed_version(), text)

    def test_status_reports_ui_path(self):
        with patch('server.lan_ip', return_value='192.168.1.20'):
            code, body = self.request('GET', '/api/status', headers=self.auth())
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)['address'], 'http://192.168.1.20:8807/dpanel/ui')


class UiTests(unittest.TestCase):
    def setUp(self):
        self.web = Path(__file__).resolve().parents[1] / 'web'

    def test_ui_uses_relative_api(self):
        script = (self.web / 'app.js').read_text(encoding='utf-8')
        self.assertNotIn("'/api", script)
        self.assertIn("fetch('api' + path", script)

    def test_html_has_icon_and_setup_fields(self):
        html = (self.web / 'index.html').read_text(encoding='utf-8')
        self.assertIn('assets/dpanel.png', html)
        self.assertIn('name="configPath"', html)
        self.assertIn('__PLUGIN_VERSION__', html)
        self.assertTrue((self.web / 'assets' / 'dpanel.png').is_file())

    def test_html_references_existing_files(self):
        html = (self.web / 'index.html').read_text(encoding='utf-8')
        referenced = re.findall(r'(?:href|src)="([^"]+\.(?:css|js|png))(?:\?[^"]*)?"', html)
        self.assertTrue(referenced)
        for name in referenced:
            self.assertTrue((self.web / name).is_file(), name)


if __name__ == '__main__':
    unittest.main()
