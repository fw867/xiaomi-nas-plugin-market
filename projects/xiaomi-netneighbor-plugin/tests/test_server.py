from __future__ import annotations

import http.client
import json
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import engine
import server
from engine import Result
from test_engine import FakeResponder, Sandbox, successful_manager


def _ok_handler(sandbox):
    return successful_manager(sandbox)


class ServerHarness(unittest.TestCase):
    """真的起一个 HTTP 服务（端口 0，只监听回环），但引擎与命令执行全是假的。"""

    def setUp(self):
        FakeResponder.instances = []
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sandbox = Sandbox(self.tmp.name)
        self.addCleanup(self.sandbox.close)
        self.runner = successful_manager(self.sandbox)
        self.engine = self.sandbox.engine
        self.server = server.Server(('127.0.0.1', 0), self.engine)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)
        self.token = ''
        self.csrf = ''
        # 令牌只发给可信来源：nginx 走 `X-Real-IP`（回环）或客户端证书头。
        # 直连也要模拟这一条，否则页面里的占位符会被替换成空串、所有接口都 401。
        self.load_page(headers={'X-Real-IP': '127.0.0.1'})

    def _stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    # ---- 请求工具 -------------------------------------------------------
    def request(self, method, route, payload=None, headers=None, body=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=15)
        try:
            data = body
            if payload is not None:
                data = json.dumps(payload).encode('utf-8')
            connection.request(method, route, data, headers or {})
            response = connection.getresponse()
            raw = response.read()
            return response.status, dict(response.getheaders()), raw
        finally:
            connection.close()

    def json_request(self, method, route, payload=None, headers=None):
        status, headers, raw = self.request(method, route, payload, headers)
        return status, json.loads(raw.decode('utf-8')) if raw else {}

    def raw_get(self, route):
        """发一条不经客户端规范化的原始请求行（用于路径穿越用例）。"""
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=15)
        try:
            connection.putrequest('GET', route, skip_host=True, skip_accept_encoding=True)
            connection.putheader('Host', '127.0.0.1:%d' % self.server.server_port)
            connection.endheaders()
            response = connection.getresponse()
            raw = response.read()
            return response.status, raw
        finally:
            connection.close()

    def auth(self, write=False):
        headers = {'X-NN-Session': self.token}
        if write:
            headers['X-CSRF-Token'] = self.csrf
            headers['Content-Type'] = 'application/json'
        return headers

    def load_page(self, headers=None):
        status, response_headers, raw = self.request('GET', '/index.html', headers=headers)
        self.assertEqual(status, 200)
        html = raw.decode('utf-8')
        self.token = re.search(r'name="nn-session" content="([^"]*)"', html)[1]
        self.csrf = re.search(r'name="csrf-token" content="([^"]*)"', html)[1]
        return html, response_headers

    def target_dir(self, name='照片'):
        """在沙箱里建目录，返回引擎侧的**受管 POSIX 路径**（接口传的就是它）。"""
        target = self.sandbox.root / 'home' / 'u3943892' / 'pool0' / 'data' / name
        target.mkdir(parents=True, exist_ok=True)
        return self.sandbox.virtual(target)


