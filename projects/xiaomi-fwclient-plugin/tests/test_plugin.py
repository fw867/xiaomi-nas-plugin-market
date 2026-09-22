import http.client
import json
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from engine import Engine, Error, validate_server, validate_token, installed_version, VERSION
from server import Server


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / 'data'
        self.runtime = Path(__file__).resolve().parents[1]
        self.engine = Engine(self.data, self.runtime, dev=True)

    def test_validate_server_and_token(self):
        self.assertEqual(validate_server('fw867.com'), 'fw867.com')
        self.assertEqual(validate_token('tk_' + 'a' * 16), 'tk_' + 'a' * 16)
        for bad in ['', 'a b', 'x' * 300, 'host/name']:
            with self.subTest(bad=bad), self.assertRaises(Error):
                validate_server(bad)
        for bad in ['', 'short', 'x' * 300, 'tk\nabc']:
            with self.subTest(bad=bad), self.assertRaises(Error):
                validate_token(bad)

    def test_installed_version(self):
        self.assertEqual(installed_version(), VERSION)

    def test_reconfigure_updates_server_and_keeps_token(self):
        """已配置后可改服务器域名；令牌留空表示保持原值（页面不回显明文）。"""
        with patch('engine.Engine.ensure_binary'):
            self.engine.setup('fw867.com', 'tk_' + 'c' * 20)
        with patch('engine.Engine.find_pids', return_value=[]), patch('engine.Engine.start'):
            self.engine.reconfigure('new.example.com', '')
        self.assertEqual(self.engine.config['server'], 'new.example.com')
        self.assertEqual(self.engine.config['token'], 'tk_' + 'c' * 20)

    def test_reconfigure_replaces_token_when_provided(self):
        with patch('engine.Engine.ensure_binary'):
            self.engine.setup('fw867.com', 'tk_' + 'c' * 20)
        with patch('engine.Engine.find_pids', return_value=[]), patch('engine.Engine.start'):
            self.engine.reconfigure('fw867.com', 'tk_' + 'e' * 20)
        self.assertEqual(self.engine.config['token'], 'tk_' + 'e' * 20)

    def test_reconfigure_rejects_empty_changes(self):
        with patch('engine.Engine.ensure_binary'):
            self.engine.setup('fw867.com', 'tk_' + 'c' * 20)
        with patch('engine.Engine.find_pids', return_value=[]), patch('engine.Engine.start'):
            with self.assertRaises(Error):
                self.engine.reconfigure('fw867.com', '')

    def test_reconfigure_requires_existing_config(self):
        with self.assertRaises(Error):
            self.engine.reconfigure('fw867.com', 'tk_' + 'd' * 20)

    def test_setup_saves_config_without_echo(self):
        with patch('engine.Engine.ensure_binary'):
            self.engine.setup('fw867.com', 'tk_' + 'b' * 20, insecure=True)
        snap = self.engine.snapshot()
        self.assertTrue(snap['configured'])
        self.assertEqual(snap['server'], 'fw867.com')
        self.assertTrue(snap['insecure'])
        self.assertTrue(snap['hasToken'])
        self.assertNotIn('token', snap)
        self.assertNotIn('tk_' + 'b' * 20, json.dumps(snap))

    def test_setup_twice_rejected(self):
        with patch('engine.Engine.ensure_binary'):
            self.engine.setup('fw867.com', 'tk_' + 'c' * 20)
            with self.assertRaises(Error):
                self.engine.setup('other.com', 'tk_' + 'd' * 20)

    def test_dev_cannot_start(self):
        self.engine.config = {'server': 'fw867.com', 'token': 'tk_xxxxxxxx'}
        with self.assertRaises(Error):
            self.engine.launch('start', {})


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.engine = Engine(root / 'data', Path(__file__).resolve().parents[1], dev=True)
        self.server = Server(('127.0.0.1', 0), self.engine, 'u123456', dev=True)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

        def close():
            self.server.shutdown()
            self.server.server_close()
            self.thread.join()

        self.addCleanup(close)
        _, html = self.request('GET', '/')
        self.token = re.search(r'name="fw-session" content="([^"]+)"', html.decode())[1]
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
            'X-Fw-Session': self.token,
            'X-CSRF-Token': self.csrf,
            'Content-Type': 'application/json',
        }

    def test_unauthenticated_denied(self):
        self.assertEqual(self.request('GET', '/api/status')[0], 401)

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
        self.assertIn('内网穿透 · ' + installed_version(), text)


class UiTests(unittest.TestCase):
    def setUp(self):
        self.web = Path(__file__).resolve().parents[1] / 'web'

    def test_setup_fields_present(self):
        html = (self.web / 'index.html').read_text(encoding='utf-8')
        for name in ('server', 'token', 'insecure'):
            self.assertIn('name="' + name + '"', html)
        self.assertIn('检查升级', html)
        self.assertIn('__PLUGIN_VERSION__', html)
        self.assertTrue((self.web / 'assets' / 'fwclient.png').is_file())

    def test_relative_api(self):
        script = (self.web / 'app.js').read_text(encoding='utf-8')
        self.assertNotIn("'/api", script)
        self.assertIn("fetch('api' + path", script)


if __name__ == '__main__':
    unittest.main()
