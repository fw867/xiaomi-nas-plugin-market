"""路由器软件中心插件的单元测试。

重点是三件容易出事的事：
  1. 目标地址规整（用户会填 192.168.1.1:9958 这种没有协议、没有结尾斜杠的形式）；
  2. 换目标地址时"渲染 → nginx -t → reload"的失败回滚（绝不能把坏配置留在盘上）；
  3. 探测路由器时只读、不因为超时/401 抛异常打断界面。
"""

from __future__ import annotations

import json
import os
import re
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
            # 软件中心比的是令牌本身；这里顺手也认一下带 Bearer 前缀的写法，
            # 好让"客户端自己带的真令牌原样透传"这条用例能整条走通
            authorization = (self.headers.get('Authorization') or '').strip()
            if authorization not in (self.token, f'Bearer {self.token}'):
                self._send(401, b'', 'application/json')
                return
            self._send(200, json.dumps({'Version': '0.0.0-mock', 'Device': 'MockUDM'}).encode(),
                       'application/json')
            return
        # 首页带三个外部 CDN 脚本，用来验证重写
        page = ('<html><head><title>UniFi SoftCenter - 安全管理中心</title>'
                '<script src="https://cdn.tailwindcss.com"></script>'
                '<script src="https://cdn.jsdelivr.net/npm/vue@3.4.21/dist/vue.global.min.js"></script>'
                '<script src="https://cdn.jsdelivr.net/npm/lucide@0.359.0/dist/umd/lucide.min.js"></script>'
                '</head><body>softcenter</body></html>')
        self._send(200, page.encode('utf-8'))


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
            # 与真实模板同构：不含任何占位符（商店安装会原样使用这份文件）
            'location ~ ^/plugin/(?<rc_user>[^/]+)/rtrcenter/(?:index\\.html|control(?:\\.html)?|)$ {\n'
            '    rewrite ^/plugin/[^/]+/rtrcenter/.*$ /root/ break;\n'
            '    proxy_pass http://127.0.0.1:18101;\n'
            '    proxy_set_header X-Plugin-Prefix /plugin/$rc_user/rtrcenter;\n'
            '}\n'
            'location ~ ^/plugin/[^/]+/rtrcenter/api/(.*)$ {\n'
            '    rewrite ^/plugin/[^/]+/rtrcenter/api/(.*)$ /view/api/$1 break;\n'
            '    proxy_pass http://127.0.0.1:18101;\n'
            '}\n'
            'location ~ ^/plugin/[^/]+/rtrcenter/(.*)$ {\n'
            '    alias /data/plugin/router-center/current/web/$1;\n'
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

    def test_render_conf_is_placeholder_free(self) -> None:
        text = engine.render_conf('http://10.0.0.9:9958/', 'u1', 18101)
        self.assertIsNone(re.search(r'__[A-Z_]+__', text), '模板里不能有占位符')
        self.assertIn('proxy_pass http://127.0.0.1:18101;', text)          # 端口写死
        self.assertIn('rewrite ^/plugin/[^/]+/rtrcenter/api/(.*)$ /view/api/$1 break;', text)
        self.assertIn('/data/plugin/router-center/current/web/', text)
        for line in text.splitlines():
            if 'proxy_pass' in line and line.rstrip().endswith('/;'):
                self.fail(f'proxy_pass 不能带 URI：{line.strip()}')

    def test_apply_target_only_saves_settings(self) -> None:
        """目标地址只存设置：接口由插件服务转发，改地址不需要动 nginx。"""
        engine.apply_target('http://10.0.0.9:9958', 'u1', 18101)
        self.assertEqual(engine.load_settings()['target'], 'http://10.0.0.9:9958/')
        self.assertFalse(self.conf.exists(), '改目标地址不应该再写 nginx 配置')

    def test_ensure_conf_rewrites_stale_placeholder_config(self) -> None:
        """商店可能装进一份带占位符的旧模板；服务启动时要纠正过来（并保证 nginx -t 通过）。"""
        self.conf.write_text('location ~ ^/plugin/[^/]+/rtrcenter/ {\n'
                             '    proxy_pass http://127.0.0.1:__PLUGIN_PORT__;\n'
                             '}\n', encoding='utf-8')
        result = engine.ensure_conf('u1', 18101)
        self.assertTrue(result['changed'])
        self.assertTrue(result['ok'])
        written = self.conf.read_text(encoding='utf-8')
        self.assertNotIn('__PLUGIN_PORT__', written)
        self.assertIn('proxy_pass http://127.0.0.1:18101;', written)

    def test_ensure_conf_is_noop_when_already_correct(self) -> None:
        engine.ensure_conf('u1', 18101)
        again = engine.ensure_conf('u1', 18101)
        self.assertFalse(again['changed'])
        self.assertTrue(again['ok'])

    def test_ensure_conf_rolls_back_when_nginx_rejects_it(self) -> None:
        stale = 'proxy_pass http://127.0.0.1:__PLUGIN_PORT__;\n'
        self.conf.write_text(stale, encoding='utf-8')
        with mock.patch.object(engine, 'nginx_test', lambda: (False, 'syntax error')):
            result = engine.ensure_conf('u1', 18101)
        self.assertTrue(result['changed'])
        self.assertFalse(result['ok'])
        self.assertEqual(self.conf.read_text(encoding='utf-8'), stale)   # 回滚成原样