class AuthTests(ServerHarness):
    def test_healthz_is_open(self):
        status, data = self.json_request('GET', '/healthz')
        self.assertEqual(status, 200)
        self.assertTrue(data['ok'])
        self.assertEqual(data['version'], engine.VERSION)

    def test_page_hands_out_session_and_csrf(self):
        self.assertTrue(self.token)
        self.assertTrue(self.csrf)
        self.assertNotEqual(self.token, self.csrf)
        # 占位符必须都被替换掉
        status, _headers, raw = self.request('GET', '/index.html')
        text = raw.decode('utf-8')
        self.assertNotIn('__SESSION_TOKEN__', text)
        self.assertNotIn('__CSRF_TOKEN__', text)
        self.assertNotIn('__PLUGIN_VERSION__', text)
        self.assertIn(engine.VERSION, text)

    def test_page_sets_session_cookie(self):
        _html, headers = self.load_page(headers={'X-Real-IP': '127.0.0.1'})
        cookie = headers.get('Set-Cookie', '')
        self.assertIn('nn_session=', cookie)
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Strict', cookie)

    def test_token_only_issued_to_trusted_sources(self):
        for headers in ({'X-Real-IP': '8.8.8.8'}, {}):
            with self.subTest(headers=headers):
                _html, response_headers = self.load_page(headers=headers)
                self.assertNotIn('Set-Cookie', response_headers)

    def test_session_reads_cookie_too(self):
        self.load_page(headers={'X-Real-IP': '127.0.0.1'})
        status, data = self.json_request('GET', '/api/status',
                                         headers={'Cookie': 'nn_session=' + self.token})
        self.assertEqual(status, 200)
        self.assertTrue(data['ok'])

    def test_reads_without_token_are_denied(self):
        self.assertEqual(self.request('GET', '/api/status')[0], 401)

    def test_bogus_token_is_denied(self):
        self.assertEqual(
            self.request('GET', '/api/status', headers={'X-NN-Session': 'x.y.z'})[0], 401)
        self.assertEqual(
            self.request('GET', '/api/status',
                         headers={'X-NN-Session': self.token + 'x'})[0], 401)

    def test_writes_require_csrf(self):
        for route, payload in (('/api/share/add', {'account': 'fw867', 'path': '/tmp'}),
                               ('/api/share/add', {'account': 'fw867', 'paths': ['/tmp']}),
                               ('/api/share/delete', {'shareName': 'fw867_nb_1'}),
                               ('/api/discovery', {'enabled': False}),
                               ('/api/hostname', {'hostname': 'MyNAS'}),
                               ('/api/detect/restart', {})):
            with self.subTest(route=route):
                status, data = self.json_request('POST', route, payload,
                                                 {'X-NN-Session': self.token,
                                                  'Content-Type': 'application/json'})
                self.assertEqual(status, 403)
                self.assertFalse(data['ok'])

    def test_writes_without_session_are_denied(self):
        status, _data = self.json_request(
            'POST', '/api/share/add', {'account': 'fw867', 'path': '/tmp'},
            {'X-CSRF-Token': self.csrf, 'Content-Type': 'application/json'})
        self.assertEqual(status, 401)

    def test_bad_json_is_rejected(self):
        status, _headers, _raw = self.request(
            'POST', '/api/share/add', headers=self.auth(write=True), body=b'not json')
        self.assertEqual(status, 400)
        status, _headers, _raw = self.request(
            'POST', '/api/share/add', headers=self.auth(write=True), body=b'[1,2]')
        self.assertEqual(status, 400)             # 必须是 JSON 对象

    def test_body_size_is_bounded(self):
        status, _headers, _raw = self.request(
            'POST', '/api/share/add', headers=self.auth(write=True), body=b'x' * (server.MAX_BODY + 10))
        self.assertEqual(status, 400)

    def test_unknown_route(self):
        self.assertEqual(self.request('GET', '/api/nope', headers=self.auth())[0], 404)
        self.assertEqual(
            self.request('POST', '/api/nope', {}, self.auth(write=True))[0], 404)

    def test_static_files_are_served_and_traversal_blocked(self):
        status, headers, raw = self.request('GET', '/app.js')
        self.assertEqual(status, 200)
        self.assertIn(b'nn-session', raw)
        # `http.client` 会把 `..` 规范化掉，所以路径穿越要手写原始请求行才发得出去
        status, _raw = self.raw_get('/api/../app.js')
        self.assertIn(status, (400, 401, 403, 404))
        status, _raw = self.raw_get('/../../etc/passwd')
        self.assertIn(status, (400, 401, 403, 404))


