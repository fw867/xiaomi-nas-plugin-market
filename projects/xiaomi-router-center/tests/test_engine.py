"""路由器软件中心插件的单元测试。

重点是三件容易出事的事：
  1. 目标地址规整（用户会填 192.168.1.1:9958 这种没有协议、没有结尾斜杠的形式）；
  2. 换目标地址时"渲染 → nginx -t → reload"的失败回滚（绝不能把坏配置留在盘上）；
  3. 探测路由器时只读、不因为超时/401 抛异常打断界面。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

import engine  # noqa: E402


class TargetNormalizeTests(unittest.TestCase):
    def test_accepts_common_forms(self) -> None:
        cases = {
            'http://192.168.1.1:9958/': 'http://192.168.1.1:9958/',
            'http://192.168.1.1:9958': 'http://192.168.1.1:9958/',
            '192.168.1.1:9958': 'http://192.168.1.1:9958/',
            '192.168.1.1': 'http://192.168.1.1/',
            'https://router.local:8443': 'https://router.local:8443/',
            '  http://10.0.0.2:9958/status  ': 'http://10.0.0.2:9958/',
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(engine.normalize_target(raw), expected)

    def test_rejects_bad_forms(self) -> None:
        for bad in ('', '   ', 'http://', 'http:// bad host/', 'ftp://192.168.1.1/'):
            with self.subTest(raw=bad):
                with self.assertRaises(RuntimeError):
                    engine.normalize_target(bad)


class SettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.state = root / 'settings.json'
        self.token = root / 'router-token'
        self.patchers = [
            mock.patch.object(engine, 'STATE_FILE', self.state),
            mock.patch.object(engine, 'TOKEN_FILE', self.token),
            mock.patch.object(engine, 'DEFAULT_TARGET', 'http://192.168.1.1:9958/'),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in self.patchers:
            patcher.stop()
        self.temp.cleanup()

    def test_default_target_when_missing(self) -> None:
        self.assertEqual(engine.load_settings()['target'], 'http://192.168.1.1:9958/')

    def test_save_and_reload(self) -> None:
        engine.save_settings({'target': 'http://10.1.2.3:9958'})
        self.assertEqual(engine.load_settings()['target'], 'http://10.1.2.3:9958/')
        payload = json.loads(self.state.read_text(encoding='utf-8'))
        self.assertEqual(payload['target'], 'http://10.1.2.3:9958/')

    def test_broken_settings_falls_back(self) -> None:
        self.state.write_text('{not json', encoding='utf-8')
        self.assertEqual(engine.load_settings()['target'], 'http://192.168.1.1:9958/')

    def test_token_roundtrip_and_mode(self) -> None:
        engine.save_router_token('secret-token-123')
        self.assertEqual(engine.router_token(), 'secret-token-123')
        if os.name != 'nt':
            self.assertEqual(self.token.stat().st_mode & 0o777, 0o600)
        engine.save_router_token('')
        self.assertEqual(engine.router_token(), '')
        self.assertFalse(self.token.exists())


class _MockRouter(BaseHTTPRequestHandler):
    """假的路由器软件中心：首页 + 需要令牌的 /api/system/info。"""

    protocol_version = 'HTTP/1.1'
    token = 'good-token'

    def log_message(self, *args):        # noqa: A003
        pass

    def _send(self, status: int, body: bytes, content_type: str = 'text/html; charset=utf-8') -> None:
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):                                          # noqa: N802
        if self.path.startswith('/api/system/info'):
            if self.headers.get('Authorization') != self.token:
                self._send(401, b'', 'application/json')
                return
            self._send(200, json.dumps({'Version': '0.0.0-mock', 'Device': 'MockUDM'}).encode(),
                       'application/json')
            return
        self._send(200, b'<html><head><title>UniFi SoftCenter - \xe5\xae\x89\xe5\x85\xa8\xe7\xae\xa1\xe7\x90\x86\xe4\xb8\xad\xe5\xbf\x83</title></head></html>')


class ProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self.temp = tempfile.TemporaryDirectory()
        engine.TOKEN_FILE = Path(self.temp.name) / 'router-token'
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), _MockRouter)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.target = f'http://127.0.0.1:{self.port}/'

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.temp.cleanup()

    def test_probe_without_token(self) -> None:
        result = engine.probe_router(self.target, token='')
        self.assertTrue(result['reachable'])
        self.assertEqual(result['status'], 200)
        self.assertIn('SoftCenter', result['title'])
        self.assertIsInstance(result['latency_ms'], float)
        self.assertFalse(result['authenticated'])

    def test_probe_with_valid_token(self) -> None:
        result = engine.probe_router(self.target, token='good-token')
        self.assertTrue(result['reachable'])
        self.assertTrue(result['authenticated'])
        self.assertEqual(result['device'], 'MockUDM')

    def test_probe_with_wrong_token_is_not_fatal(self) -> None:
        result = engine.probe_router(self.target, token='bad-token')
        self.assertTrue(result['reachable'])
        self.assertFalse(result['authenticated'])
        self.assertEqual(result['error'], '令牌无效')

    def test_probe_unreachable_is_not_fatal(self) -> None:
        result = engine.probe_router('http://127.0.0.1:1/', token='')
        self.assertFalse(result['reachable'])
        self.assertTrue(result['error'])


class ApplyTargetTests(unittest.TestCase):
    """换目标地址：成功才落盘，nginx 校验失败必须回滚配置且不改设置。"""

    def setUp(self) -> None:
        import tempfile

        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.conf = root / 'xiaomi-router-center.conf'
        self.template = root / 'template.conf'
        self.state = root / 'settings.json'
        self.template.write_text(
            'location ^~ /plugin/__NAS_USER_ID__/rtrcenter/site/ {\n'
            '    proxy_pass __ROUTER_TARGET__;\n'
            '}\n'
            'location ^~ /plugin/__NAS_USER_ID__/rtrcenter/api/ {\n'
            '    proxy_pass http://127.0.0.1:__PLUGIN_PORT__/api/;\n'
            '}\n', encoding='utf-8')
        self.patchers = [
            mock.patch.object(engine, 'NGINX_CONF', self.conf),
            mock.patch.object(engine, 'NGINX_TEMPLATE', self.template),
            mock.patch.object(engine, 'STATE_FILE', self.state),
            mock.patch.object(engine, 'DEFAULT_TARGET', 'http://192.168.1.1:9958/'),
            mock.patch.object(engine, 'nginx_test', lambda: (True, 'ok')),
            mock.patch.object(engine, 'nginx_reload', lambda: True),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in self.patchers:
            patcher.stop()
        self.temp.cleanup()

    def test_render_substitutes_everything(self) -> None:
        text = engine.render_conf('http://10.0.0.9:9958/', 'u1', 18101)
        self.assertIn('http://10.0.0.9:9958/', text)
        self.assertIn('/plugin/u1/rtrcenter/site/', text)
        self.assertIn('http://127.0.0.1:18101/api/', text)
        self.assertNotIn('__', text)

    def test_success_writes_conf_and_settings(self) -> None:
        engine.apply_target('http://10.0.0.9:9958', 'u1', 18101)
        self.assertIn('http://10.0.0.9:9958/', self.conf.read_text(encoding='utf-8'))
        self.assertEqual(engine.load_settings()['target'], 'http://10.0.0.9:9958/')

    def test_bad_nginx_rolls_back_conf_and_settings(self) -> None:
        engine.apply_target('http://10.0.0.9:9958/', 'u1', 18101)
        before = self.conf.read_text(encoding='utf-8')
        with mock.patch.object(engine, 'nginx_test', lambda: (False, 'syntax error')):
            with self.assertRaises(RuntimeError):
                engine.apply_target('http://10.0.0.10:9958/', 'u1', 18101)
        self.assertEqual(self.conf.read_text(encoding='utf-8'), before)
        self.assertEqual(engine.load_settings()['target'], 'http://10.0.0.9:9958/')

    def test_reload_failure_rolls_back(self) -> None:
        engine.apply_target('http://10.0.0.9:9958/', 'u1', 18101)
        before = self.conf.read_text(encoding='utf-8')
        with mock.patch.object(engine, 'nginx_reload', lambda: False):
            with self.assertRaises(RuntimeError):
                engine.apply_target('http://10.0.0.11:9958/', 'u1', 18101)
        self.assertEqual(self.conf.read_text(encoding='utf-8'), before)
        self.assertEqual(engine.load_settings()['target'], 'http://10.0.0.9:9958/')


class HttpTests(unittest.TestCase):
    """真起一个服务打一遍关键路由（曾经因为路由被误删而线上 502）。"""

    def setUp(self) -> None:
        import tempfile

        import server

        self.server_module = server
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        static = root / 'web'
        static.mkdir()
        (static / 'index.html').write_text('<html><body>router-center-shell</body></html>', encoding='utf-8')
        (static / 'app.js').write_text('void 0;', encoding='utf-8')
        self.state = root / 'settings.json'
        self.token = root / 'router-token'
        self.static = static

        self.patchers = [
            mock.patch.object(engine, 'STATE_FILE', self.state),
            mock.patch.object(engine, 'TOKEN_FILE', self.token),
            mock.patch.object(engine, 'DEFAULT_TARGET', 'http://192.168.1.1:9958/'),
            mock.patch.object(server, 'STATIC_DIR', static),
        ]
        for patcher in self.patchers:
            patcher.start()

        handler = type('BoundHandler', (server.Handler,), {'app': server.RouterCenter(18101)})
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        for patcher in self.patchers:
            patcher.stop()
        self.temp.cleanup()

    def request(self, path: str, method: str = 'GET', body: bytes | None = None):
        import urllib.error
        import urllib.request

        request = urllib.request.Request(f'http://127.0.0.1:{self.port}{path}', data=body, method=method)
        if body is not None:
            request.add_header('Content-Type', 'application/json')
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def test_healthz(self) -> None:
        status, body = self.request('/healthz')
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertTrue(payload['ok'])
        self.assertEqual(payload['target'], 'http://192.168.1.1:9958/')

    def test_status_and_settings(self) -> None:
        status, body = self.request('/api/status')
        self.assertEqual(status, 200)
        payload = json.loads(body)
        for field in ('version', 'target', 'token_set', 'router', 'nginx_conf_ready'):
            self.assertIn(field, payload)
        status, body = self.request('/api/settings')
        self.assertEqual(status, 200)
        self.assertIn('target', json.loads(body))

    def test_settings_post_saves_token(self) -> None:
        status, body = self.request('/api/settings', 'POST',
                                    json.dumps({'token': 'tok-123'}).encode())
        self.assertEqual(status, 200)
        self.assertEqual(engine.router_token(), 'tok-123')
        self.assertTrue(json.loads(body)['token_set'])

    def test_shell_page_and_static(self) -> None:
        status, body = self.request('/')
        self.assertEqual(status, 200)
        self.assertIn(b'router-center-shell', body)
        status, _ = self.request('/app.js')
        self.assertEqual(status, 200)

    def test_unknown_api_is_404(self) -> None:
        status, _ = self.request('/api/nope')
        self.assertEqual(status, 404)

    def test_path_traversal_is_refused(self) -> None:
        status, _ = self.request('/../settings.json')
        self.assertIn(status, (403, 404))


class FrontendTests(unittest.TestCase):
    def test_app_js_syntax_and_ids(self) -> None:
        import re
        import shutil
        import subprocess

        project = PROJECT
        app_js = (project / 'web' / 'app.js').read_text(encoding='utf-8')
        index_html = (project / 'web' / 'index.html').read_text(encoding='utf-8')

        node = shutil.which('node')
        if node:
            result = subprocess.run([node, '--check', str(project / 'web' / 'app.js')],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

        ids = set(re.findall(r"\$\('([A-Za-z]+)'\)", app_js))
        self.assertTrue(ids)
        for name in sorted(ids):
            self.assertIn(f'id="{name}"', index_html, f'app.js 引用了不存在的 id: {name}')

        self.assertNotIn('style="', index_html, '内联 style 会被严格 CSP 拦掉')
        self.assertNotIn("'/api/", app_js, '前端必须用相对接口基址（api/...）')

    def test_install_script_has_no_leftover_placeholder(self) -> None:
        script = (PROJECT / 'deploy' / 'install-on-nas.sh').read_text(encoding='utf-8')
        for token in ('__NAS_USER_ID__', '__PLUGIN_PORT__', '__ROUTER_TARGET__'):
            self.assertIn(token, script)                      # 模板里必须有占位符
        self.assertIn('nginx -t', script)
        self.assertIn('__[A-Z_]+__', script)                  # 渲染后残留占位符的硬校验


class VersionTests(unittest.TestCase):
    def test_version_file(self) -> None:
        version = (PROJECT / 'VERSION').read_text(encoding='utf-8').strip()
        self.assertRegex(version, r'^\d+\.\d+\.\d+$')
        self.assertEqual(version, engine.VERSION)

    def test_version_from_release_dir(self) -> None:
        cases = {
            '/data/plugin/router-center/releases/0.1.1-1790960335-238127': '0.1.1',
            '/data/plugin/router-center/releases/v0.1.0-20261004012015': '0.1.0',
            '/data/plugin/router-center/current': '',
        }
        for raw, expected in cases.items():
            with self.subTest(path=raw):
                self.assertEqual(engine.version_from_release_dir(Path(raw)), expected)


if __name__ == '__main__':
    unittest.main()