class RewriteTests(unittest.TestCase):
    """页面加工：外部 CDN 换本地副本、GitHub 改走 NAS、注入令牌与设置按钮。"""

    PAGE = (
        '<!doctype html><html><head>'
        '<script src="https://cdn.tailwindcss.com"></script>\n'
        '<script src="https://cdn.jsdelivr.net/npm/vue@3.4.21/dist/vue.global.min.js"></script>\n'
        '<script src="https://cdn.jsdelivr.net/npm/lucide@0.359.0/dist/umd/lucide.min.js"></script>\n'
        '<script src="https://example.com/other.js"></script>\n'
        '<script>const PAGE_DIR = 1;</script>'
        '</head><body>hi</body></html>'
    )

    def test_all_three_cdns_become_local(self) -> None:
        rewritten, unknown = engine.rewrite_page(self.PAGE, '/plugin/u1/rtrcenter')
        self.assertIn('/plugin/u1/rtrcenter/assets/tailwind.js', rewritten)
        self.assertIn('/plugin/u1/rtrcenter/assets/vue.js', rewritten)
        self.assertIn('/plugin/u1/rtrcenter/assets/lucide.js', rewritten)
        self.assertNotIn('cdn.tailwindcss.com', rewritten)
        self.assertNotIn('cdn.jsdelivr.net', rewritten)
        self.assertEqual(unknown, ['https://example.com/other.js'])   # 不认识的保持原样

    def test_other_content_untouched(self) -> None:
        rewritten, _ = engine.rewrite_page(self.PAGE, '/p/u/rtrcenter')
        self.assertIn('<script>const PAGE_DIR = 1;</script>', rewritten)
        self.assertIn('<body>hi</body>', rewritten)

    def test_prefix_with_or_without_slashes(self) -> None:
        for prefix in ('/plugin/u1/rtrcenter', 'plugin/u1/rtrcenter/', '/plugin/u1/rtrcenter/'):
            with self.subTest(prefix=prefix):
                rewritten, _ = engine.rewrite_page(self.PAGE, prefix)
                self.assertIn('/plugin/u1/rtrcenter/assets/vue.js', rewritten)

    def test_page_without_cdn_is_unchanged(self) -> None:
        html = '<html><body><script src="app.js"></script></body></html>'
        rewritten, unknown = engine.rewrite_page(html, '/x')
        self.assertEqual(rewritten, html)
        self.assertEqual(unknown, [])

    def test_github_direct_calls_go_through_the_plugin(self) -> None:
        """云端插件库/Release 检查是页面里直接 fetch GitHub 的，也得改走 NAS。"""
        html = ("<script>const a = await fetch('https://raw.githubusercontent.com/fw867/unifi-softcenterstore"
                "/master/apps/apps.json');</script>"
                "<script>fetch('https://api.github.com/repos/fw867/unifi-softcenterstore/releases/latest')</script>")
        rewritten, _ = engine.rewrite_page(html, '/plugin/3943892/rtrcenter', service_base='/plugin/3943892/rtrcenter/ctl')
        self.assertNotIn('raw.githubusercontent.com', rewritten)
        self.assertNotIn('api.github.com', rewritten)
        self.assertIn('/plugin/3943892/rtrcenter/ctl/github/raw/fw867/unifi-softcenterstore/master/apps/apps.json', rewritten)
        self.assertIn('/plugin/3943892/rtrcenter/ctl/github/api/repos/fw867/unifi-softcenterstore/releases/latest', rewritten)

    def test_prepare_page_injects_token_icon_and_overlay(self) -> None:
        """prepare_page 是"能直接在 App 里打开"的关键：令牌注入 + favicon + 设置按钮。"""
        prepared, unknown = engine.prepare_page(
            self.PAGE, '/plugin/3943892/rtrcenter', service_base='/plugin/3943892/rtrcenter/ctl',
            token='tok-"x"')
        self.assertEqual(unknown, ['https://example.com/other.js'])
        self.assertIn('href="/plugin/3943892/rtrcenter/icon.png"', prepared)
        self.assertIn('localStorage.setItem("sc_token","tok-\\"x\\"")', prepared)     # JSON 转义过
        self.assertLess(prepared.index('sc_token'), prepared.index('cdn.jsdelivr.net') if 'cdn.jsdelivr.net' in prepared else len(prepared))

    def test_prepare_page_without_token_skips_injection(self) -> None:
        prepared, _ = engine.prepare_page(self.PAGE, '/plugin/u/rtrcenter', service_base='/plugin/u/rtrcenter/ctl')
        self.assertNotIn('sc_token', prepared)

    def test_github_upstream_mapping(self) -> None:
        cases = {
            'github/raw/fw867/repo/master/apps/apps.json': 'https://raw.githubusercontent.com/fw867/repo/master/apps/apps.json',
            '/github/api/repos/fw867/repo/releases/latest': 'https://api.github.com/repos/fw867/repo/releases/latest',
            'api/system/info': '',
            '': '',
        }
        for raw, expected in cases.items():
            with self.subTest(path=raw):
                self.assertEqual(engine.github_upstream(raw), expected)

    def test_local_assets_exist_in_repo(self) -> None:
        present = engine.assets_present(PROJECT / 'web')
        self.assertEqual(present, {name: True for name in engine.LOCAL_ASSETS})
        for name in engine.LOCAL_ASSETS:
            size = (PROJECT / 'web' / name).stat().st_size
            self.assertGreater(size, 50_000, f'{name} 看起来不是完整的库（{size} 字节）')
        self.assertTrue((PROJECT / 'web' / 'assets' / 'overlay.js').is_file())


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
        self.user = mock.patch.dict(os.environ, {'NAS_USER_ID': 'u3943892'})

        self.patchers = [
            mock.patch.object(engine, 'STATE_FILE', self.state),
            mock.patch.object(engine, 'TOKEN_FILE', self.token),
            mock.patch.object(engine, 'DEFAULT_TARGET', 'http://192.168.1.1:9958/'),
            mock.patch.object(engine, 'REGISTRY_ROOT', root / 'registry'),
            mock.patch.object(engine, 'HOME_ROOT', root / 'home'),
            mock.patch.object(server, 'STATIC_DIR', static),
        ]
        for patcher in self.patchers:
            patcher.start()
        self.user.start()

        handler = type('BoundHandler', (server.Handler,), {'app': server.RouterCenter(18101)})
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

        # 假路由器：/view/ 的页面取回与接口转发都打到它
        self.router = ThreadingHTTPServer(('127.0.0.1', 0), _MockRouter)
        self.router_target = f'http://127.0.0.1:{self.router.server_address[1]}/'
        threading.Thread(target=self.router.serve_forever, daemon=True).start()
        engine.STATE_FILE.write_text(json.dumps({'target': self.router_target}), encoding='utf-8')

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.router.shutdown()
        self.router.server_close()
        self.user.stop()
        for patcher in self.patchers:
            patcher.stop()
        self.temp.cleanup()

    def request(self, path: str, method: str = 'GET', body: bytes | None = None, headers: dict | None = None):
        import urllib.error
        import urllib.request

        request = urllib.request.Request(f'http://127.0.0.1:{self.port}{path}', data=body, method=method)
        if body is not None:
            request.add_header('Content-Type', 'application/json')
        for name, value in (headers or {}).items():
            request.add_header(name, value)
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
        self.assertEqual(payload['target'], self.router_target)
        # 夹具目录里没有本地副本，所以这里只看结构；真实资源的存在性由 RewriteTests 校验
        self.assertEqual(sorted(payload['assets']), sorted(engine.LOCAL_ASSETS))

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

    def test_settings_returns_plaintext_only_to_verified_xiaomi_client(self) -> None:
        """明文令牌只给设备所有者的小米客户端（看客户端证书，不看 X-Console-Entry）。"""
        engine.save_router_token('good-token')
        status, body = self.request('/api/settings', headers={
            'X-Xiaomi-Client-Verify': 'SUCCESS',
            'X-Xiaomi-Client-DN': 'CN=nas.3943892.test.2,OU=devices',
            'X-Console-Entry': 'xiaomi',
        })
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload['token'], 'good-token')       # 小米客户端（客户端证书）→ 明文
        self.assertTrue(payload['token_visible'])
        self.assertEqual(payload['token_hint'], 'good…en')
        self.assertEqual(body.decode('utf-8').count('good-token'), 1)

    def test_settings_hides_plaintext_from_console_loopback(self) -> None:
        """控制台电脑端那一跳（回环 + 占位 Authorization、没有客户端证书）拿不到明文。"""
        engine.save_router_token('s3cret-router-token')
        status, body = self.request('/api/settings', headers={
            'X-Console-Entry': 'xiaomi',                          # 插件 nginx 对谁都加这个头 → 不算数
            'Authorization': engine.CONSOLE_PLACEHOLDER_AUTHORIZATION,
        })
        self.assertEqual(status, 200)
        text = body.decode('utf-8')
        payload = json.loads(body)
        self.assertEqual(payload['token'], '')
        self.assertFalse(payload['token_visible'])
        self.assertTrue(payload['token_set'])
        self.assertEqual(payload['token_hint'], 's3cr…en')
        self.assertNotIn('s3cret-router-token', text)             # 响应体里绝不能出现真实令牌

    def test_settings_hides_plaintext_from_direct_browser_and_bad_certificate(self) -> None:
        """浏览器直连 / 局域网 curl / 证书验签失败 / CN 不是本机用户 → 都只给提示。"""
        engine.save_router_token('s3cret-router-token')
        cases = {
            '回环直连（无任何头）': {},
            '证书验签失败': {'X-Xiaomi-Client-Verify': 'FAILED',
                        'X-Xiaomi-Client-DN': 'CN=nas.3943892.test.2'},
            '没有证书': {'X-Xiaomi-Client-Verify': 'NONE',
                     'X-Xiaomi-Client-DN': 'CN=nas.3943892.test.2'},
            '别人的客户端证书': {'X-Xiaomi-Client-Verify': 'SUCCESS',
                          'X-Xiaomi-Client-DN': 'CN=nas.999999.test.2'},
        }
        for label, headers in cases.items():
            with self.subTest(entry=label):
                status, body = self.request('/api/settings', headers=headers)
                text = body.decode('utf-8')
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body)['token'], '')
                self.assertFalse(json.loads(body)['token_visible'])
                self.assertNotIn('s3cret-router-token', text)
                self.assertIn('s3cr…en', text)                    # 只给提示

    def test_settings_never_exposes_plaintext_without_nas_user_id(self) -> None:
        """拿不到用户号（没设 NAS_USER_ID）时核对不了证书 CN → 失败关闭，只给提示。"""
        engine.save_router_token('s3cret-router-token')
        with mock.patch.dict(os.environ, {'NAS_USER_ID': ''}):
            status, body = self.request('/api/settings', headers={
                'X-Xiaomi-Client-Verify': 'SUCCESS',
                'X-Xiaomi-Client-DN': 'CN=nas.3943892.test.2',
            })
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['token'], '')
        self.assertNotIn('s3cret-router-token', body.decode('utf-8'))

    def test_page_injection_is_not_affected_by_the_plaintext_gate(self) -> None:
        """注入是插件服务端自己读文件做的：没有客户端证书也照样注入 sc_token。"""
        engine.save_router_token('s3cret-router-token')
        status, body = self.request('/root/', headers={'X-Plugin-Prefix': '/plugin/3943892/rtrcenter'})
        self.assertEqual(status, 200)
        self.assertIn('localStorage.setItem("sc_token","s3cret-router-token")', body.decode('utf-8'))

    def test_page_injection_also_works_for_verified_client_certificate(self) -> None:
        """小米客户端（带证书）打开同一个页面：注入结果一样。"""
        engine.save_router_token('s3cret-router-token')
        status, body = self.request('/root/', headers={
            'X-Plugin-Prefix': '/plugin/3943892/rtrcenter',
            'X-Xiaomi-Client-Verify': 'SUCCESS',
            'X-Xiaomi-Client-DN': 'CN=nas.3943892.test.2',
        })
        self.assertEqual(status, 200)
        self.assertIn('localStorage.setItem("sc_token","s3cret-router-token")', body.decode('utf-8'))

    def test_shell_page_and_static(self) -> None:
        status, body = self.request('/')
        self.assertEqual(status, 200)
        self.assertIn(b'router-center-shell', body)
        status, _ = self.request('/app.js')
        self.assertEqual(status, 200)

    def test_unknown_api_is_404(self) -> None:
        status, _ = self.request('/api/nope')
        self.assertEqual(status, 404)

    def test_view_page_is_rewritten_to_local_assets(self) -> None:
        """/root/（插件根路径）必须把外部 CDN 换成本地副本，并按配置状态注入脚本。"""
        engine.STATE_FILE.write_text(json.dumps({'target': self.router_target}), encoding='utf-8')
        engine.save_router_token('')
        status, body = self.request('/root/', headers={'X-Plugin-Prefix': '/plugin/3943892/rtrcenter'})
        self.assertEqual(status, 200)
        text = body.decode('utf-8')
        self.assertIn('/plugin/3943892/rtrcenter/assets/vue.js', text)
        self.assertIn('/plugin/3943892/rtrcenter/assets/tailwind.js', text)
        self.assertIn('/plugin/3943892/rtrcenter/assets/lucide.js', text)
        self.assertIn('/plugin/3943892/rtrcenter/assets/overlay.js', text)   # 插件只注入常驻入口
        self.assertIn('/plugin/3943892/rtrcenter/assets/close.js', text)     # 右侧中间的关闭按钮
        self.assertIn('href="/plugin/3943892/rtrcenter/icon.png"', text)
        self.assertNotIn('cdn.jsdelivr.net', text)

    def test_view_api_forwards_authorization(self) -> None:
        """/view/api/... 要原样把 Authorization 转给路由器（软件中心靠它鉴权）。"""
        engine.STATE_FILE.write_text(json.dumps({'target': self.router_target}), encoding='utf-8')
        status, body = self.request('/view/api/system/info', headers={'Authorization': _MockRouter.token})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['Device'], 'MockUDM')
        status, _ = self.request('/view/api/system/info')
        self.assertEqual(status, 401)                       # 没令牌 → 路由器的 401 原样透传

    def test_ctl_github_route_reports_unmapped_path(self) -> None:
        status, _ = self.request('/api/github/not-a-github-path')
        self.assertEqual(status, 404)

    def test_path_traversal_is_refused(self) -> None:
        status, _ = self.request('/../settings.json')
        self.assertIn(status, (403, 404))