class StatusApiTests(ServerHarness):
    def test_status_reports_discovery_and_accounts(self):
        self.engine.start()
        status, data = self.json_request('GET', '/api/status', headers=self.auth())
        self.assertEqual(status, 200)
        self.assertTrue(data['ok'])
        discovery = data['discovery']
        self.assertTrue(discovery['running'])
        self.assertTrue(discovery['managed'])
        self.assertTrue(discovery['wsddDropin'])
        self.assertEqual(discovery['hostname'], 'SmartStorage')
        self.assertEqual(data['accounts'][0]['account'], 'fw867')
        self.assertIn('/home/u3943892/pool0/data', data['allowedRoots'])
        self.assertTrue(any('nb_' not in item['name'] for item in self.engine.shares()))

    def test_log_endpoint(self):
        self.engine.log('测试日志')
        status, data = self.json_request('GET', '/api/log', headers=self.auth())
        self.assertEqual(status, 200)
        self.assertTrue(any('测试日志' in line for line in data['lines']))


class ShareApiTests(ServerHarness):
    def test_add_share_happy_path(self):
        # 别用 `/home/u3943892/pool0/data/我的照片`：默认配置里它已经被段 id
        # `u3943892_nb_1` 共享，会被「这个目录已经是共享」挡住。
        target = self.target_dir('新照片')
        status, data = self.json_request(
            'POST', '/api/share/add',
            {'account': 'fw867', 'path': str(target), 'sharePoint': '新照片-3943892'},
            self.auth(write=True))
        self.assertEqual(status, 200)
        self.assertTrue(data['ok'])
        result = data['result']
        self.assertEqual(result['shareName'], 'fw867_nb_1')
        self.assertEqual(result['sharePoint'], '新照片-3943892')
        self.assertEqual(result['users'], ['fw867'])
        self.assertTrue(result['verified'])
        argv = self.runner.argv_for('add_dir')
        self.assertEqual(argv, [self.sandbox.smb_mgr, 'shares', 'add_dir', 'fw867_nb_1',
                                str(target), 'fw867', '新照片-3943892', 'u3943892'])
        # 返回里带上最新状态，页面一次渲染
        self.assertIn('status', data)
        names = [item['name'] for item in data['status']['accounts'][0]['shares']]
        self.assertIn('fw867_nb_1', names)

    def test_add_share_validation_error_is_chinese(self):
        status, data = self.json_request(
            'POST', '/api/share/add', {'account': 'fw867', 'path': '/etc'},
            self.auth(write=True))
        self.assertEqual(status, 400)
        self.assertFalse(data['ok'])
        self.assertIn('不在允许的根目录内', data['error'])

    def test_add_share_missing_path(self):
        status, data = self.json_request(
            'POST', '/api/share/add', {'account': 'fw867'}, self.auth(write=True))
        self.assertEqual(status, 400)
        self.assertIn('路径', data['error'])

    def test_add_share_unknown_account(self):
        target = self.target_dir()
        status, data = self.json_request(
            'POST', '/api/share/add', {'account': 'nobody', 'path': str(target)},
            self.auth(write=True))
        self.assertEqual(status, 400)
        self.assertIn('没有这个账号', data['error'])

    def test_add_share_surfaces_command_output_on_failure(self):
        self.runner.handler = lambda inner, key, timeout: (
            Result(1, 'stdout line', 'share exists') if 'add_dir' in key else None)
        target = self.target_dir()
        status, data = self.json_request(
            'POST', '/api/share/add', {'account': 'fw867', 'path': str(target)},
            self.auth(write=True))
        self.assertEqual(status, 400)
        self.assertIn('share exists', data['error'])
        self.assertIn('exit code: 1', data['error'])

    def test_delete_share_happy_path(self):
        target = self.target_dir()
        self.json_request('POST', '/api/share/add',
                          {'account': 'fw867', 'path': str(target), 'sharePoint': '照片'},
                          self.auth(write=True))
        status, data = self.json_request('POST', '/api/share/delete',
                                         {'shareName': 'fw867_nb_1'}, self.auth(write=True))
        self.assertEqual(status, 200)
        self.assertTrue(data['ok'])
        self.assertEqual(self.runner.argv_for('del_dir'),
                         [self.sandbox.smb_mgr, 'shares', 'del_dir', 'fw867_nb_1'])
        names = [item['name'] for item in self.engine.shares()]
        self.assertNotIn('fw867_nb_1', names)

    def test_delete_refuses_system_share(self):
        """系统/App 的共享（段 id 是 NAS 用户命名空间 `u3943892_nb_1`）不许删。"""
        status, data = self.json_request('POST', '/api/share/delete',
                                         {'shareName': 'u3943892_nb_1'}, self.auth(write=True))
        self.assertEqual(status, 400)
        self.assertIn('只允许删除插件自己添加的共享', data['error'])
        self.assertIsNone(self.runner.argv_for('del_dir'))

    def test_restart_endpoint_announces(self):
        self.engine.start()
        first = self.engine.responder
        status, data = self.json_request('POST', '/api/detect/restart', {}, self.auth(write=True))
        self.assertEqual(status, 200)
        self.assertTrue(data['ok'])
        self.assertTrue(first.stopped)
        self.assertEqual(self.engine.responder.hellos, [2])
        self.assertTrue(data['status']['discovery']['running'])


