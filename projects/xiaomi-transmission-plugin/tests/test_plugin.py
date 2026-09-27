import base64
import http.client
import io
import json
import os
import re
import socket
import struct
import tempfile
import threading
import time
import types
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from engine import (
    Engine, Error, IMAGE, NAME, LABEL, PORT, BT_PORT, VERSION, IMAGE_VERSION,
    LEGACY_SETTINGS_FOLDER, confined, container_config, installed_version,
    settings_document, settings_path, WEBUI_SUBDIR, webui_password, webui_username,
    FORWARD_RETRY_SECONDS,
)
import engine  # noqa: E402  （按模块打桩，例如 engine.PROC_NET）
import upnp  # noqa: E402
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
        # 单测绝不碰真实网络：UPnP/NAT-PMP 默认打桩，专门的用例再自己覆盖
        self.addCleanup(patch.object(engine.upnp, 'lan_address',
                                     return_value='192.168.1.8').stop)
        self.addCleanup(patch.object(engine.upnp, 'forward_ports', return_value={
            'ok': False, 'method': '', 'detail': '测试环境跳过', 'at': 0}).stop)

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
        conf = json.loads((self.root / 'Config/settings.json').read_text(encoding='utf-8'))
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

    def test_ensure_settings_keeps_user_values(self):
        """用户改过的键在插件启动时不再被默认值覆盖（旧实现会把整个文件重写）。"""
        self.engine.config = self._identity_config()
        path = self.root / 'Config/settings.json'
        path.write_text(json.dumps({
            'cache-size-mb': 256, 'dht-enabled': False, 'peer-limit-global': 1000,
            'speed-limit-up': 10240, 'ratio-limit': 20,
        }), encoding='utf-8')
        with patch('engine.os.chown', create=True):
            changed = self.engine.ensure_settings()
        saved = json.loads(path.read_text(encoding='utf-8'))
        self.assertTrue(changed)
        self.assertEqual(saved['cache-size-mb'], 256)
        self.assertFalse(saved['dht-enabled'])
        self.assertEqual(saved['peer-limit-global'], 1000)
        self.assertEqual(saved['speed-limit-up'], 10240)
        self.assertEqual(saved['ratio-limit'], 20)
        # 缺失的键补上，容器需要的键仍然正确
        self.assertEqual(saved['watch-dir'], '/watch')
        self.assertEqual(saved['rpc-bind-address'], '0.0.0.0')
        # 第二次启动没有可补的键，文件不再被动过
        with patch('engine.os.chown', create=True):
            self.assertFalse(self.engine.ensure_settings())
        self.assertEqual(saved, json.loads(path.read_text(encoding='utf-8')))

    def test_ensure_settings_repairs_container_bound_keys(self):
        """rpc-enabled / rpc-port / rpc-bind-address 绑死在容器映射上，必须纠正。"""
        self.engine.config = self._identity_config()
        path = self.root / 'Config/settings.json'
        path.write_text(json.dumps({
            'rpc-enabled': False, 'rpc-port': 9999, 'rpc-bind-address': '[::]',
            'cache-size-mb': 256,
        }), encoding='utf-8')
        with patch('engine.os.chown', create=True):
            self.assertTrue(self.engine.ensure_settings())
        saved = json.loads(path.read_text(encoding='utf-8'))
        self.assertTrue(saved['rpc-enabled'])
        self.assertEqual(saved['rpc-port'], PORT)
        self.assertEqual(saved['rpc-bind-address'], '0.0.0.0')
        self.assertEqual(saved['cache-size-mb'], 256)

    def test_ensure_settings_moves_unreadable_file_aside(self):
        """文件存在但解析不出 JSON 时先留一份，再写默认值，不直接抹掉用户内容。"""
        self.engine.config = self._identity_config()
        path = self.root / 'Config/settings.json'
        path.write_text('{ not json', encoding='utf-8')
        with patch('engine.os.chown', create=True):
            self.assertTrue(self.engine.ensure_settings())
        self.assertTrue((self.root / 'Config/settings.json.invalid').is_file())
        saved = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(saved['download-dir'], '/downloads')

    def test_legacy_native_settings_adopted_once(self):
        """旧版 transmission-daemon/settings.json 里的可调项搬进生效文件，且只搬一次。"""
        self.engine.config = self._identity_config()
        legacy_dir = self.root / 'Config' / LEGACY_SETTINGS_FOLDER
        legacy_dir.mkdir()
        legacy = legacy_dir / 'settings.json'
        legacy.write_text(json.dumps({
            'cache-size-mb': 256, 'dht-enabled': False, 'peer-limit-global': 1000,
            # 下面这些和容器挂载/端口/账号绑定，不能搬
            'download-dir': '/tmp/mnt/sda1/pt', 'watch-dir': '/tmp/watch',
            'incomplete-dir': '/root/Downloads',
            'rpc-username': 'someone', 'rpc-password': '{old}', 'peer-port': 50000,
            'umask': '000',
        }), encoding='utf-8')
        path = self.root / 'Config/settings.json'
        path.write_text(json.dumps({'download-dir': '/downloads', 'watch-dir': '/watch'}),
                        encoding='utf-8')
        with patch('engine.os.chown', create=True):
            self.assertTrue(self.engine.adopt_legacy_settings())
        saved = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(saved['cache-size-mb'], 256)
        self.assertFalse(saved['dht-enabled'])
        self.assertEqual(saved['peer-limit-global'], 1000)
        self.assertEqual(saved['download-dir'], '/downloads')
        self.assertEqual(saved['watch-dir'], '/watch')
        self.assertNotIn('incomplete-dir', saved)
        self.assertNotIn('rpc-username', saved)
        self.assertNotIn('rpc-password', saved)
        self.assertNotEqual(saved.get('peer-port'), 50000)
        self.assertNotEqual(saved.get('umask'), '000')
        # 旧文件改名留在原处，之后不再重复搬
        self.assertFalse(legacy.is_file())
        self.assertTrue((legacy_dir / 'settings.json.legacy').is_file())
        with patch('engine.os.chown', create=True):
            self.assertFalse(self.engine.adopt_legacy_settings())
        self.assertTrue(json.loads(self.engine.cfgfile.read_text(encoding='utf-8'))['settingsAdopted'])

    def test_start_keeps_user_edited_settings(self):
        """回归：用户调过的配置在 start()（含重建容器）之后必须原样保留。"""
        self.engine.config = self._identity_config()
        self.engine.credentialfile.write_text(json.dumps({'password': 'Example123!'}), encoding='utf-8')
        path = self.root / 'Config/settings.json'
        path.write_text(json.dumps({
            'cache-size-mb': 512, 'dht-enabled': False, 'pex-enabled': False,
            'peer-limit-global': 900, 'download-dir': '/downloads', 'watch-dir': '/watch',
        }), encoding='utf-8')

        def fake_api(method, route, body=None, timeout=30):
            if route == '/containers/' + NAME + '/json':
                return 404, b'{"message":"No such container"}'
            if route.startswith('/images/create'):
                return 200, b'{}\n'
            return 201, b'{}'

        with patch('engine.docker_api', side_effect=fake_api), \
                patch('engine.os.chown', create=True), \
                patch('engine.fetch_bytes', return_value=self._webui_archive()), \
                patch('engine.tr_rpc_probe', return_value=True):
            self.engine.start()

        saved = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(saved['cache-size-mb'], 512)
        self.assertFalse(saved['dht-enabled'])
        self.assertFalse(saved['pex-enabled'])
        self.assertEqual(saved['peer-limit-global'], 900)
        self.assertEqual(saved['rpc-bind-address'], '0.0.0.0')

    def test_settings_file_path_is_config_root(self):
        """daemon 以 -g /config 启动，生效的文件只能是 <配置目录>/settings.json。"""
        self.assertEqual(settings_path(self.root / 'Config'), self.root / 'Config/settings.json')

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
                patch('engine.os.chown', create=True), \
                patch('engine.fetch_bytes', return_value=self._webui_archive()), \
                patch('engine.tr_rpc_probe', return_value=True):
            self.engine.start()
        create = next(c for c in calls if c[1].startswith('/containers/create'))
        mounts = create[2]['HostConfig']['Mounts']
        self.assertEqual([m['Target'] for m in mounts], ['/config', '/downloads', '/watch'])
        self.assertIn('USER=admin', create[2]['Env'])
        self.assertIn('PASS=Example123!', create[2]['Env'])
        # LSIO 以 -g /config 启动，配置必须落在 <配置目录>/settings.json；
        # 写到 transmission-daemon/ 子目录不会被读取，daemon 会用镜像默认的 [::] 绑定 9091，
        # 无 IPv6 的容器里 Web 永远起不来。
        conf = json.loads((self.root / 'Config/settings.json').read_text(encoding='utf-8'))
        self.assertEqual(conf['rpc-bind-address'], '0.0.0.0')
        self.assertEqual(conf['download-dir'], '/downloads')
        # 控制台固定指向 <配置目录>/webui
        self.assertIn('TRANSMISSION_WEB_HOME=/config/webui', create[2]['Env'])

    def _identity_config(self):
        def identity(relative):
            folder = self.root / relative
            s = folder.stat()
            return str(folder), relative, s.st_dev, s.st_ino

        download, config, watch = identity('Downloads'), identity('Config'), identity('Watch')
        return {
            'owner': 'owner-token', 'uid': 1000, 'gid': 1000, 'username': 'admin',
            'download': download[0], 'download_relative': download[1],
            'download_device': download[2], 'download_inode': download[3],
            'config': config[0], 'config_relative': config[1],
            'config_device': config[2], 'config_inode': config[3],
            'watch': watch[0], 'watch_relative': watch[1],
            'watch_device': watch[2], 'watch_inode': watch[3],
            'enabled': True,
        }

    def _webui_archive(self, index_html='<html></html>'):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, 'w') as archive:
            archive.writestr(WEBUI_SUBDIR + '/index.html', index_html)
            archive.writestr(WEBUI_SUBDIR + '/tr-web-control/config.js', '/*c*/')
            archive.writestr('transmission-web-control-1.6.1-update1/README.md', 'readme')
        return buffer.getvalue()

    def test_start_installs_default_webui_under_config(self):
        self.engine.config = self._identity_config()
        self.engine.credentialfile.write_text(json.dumps({'password': 'Example123!'}), encoding='utf-8')
        calls = []

        def fake_api(method, path, body=None, timeout=30):
            calls.append((method, path, body))
            if path == '/containers/' + NAME + '/json':
                return 404, b'{"message":"No such container"}'
            if path.startswith('/images/create'):
                return 200, b'{}\n'
            return 201, b'{}'

        with patch('engine.fetch_bytes', return_value=self._webui_archive()), \
                patch('engine.os.chown', create=True), \
                patch('engine.docker_api', side_effect=fake_api), \
                patch('engine.tr_rpc_probe', return_value=True):
            self.engine.start()

        # 只保留包内 src/ 的内容，index.html 落在 webui/ 根
        self.assertTrue((self.root / 'Config/webui/index.html').is_file())
        self.assertTrue((self.root / 'Config/webui/tr-web-control/config.js').is_file())
        self.assertFalse((self.root / 'Config/webui/README.md').exists())
        create = next(c for c in calls if c[1].startswith('/containers/create'))
        self.assertIn('TRANSMISSION_WEB_HOME=/config/webui', create[2]['Env'])

    def test_existing_webui_is_not_overwritten(self):
        self.engine.config = self._identity_config()
        self.engine.credentialfile.write_text(json.dumps({'password': 'Example123!'}), encoding='utf-8')
        target = self.root / 'Config/webui'
        target.mkdir(parents=True)
        (target / 'index.html').write_text('<html>mine</html>', encoding='utf-8')

        with patch('engine.fetch_bytes') as fetch, \
                patch('engine.os.chown', create=True), \
                patch('engine.docker_api', side_effect=lambda method, path, body=None, timeout=30:
                      (404, b'{"message":"No such container"}') if path.endswith('/json') else (201, b'{}')), \
                patch('engine.tr_rpc_probe', return_value=True):
            self.engine.start()

        fetch.assert_not_called()
        self.assertEqual((target / 'index.html').read_text(encoding='utf-8'), '<html>mine</html>')

    def test_container_with_old_webui_path_is_rebuilt(self):
        self.engine.config = self._identity_config()
        self.engine.credentialfile.write_text(json.dumps({'password': 'Example123!'}), encoding='utf-8')
        calls = []
        exists = {'value': True}

        def fake_api(method, path, body=None, timeout=30):
            calls.append((method, path, body))
            if path == '/containers/' + NAME + '/json':
                if not exists['value']:
                    return 404, b'{"message":"No such container"}'
                return 200, json.dumps({
                    'State': {'Running': True},
                    'Config': {
                        'Labels': {LABEL: 'owner-token'},
                        'Env': ['TRANSMISSION_WEB_HOME=/config/webui/transmissionic/web'],
                    },
                }).encode('utf-8')
            if method == 'DELETE':
                exists['value'] = False
                return 204, b''
            if path.startswith('/images/create'):
                return 200, b'{}\n'
            return 201, b'{}'

        with patch('engine.fetch_bytes', return_value=self._webui_archive()), \
                patch('engine.os.chown', create=True), \
                patch('engine.docker_api', side_effect=fake_api), \
                patch('engine.tr_rpc_probe', return_value=True):
            self.engine.start()

        self.assertTrue(any(c[0] == 'DELETE' for c in calls))
        create = next(c for c in calls if c[1].startswith('/containers/create'))
        self.assertIn('TRANSMISSION_WEB_HOME=/config/webui', create[2]['Env'])

    def test_port_test_records_result(self):
        self.engine.config = self._identity_config()
        self.engine.credentialfile.write_text(json.dumps({'password': 'Example123!'}), encoding='utf-8')
        body = json.dumps({'result': 'success', 'arguments': {'port-is-open': True}}).encode('utf-8')
        with patch('engine.tr_rpc', return_value=(200, body, '')), \
                patch('engine.docker_api', return_value=(404, b'{}')):
            state = self.engine.test_port()
            snapshot = self.engine.snapshot()
        self.assertTrue(state['open'])
        self.assertTrue(state['testedAt'])
        self.assertEqual(snapshot['port']['peerPort'], BT_PORT)
        self.assertTrue(snapshot['port']['open'])

    def test_port_test_surfaces_daemon_failure(self):
        self.engine.config = self._identity_config()
        self.engine.credentialfile.write_text(json.dumps({'password': 'Example123!'}), encoding='utf-8')
        body = json.dumps({'result': 'error', 'arguments': {}}).encode('utf-8')
        with patch('engine.tr_rpc', return_value=(200, body, '')):
            with self.assertRaises(Error):
                self.engine.test_port()


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

    def request_full(self, method, route, data=None, headers=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.server.server_port)
        try:
            conn.request(method, route, json.dumps(data) if data is not None else None, headers or {})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def prepare_console(self):
        """造一个已初始化的插件：配置目录下有 webui/index.html，并保存了 WebUI 口令。"""
        config = Path(self.tmp.name) / 'config'
        webui = config / 'webui'
        (webui / 'tr-web-control').mkdir(parents=True)
        (webui / 'index.html').write_text('<html>console</html>', encoding='utf-8')
        (webui / 'tr-web-control' / 'app.js').write_text('// twc', encoding='utf-8')
        self.engine.config = {'owner': 'owner-token', 'config': str(config), 'username': 'admin'}
        self.engine.credentialfile.write_text(
            json.dumps({'username': 'admin', 'password': 'Example123'}), encoding='utf-8')
        return webui

    def console_cookie(self):
        """走一次入口换 Cookie，模拟插件页点「打开控制台」。"""
        status, headers, _ = self.request_full('GET', '/console?t=' + self.token)
        self.assertEqual(status, 302)
        self.assertEqual(headers.get('Location'), 'console/')
        return {'Cookie': headers['Set-Cookie'].split(';')[0]}

    def test_unauthenticated_reads_denied(self):
        self.assertEqual(self.request('GET', '/api/status')[0], 401)

    def test_csrf_required(self):
        self.assertEqual(self.request('POST', '/api/service/stop', {}, {'X-TR-Session': self.token})[0], 403)

    def test_status_carries_the_glance_stats(self):
        """首页小组件做不成，这几个数字就放在插件页顶部；取不到时给 null 而不是报错。"""
        code, body = self.request('GET', '/api/status', headers=self.auth())
        self.assertEqual(code, 200)
        data = json.loads(body)
        self.assertIn('transfer', data)
        for key in ('dlspeed', 'upspeed', 'downloaded', 'uploaded', 'seeding', 'downloading'):
            with self.subTest(key=key):
                self.assertIn(key, data['transfer'])
        # 预览模式没有 Transmission 账号，取不到就全为 null
        self.assertIsNone(data['transfer']['dlspeed'])
        self.assertIsNone(data['transfer']['seeding'])

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
        with patch('server.time.sleep', side_effect=fake_sleep):
            with self.assertRaises(KeyboardInterrupt):
                forward_keeper(stub)
        self.assertGreaterEqual(len(ticks), 3)

    def test_forward_endpoint_needs_csrf(self):
        self.assertEqual(
            self.request('POST', '/api/forward', {}, {'X-TR-Session': self.token})[0], 403)

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

    def test_torrents_endpoint_calls_handler_method(self):
        """/api/torrents 必须调 Handler.engine_snapshot_torrents，不能挂到 Server 上。"""
        with patch('server.Handler.engine_snapshot_torrents',
                   return_value={'items': [], 'transfer': {}}) as mocked:
            code, body = self.request('GET', '/api/torrents', headers=self.auth())
        self.assertEqual(code, 200)
        self.assertTrue(mocked.called)

    def test_status_reports_lan_address(self):
        with patch('server.lan_ip', return_value='192.168.1.15'):
            code, body = self.request('GET', '/api/status', headers=self.auth())
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)['address'], 'http://192.168.1.15:' + str(PORT))

    def test_console_entry_issues_cookie_and_redirects(self):
        """入口收插件令牌换 Cookie，并跳到带斜杠的地址（twc 的相对路径要按目录算）。"""
        self.server.dev = False
        self.prepare_console()
        self.assertIn('tr_console=', self.console_cookie()['Cookie'])

    def test_console_entry_accepts_trailing_slash(self):
        """插件页生成的是 console/?t=...，入口必须接受带斜杠的形式，否则第一次点开会 403。"""
        self.server.dev = False
        self.prepare_console()
        status, headers, _ = self.request_full('GET', '/console/?t=' + self.token)
        self.assertEqual(status, 302)
        self.assertEqual(headers.get('Location'), './')
        self.assertIn('tr_console=', headers['Set-Cookie'])

    def test_console_rejects_requests_without_credential(self):
        """插件会代填 WebUI 账号，所以没有证书/令牌时必须挡住，否则同网段设备可直接控制。"""
        self.server.dev = False
        self.prepare_console()
        self.assertEqual(self.request('GET', '/console/')[0], 403)
        self.assertEqual(self.request('GET', '/console?t=bad.token')[0], 403)
        self.assertEqual(self.request('POST', '/rpc', {'method': 'session-stats'})[0], 403)

    def test_console_rpc_accepts_referer_token(self):
        """控制台 XHR 带不上自定义头、也可能拿不到 Cookie：Referer 上的令牌要能授权。

        控制台页就是从 /console/index.html?t=<令牌> 打开的，页面内 RPC 的同源 Referer
        带着同一个令牌。没有 Cookie 的 App 内置 WebView 因此也能取到数据。
        """
        self.server.dev = False
        self.prepare_console()
        status, _, _ = self.request_full(
            'POST', '/rpc',
            {'method': 'session-stats'},
            {'Referer': 'https://nas.example/plugin/u1/transmission/console/index.html?t='
                        + self.token},
        )
        self.assertNotEqual(403, status)

    def test_console_rpc_rejects_bad_referer(self):
        self.server.dev = False
        self.prepare_console()
        status, _, _ = self.request_full(
            'POST', '/rpc',
            {'method': 'session-stats'},
            {'Referer': 'https://nas.example/plugin/u1/transmission/console/index.html?t=bad.token'},
        )
        self.assertEqual(403, status)

    def test_console_serves_files_from_config_webui(self):
        self.prepare_console()
        cookie = self.console_cookie()
        status, _, body = self.request_full('GET', '/console/', headers=cookie)
        self.assertEqual(status, 200)
        self.assertIn(b'<html>console', body)
        self.assertIn(b'rpcpath', body)
        status, _, body = self.request_full('GET', '/console/tr-web-control/app.js', headers=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(body, b'// twc')

    def test_console_refuses_path_traversal(self):
        self.prepare_console()
        cookie = self.console_cookie()
        self.assertEqual(self.request_full('GET', '/console/../engine.py', headers=cookie)[0], 403)

    def test_console_reports_missing_files(self):
        self.engine.config = {'owner': 'owner-token', 'config': str(Path(self.tmp.name) / 'nope')}
        cookie = self.console_cookie()
        status, _, body = self.request_full('GET', '/console/', headers=cookie)
        self.assertEqual(status, 409)
        self.assertIn('webui', json.loads(body)['error'])

    def test_console_rpc_forwards_with_credentials(self):
        """RPC 转发要带上插件保存的 WebUI 账号，并透传 session id 与响应。"""
        self.prepare_console()
        cookie = self.console_cookie()
        captured = {}

        class FakeResponse:
            status = 200
            def read(self):
                return b'{"result":"success"}'
            def getheader(self, name):
                if name.lower() == 'x-transmission-session-id':
                    return 'session-42'
                return 'application/json'

        class FakeConnection:
            def __init__(self, host, port, timeout=None):
                captured['host'], captured['port'] = host, port
            def request(self, method, path, body=None, headers=None):
                captured['method'], captured['path'], captured['headers'] = method, path, headers or {}
            def getresponse(self):
                return FakeResponse()
            def close(self):
                pass

        with patch('server.http', types.SimpleNamespace(
                client=types.SimpleNamespace(HTTPConnection=FakeConnection,
                                             HTTPException=http.client.HTTPException))):
            status, headers, body = self.request_full(
                'POST', '/rpc', {'method': 'session-stats'},
                {**cookie, 'X-Transmission-Session-Id': 'incoming', 'Content-Type': 'application/json'})
        self.assertEqual(status, 200)
        self.assertEqual(headers.get('X-Transmission-Session-Id'), 'session-42')
        self.assertEqual(body, b'{"result":"success"}')
        self.assertEqual(captured['host'], '127.0.0.1')
        self.assertEqual(captured['port'], PORT)
        self.assertEqual(captured['path'], '/transmission/rpc')
        self.assertEqual(captured['headers']['X-Transmission-Session-Id'], 'incoming')
        self.assertEqual(captured['headers']['Authorization'],
                         'Basic ' + base64.b64encode(b'admin:Example123').decode())


class PortPublishTests(unittest.TestCase):
    """docker-proxy 是用户态进程，掉线后宿主上就没人监听，而 Docker 的
    inspect 依然说端口已发布——这段自检直接读 /proc/net，丢了就重启容器补回来。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'root'
        self.root.mkdir()
        for name in ('Downloads', 'Config', 'Watch'):
            (self.root / name).mkdir()
        self.engine = Engine(Path(self.tmp.name) / 'private', self.root)
        self.net = Path(self.tmp.name) / 'net'
        self.net.mkdir()
        patcher = patch.object(engine, 'PROC_NET', self.net)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_net(self, tcp_listen=(), tcp_other=(), udp=()):
        header = ('  sl  local_address rem_address   st tx_queue rx_queue tr tm->when'
                  ' retrnsmt   uid  timeout inode\n')

        def body(entries):
            rows = []
            for index, (port, state) in enumerate(entries):
                rows.append('%4d: 00000000:%04X 00000000:0000 %s 00000000:00000000 '
                            '00:00000000 00000000     0        0 %d 1 0000000000000000 100 0 0 10 0'
                            % (index, port, state, 1000 + index))
            return header + '\n'.join(rows) + ('\n' if rows else '')

        (self.net / 'tcp').write_text(
            body([(p, '0A') for p in tcp_listen] + [(p, '01') for p in tcp_other]), encoding='utf-8')
        (self.net / 'tcp6').write_text(header, encoding='utf-8')
        (self.net / 'udp').write_text(body([(p, '07') for p in udp]), encoding='utf-8')
        (self.net / 'udp6').write_text(header, encoding='utf-8')

    def test_tcp_only_counts_listening_sockets(self):
        self.write_net(tcp_listen=[9091], tcp_other=[51413], udp=[51413])
        ports = engine.listening_ports()
        self.assertIn(('tcp', 9091), ports)
        self.assertIn(('udp', 51413), ports)
        self.assertNotIn(('tcp', 51413), ports)          # 已建立连接的 51413 不算发布

    def test_missing_port_is_reported(self):
        self.write_net(tcp_listen=[9091], udp=[51413])
        self.assertEqual(self.engine.missing_published_ports(), ['51413/tcp'])

    def test_nothing_missing_when_all_published(self):
        self.write_net(tcp_listen=[9091, 51413], udp=[51413])
        self.assertEqual(self.engine.missing_published_ports(), [])

    def test_ensure_restarts_container_and_recovers(self):
        self.write_net(tcp_listen=[9091], udp=[51413])
        calls = []

        def fake_api(method, path, body=None, timeout=30):
            calls.append((method, path))
            if path.startswith('/containers/' + NAME + '/restart'):
                self.write_net(tcp_listen=[9091, 51413], udp=[51413])
            return 204, b'{}'

        with patch('engine.docker_api', side_effect=fake_api):
            missing = self.engine.ensure_published_ports()
        self.assertEqual(missing, [])
        self.assertTrue(self.engine.ports_state['repaired'])
        self.assertEqual(calls, [('POST', '/containers/' + NAME + '/restart?t=15')])

    def test_ensure_reports_when_restart_does_not_help(self):
        self.write_net(tcp_listen=[9091], udp=[51413])
        with patch('engine.docker_api', return_value=(204, b'{}')) as api, \
                patch.object(self.engine, 'wait_published_ports', return_value=['51413/tcp']):
            missing = self.engine.ensure_published_ports()
        self.assertEqual(missing, ['51413/tcp'])
        self.assertTrue(self.engine.ports_state['repaired'])
        self.assertEqual(api.call_args.args[1], '/containers/' + NAME + '/restart?t=15')

    def test_ensure_does_nothing_when_ports_are_published(self):
        self.write_net(tcp_listen=[9091, 51413], udp=[51413])
        with patch('engine.docker_api') as api:
            missing = self.engine.ensure_published_ports()
        self.assertEqual(missing, [])
        self.assertFalse(self.engine.ports_state['repaired'])
        self.assertEqual(api.call_count, 0)

    def test_dev_mode_never_restarts(self):
        self.engine.dev = True
        self.write_net(tcp_listen=[9091], udp=[51413])
        with patch('engine.docker_api') as api:
            self.engine.ensure_published_ports()
        self.assertEqual(api.call_count, 0)

    def test_snapshot_exposes_port_state(self):
        self.write_net(tcp_listen=[9091], udp=[51413])
        self.engine.config = {'owner': 'tok', 'config': str(self.root / 'Config')}
        running = {'Config': {'Labels': {LABEL: 'tok'}}, 'State': {'Running': True}}
        with patch.object(self.engine, 'owned', return_value=running), \
                patch('engine.tr_rpc_probe', return_value=True):
            data = self.engine.snapshot()
        self.assertEqual(data['ports']['missing'], ['51413/tcp'])
        self.assertEqual(sorted(data['ports']['published']),
                         ['51413/tcp', '51413/udp', '9091/tcp'])

    def test_snapshot_ignores_missing_ports_when_container_is_stopped(self):
        self.write_net(tcp_listen=[9091], udp=[51413])
        self.engine.config = {'owner': 'tok', 'config': str(self.root / 'Config')}
        stopped = {'Config': {'Labels': {LABEL: 'tok'}}, 'State': {'Running': False}}
        with patch.object(self.engine, 'owned', return_value=stopped):
            data = self.engine.snapshot()
        self.assertFalse(data['running'])
        self.assertEqual(data['ports']['missing'], [])


class WidgetSourceTests(unittest.TestCase):
    """首页小组件：客户端认 registry 里的 widget 声明，但正文不从插件取。

    实测（NAS）：声明写进去后 app 的「小组件 → 应用」里会出现 Transmission 下载，
    点开能看到卡片、客户端也来取了卡片图标（/plugin/<user>/<uikey>/assets/...），
    但它**从不加载 widget.url**——url 给 /widget.html 时没有对应请求，卡片报连接超时，
    url 为空时报的也是连接超时，说明正文由客户端/云端按 widget 类型提供。
    所以这里只保证「插件页上的数字」是准的，不再往首页塞卡片。
    """

    def test_transfer_reports_session_totals(self):
        source = (Path(__file__).resolve().parents[1] / 'server.py').read_text(encoding='utf-8')
        self.assertIn("'uploaded': int(cur.get('uploadedBytes')", source)
        self.assertIn("'secondsActive': int(cur.get('secondsActive')", source)

    def test_stats_summary_counts_torrent_states(self):
        source = (Path(__file__).resolve().parents[1] / 'server.py').read_text(encoding='utf-8')
        # 3/4 下载中与排队下载，5/6 做种中与排队做种
        self.assertIn('status in (5, 6)', source)
        self.assertIn('status in (3, 4)', source)

    def test_no_public_widget_page(self):
        """实验用的统计页已经删掉，不留没有入口的公开路由。"""
        root = Path(__file__).resolve().parents[1]
        self.assertFalse((root / 'web' / 'widget.html').is_file())
        self.assertNotIn("'/widget.html'", (root / 'server.py').read_text(encoding='utf-8'))


class UpnpTests(unittest.TestCase):
    """UPnP/NAT-PMP 客户端：只用标准库，全部离线打桩。"""

    def test_gateway_picks_lowest_metric_default_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            route = Path(tmp) / 'route'
            route.write_text(
                'Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n'
                'docker0\t000011AC\t00000000\t0001\t0\t0\t0\t0000FFFF\t0\t0\t0\n'
                'enu1u3\t00000000\t0101A8C0\t0003\t0\t0\t1004\t00000000\t0\t0\t0\n'
                'eth9\t00000000\t0101A8C1\t0003\t0\t0\t20\t00000000\t0\t0\t0\n',
                encoding='utf-8')
            with patch.object(upnp, 'PROC_ROUTE', str(route)):
                self.assertEqual(upnp.gateway(), '193.168.1.1')

    def test_gateway_without_default_route(self):
        with tempfile.TemporaryDirectory() as tmp:
            route = Path(tmp) / 'route'
            route.write_text('Iface\tDestination\n', encoding='utf-8')
            with patch.object(upnp, 'PROC_ROUTE', str(route)):
                self.assertEqual(upnp.gateway(), '')

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

    def test_control_point_without_wan_service(self):
        description = ('<?xml version="1.0"?><root xmlns="urn:schemas-upnp-org:device-1-0">'
                       '<device/></root>')
        with patch.object(upnp, '_request', return_value=(200, description)):
            with self.assertRaises(upnp.UpnpError):
                upnp.control_point('http://192.168.1.1/rootDesc.xml')

    def test_control_point_reports_bad_description(self):
        with patch.object(upnp, '_request', return_value=(200, 'not xml')):
            with self.assertRaises(upnp.UpnpError) as ctx:
                upnp.control_point('http://192.168.1.1/rootDesc.xml')
        self.assertIn('解析失败', str(ctx.exception))

    def test_add_mapping_reports_router_fault(self):
        fault = ('<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
                 '<s:Fault><detail><UPnPError xmlns="urn:schemas-upnp-org:control-1-0">'
                 '<errorCode>501</errorCode><errorDescription>Action Failed</errorDescription>'
                 '</UPnPError></detail></s:Fault></s:Body></s:Envelope>')
        with patch.object(upnp, '_request', return_value=(500, fault)):
            with self.assertRaises(upnp.UpnpError) as ctx:
                upnp.add_mapping('http://192.168.1.1/ctl', 'urn:x:WANIPConnection:2',
                                 51413, 51413, '192.168.1.8', 'TCP', 'desc')
        self.assertIn('501', str(ctx.exception))
        self.assertIn('Action Failed', str(ctx.exception))

    def test_add_mapping_sends_the_expected_arguments(self):
        seen = {}

        def fake_request(url, data=None, headers=None, timeout=None):
            action = (headers or {}).get('SOAPAction', '')
            if 'AddPortMapping' in action:
                seen['body'] = data.decode()
                seen['url'] = url
                return 200, '<ok/>'
            return 200, ('<NewInternalClient>192.168.1.8</NewInternalClient>'
                         '<NewInternalPort>51413</NewInternalPort>')

        with patch.object(upnp, '_request', side_effect=fake_request):
            status = upnp.add_mapping('http://192.168.1.1:41795/ctl/IPConn',
                                      'urn:x:WANIPConnection:2', 51413, 51413,
                                      '192.168.1.8', 'TCP', 'Xiaomi NAS Transmission')
        self.assertIn('<NewExternalPort>51413</NewExternalPort>', seen['body'])
        self.assertIn('<NewProtocol>TCP</NewProtocol>', seen['body'])
        self.assertIn('<NewInternalPort>51413</NewInternalPort>', seen['body'])
        self.assertIn('<NewInternalClient>192.168.1.8</NewInternalClient>', seen['body'])
        self.assertIn('<NewEnabled>1</NewEnabled>', seen['body'])
        self.assertEqual(seen['url'], 'http://192.168.1.1:41795/ctl/IPConn')
        self.assertEqual(status['internal'], '192.168.1.8')

    def test_mapping_status_none_when_absent(self):
        fault = ('<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
                 '<s:Fault><detail><UPnPError xmlns="urn:schemas-upnp-org:control-1-0">'
                 '<errorCode>714</errorCode></UPnPError></detail></s:Fault></s:Body></s:Envelope>')
        with patch.object(upnp, '_request', return_value=(500, fault)):
            self.assertIsNone(upnp.mapping_status('http://192.168.1.1/ctl',
                                                  'urn:x:WANIPConnection:2', 51413, 'TCP'))

    def test_natpmp_success(self):
        # 16 字节应答：末尾 4 字节是路由器给的租期
        reply = struct.pack('!BBHIHHI', 0, 130, 0, 100, 51413, 51413, 3600)
        sock = _FakeSocket([reply])
        with patch.object(upnp.socket, 'socket', return_value=sock):
            ok, detail, lease = upnp.natpmp_map('192.168.1.1', 51413, 51413, 'tcp')
        self.assertTrue(ok)
        self.assertIn('51413', detail)
        self.assertEqual(lease, 3600)
        self.assertEqual(sock.sent[0][1], ('192.168.1.1', 5351))
        self.assertEqual(sock.sent[0][0][:2], bytes([0, 2]))       # 版本 0，MAP TCP

    def test_natpmp_success_without_lease_field(self):
        reply = struct.pack('!BBHIHH', 0, 130, 0, 100, 51413, 51413)
        with patch.object(upnp.socket, 'socket', return_value=_FakeSocket([reply])):
            ok, _, lease = upnp.natpmp_map('192.168.1.1', 51413, 51413, 'tcp')
        self.assertTrue(ok)
        self.assertEqual(lease, 7200)                              # 应答没带租期就用请求值

    def test_natpmp_refused(self):
        reply = struct.pack('!BBHIHH', 0, 130, 3, 100, 51413, 0)
        with patch.object(upnp.socket, 'socket', return_value=_FakeSocket([reply])):
            ok, detail, lease = upnp.natpmp_map('192.168.1.1', 51413, 51413, 'tcp')
        self.assertFalse(ok)
        self.assertIn('网络故障', detail)
        self.assertEqual(lease, 0)

    def test_natpmp_timeout(self):
        with patch.object(upnp.socket, 'socket', return_value=_FakeSocket([])):
            ok, detail, lease = upnp.natpmp_map('192.168.1.1', 51413, 51413, 'udp')
        self.assertFalse(ok)
        self.assertIn('没有响应', detail)
        self.assertEqual(lease, 0)

    def test_forward_prefers_upnp(self):
        with patch.object(upnp, '_try_upnp', return_value=(
                ['TCP', 'UDP'], [], {'gateway': '192.168.1.1', 'external': '1.2.3.4'})):
            state = upnp.forward_ports(51413, 51413, '192.168.1.8', 'desc')
        self.assertTrue(state['ok'])
        self.assertEqual(state['method'], 'UPnP')
        self.assertEqual(state['external'], '1.2.3.4')
        self.assertEqual(state['mapped'], ['TCP', 'UDP'])

    def test_forward_falls_back_to_natpmp(self):
        with patch.object(upnp, '_try_upnp',
                          side_effect=upnp.UpnpError('路由器没有响应 UPnP 搜索')), \
                patch.object(upnp, 'gateway', return_value='192.168.1.1'), \
                patch.object(upnp, '_try_natpmp', return_value=(['TCP', 'UDP'], [], 3600)):
            state = upnp.forward_ports(51413, 51413, '192.168.1.8', 'desc')
        self.assertTrue(state['ok'])
        self.assertEqual(state['method'], 'NAT-PMP')
        # NAT-PMP 有租期，得让上层知道要续期
        self.assertEqual(state['lease'], 3600)

    def test_forward_reports_both_failures(self):
        with patch.object(upnp, '_try_upnp',
                          side_effect=upnp.UpnpError('路由器没有响应 UPnP 搜索')), \
                patch.object(upnp, 'gateway', return_value='192.168.1.1'), \
                patch.object(upnp, '_try_natpmp', return_value=([], ['TCP NAT-PMP 被拒绝'], 0)):
            state = upnp.forward_ports(51413, 51413, '192.168.1.8', 'desc')
        self.assertFalse(state['ok'])
        self.assertIn('UPnP', state['detail'])
        self.assertIn('NAT-PMP', state['detail'])

    def test_forward_partial_upnp_keeps_tcp(self):
        with patch.object(upnp, '_try_upnp',
                          return_value=(['TCP'], ['UDP UPnP 错误 501'], {})):
            state = upnp.forward_ports(51413, 51413, '192.168.1.8', 'desc')
        self.assertFalse(state['ok'])
        self.assertEqual(state['mapped'], ['TCP'])
        self.assertIn('部分成功', state['detail'])

    def test_forward_without_any_router(self):
        with patch.object(upnp, '_try_upnp', side_effect=upnp.UpnpError('没响应')), \
                patch.object(upnp, 'gateway', return_value=''):
            state = upnp.forward_ports(51413, 51413, '192.168.1.8', 'desc')
        self.assertFalse(state['ok'])
        self.assertIn('没响应', state['detail'])
        self.assertEqual(state['internal'], '192.168.1.8')


class PortForwardEngineTests(unittest.TestCase):
    """引擎侧的端口映射：尽力而为，绝不阻断启动。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name) / 'root'
        root.mkdir()
        self.engine = Engine(Path(self.tmp.name) / 'data', root)

    def test_records_failure_detail(self):
        with patch.object(engine.upnp, 'lan_address', return_value='192.168.1.8'), \
                patch.object(engine.upnp, 'forward_ports', return_value={
                    'ok': False, 'method': '', 'detail': 'UPnP：UPnP 错误 501（Action Failed）',
                    'at': 1}):
            state = self.engine.ensure_port_forward(True)
        self.assertFalse(state['ok'])
        self.assertIn('501', state['detail'])
        self.assertIn('501', self.engine.forward_snapshot()['detail'])

    def test_records_success(self):
        with patch.object(engine.upnp, 'lan_address', return_value='192.168.1.8'), \
                patch.object(engine.upnp, 'forward_ports', return_value={
                    'ok': True, 'method': 'UPnP', 'detail': 'UPnP 映射成功', 'at': 1}) as call:
            self.engine.ensure_port_forward(True)
        self.assertTrue(self.engine.forward_snapshot()['ok'])
        self.assertEqual(call.call_args.args[2], '192.168.1.8')      # internal client
        self.assertEqual(call.call_args.args[0], BT_PORT)

    def test_skips_without_lan_address(self):
        with patch.object(engine.upnp, 'lan_address', return_value=''), \
                patch.object(engine.upnp, 'forward_ports') as call:
            state = self.engine.ensure_port_forward(True)
        self.assertFalse(state['ok'])
        self.assertIn('局域网地址', state['detail'])
        self.assertEqual(call.call_count, 0)

    def test_snapshot_carries_forward_state(self):
        self.engine.config = {'owner': 'tok', 'config': '/tmp/config'}
        stopped = {'Config': {'Labels': {LABEL: 'tok'}}, 'State': {'Running': False}}
        with patch.object(self.engine, 'owned', return_value=stopped):
            data = self.engine.snapshot()
        self.assertIn('forward', data)
        self.assertEqual(data['forward']['externalPort'], BT_PORT)
        self.assertEqual(sorted(data['forward']['protocols']), ['TCP', 'UDP'])

    def test_permanent_upnp_mapping_is_not_renewed(self):
        self.engine.forward_state = {'ok': True, 'method': 'UPnP', 'lease': 0, 'at': 1000}
        self.assertFalse(self.engine.forward_due(now=1000 + 86400))

    def test_natpmp_mapping_renews_before_expiry(self):
        self.engine.forward_state = {'ok': True, 'method': 'NAT-PMP', 'lease': 7200, 'at': 1000}
        self.assertFalse(self.engine.forward_due(now=1000 + 3599))
        self.assertTrue(self.engine.forward_due(now=1000 + 3600))

    def test_failed_mapping_is_retried_later(self):
        self.engine.forward_state = {'ok': False, 'at': 1000, 'detail': 'UPnP 错误 501'}
        self.assertFalse(self.engine.forward_due(now=1000 + 60))
        self.assertTrue(self.engine.forward_due(now=1000 + FORWARD_RETRY_SECONDS))

    def test_never_attempted_is_not_due(self):
        self.assertFalse(self.engine.forward_due(now=10 ** 9))

    def test_keep_forward_alive_renews_when_due(self):
        self.engine.forward_state = {'ok': True, 'method': 'NAT-PMP', 'lease': 60,
                                     'at': int(time.time()) - 120}
        with patch.object(self.engine, 'ensure_port_forward') as call:
            self.assertTrue(self.engine.keep_forward_alive())
        self.assertEqual(call.call_count, 1)

    def test_keep_forward_alive_skips_permanent_mapping(self):
        self.engine.forward_state = {'ok': True, 'method': 'UPnP', 'lease': 0,
                                     'at': int(time.time()) - 86400}
        with patch.object(self.engine, 'ensure_port_forward') as call:
            self.assertFalse(self.engine.keep_forward_alive())
        self.assertEqual(call.call_count, 0)

    def test_start_tries_the_router_mapping(self):
        self.engine.config = {'owner': 'tok'}
        with patch.object(self.engine, 'check_directories'), \
                patch.object(self.engine, 'adopt_legacy_settings', return_value=False), \
                patch.object(self.engine, 'ensure_settings', return_value=False), \
                patch.object(self.engine, 'ensure_webui'), \
                patch.object(self.engine, 'owned', return_value=None), \
                patch.object(self.engine, 'saved_password', return_value='Secret123!'), \
                patch.object(self.engine, 'pull'), \
                patch.object(self.engine, '_call'), \
                patch('engine.container_config', return_value={}), \
                patch.object(self.engine, 'ensure_published_ports'), \
                patch.object(self.engine, 'ensure_port_forward') as forward, \
                patch('engine.tr_rpc_probe', return_value=True), \
                patch('engine.atomic_json'):
            self.engine.start()
        self.assertEqual(forward.call_count, 1)


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