class _RecordingRouter(_MockRouter):
    """在上面那个假路由器上多记一笔：每次请求带来的 Authorization。"""

    seen: list[tuple[str, str]] = []

    def do_GET(self):                                          # noqa: N802
        type(self).seen.append((self.path.split('?')[0], self.headers.get('Authorization') or ''))
        super().do_GET()


class ForwardedAuthorizationTests(unittest.TestCase):
    """从「控制台」电脑端打开时的报障回归：转发到路由器用的必须是真令牌。

    控制台的本机 nginx（xiaomi-nas-console-lan.nginx.conf）把 Authorization 覆盖成
    "Bearer console-loopback"，插件转发时若原样透传，路由器就报「令牌不正确」。
    """

    def setUp(self) -> None:
        import tempfile

        import server

        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        static = root / 'web'
        static.mkdir()
        (static / 'index.html').write_text('<html><body>shell</body></html>', encoding='utf-8')
        self.token = root / 'router-token'

        self.patchers = [
            mock.patch.object(engine, 'STATE_FILE', root / 'settings.json'),
            mock.patch.object(engine, 'TOKEN_FILE', self.token),
            mock.patch.object(engine, 'DEFAULT_TARGET', 'http://192.168.1.1:9958/'),
            mock.patch.object(server, 'STATIC_DIR', static),
        ]
        for patcher in self.patchers:
            patcher.start()

        handler = type('BoundHandler', (server.Handler,), {'app': server.RouterCenter(18101)})
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.port = self.httpd.server_address[1]

        _RecordingRouter.seen = []
        self.router = ThreadingHTTPServer(('127.0.0.1', 0), _RecordingRouter)
        threading.Thread(target=self.router.serve_forever, daemon=True).start()
        self.router_target = f'http://127.0.0.1:{self.router.server_address[1]}/'
        engine.STATE_FILE.write_text(json.dumps({'target': self.router_target}), encoding='utf-8')

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.router.shutdown()
        self.router.server_close()
        for patcher in self.patchers:
            patcher.stop()
        self.temp.cleanup()

    def request(self, headers: dict | None = None):
        import urllib.error
        import urllib.request

        request = urllib.request.Request(f'http://127.0.0.1:{self.port}/view/api/system/info')
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def test_console_placeholder_is_replaced_by_saved_token(self) -> None:
        """① 控制台占位 Authorization → 转发时换成插件保存的令牌。"""
        engine.save_router_token('good-token')
        status, body = self.request({'Authorization': engine.CONSOLE_PLACEHOLDER_AUTHORIZATION})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['Device'], 'MockUDM')
        self.assertEqual(_RecordingRouter.seen[-1][1], 'good-token')

    def test_missing_authorization_uses_saved_token(self) -> None:
        """② 完全没有 Authorization → 同样用插件保存的令牌，不再白挨一次 401。"""
        engine.save_router_token('good-token')
        status, body = self.request()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['Device'], 'MockUDM')
        self.assertEqual(_RecordingRouter.seen[-1][1], 'good-token')

    def test_real_token_is_passed_through_untouched(self) -> None:
        """③ 客户端自己的合法令牌必须原样透传，不能被插件保存的令牌覆盖。"""
        engine.save_router_token('good-token')
        for real in ('good-token', 'Bearer good-token'):
            with self.subTest(authorization=real):
                status, _ = self.request({'Authorization': real})
                self.assertEqual(status, 200)
                self.assertEqual(_RecordingRouter.seen[-1][1], real)

    def test_other_real_tokens_are_not_replaced_by_the_saved_one(self) -> None:
        """③ 续：与保存的令牌不同的真值也照样透传（路由器判它无效是路由器的事）。"""
        engine.save_router_token('good-token')
        for real in ('other-real-token', 'Bearer other-real-token'):
            with self.subTest(authorization=real):
                status, _ = self.request({'Authorization': real})
                self.assertEqual(status, 401)
                self.assertEqual(_RecordingRouter.seen[-1][1], real)

    def test_placeholder_without_saved_token_forwards_nothing(self) -> None:
        """④ 没保存令牌时不把占位值当令牌送去：这一跳不带 Authorization，401 由页面照实显示。"""
        engine.save_router_token('')
        status, _ = self.request({'Authorization': engine.CONSOLE_PLACEHOLDER_AUTHORIZATION})
        self.assertEqual(status, 401)
        self.assertEqual(_RecordingRouter.seen[-1][1], '')

    def test_missing_authorization_and_no_saved_token_forwards_nothing(self) -> None:
        """④ 续：既没带凭据、插件也没保存令牌 → 同样不带 Authorization。"""
        engine.save_router_token('')
        status, _ = self.request()
        self.assertEqual(status, 401)
        self.assertEqual(_RecordingRouter.seen[-1][1], '')

    def test_placeholder_detection_tolerates_case_and_spaces(self) -> None:
        for value in ('Bearer console-loopback', 'bearer console-loopback', 'Bearer  console-loopback ',
                      'BEARER console-loopback', ' console-loopback '):
            with self.subTest(authorization=value):
                self.assertTrue(engine.is_console_placeholder_authorization(value))
        for value in ('', 'console-loopbacks', 'Bearer other-token', 'Basic console-loopback',
                      'Bearer good-token', 'Bearerconsole-loopback'):
            with self.subTest(authorization=value):
                self.assertFalse(engine.is_console_placeholder_authorization(value))

    def test_console_placeholder_matches_the_console_nginx_template(self) -> None:
        """占位值是从控制台那份 nginx 模板里核对的，这里把两者钉在一起，防止哪天改了没人发现。"""
        template = (Path(__file__).resolve().parents[2] / 'xiaomi-nas-console' / 'deploy'
                    / 'xiaomi-nas-console-lan.nginx.conf')
        if not template.is_file():
            self.skipTest('仓库里没有控制台插件，跳过交叉核对')
        text = template.read_text(encoding='utf-8')
        found = re.findall(r'proxy_set_header\s+Authorization\s+"([^"]*)"', text)
        self.assertIn(engine.CONSOLE_PLACEHOLDER_AUTHORIZATION, found,
                      '控制台模板改了这个占位值，插件侧常量要跟着改')