class DiscoveryApiTests(ServerHarness):
    """`POST /api/discovery`：开关的鉴权、CSRF、幂等关闭。"""

    def test_requires_a_session_and_csrf(self):
        for route, payload in (('/api/discovery', {'enabled': False}),
                               ('/api/hostname', {'hostname': 'MyNAS'})):
            with self.subTest(route=route):
                status, data = self.json_request(
                    'POST', route, payload,
                    {'X-NN-Session': self.token, 'Content-Type': 'application/json'})
                self.assertEqual(status, 403)                   # 缺 CSRF
                self.assertFalse(data['ok'])
                status, data = self.json_request(
                    'POST', route, payload,
                    {'X-CSRF-Token': self.csrf, 'Content-Type': 'application/json'})
                self.assertEqual(status, 401)                   # 缺会话
                self.assertFalse(data['ok'])

    def test_status_exposes_settings(self):
        status, data = self.json_request('GET', '/api/status', headers=self.auth())
        self.assertEqual(status, 200)
        self.assertEqual(data['settings'],
                         {'discoveryEnabled': True, 'hostname': 'SmartStorage'})
        self.assertTrue(data['discovery']['enabled'])

    def test_disabling_returns_the_new_status(self):
        self.engine.start()
        self.assertTrue(self.engine.discovery['dropin'])
        status, data = self.json_request('POST', '/api/discovery', {'enabled': False},
                                         self.auth(write=True))
        self.assertEqual(status, 200)
        self.assertTrue(data['ok'])
        self.assertFalse(data['status']['settings']['discoveryEnabled'])
        self.assertFalse(data['status']['discovery']['running'])
        self.assertFalse(data['status']['discovery']['wsddDropin'])
        self.assertFalse(self.sandbox.dropin.exists())
        self.assertIsNone(self.engine.responder)
        # 页面刷新后读到的仍然是「关闭」
        status, again = self.json_request('GET', '/api/status', headers=self.auth())
        self.assertFalse(again['settings']['discoveryEnabled'])

    def test_disabling_twice_is_not_an_error(self):
        for _ in range(2):
            status, data = self.json_request('POST', '/api/discovery', {'enabled': False},
                                             self.auth(write=True))
            self.assertEqual(status, 200)
            self.assertTrue(data['ok'])
        self.assertIn(['/bin/systemctl', 'start', 'wsdd'], self.runner.calls)
        self.assertFalse(self.sandbox.dropin.exists())

    def test_enabling_takes_over(self):
        self.engine.set_discovery_enabled(False)
        status, data = self.json_request('POST', '/api/discovery', {'enabled': True},
                                         self.auth(write=True))
        self.assertEqual(status, 200)
        self.assertTrue(data['status']['settings']['discoveryEnabled'])
        self.assertTrue(data['status']['discovery']['running'])
        self.assertTrue(self.sandbox.dropin.exists())