class UiTests(unittest.TestCase):
    def setUp(self):
        self.web = Path(__file__).resolve().parents[1] / 'web'
        self.icons = {p.stem for p in (self.web / 'assets').glob('*.png')}

    def test_ui_calls_the_api_with_relative_paths(self):
        script = (self.web / 'app.js').read_text(encoding='utf-8')
        self.assertNotIn("'/api", script)
        self.assertNotIn('"/api', script)
        # Windows 客户端 location 可能带 /D:/ 盘符，必须用 script 基址拼绝对 URL
        self.assertIn("function pluginAssetBase()", script)
        self.assertIn("fetch(assetUrl('api/' + route)", script)

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

    def test_page_warns_when_inbound_ports_are_not_published(self):
        html = (self.web / 'index.html').read_text(encoding='utf-8')
        self.assertIn('id="portPublish"', html)
        for name in ('app.js', 'app.bundle.js'):
            with self.subTest(name=name):
                script = (self.web / name).read_text(encoding='utf-8')
                self.assertIn("$('#portPublish')", script)
                self.assertIn('入站端口未发布', script)

    def test_page_shows_router_mapping_row(self):
        html = (self.web / 'index.html').read_text(encoding='utf-8')
        for name in ('forwardState', 'forwardPort'):
            with self.subTest(name=name):
                self.assertIn('id="%s"' % name, html)
        for name in ('app.js', 'app.bundle.js'):
            with self.subTest(name=name):
                script = (self.web / name).read_text(encoding='utf-8')
                self.assertIn('function renderForward(current)', script)
                self.assertIn("$('#forwardPort')", script)

    def test_page_shows_glance_stats(self):
        html = (self.web / 'index.html').read_text(encoding='utf-8')
        for name in ('statusStats', 'statsDown', 'statsUp', 'statsSeeding',
                     'statsDownloading', 'statsSessionDown', 'statsSessionUp'):
            with self.subTest(name=name):
                self.assertIn('id="%s"' % name, html)
        for name in ('app.js', 'app.bundle.js'):
            with self.subTest(name=name):
                script = (self.web / name).read_text(encoding='utf-8')
                self.assertIn('function renderStats(current)', script)
                self.assertIn("$('#statusStats')", script)

    def test_html_references_existing_files(self):
        html = (self.web / 'index.html').read_text(encoding='utf-8')
        referenced = re.findall(r'(?:href|src)="([^"]+\.(?:css|js))(?:\?[^"]*)?"', html)
        self.assertTrue(referenced)
        for name in referenced:
            self.assertTrue((self.web / name).is_file(), name)

    def test_console_button_opens_native_console(self):
        """WebUI 卡片的「打开控制台」在插件内打开原生控制台（同源 API，不跳第三方页）。"""
        html = (self.web / 'index.html').read_text(encoding='utf-8')
        self.assertIn('id="openConsole"', html)
        self.assertIn('id="consoleView"', html)
        self.assertIn('id="statusView"', html)
        script = (self.web / 'app.js').read_text(encoding='utf-8')
        self.assertIn("showConsole(true)", script)
        self.assertIn("api('torrents')", script)

    def test_narrow_screen_keeps_status_buttons_compact(self):
        """窄屏下状态卡的两个按钮不能被拉满整行，否则会变成一条很长的按钮。"""
        css = (self.web / 'styles.css').read_text(encoding='utf-8')
        marker = '@media (max-width: 430px)'
        start = css.find(marker)
        self.assertGreaterEqual(start, 0)
        narrow = css[start:start + 400]
        self.assertNotIn('width: 100%', narrow)
        self.assertIn('.status-actions', narrow)
        self.assertIn('.status-port', narrow)
        self.assertIn('.address-row code', narrow)
        self.assertIn('flex: 1 1 100%', narrow)

    def test_ui_shows_effective_settings_path(self):
        """daemon 真正读取的 settings.json 要显示在底部「说明与限制」里，不占服务卡片。"""
        html = (self.web / 'index.html').read_text(encoding='utf-8')
        self.assertIn('id="settingsFile"', html)
        self.assertIn('id="legacyHint"', html)
        self.assertNotIn('id="settingsHint"', html)
        self.assertLess(html.index('id="serviceActions"'), html.index('id="notes"'))
        self.assertGreater(html.index('id="settingsFile"'), html.index('id="notes"'))
        script = (self.web / 'app.js').read_text(encoding='utf-8')
        self.assertIn('current.settingsFile', script)
        self.assertIn('current.legacySettings', script)

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