class PageInjectionEntryTests(unittest.TestCase):
    """⑤ 令牌注入不挑入口：App 标记 / 无标记（控制台电脑端与浏览器直连）/ /view/ 都要注入。"""

    def setUp(self) -> None:
        import tempfile

        import server

        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        static = root / 'web'
        static.mkdir()
        (static / 'index.html').write_text('<html><body>shell</body></html>', encoding='utf-8')

        self.patchers = [
            mock.patch.object(engine, 'STATE_FILE', root / 'settings.json'),
            mock.patch.object(engine, 'TOKEN_FILE', root / 'router-token'),
            mock.patch.object(engine, 'DEFAULT_TARGET', 'http://192.168.1.1:9958/'),
            mock.patch.object(server, 'STATIC_DIR', static),
        ]
        for patcher in self.patchers:
            patcher.start()
        engine.save_router_token('good-token')

        handler = type('BoundHandler', (server.Handler,), {'app': server.RouterCenter(18101)})
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.port = self.httpd.server_address[1]

        self.router = ThreadingHTTPServer(('127.0.0.1', 0), _MockRouter)
        threading.Thread(target=self.router.serve_forever, daemon=True).start()
        engine.STATE_FILE.write_text(
            json.dumps({'target': f'http://127.0.0.1:{self.router.server_address[1]}/'}), encoding='utf-8')

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.router.shutdown()
        self.router.server_close()
        for patcher in self.patchers:
            patcher.stop()
        self.temp.cleanup()

    def fetch(self, path: str, headers: dict | None = None) -> str:
        import urllib.request

        request = urllib.request.Request(f'http://127.0.0.1:{self.port}{path}')
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.read().decode('utf-8')

    def test_token_is_injected_for_every_entry(self) -> None:
        cases = {
            '小米 App 入口': ('/root/', {'X-Console-Entry': 'xiaomi', 'X-Plugin-Prefix': '/plugin/3943892/rtrcenter'}),
            '控制台电脑端（无标记）': ('/root/', {'X-Plugin-Prefix': '/plugin/3943892/rtrcenter'}),
            '浏览器直连（无前缀）': ('/root/', None),
            '/view/ 兼容入口': ('/view/', {'X-Plugin-Prefix': '/plugin/3943892/rtrcenter'}),
        }
        for label, (path, headers) in cases.items():
            with self.subTest(entry=label):
                text = self.fetch(path, headers)
                self.assertIn('localStorage.setItem("sc_token","good-token")', text)
                self.assertLess(text.index('sc_token'), text.index('</head>'))

    def test_no_injection_when_no_token_saved(self) -> None:
        engine.save_router_token('')
        self.assertNotIn('sc_token', self.fetch('/root/', {'X-Plugin-Prefix': '/plugin/u/rtrcenter'}))


