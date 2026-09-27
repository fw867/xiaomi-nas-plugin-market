import base64
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import socket
import struct
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch

import engine  # 按模块打桩，例如 engine.upnp
import upnp
from engine import (Engine, Error, IMAGE, NAME, LABEL, PORT, BT_PORT, VERSION, confined, mutation,
                    password_hash, container_config, installed_version, FORWARD_RETRY_SECONDS)
from server import Server, accepted, AUTO_LOGIN_MAX_FAILURES


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'root'
        self.root.mkdir()
        (self.root / 'MiShare').mkdir()
        self.engine = Engine(Path(self.tmp.name) / 'private', self.root)
        # 单测绝不碰真实网络：UPnP/NAT-PMP 默认打桩，专门的用例再自己覆盖
        self.addCleanup(patch.object(engine.upnp, 'lan_address',
                                     return_value='192.168.1.8').stop)
        self.addCleanup(patch.object(engine.upnp, 'forward_ports', return_value={
            'ok': False, 'method': '', 'detail': '测试环境跳过', 'at': 0}).stop)

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

    def test_setup_saves_credential(self):
        """安装时输入的密码要记下来，供以后自动登录，用户不必再输一次。"""
        if not self.root.stat().st_uid or not self.root.stat().st_gid:
            self.skipTest('requires non-root test directory owner')

        def fake_api(method, path, body=None, timeout=30):
            if path == '/info':
                return 200, b'{"Architecture":"aarch64"}'
            return 404, b'{"message":"No such container"}'

        with patch('engine.os.chown'), patch('engine.docker_api', side_effect=fake_api):
            self.engine.setup('MiShare', 'Example123')
        self.assertEqual(self.engine.saved_credential(), 'Example123')
        # 凭据文件权限收紧到仅属主可读
        self.assertEqual(self.engine.credentialfile.stat().st_mode & 0o077, 0)

    def test_setup_refuses_existing_container(self):
        if not self.root.stat().st_uid or not self.root.stat().st_gid:
            self.skipTest('requires non-root test directory owner')

        def fake_api(method, path, body=None, timeout=30):
            if path == '/info':
                return 200, b'{"Architecture":"aarch64"}'
            return 200, json.dumps({'Config': {'Labels': {LABEL: 'other'}}}).encode('utf-8')

        with patch('engine.docker_api', side_effect=fake_api), self.assertRaises(Error):
            self.engine.setup('MiShare', 'Example123')