class HostnameApiTests(ServerHarness):
    """`POST /api/hostname`：校验 → 落盘 → 重建回应器 + 重发 Hello。"""

    def test_saves_name_and_announces(self):
        self.engine.start()
        first = self.engine.responder
        status, data = self.json_request('POST', '/api/hostname', {'hostname': 'XiaoMiNAS'},
                                         self.auth(write=True))
        self.assertEqual(status, 200)
        self.assertTrue(data['ok'])
        self.assertEqual(data['status']['settings']['hostname'], 'XiaoMiNAS')
        self.assertEqual(data['status']['discovery']['hostname'], 'XiaoMiNAS')
        self.assertTrue(first.stopped)
        self.assertEqual(self.engine.responder.hostname, 'XiaoMiNAS')
        self.assertEqual(self.engine.responder.hellos, [2])
        saved = json.loads((self.sandbox.data / 'settings.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['hostname'], 'XiaoMiNAS')

    def test_reset_clears_saved_name_and_falls_back_to_default(self):
        self.engine.start()
        self.engine.set_hostname('XiaoMiNAS')
        status, data = self.json_request('POST', '/api/hostname', {'reset': True},
                                         self.auth(write=True))
        self.assertEqual(status, 200)
        self.assertTrue(data['ok'])
        # 生效名回到 samba 配置里的那个；落盘值被清空（见下面的文件断言）
        self.assertEqual(data['status']['settings']['hostname'], 'SmartStorage')
        self.assertEqual(data['status']['discovery']['hostname'], 'SmartStorage')
        self.assertEqual(self.engine.responder.hostname, 'SmartStorage')
        self.assertIn('恢复默认', data['status']['discovery']['message'])
        saved = json.loads((self.sandbox.data / 'settings.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['hostname'], '')

    def test_invalid_names_are_rejected_in_chinese(self):
        self.engine.start()
        responder = self.engine.responder
        for bad, needle in [('', '请填写主机名'), ('a' * 16, '最长 15 个字符'),
                            ('-bad', '字母或数字开头'), ('bad name', '只能包含')]:
            with self.subTest(bad=bad):
                status, data = self.json_request('POST', '/api/hostname', {'hostname': bad},
                                                 self.auth(write=True))
                self.assertEqual(status, 400)
                self.assertFalse(data['ok'])
                self.assertIn(needle, data['error'])
        self.assertIs(self.engine.responder, responder)          # 没有重建回应器


class DirsApiTests(ServerHarness):
    """`GET /api/dirs`：弹窗里的目录列表。"""

    def test_requires_a_session(self):
        status, _data = self.json_request('GET', '/api/dirs?account=fw867')
        self.assertEqual(status, 401)

    def test_lists_directories_with_shared_flags(self):
        shared_dir = self.target_dir('我的照片')
        free_dir = self.target_dir('下载')
        self.sandbox.root.joinpath('home', 'u3943892', 'pool0', 'data', '笔记.txt').write_text(
            'x', encoding='utf-8')
        status, data = self.json_request('GET', '/api/dirs?account=fw867', headers=self.auth())
        self.assertEqual(status, 200)
        self.assertTrue(data['ok'])
        self.assertEqual(data['account'], 'fw867')
        self.assertEqual(data['root'], '/home/u3943892/pool0/data')
        paths = [item['path'] for item in data['dirs']]
        self.assertEqual(sorted(paths), sorted([shared_dir, free_dir]))
        by_path = {item['path']: item for item in data['dirs']}
        self.assertTrue(by_path[shared_dir]['shared'])
        self.assertEqual(by_path[shared_dir]['shareName'], 'u3943892_nb_1')
        self.assertEqual(by_path[shared_dir]['display'], '照片-3943892')
        self.assertFalse(by_path[free_dir]['shared'])
        self.assertEqual(by_path[free_dir]['shareName'], '')

    def test_unknown_account_and_missing_query(self):
        status, data = self.json_request('GET', '/api/dirs?account=nobody', headers=self.auth())
        self.assertEqual(status, 400)
        self.assertIn('没有这个账号', data['error'])
        status, data = self.json_request('GET', '/api/dirs', headers=self.auth())
        self.assertEqual(status, 400)
        self.assertIn('请选择账号', data['error'])


class ShareApiBatchTests(ServerHarness):
    """`POST /api/share/add` 的多选分支：一次提交多个目录。"""

    def test_multiple_paths_add_in_one_batch(self):
        first = self.target_dir('照片')
        second = self.target_dir('视频')
        status, data = self.json_request(
            'POST', '/api/share/add', {'account': 'fw867', 'paths': [first, second]},
            self.auth(write=True))
        self.assertEqual(status, 200)
        self.assertTrue(data['ok'])
        result = data['result']
        self.assertEqual([item['shareName'] for item in result['added']],
                         ['fw867_nb_1', 'fw867_nb_2'])
        self.assertTrue(all(item['verified'] for item in result['added']))
        self.assertEqual(result['errors'], [])
        self.assertEqual(self.runner.count('add_dir'), 2)
        self.assertEqual(self.runner.count('init_config'), 1)
        self.assertEqual(
            self.runner.calls.count(['/bin/systemctl', 'reload', 'smb', 'nmb']), 1)
        names = [item['name'] for item in data['status']['accounts'][0]['shares']]
        self.assertIn('fw867_nb_1', names)
        self.assertIn('fw867_nb_2', names)

    def test_already_shared_path_is_skipped_not_an_error(self):
        target = self.target_dir('照片')
        first = self.json_request(
            'POST', '/api/share/add', {'account': 'fw867', 'paths': [target]},
            self.auth(write=True))
        self.assertEqual(first[0], 200)
        status, data = self.json_request(
            'POST', '/api/share/add', {'account': 'fw867', 'paths': [target]},
            self.auth(write=True))
        self.assertEqual(status, 200)
        self.assertEqual(data['result']['added'], [])
        self.assertEqual(len(data['result']['skipped']), 1)
        self.assertIn('已经是账号 fw867 的共享', data['result']['skipped'][0]['error'])

    def test_one_bad_path_does_not_block_the_others(self):
        first = self.target_dir('照片')
        status, data = self.json_request(
            'POST', '/api/share/add',
            {'account': 'fw867', 'paths': [first, '/etc']}, self.auth(write=True))
        self.assertEqual(status, 200)
        self.assertEqual([item['shareName'] for item in data['result']['added']],
                         ['fw867_nb_1'])
        self.assertEqual(data['result']['errors'][0]['path'], '/etc')
        self.assertIn('不在允许的根目录内', data['result']['errors'][0]['error'])

    def test_multiple_paths_need_csrf(self):
        first = self.target_dir('照片')
        second = self.target_dir('视频')
        status, data = self.json_request(
            'POST', '/api/share/add', {'account': 'fw867', 'paths': [first, second]},
            {'X-NN-Session': self.token, 'Content-Type': 'application/json'})
        self.assertEqual(status, 403)
        self.assertFalse(data['ok'])
        self.assertIsNone(self.runner.argv_for('add_dir'))


class ShareApiBatchDeleteTests(ServerHarness):
    """`POST /api/share/delete` 的多选分支：一次提交多个共享名。"""

    def add(self, *names):
        for name in names:
            self.json_request('POST', '/api/share/add',
                              {'account': 'fw867', 'path': self.target_dir(name),
                               'sharePoint': name}, self.auth(write=True))
        return [item['name'] for item in self.engine.shares() if item['name'].startswith('fw867_')]

    def test_multiple_names_delete_in_one_batch(self):
        names = self.add('照片', '视频')
        self.assertEqual(names[:2], ['fw867_nb_1', 'fw867_nb_2'])
        self.runner.calls.clear()

        status, data = self.json_request(
            'POST', '/api/share/delete', {'shareNames': names[:2]}, self.auth(write=True))

        self.assertEqual(status, 200)
        self.assertTrue(data['ok'])
        result = data['result']
        self.assertEqual(sorted(result), ['errors', 'initReturncode', 'output',
                                         'reloadReturncode', 'removed', 'verified'])
        self.assertEqual([item['shareName'] for item in result['removed']], names[:2])
        self.assertTrue(result['verified'])
        self.assertEqual(result['errors'], [])
        self.assertEqual(self.runner.count('del_dir'), 2)
        self.assertEqual(self.runner.count('init_config'), 1)
        self.assertEqual(
            self.runner.calls.count(['/bin/systemctl', 'reload', 'smb', 'nmb']), 1)
        # 返回里带上最新状态，页面一次渲染
        self.assertIn('status', data)
        remaining = [item['name'] for item in data['status']['accounts'][0]['shares']]
        for name in names[:2]:
            self.assertNotIn(name, remaining)

    def test_batch_delete_rejects_protected_without_commands(self):
        names = self.add('照片')
        self.runner.calls.clear()
        status, data = self.json_request(
            'POST', '/api/share/delete', {'shareNames': names[:1] + ['public']},
            self.auth(write=True))
        self.assertEqual(status, 400)
        self.assertFalse(data['ok'])
        self.assertIn('不允许删除', data['error'])
        self.assertIsNone(self.runner.argv_for('del_dir'))
        self.assertIsNone(self.runner.argv_for('init_config'))

    def test_batch_delete_is_empty_when_all_names_are_blank(self):
        status, data = self.json_request(
            'POST', '/api/share/delete', {'shareNames': ['', '  ']}, self.auth(write=True))
        self.assertEqual(status, 400)
        self.assertIn('请选择要删除的共享', data['error'])

    def test_batch_delete_requires_session_and_csrf(self):
        names = self.add('照片')
        status, data = self.json_request(
            'POST', '/api/share/delete', {'shareNames': names},
            {'X-NN-Session': self.token, 'Content-Type': 'application/json'})
        self.assertEqual(status, 403)
        self.assertFalse(data['ok'])
        status, data = self.json_request(
            'POST', '/api/share/delete', {'shareNames': names},
            {'X-CSRF-Token': self.csrf, 'Content-Type': 'application/json'})
        self.assertEqual(status, 401)
        self.assertFalse(data['ok'])
        self.assertIsNone(self.runner.argv_for('del_dir'))

    def test_single_share_name_still_uses_the_old_shape(self):
        """旧的单条 body 返回结构不变（没有 removed/verified 这批批量字段）。"""
        names = self.add('照片')
        status, data = self.json_request('POST', '/api/share/delete',
                                         {'shareName': names[0]}, self.auth(write=True))
        self.assertEqual(status, 200)
        self.assertEqual(data['result']['shareName'], names[0])
        self.assertNotIn('removed', data['result'])
        self.assertTrue(data['result']['verified'])


class RestoreCliTests(unittest.TestCase):
    def test_restore_flag_is_an_early_exit(self):
        """--restore-wsdd 只恢复官方发现服务，不建 Engine、不开 HTTP 端口。"""
        with patch.object(engine, 'restore_wsdd', return_value='已恢复') as restore, \
                patch.object(server, 'engine_factory') as factory:
            code = server.main(['--restore-wsdd'])
        self.assertEqual(code, 0)
        restore.assert_called_once()
        factory.assert_not_called()

    def test_restore_flag_does_not_need_a_token_or_http(self):
        calls: list = []

        def fake_restore(data_dir=None, runner=None):
            calls.append(data_dir)
            return '未发现官方 wsdd（UnitFileState 为空），无需恢复'

        with patch.object(engine, 'restore_wsdd', side_effect=fake_restore):
            code = server.restore_path()
        self.assertEqual(code, 0)
        self.assertEqual(calls, [server.DATA_DIR])

    def test_main_without_flag_starts_the_http_service(self):
        """不给参数时应该走正常启动路径（这里把 Server 换成假的，避免真的监听）。"""
        started: list = []

        class FakeServer:
            def __init__(self, address, engine_instance, dev=False):
                started.append((address, dev))

            def serve_forever(self):
                raise KeyboardInterrupt

            def shutdown(self):
                return None

            def server_close(self):
                return None

        fake_engine = engine.Engine(data_dir=tempfile.mkdtemp(prefix='nn-cli-'),
                                    start_responder=False)
        with patch.object(server, 'engine_factory', return_value=fake_engine), \
                patch.object(server, 'Server', FakeServer), \
                patch.object(engine.Engine, 'shutdown') as shutdown:
            code = server.main([])
        self.assertEqual(code, 0)
        self.assertEqual(started, [(('127.0.0.1', server.PORT), False)])
        shutdown.assert_called_once()


if __name__ == '__main__':
    unittest.main()