class TokenVisibilityTests(unittest.TestCase):
    """明文 AdminToken 只给"设备所有者的小米客户端"：同一个接口，三种入口返回不同。

    判别依据是客户端证书（`$ssl_client_verify` + `$ssl_client_s_dn`），不是
    `X-Console-Entry` —— 后者是插件 nginx 对所有请求都加的头，控制台那一跳也带着。
    """

    def setUp(self) -> None:
        import tempfile

        import server

        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        static = root / 'web'
        static.mkdir()
        (static / 'index.html').write_text('<html><body>shell</body></html>', encoding='utf-8')
        self.user = mock.patch.dict(os.environ, {'NAS_USER_ID': 'u3943892'})
        self.user.start()
        self.patchers = [
            mock.patch.object(engine, 'STATE_FILE', root / 'settings.json'),
            mock.patch.object(engine, 'TOKEN_FILE', root / 'router-token'),
            mock.patch.object(engine, 'DEFAULT_TARGET', 'http://192.168.1.1:9958/'),
            mock.patch.object(engine, 'REGISTRY_ROOT', root / 'registry'),
            mock.patch.object(engine, 'HOME_ROOT', root / 'home'),
            mock.patch.object(server, 'STATIC_DIR', static),
        ]
        for patcher in self.patchers:
            patcher.start()
        handler = type('BoundHandler', (server.Handler,), {'app': server.RouterCenter(18101)})
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.port = self.httpd.server_address[1]

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.user.stop()
        for patcher in self.patchers:
            patcher.stop()
        self.temp.cleanup()

    def settings(self, headers: dict | None = None) -> tuple[int, str]:
        import urllib.error
        import urllib.request

        request = urllib.request.Request(f'http://127.0.0.1:{self.port}/api/settings')
        for name, value in (headers or {}).items():
            request.add_header(name, value)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status, response.read().decode('utf-8')
        except urllib.error.HTTPError as error:
            return error.code, error.read().decode('utf-8')

    def test_verified_xiaomi_client_gets_plaintext(self) -> None:
        engine.save_router_token('good-token')
        _, text = self.settings({'X-Xiaomi-Client-Verify': 'SUCCESS',
                                 'X-Xiaomi-Client-DN': 'CN=nas.3943892.test.2,OU=devices',
                                 'X-Console-Entry': 'xiaomi'})
        self.assertEqual(json.loads(text)['token'], 'good-token')
        self.assertTrue(json.loads(text)['token_visible'])

    def test_console_entry_without_client_certificate_gets_hint(self) -> None:
        engine.save_router_token('s3cret-router-token')
        _, text = self.settings({'X-Console-Entry': 'xiaomi',
                                 'Authorization': engine.CONSOLE_PLACEHOLDER_AUTHORIZATION})
        payload = json.loads(text)
        self.assertEqual(payload['token'], '')
        self.assertFalse(payload['token_visible'])
        self.assertEqual(payload['token_hint'], 's3cr…en')
        self.assertNotIn('s3cret-router-token', text)

    def test_loopback_without_headers_gets_hint(self) -> None:
        engine.save_router_token('s3cret-router-token')
        _, text = self.settings()
        self.assertEqual(json.loads(text)['token'], '')
        self.assertNotIn('s3cret-router-token', text)
        self.assertIn('s3cr…en', text)

    def test_engine_judgement_covers_verify_and_dn(self) -> None:
        cases = {
            ('SUCCESS', 'CN=nas.3943892.test.2,OU=devices', 'u3943892'): True,
            ('success', 'CN=nas.3943892.test.2', '3943892'): True,       # 验签值/用户名写法都容错
            ('SUCCESS', 'CN=nas.999999.test.2', 'u3943892'): False,      # 别人的证书
            ('NONE', 'CN=nas.3943892.test.2', 'u3943892'): False,        # 没有客户端证书（控制台/浏览器）
            ('FAILED', 'CN=nas.3943892.test.2', 'u3943892'): False,      # 验签失败
            ('SUCCESS', '', 'u3943892'): False,                          # 没有 DN
            ('SUCCESS', 'CN=nas.3943892.test.2', ''): False,             # 不知道用户号就不猜
        }
        for (verify, dn, user), expected in cases.items():
            with self.subTest(verify=verify, dn=dn, user=user):
                self.assertEqual(engine.client_certificate_owner(verify, dn, user), expected)

    def test_nginx_template_forwards_client_certificate_headers(self) -> None:
        """判据依赖 nginx 透传证书信息：页面与 ctl 两个 location 都得带上。"""
        conf = (PROJECT / 'deploy' / 'xiaomi-router-center.nginx.conf').read_text(encoding='utf-8')
        self.assertGreaterEqual(conf.count('proxy_set_header X-Xiaomi-Client-Verify $ssl_client_verify;'), 2)
        self.assertGreaterEqual(conf.count('proxy_set_header X-Xiaomi-Client-DN $ssl_client_s_dn;'), 2)