class PackagingTests(unittest.TestCase):
    """打包清单是显式列文件的：插件目录里新增的模块必须同步进去。

    回归用例：upnp.py 加进插件后忘了写进 scripts/build_apps.py 的 runtime，
    CI 构建出的商店包里就没有这个文件，用户从商店装完启动即
    `ModuleNotFoundError: No module named 'upnp'`（手工拷文件部署时看不出来）。
    """

    def setUp(self):
        self.plugin = Path(__file__).resolve().parents[1]
        self.build = self.plugin.parents[1] / 'scripts' / 'build_apps.py'

    def _spec(self):
        import ast
        tree = ast.parse(self.build.read_text(encoding='utf-8'))
        for node in tree.body:
            if isinstance(node, ast.AnnAssign) and getattr(node.target, 'id', '') == 'PACKAGE_SPECS':
                for spec in ast.literal_eval(node.value):
                    if spec.get('project') == self.plugin.name:
                        return spec
        self.fail('build_apps.py 里找不到本插件的 PACKAGE_SPECS 条目')

    def test_every_module_is_packaged(self):
        runtime = self._spec()['runtime']
        packaged = set(runtime.keys()) | set(runtime.values())
        for module in sorted(p.name for p in self.plugin.glob('*.py')):
            with self.subTest(module=module):
                self.assertIn(module, packaged)

    def test_every_imported_local_module_exists(self):
        """engine/server 里 import 的本地模块必须在插件目录里真的有。"""
        import ast
        for source in ('engine.py', 'server.py'):
            tree = ast.parse((self.plugin / source).read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name.split('.')[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names = [node.module.split('.')[0]]
                for name in names:
                    target = self.plugin / (name + '.py')
                    if target.is_file():
                        with self.subTest(module=name, source=source):
                            self.assertTrue(target.is_file())


class UpnpTests(unittest.TestCase):
    """UPnP/NAT-PMP 客户端（与 transmission 插件同一份实现），全部离线打桩。"""

    def test_gateway_reads_default_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            route = Path(tmp) / 'route'
            route.write_text(
                'Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n'
                'docker0\t000011AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n'
                'enu1u3\t00000000\t0101A8C0\t0003\t0\t0\t1004\t00000000\t0\t0\t0\n',
                encoding='utf-8')
            with patch.object(upnp, 'PROC_ROUTE', str(route)):
                self.assertEqual(upnp.gateway(), '192.168.1.1')

    def test_control_point_prefers_wan_service(self):
        description = (
            '<?xml version="1.0"?><root xmlns="urn:schemas-upnp-org:device-1-0"><device>'
            '<serviceList>'
            '<service><serviceType>urn:schemas-upnp-org:service:Layer3Forwarding:1</serviceType>'
            '<controlURL>/ctl/L3F</controlURL></service>'
            '<service><serviceType>urn:schemas-upnp-org:service:WANIPConnection:2</serviceType>'
            '<controlURL>/ctl/IPConn</controlURL></service>'
            '</serviceList></device></root>')
        with patch.object(upnp, '_request', return_value=(200, description)):
            service_type, control = upnp.control_point('http://192.168.1.1:41795/rootDesc.xml')
        self.assertEqual(service_type, 'urn:schemas-upnp-org:service:WANIPConnection:2')
        self.assertEqual(control, 'http://192.168.1.1:41795/ctl/IPConn')

    def test_add_mapping_reports_router_fault(self):
        fault = ('<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
                 '<s:Fault><detail><UPnPError xmlns="urn:schemas-upnp-org:control-1-0">'
                 '<errorCode>501</errorCode><errorDescription>Action Failed</errorDescription>'
                 '</UPnPError></detail></s:Fault></s:Body></s:Envelope>')
        with patch.object(upnp, '_request', return_value=(500, fault)):
            with self.assertRaises(upnp.UpnpError) as ctx:
                upnp.add_mapping('http://192.168.1.1/ctl', 'urn:x:WANIPConnection:2',
                                 BT_PORT, BT_PORT, '192.168.1.8', 'TCP', 'desc')
        self.assertIn('501', str(ctx.exception))

    def test_natpmp_success_returns_lease(self):
        reply = struct.pack('!BBHIHHI', 0, 130, 0, 100, BT_PORT, BT_PORT, 3600)
        sock = _FakeSocket([reply])
        with patch.object(upnp, '_udp_socket', return_value=sock):
            ok, detail, lease = upnp.natpmp_map('192.168.1.1', BT_PORT, BT_PORT, 'tcp')
        self.assertTrue(ok)
        self.assertEqual(lease, 3600)
        self.assertEqual(sock.sent[0][1], ('192.168.1.1', 5351))

    def test_natpmp_refused(self):
        reply = struct.pack('!BBHIHH', 0, 130, 3, 100, BT_PORT, 0)
        with patch.object(upnp, '_udp_socket', return_value=_FakeSocket([reply])):
            ok, detail, lease = upnp.natpmp_map('192.168.1.1', BT_PORT, BT_PORT, 'tcp')
        self.assertFalse(ok)
        self.assertIn('网络故障', detail)
        self.assertEqual(lease, 0)

    def test_natpmp_delete_uses_opcode_4_for_tcp(self):
        reply = struct.pack('!BBHIHH', 0, 132, 0, 100, BT_PORT, BT_PORT)
        sock = _FakeSocket([reply])
        with patch.object(upnp, '_udp_socket', return_value=sock):
            ok, _ = upnp.natpmp_delete('192.168.1.1', BT_PORT, BT_PORT, 'tcp')
        self.assertTrue(ok)
        self.assertEqual(sock.sent[0][0][:2], bytes([0, 4]))

    def test_delete_mapping_tolerates_missing_entry(self):
        fault = ('<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
                 '<s:Fault><detail><UPnPError xmlns="urn:schemas-upnp-org:control-1-0">'
                 '<errorCode>714</errorCode></UPnPError></detail></s:Fault></s:Body></s:Envelope>')
        with patch.object(upnp, '_request', return_value=(500, fault)):
            self.assertFalse(upnp.delete_mapping('http://192.168.1.1/ctl',
                                                 'urn:x:WANIPConnection:2', BT_PORT, 'TCP'))

    def test_forward_prefers_upnp(self):
        with patch.object(upnp, '_try_upnp', return_value=(
                ['TCP', 'UDP'], [], {'gateway': '192.168.1.1', 'external': '1.2.3.4'})):
            state = upnp.forward_ports(BT_PORT, BT_PORT, '192.168.1.8', 'desc')
        self.assertTrue(state['ok'])
        self.assertEqual(state['method'], 'UPnP')
        self.assertEqual(state['externalPort'], BT_PORT)

    def test_forward_falls_back_to_natpmp(self):
        with patch.object(upnp, '_try_upnp',
                          side_effect=upnp.UpnpError('路由器没有响应 UPnP 搜索')), \
                patch.object(upnp, 'gateway', return_value='192.168.1.1'), \
                patch.object(upnp, '_try_natpmp', return_value=(['TCP', 'UDP'], [], 3600)):
            state = upnp.forward_ports(BT_PORT, BT_PORT, '192.168.1.8', 'desc')
        self.assertTrue(state['ok'])
        self.assertEqual(state['method'], 'NAT-PMP')
        self.assertEqual(state['lease'], 3600)

    def test_forward_partial_upnp_keeps_tcp(self):
        with patch.object(upnp, '_try_upnp',
                          return_value=(['TCP'], ['UDP UPnP 错误 501'], {})):
            state = upnp.forward_ports(BT_PORT, BT_PORT, '192.168.1.8', 'desc')
        self.assertFalse(state['ok'])
        self.assertEqual(state['mapped'], ['TCP'])
        self.assertIn('部分成功', state['detail'])

    def test_remove_forward_uses_recorded_control_point(self):
        seen = []

        def fake_request(url, data=None, headers=None, timeout=None):
            seen.append((url, (headers or {}).get('SOAPAction', '')))
            return 200, '<ok/>'

        with patch.object(upnp, '_request', side_effect=fake_request):
            result = upnp.remove_forward(BT_PORT, BT_PORT, method='UPnP',
                                         control='http://192.168.1.1:41795/ctl/IPConn',
                                         service_type='urn:x:WANIPConnection:2')
        self.assertTrue(result['ok'])
        self.assertEqual(len(seen), 2)                              # TCP + UDP
        self.assertTrue(all('DeletePortMapping' in action for _, action in seen))

    def test_remove_forward_rediscovers_stale_control_point(self):
        calls = []

        def fake_request(url, data=None, headers=None, timeout=None):
            calls.append(url)
            if url.startswith('http://192.168.1.1:41795'):
                raise upnp.UpnpError('路由器 192.168.1.1:41795 无响应')
            if url.endswith('rootDesc.xml'):
                return 200, ('<?xml version="1.0"?>'
                             '<root xmlns="urn:schemas-upnp-org:device-1-0"><device><serviceList>'
                             '<service><serviceType>urn:x:WANIPConnection:2</serviceType>'
                             '<controlURL>/ctl/IPConn</controlURL></service>'
                             '</serviceList></device></root>')
            return 200, '<ok/>'

        with patch.object(upnp, '_request', side_effect=fake_request), \
                patch.object(upnp, 'ssdp_location',
                             return_value=('http://192.168.1.1:33299/rootDesc.xml', '192.168.1.1')):
            result = upnp.remove_forward(BT_PORT, BT_PORT, method='UPnP',
                                         control='http://192.168.1.1:41795/ctl/IPConn',
                                         service_type='urn:x:WANIPConnection:2')
        self.assertTrue(result['ok'])
        self.assertIn('http://192.168.1.1:33299/ctl/IPConn', calls)


class _FakeSocket:
    """NAT-PMP 测试用：recvfrom 依次吐出预设应答，没有应答就模拟超时。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.sent = []

    def settimeout(self, value):
        return None

    def sendto(self, data, address):
        self.sent.append((data, address))

    def recvfrom(self, size):
        if not self.replies:
            raise socket.timeout
        return self.replies.pop(0), ('192.168.1.1', 5351)

    def close(self):
        return None


class PortForwardEngineTests(unittest.TestCase):
    """引擎侧的端口映射：尽力而为，绝不阻断启动；停止服务时删掉。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name) / 'root'
        root.mkdir()
        self.engine = Engine(Path(self.tmp.name) / 'data', root)

    def test_ipv4_port_is_the_bt_port(self):
        """映射的必须是 BT 端口（36754），不能是 WebUI 的。"""
        with patch.object(engine.upnp, 'lan_address', return_value='192.168.1.8'), \
                patch.object(engine.upnp, 'forward_ports', return_value={
                    'ok': True, 'method': 'UPnP', 'detail': 'UPnP 映射成功', 'at': 1}) as call:
            self.engine.ensure_port_forward(True)
        self.assertEqual(call.call_args.args[0], BT_PORT)
        self.assertEqual(call.call_args.args[1], BT_PORT)
        self.assertEqual(call.call_args.args[2], '192.168.1.8')
        self.assertNotEqual(BT_PORT, PORT)

    def test_records_failure_detail(self):
        with patch.object(engine.upnp, 'lan_address', return_value='192.168.1.8'), \
                patch.object(engine.upnp, 'forward_ports', return_value={
                    'ok': False, 'method': '', 'detail': 'UPnP：UPnP 错误 501（Action Failed）',
                    'at': 1}):
            state = self.engine.ensure_port_forward(True)
        self.assertFalse(state['ok'])
        self.assertIn('501', self.engine.forward_snapshot()['detail'])

    def test_skips_without_lan_address(self):
        with patch.object(engine.upnp, 'lan_address', return_value=''), \
                patch.object(engine.upnp, 'forward_ports') as call:
            state = self.engine.ensure_port_forward(True)
        self.assertIn('局域网地址', state['detail'])
        self.assertEqual(call.call_count, 0)

    def test_snapshot_carries_forward_state(self):
        data = self.engine.snapshot()
        self.assertIn('forward', data)
        self.assertEqual(data['forward']['externalPort'], BT_PORT)
        self.assertEqual(sorted(data['forward']['protocols']), ['TCP', 'UDP'])

    def test_forward_state_survives_a_new_process(self):
        """停止服务时 systemd 的 ExecStopPost 是另一个进程，得靠这个文件才知道删哪条。"""
        self.engine.forward_state = {'ok': True, 'method': 'UPnP', 'at': 1000,
                                     'control': 'http://192.168.1.1:41795/ctl/IPConn',
                                     'service': 'urn:x:WANIPConnection:2',
                                     'protocols': ['TCP', 'UDP'], 'detail': 'UPnP 映射成功'}
        self.engine.save_forward()
        fresh = Engine(Path(self.engine.data), Path(self.engine.root))
        self.assertTrue(fresh.forward_snapshot()['ok'])
        self.assertEqual(fresh.forward_state['method'], 'UPnP')

    def test_stop_removes_the_mapping(self):
        self.engine.config = {'owner': 'tok'}
        self.engine.forward_state = {'ok': True, 'method': 'UPnP', 'at': 1000,
                                     'control': 'http://192.168.1.1:41795/ctl/IPConn',
                                     'service': 'urn:x:WANIPConnection:2',
                                     'protocols': ['TCP', 'UDP']}
        running = {'Config': {'Labels': {LABEL: 'tok'}}, 'State': {'Running': True}}
        with patch.object(self.engine, 'owned', return_value=running), \
                patch.object(self.engine, '_call') as call, \
                patch.object(engine.upnp, 'remove_forward',
                             return_value={'ok': True, 'detail': '已移除路由器映射'}) as remove, \
                patch('engine.atomic_json'):
            self.engine.stop()
        self.assertEqual(remove.call_count, 1)
        self.assertEqual(remove.call_args.kwargs['control'], 'http://192.168.1.1:41795/ctl/IPConn')
        self.assertEqual(remove.call_args.kwargs['protocols'], ('TCP', 'UDP'))
        self.assertIn('/stop', call.call_args.args[1])
        self.assertTrue(self.engine.forward_snapshot()['removed'])

    def test_remove_is_skipped_when_never_mapped(self):
        with patch.object(engine.upnp, 'remove_forward') as remove:
            result = self.engine.remove_port_forward()
        self.assertFalse(result['ok'])
        self.assertEqual(remove.call_count, 0)

    def test_permanent_upnp_mapping_is_not_renewed(self):
        self.engine.forward_state = {'ok': True, 'method': 'UPnP', 'lease': 0, 'at': 1000}
        self.assertFalse(self.engine.forward_due(now=1000 + 86400))

    def test_natpmp_mapping_renews_before_expiry(self):
        self.engine.forward_state = {'ok': True, 'method': 'NAT-PMP', 'lease': 7200, 'at': 1000}
        self.assertFalse(self.engine.forward_due(now=1000 + 3599))
        self.assertTrue(self.engine.forward_due(now=1000 + 3600))

    def test_failed_mapping_is_retried_later(self):
        self.engine.forward_state = {'ok': False, 'at': 1000, 'detail': '501'}
        self.assertFalse(self.engine.forward_due(now=1000 + 60))
        self.assertTrue(self.engine.forward_due(now=1000 + FORWARD_RETRY_SECONDS))

    def test_removed_state_is_not_re_added(self):
        self.engine.forward_state = {'ok': False, 'removed': True, 'at': 1000}
        self.assertFalse(self.engine.forward_due(now=1000 + 86400))

    def test_keep_forward_alive_renews_when_due(self):
        self.engine.config = {'enabled': True}
        self.engine.forward_state = {'ok': True, 'method': 'NAT-PMP', 'lease': 60,
                                     'at': int(time.time()) - 120}
        with patch.object(self.engine, 'ensure_port_forward') as call:
            self.assertTrue(self.engine.keep_forward_alive())
        self.assertEqual(call.call_count, 1)

    def test_keep_forward_alive_skips_when_service_disabled(self):
        self.engine.config = {'enabled': False}
        self.engine.forward_state = {'ok': True, 'method': 'NAT-PMP', 'lease': 60,
                                     'at': int(time.time()) - 120}
        with patch.object(self.engine, 'ensure_port_forward') as call:
            self.assertFalse(self.engine.keep_forward_alive())
        self.assertEqual(call.call_count, 0)

    def test_start_tries_the_router_mapping(self):
        self.engine.config = {'owner': 'tok'}
        with patch.object(self.engine, 'check_directory'), \
                patch.object(self.engine, 'owned', return_value=None), \
                patch.object(self.engine, 'pull'), \
                patch.object(self.engine, '_call'), \
                patch('engine.container_config', return_value={}), \
                patch.object(self.engine, 'ensure_port_forward') as forward, \
                patch('engine.qb_request', return_value=(200, {}, {})), \
                patch('engine.atomic_json'):
            self.engine.start()
        self.assertEqual(forward.call_count, 1)


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

    def test_forward_endpoint_returns_router_state(self):
        with patch.object(self.engine, 'ensure_port_forward', return_value={
                'ok': False, 'method': '', 'detail': 'UPnP：UPnP 错误 501（Action Failed）',
                'at': 1, 'externalPort': BT_PORT}):
            code, body = self.request('POST', '/api/forward', {}, self.auth())
        self.assertEqual(code, 200)
        data = json.loads(body)
        self.assertTrue(data['ok'])
        self.assertFalse(data['forward']['ok'])
        self.assertIn('501', data['forward']['detail'])

    def test_forward_endpoint_needs_csrf(self):
        self.assertEqual(
            self.request('POST', '/api/forward', {}, {'X-QB-Session': self.token})[0], 403)

    def test_forward_keeper_survives_errors(self):
        """定时器里的一时失败不能把循环打断（否则 NAT-PMP 就没人续期了）。"""
        from server import forward_keeper

        ticks = []

        def fake_sleep(_seconds):
            ticks.append(1)
            if len(ticks) > 2:
                raise KeyboardInterrupt

        def boom():
            raise RuntimeError('路由器抽风')

        stub = types.SimpleNamespace(keep_forward_alive=boom)
        with self.assertRaises(KeyboardInterrupt):
            forward_keeper(stub, sleep=fake_sleep)
        self.assertGreaterEqual(len(ticks), 3)

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

    def test_auto_login_uses_saved_credential(self):
        """容器在跑但没有会话时，用安装时记下的密码自动补登，用户不必再输密码。"""
        self.server.engine.config = {'relative': 'MiShare', 'download': '', 'device': 0, 'inode': 0}
        self.server.engine.save_credential('saved-pass')
        header = 'QBT_SID_18123=' + 'd' * 32 + '; HttpOnly; path=/'
        with patch.object(self.server.engine, 'owned', return_value={'State': {'Running': True}}), \
                patch('server.qb_request', return_value=(204, b'', header)) as login:
            code, body = self.request('GET', '/api/status', headers=self.auth())
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)['loggedIn'])
        self.assertEqual(login.call_args.args[0], 'auth/login')
        self.assertEqual(login.call_args.args[1]['password'], 'saved-pass')
        self.assertEqual(self.server.qb_session(), 'QBT_SID_18123=' + 'd' * 32)

    def test_auto_login_backs_off_after_failure(self):
        """自动登录失败要冷却：下载列表是 3 秒轮询，不冷却会一直重试，
        反而把 qB 的失败计数顶上去触发临时封禁。"""
        self.server.engine.config = {'relative': 'MiShare', 'download': '', 'device': 0, 'inode': 0}
        self.server.engine.save_credential('wrong')
        with patch.object(self.server.engine, 'owned', return_value={'State': {'Running': True}}), \
                patch('server.qb_request', return_value=(401, b'Unauthorized', '')) as login:
            self.request('GET', '/api/status', headers=self.auth())
            self.request('GET', '/api/status', headers=self.auth())
        self.assertEqual(login.call_count, 1)

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

    def test_auto_login_stops_after_repeated_failures(self):
        """连续失败到上限就停手。

        回归用例：qB 默认失败 5 次按源 IP 封禁一小时，而所有容器的来源 IP 都是
        docker0 网关 —— 无节制的自动登录会把插件页和局域网 WebUI 一起挡在门外。
        """
        self.server.engine.config = {'relative': 'MiShare', 'download': '', 'device': 0, 'inode': 0}
        self.server.engine.save_credential('wrong')
        with patch.object(self.server.engine, 'owned', return_value={'State': {'Running': True}}), \
                patch('server.qb_request', return_value=(401, b'Unauthorized', '')) as login:
            for _ in range(10):
                self.server.auto_login_at = 0        # 跳过冷却，单看次数上限
                self.server.ensure_qb_session()
        self.assertEqual(login.call_count, AUTO_LOGIN_MAX_FAILURES)

    def test_login_after_ban_reports_the_real_reason(self):
        """被封禁时密码其实是对的，必须如实说明，否则用户会反复试、给封禁续期。"""
        banned = b'Your IP address has been banned after too many failed login attempts'
        with patch('server.qb_request', return_value=(403, banned, '')):
            code, body = self.request('POST', '/api/login', {'password': 'whatever'}, self.auth())
        self.assertEqual(code, 400)
        self.assertIn('封禁', json.loads(body)['error'])

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

    def test_page_shows_router_mapping_row(self):
        html = (self.web / 'index.html').read_text(encoding='utf-8')
        for name in ('forwardState', 'forwardPort'):
            with self.subTest(name=name):
                self.assertIn('id="%s"' % name, html)
        for name in ('app.js', 'app.bundle.js'):
            with self.subTest(name=name):
                script = (self.web / name).read_text(encoding='utf-8')
                self.assertIn('function renderForward(current)', script)
                self.assertIn("$('#forwardState')", script)
                self.assertIn("$('#forwardPort')", script)
                self.assertIn('路由器映射已移除', script)

    def test_ui_calls_the_api_with_relative_paths(self):
        """插件页挂在 /plugin/<用户>/qbittorrent/ 下，接口必须用相对路径。

        回归用例：写成 fetch('/api/status') 会打到站点根，nginx 没有对应 location，
        返回 404，插件页只会显示「正在连接 / 请求失败」。
        """
        script = (self.web / 'app.js').read_text(encoding='utf-8')
        self.assertNotIn("'/api", script)
        self.assertNotIn('"/api', script)
        # Windows 客户端 location 可能带 /D:/ 盘符，必须用 script 基址拼绝对 URL
        self.assertIn("function pluginAssetBase()", script)
        self.assertIn("fetch(assetUrl('api/' + route)", script)

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