class PluginOwnerTests(unittest.TestCase):
    """单元文件没渲染 NAS_USER_ID 时的兜底：从注册表/插件目录反推用户号。"""

    def setUp(self) -> None:
        import tempfile

        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.registry = root / 'registry'
        self.registry.mkdir()
        self.home = root / 'home'
        self.home.mkdir()
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop('NAS_USER_ID', None)
        self.patchers = [
            mock.patch.object(engine, 'REGISTRY_ROOT', self.registry),
            mock.patch.object(engine, 'HOME_ROOT', self.home),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in self.patchers:
            patcher.stop()
        self.env.stop()
        self.temp.cleanup()

    def test_env_var_wins(self) -> None:
        os.environ['NAS_USER_ID'] = 'u111111'
        self.assertEqual(engine.plugin_owner(), 'u111111')

    def test_unrendered_placeholder_falls_back_to_registry(self) -> None:
        """单元文件里占位符没被渲染时，不能拿它当用户号去比对证书。"""
        os.environ['NAS_USER_ID'] = '__NAS_USER_ID__'
        (self.registry / 'u3943892.list').write_text(
            json.dumps({'rtrcenter': {'info': {'id': 11021}}}), encoding='utf-8')
        self.assertEqual(engine.plugin_owner(), 'u3943892')

    def test_registry_entry_of_other_plugin_is_ignored(self) -> None:
        (self.registry / 'u111111.list').write_text(json.dumps({'jellyfin': {}}), encoding='utf-8')
        (self.registry / 'u222222.list').write_text(json.dumps({'rtrcenter': {}}), encoding='utf-8')
        self.assertEqual(engine.plugin_owner(), 'u222222')

    def test_falls_back_to_home_directory(self) -> None:
        (self.home / 'u333333' / 'plugin' / 'rtrcenter').mkdir(parents=True)
        self.assertEqual(engine.plugin_owner(), 'u333333')

    def test_unknown_owner_is_empty(self) -> None:
        """注册表与 /home 都没有 → 空串（调用方按"不给明文"处理）。"""
        self.assertEqual(engine.plugin_owner(), '')
        (self.home / 'u111111' / 'plugin' / 'rtrcenter').mkdir(parents=True)
        (self.home / 'u222222' / 'plugin' / 'rtrcenter').mkdir(parents=True)
        self.assertEqual(engine.plugin_owner(), '')           # 多个用户不猜


class FrontendTests(unittest.TestCase):
    def test_panel_js_syntax_and_ids(self) -> None:
        import re
        import shutil
        import subprocess

        project = PROJECT
        app_js = (project / 'web' / 'app.js').read_text(encoding='utf-8')
        panel_html = (project / 'web' / 'panel.html').read_text(encoding='utf-8')

        node = shutil.which('node')
        if node:
            for script in ('app.js', os.path.join('assets', 'overlay.js'), os.path.join('assets', 'close.js')):
                result = subprocess.run([node, '--check', str(project / 'web' / script)],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, f'{script}: {result.stderr}')

        ids = set(re.findall(r"\$\('([A-Za-z]+)'\)", app_js))
        self.assertTrue(ids)
        for name in sorted(ids):
            self.assertIn(f'id="{name}"', panel_html, f'app.js 引用了不存在的 id: {name}')

        self.assertNotIn('style="', panel_html, '内联 style 会被严格 CSP 拦掉')
        self.assertNotIn("'/api/", app_js, '前端必须用相对接口基址（ctl/...）')
        self.assertIn("const API = 'ctl'", app_js)
        # 明文令牌只有小米客户端拿得到，设置页必须区分"明文"和"提示"，绝不能把提示当令牌：
        # 输入框永远不预填（value 只清空）、payload.token 只在用户真的输入时才带
        self.assertIn("$('tokenInput').value = ''", app_js)
        self.assertIn("if ($('tokenInput').value.trim()) payload.token", app_js)
        self.assertIn('token_visible', app_js)
        self.assertIn('settings.token_hint', app_js)
        self.assertIn("$('tokenHint')", app_js)
        self.assertIn('id="tokenHint"', panel_html)
        self.assertIn('assets/close.js', (project / 'web' / 'panel.html').read_text(encoding='utf-8'))
        # 软件中心页面不再套 iframe（厂商插件里没有一个用 iframe，实测在 App 里不可靠）
        self.assertNotIn('<iframe', panel_html)
        self.assertNotIn('<iframe', (project / 'web' / 'index.html').read_text(encoding='utf-8'))

    def test_nginx_conf_supports_both_user_id_forms(self) -> None:
        """App 请求的是不带 u 前缀的用户名，配置必须两种写法都认。"""
        conf = (PROJECT / 'deploy' / 'xiaomi-router-center.nginx.conf').read_text(encoding='utf-8')
        # 用户名的两种写法都靠 [^/]+ 匹配（不依赖占位符，商店安装也不会漏渲染）
        self.assertIn('/plugin/(?<rc_user>[^/]+)/rtrcenter/', conf)
        # 商店安装是原样装这份配置的，所以这里绝不能有占位符（曾经因此 nginx -t 失败、更新报错）
        self.assertIsNone(re.search(r'__[A-Z_]+__', conf), '模板里不能有占位符，商店安装会原样使用')
        for route in ('api', 'ctl', 'view'):
            self.assertIn(f'rtrcenter/{route}', conf, f'缺少 {route} 路由')
        self.assertIn('/data/plugin/router-center/current/web/', conf)
        self.assertIn('proxy_pass http://127.0.0.1:18101;', conf)
        # 关键：正则 location 里绝不能出现带 URI 的 proxy_pass（nginx 会 emerg，配置根本不生效）
        for line in conf.splitlines():
            if 'proxy_pass' in line and line.rstrip().endswith('/;'):
                self.fail(f'正则 location 里的 proxy_pass 不能带 URI：{line.strip()}')
        self.assertIn('rewrite ^/plugin/[^/]+/rtrcenter/.*$ /root/ break;', conf)

    def test_install_script_has_no_leftover_placeholder(self) -> None:
        script = (PROJECT / 'deploy' / 'install-on-nas.sh').read_text(encoding='utf-8')
        self.assertIn('nginx -t', script)
        self.assertIn('__[A-Z_]+__', script)                  # 渲染后残留占位符的硬校验
        # 商店是原样安装这些文件的，所以它们一个占位符都不能有
        # （踩过：nginx 配置里的 __PLUGIN_PORT__ 让 nginx -t 直接失败、插件更新报错）
        for name in ('xiaomi-router-center.nginx.conf', 'plugin-meta.json', 'control'):
            text = (PROJECT / 'deploy' / name).read_text(encoding='utf-8')
            self.assertIsNone(re.search(r'__[A-Z_]+__', text), f'{name} 里不能有占位符')
        # 单元文件是例外：它必须带上 NAS_USER_ID（核对客户端证书用），由安装脚本 sed 渲染。
        # 这一点跟 jellyfin/transmission 等插件的单元文件做法一致；万一没渲染，插件会
        # 退回从注册表反推用户号（engine.plugin_owner），拿不到就只给提示、不给明文。
        unit = (PROJECT / 'deploy' / 'xiaomi-router-center.service').read_text(encoding='utf-8')
        self.assertIn('NAS_USER_ID=__NAS_USER_ID__', unit)
        self.assertIn('__NAS_USER_ID__', script)              # 安装脚本确实会替换它


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
