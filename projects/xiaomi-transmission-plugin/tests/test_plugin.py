import base64
import ast
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
    FORWARD_RETRY_SECONDS, MEMORY_LIMIT, CPU_LIMIT, WEBUI_HOME,
    SCHEDULE_VALUES, next_hour_epoch, covering_mount, volatile_identity, root_label, atomic_json,
)
import datetime  # noqa: E402 定时按钟点算，测试里要构造具体时刻
import engine  # noqa: E402  （按模块打桩，例如 engine.PROC_NET）
import upnp  # noqa: E402
from server import Server


def fake_owner_stat(uid=1000, gid=1000):
    """把目录属主伪造成非 root，让初始化用例在 Windows 上（st_uid/st_gid 恒为 0）也能真跑。

    只改 st_uid/st_gid，其余字段照抄真实值，所以设备号/inode 号仍然是真实的。
    """
    real_stat = Path.stat

    def stat(self, **kwargs):
        value = real_stat(self, **kwargs)
        return os.stat_result((value.st_mode, value.st_ino, value.st_dev, value.st_nlink,
                               uid, gid, value.st_size, value.st_atime, value.st_mtime, value.st_ctime))

    return patch.object(Path, 'stat', stat)


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
        self.assertEqual(host['Memory'], MEMORY_LIMIT)                    # 2 GiB，见 MEMORY_LIMIT
        self.assertEqual(host['MemorySwap'], MEMORY_LIMIT)
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

    def test_fuse_mount_identity_is_refreshed_not_rejected(self):
        """厂商存储池是 FUSE：每次挂载都会换设备号/inode 号，不该据此拒绝启动。"""
        folder = self.root / 'Downloads'
        s = folder.stat()
        self.engine.config = {
            'download': str(folder), 'download_relative': 'Downloads',
            'download_device': s.st_dev + 7, 'download_inode': s.st_ino + 7,
            'config': str(self.root / 'Config'), 'config_relative': 'Config',
            'config_device': s.st_dev, 'config_inode': s.st_ino,
            'watch': str(self.root / 'Watch'), 'watch_relative': 'Watch',
            'watch_device': s.st_dev, 'watch_inode': s.st_ino,
        }
        with patch('engine.covering_mount', return_value=('/nas/pool0', 'fuse.cfs')), \
                patch('engine.atomic_json') as saved:
            self.engine.check_directories()
        self.assertEqual(self.engine.config['download_device'], s.st_dev)
        self.assertEqual(self.engine.config['download_inode'], s.st_ino)
        self.assertTrue(saved.called)                 # 新值要写回配置
        # 普通盘（设备号稳定）仍然按老规矩拒绝
        self.engine.config['download_device'] = s.st_dev + 7
        with patch('engine.covering_mount', return_value=('/', 'ext4')), \
                patch('engine.atomic_json'), self.assertRaises(Error):
            self.engine.check_directories()
        # 目录不在任何挂载点下（没挂盘）也要拒绝
        self.engine.config['download_device'] = s.st_dev + 7
        with patch('engine.covering_mount', return_value=(None, '')), \
                patch('engine.atomic_json'), self.assertRaises(Error):
            self.engine.check_directories()

    def test_fuse_is_recognised_as_volatile_identity(self):
        self.assertFalse(volatile_identity('ext4'))
        self.assertFalse(volatile_identity('btrfs'))
        self.assertTrue(volatile_identity('fuse.cfs'))
        self.assertTrue(volatile_identity('fuseblk'))
        point, fstype = covering_mount('/')
        if point is not None:
            self.assertTrue('/'.startswith(point.rstrip('/') + '/') or point == '/')
            self.assertTrue(isinstance(fstype, str))


class MultiRootTests(unittest.TestCase):
    """多个存储位置（内置存储池 / 外接设备）：浏览、标签、绝对路径反查与启动校验。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        # 模拟 NAS：内置存储池，以及挂载在 /mnt/usb-xxxx 的外接设备
        self.pool = base / 'pool0' / 'u3943892' / 'data'
        self.usb = base / 'mnt' / 'usb'
        for name in ('下载', '电影', '监控'):
            (self.pool / name).mkdir(parents=True)
        (self.usb / '下载' / 'x').mkdir(parents=True)
        (self.usb / 'Music').mkdir()
        self.engine = self._fresh_engine()

    def _fresh_engine(self, roots=None):
        """每个用例一个独立的插件数据目录：setup() 写过配置之后就不让再初始化。"""
        data = Path(tempfile.mkdtemp(dir=self.tmp.name))
        return Engine(data, self.pool, roots=roots or [self.pool, self.usb])

    def _identity_config(self, entries, with_roots=True):
        """entries = [(键, 目录 Path, 相对路径), ...] → 一份带身份号的插件配置。"""
        config = {'owner': 'owner-token', 'uid': 1000, 'gid': 1000,
                  'username': 'admin', 'enabled': True}
        for key, folder, relative in entries:
            stat = folder.stat()
            config[key] = str(folder)
            config[key + '_relative'] = relative
            config[key + '_device'] = stat.st_dev
            config[key + '_inode'] = stat.st_ino
            if with_roots:
                config[key + '_root'] = str(self.engine.match_root(str(folder))[0])
        return config

    def _setup(self, engine, paths):
        def fake_api(method, route, body=None, timeout=30):
            if route == '/info':
                return 200, b'{"Architecture":"aarch64"}'
            return 404, b'{"message":"No such container"}'

        with fake_owner_stat(), patch('engine.os.chown', create=True), \
                patch('engine.install_webui', return_value='/config/webui'), \
                patch('engine.docker_api', side_effect=fake_api):
            engine.setup(paths, 'admin', 'Example123!')

    def test_roots_keep_order_and_dedupe(self):
        """LOCAL_ROOTS 的顺序就是页面上的顺序；self.root 仍是第 0 个。"""
        engine = Engine(Path(self.tmp.name) / 'dedupe', self.pool,
                        roots=[self.usb, self.pool, self.usb, str(self.usb) + '/'])
        self.assertEqual(engine.roots, [self.usb, self.pool])
        self.assertEqual(engine.root, self.usb)
        self.assertEqual(Engine(Path(self.tmp.name) / 'single', self.pool).roots, [self.pool])

    def test_root_label_rules(self):
        self.assertEqual(root_label('/nas/pool0/u3943892/data'), '存储池')
        self.assertEqual(root_label('/nas/pool0'), '存储池')
        self.assertEqual(root_label('/nas/pool0/'), '存储池')
        self.assertEqual(root_label('/nas/mnt/usb'), '外接设备')
        self.assertEqual(root_label('/nas/mnt/usb/下载/MT'), '外接设备')
        self.assertEqual(root_label('/mnt/usb-1a2b3c'), '外接设备')
        # 都不匹配时用最后一段目录名
        self.assertEqual(root_label('/data/volumes/disk1'), 'disk1')
        self.assertEqual(root_label('/'), '/')

    def test_browse_lists_each_root(self):
        self.assertEqual([item['name'] for item in self.engine.browse('')],
                         sorted(['下载', '电影', '监控']))
        self.assertEqual([item['name'] for item in self.engine.browse('', 1)], ['Music', '下载'])
        # 同一个相对路径分别相对各自的位置解释
        self.assertEqual(self.engine.browse('下载', 1), [{'name': 'x', 'path': '下载/x'}])
        self.assertEqual(self.engine.browse('下载', 0), [])

    def test_browse_rejects_unknown_root_index(self):
        for index in (2, -1, 'x', None):
            with self.subTest(index=index), self.assertRaises(Error):
                self.engine.browse('', index)

    def test_browse_reports_missing_root(self):
        engine = self._fresh_engine(roots=[self.pool, self.pool.parent / 'gone'])
        self.assertEqual(engine.roots_snapshot()[1]['exists'], False)
        with self.assertRaises(Error):
            engine.browse('', 1)

    def test_absolute_path_picks_the_longest_root(self):
        inner = self.pool / '电影'
        engine = self._fresh_engine(roots=[self.pool, inner])
        self.assertEqual(engine.match_root(str(inner / 'sub')), (inner, 'sub'))
        self.assertEqual(engine.match_root(str(self.pool / '下载')), (self.pool, '下载'))
        self.assertEqual(engine.match_root(str(self.pool)), (self.pool, ''))
        self.assertIsNone(engine.match_root(str(self.usb)))
        self.assertEqual(engine._claim_folder(str(self.pool / '下载'), '下载目录'), self.pool / '下载')

    def test_selection_outside_roots_is_rejected(self):
        outside = Path(self.tmp.name) / 'elsewhere'
        outside.mkdir()
        with self.assertRaises(Error) as caught:
            self.engine._claim_folder(str(outside), '下载目录')
        self.assertIn('必须位于已挂载的存储位置内', str(caught.exception))
        # setup() 里同样拒绝，而且不会写下任何配置
        engine = self._fresh_engine()
        with self.assertRaises(Error):
            self._setup(engine, {'download': str(outside), 'config': str(self.pool / '电影'),
                                 'watch': str(self.pool / '监控')})
        self.assertIsNone(engine.config)

    def test_setup_with_absolute_paths_records_roots(self):
        """表单提交绝对路径：反查出所属存储位置，*_root 与身份号都要落进配置。"""
        engine = self._fresh_engine()
        self._setup(engine, {'download': str(self.usb / '下载'),
                             'config': str(self.pool / '电影'),
                             'watch': str(self.usb / 'Music')})
        self.assertEqual(engine.config['download'], str(self.usb / '下载'))
        self.assertEqual(engine.config['download_root'], str(self.usb))
        self.assertEqual(engine.config['download_relative'], '下载')
        self.assertEqual(engine.config['config'], str(self.pool / '电影'))
        self.assertEqual(engine.config['config_root'], str(self.pool))
        self.assertEqual(engine.config['config_relative'], '电影')
        self.assertEqual(engine.config['watch_root'], str(self.usb))
        self.assertEqual(engine.config['watch_relative'], 'Music')
        stat = (self.usb / '下载').stat()
        self.assertEqual((engine.config['download_device'], engine.config['download_inode']),
                         (stat.st_dev, stat.st_ino))
        self.assertEqual((engine.config['uid'], engine.config['gid']), (1000, 1000))
        # 用这份配置做启动校验：绝对路径、*_root、身份号都能对上
        engine.check_directories()

    def test_setup_with_relative_paths_still_works(self):
        """相对路径提交（旧前端/旧习惯）仍然按第 0 个存储位置解释。"""
        engine = self._fresh_engine()
        self._setup(engine, {'download': '下载', 'config': '电影', 'watch': '监控'})
        self.assertEqual(engine.config['download'], str(self.pool / '下载'))
        self.assertEqual(engine.config['download_root'], str(self.pool))
        self.assertEqual(engine.config['download_relative'], '下载')
        self.assertEqual(engine.config['watch_relative'], '监控')

    def test_startup_check_uses_recorded_root(self):
        engine = self._fresh_engine()
        engine.config = self._identity_config([
            ('download', self.usb / 'Music', 'Music'),
            ('config', self.pool / '电影', '电影'),
            ('watch', self.pool / '监控', '监控')])
        engine.check_directories()                 # 外接设备上的目录也能通过
        engine.config['download_root'] = str(self.pool)     # 指错位置：路径对不上，拒绝启动
        with self.assertRaises(Error):
            engine.check_directories()

    def test_startup_check_accepts_legacy_config_without_roots(self):
        """升级前初始化过的配置没有 *_root，启动校验必须照旧通过、也不改写配置。"""
        engine = self._fresh_engine()
        engine.config = self._identity_config([
            ('download', self.pool / '下载', '下载'),
            ('config', self.pool / '电影', '电影'),
            ('watch', self.pool / '监控', '监控')], with_roots=False)
        engine.check_directories()
        self.assertNotIn('download_root', engine.config)
        self.assertFalse(engine.cfgfile.exists())

    def test_legacy_config_on_a_secondary_root_still_starts(self):
        """旧配置没有 *_root：按绝对路径反查它落在哪个位置（LOCAL_ROOT 曾指向 U 盘的情形）。"""
        engine = self._fresh_engine()
        engine.config = self._identity_config([
            ('download', self.usb / 'Music', 'Music'),
            ('config', self.usb / '下载' / 'x', '下载/x'),
            ('watch', self.pool / '监控', '监控')], with_roots=False)
        engine.check_directories()

    def test_snapshot_exposes_roots_and_absolute_paths(self):
        engine = self._fresh_engine()
        state = engine.snapshot()
        self.assertEqual([entry['path'] for entry in state['roots']], [str(self.pool), str(self.usb)])
        self.assertEqual([entry['index'] for entry in state['roots']], [0, 1])
        # 不是 NAS 固定路径（/nas/pool0、/nas/mnt/usb）时标签用最后一段目录名
        self.assertEqual([entry['label'] for entry in state['roots']],
                         [self.pool.name, self.usb.name])
        self.assertTrue(all(entry['exists'] for entry in state['roots']))
        engine.config = self._identity_config([
            ('download', self.usb / 'Music', 'Music'),
            ('config', self.pool / '电影', '电影'),
            ('watch', self.pool / '监控', '监控')])
        with patch.object(engine, 'inspect', return_value=None):
            state = engine.snapshot()
        self.assertEqual(state['download_abs'], str(self.usb / 'Music'))
        self.assertEqual(state['watch_abs'], str(self.pool / '监控'))
        self.assertEqual(state['download'], 'Music')       # 相对值仍照发，前端表单用得上


class DirectoryHelpers:
    """「修改目录 / 重新初始化」用例共用的脚手架（本身不是测试用例）。"""

    owner = 'owner-token'
    password = 'Example123!'

    def stub(self, target, attribute, **kwargs):
        """真正打上补丁并在用例结束时还原。

        只 addCleanup(patch.object(...).stop) 是**不生效**的：补丁从没被 start()，
        于是单测会去碰真的路由器（SSDP 超时还会把用例拖到几十秒）。
        """
        patcher = patch.object(target, attribute, **kwargs)
        patcher.start()
        self.addCleanup(patcher.stop)
        return patcher

    def setUpStorage(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.pool = self.base / 'pool0' / 'u3943892' / 'data'
        self.usb = self.base / 'mnt' / 'usb'
        # 旧位置在存储池下，新位置在外接设备下：换目录 = 换存储位置
        self.download, self.configdir, self.watch = (self.pool / name for name in ('下载', '配置', '监控'))
        self.new_download, self.new_config, self.new_watch = (
            self.usb / name for name in ('下载', '配置', '监控'))
        for folder in (self.download, self.configdir, self.watch,
                       self.new_download, self.new_config, self.new_watch):
            folder.mkdir(parents=True)
        for folder in (self.configdir, self.new_config):
            (folder / 'webui').mkdir()
            (folder / 'webui' / 'index.html').write_text('<html></html>', encoding='utf-8')
        self.engine = Engine(self.base / 'private', self.pool, roots=[self.pool, self.usb])
        # 单测绝不碰真实网络：UPnP/NAT-PMP 一律打桩；端口巡检也不真等 20 秒
        self.stub(engine.upnp, 'lan_address', return_value='192.168.1.8')
        self.stub(engine.upnp, 'forward_ports', return_value={
            'ok': False, 'method': '', 'detail': '测试环境跳过', 'at': 1})
        self.stub(engine.upnp, 'remove_forward',
                  return_value={'ok': True, 'detail': '测试环境已移除'})
        self.stub(self.engine, 'ensure_published_ports', return_value=[])

    def configure(self, **extra):
        """把引擎置成「已初始化到存储池下三个目录」的状态，并写出配置文件与凭据。"""
        config = {'owner': self.owner, 'uid': 1000, 'gid': 1000,
                  'username': 'admin', 'enabled': True}
        for key, folder in (('download', self.download), ('config', self.configdir),
                            ('watch', self.watch)):
            stat = folder.stat()
            config[key] = str(folder)
            config[key + '_root'] = str(self.pool)
            config[key + '_relative'] = folder.name
            config[key + '_device'] = stat.st_dev
            config[key + '_inode'] = stat.st_ino
        config.update(extra)
        self.engine.config = dict(config)
        atomic_json(self.engine.cfgfile, config)
        atomic_json(self.engine.credentialfile,
                    {'password': self.password, 'username': config['username']})
        return config

    def new_paths(self, **extra):
        payload = {'download': str(self.new_download), 'config': str(self.new_config),
                   'watch': str(self.new_watch)}
        payload.update(extra)
        return payload

    def docker(self, calls, container, refuse=None):
        """Docker API 桩：容器一开始存在且在跑，DELETE 之后就不存在了。

        create 时 /downloads 的挂载源等于 refuse 就返回 500，用来模拟重建失败。
        """
        def fake_api(method, path, body=None, timeout=30):
            calls.append((method, path, body))
            if path == '/containers/' + NAME + '/json':
                if not container['exists']:
                    return 404, b'{"message":"No such container"}'
                owner = (self.engine.config or {}).get('owner', self.owner)
                return 200, json.dumps({
                    'Config': {'Labels': {LABEL: owner},
                               'Env': ['TRANSMISSION_WEB_HOME=' + WEBUI_HOME]},
                    'State': {'Running': container['running']},
                    'HostConfig': {'Memory': MEMORY_LIMIT, 'MemorySwap': MEMORY_LIMIT,
                                   'NanoCpus': CPU_LIMIT}}).encode()
            if path.startswith('/containers/' + NAME + '/stop'):
                container['running'] = False
                return 204, b''
            if path.startswith('/containers/' + NAME + '/start'):
                container['running'] = True
                return 204, b''
            if path.startswith('/containers/' + NAME + '?'):
                container['exists'] = False
                return 204, b''
            if path.startswith('/containers/create'):
                if refuse and body['HostConfig']['Mounts'][1]['Source'] == refuse:
                    return 500, b'{"message":"invalid mount config for /downloads"}'
                container['exists'], container['running'] = True, False
                return 201, b'{"Id":"rebuilt"}'
            if path.startswith('/images/create'):
                return 200, b'{}\n'
            return 200, b'{}'
        return fake_api

    def created_mounts(self, calls):
        """每次 create 请求发的挂载表（按 Target 归位），顺序即调用顺序。"""
        return [{mount['Target']: mount['Source'] for mount in body['HostConfig']['Mounts']}
                for _, path, body in calls if path.startswith('/containers/create') and body]

    def container_calls(self, calls):
        stopped = any(method == 'POST' and path == '/containers/' + NAME + '/stop?t=15'
                      for method, path, _ in calls)
        deleted = any(method == 'DELETE' and path.startswith('/containers/' + NAME + '?')
                      for method, path, _ in calls)
        return stopped, deleted


class DirectoryChangeTests(DirectoryHelpers, unittest.TestCase):
    """「修改目录」与「重新初始化」：会停删容器、改写配置，绝不能碰用户的数据目录。"""

    def setUp(self):
        self.setUpStorage()

    def test_reconfigure_moves_to_another_location_and_rebuilds_the_container(self):
        self.configure()
        calls, container = [], {'exists': True, 'running': True}
        with fake_owner_stat(), patch('engine.os.chown', create=True), \
                patch('engine.docker_api', side_effect=self.docker(calls, container)), \
                patch('engine.tr_rpc_probe', return_value=True):
            state = self.engine.reconfigure(self.new_paths())
        # 旧容器必须显式停掉再删除：只 restart 的话它还挂着旧目录
        self.assertEqual(self.container_calls(calls), (True, True))
        self.assertEqual(self.created_mounts(calls), [{
            '/config': str(self.new_config), '/downloads': str(self.new_download),
            '/watch': str(self.new_watch)}])
        self.assertEqual(self.engine.config['download'], str(self.new_download))
        self.assertEqual(self.engine.config['download_root'], str(self.usb))
        self.assertEqual(self.engine.config['download_relative'], '下载')
        self.assertEqual(self.engine.config['config_root'], str(self.usb))
        self.assertEqual(self.engine.config['watch_root'], str(self.usb))
        self.assertEqual(state['download_abs'], str(self.new_download))
        self.assertEqual(state['download'], '下载')
        # 原配置另存了一份备份
        self.assertTrue(list(self.engine.data.glob('settings.json.bak-*')))
        # 应用新目录后，启动校验按记录的根走
        self.engine.check_directories()

    def test_reconfigure_rolls_back_when_the_rebuild_fails(self):
        self.configure()
        calls, container = [], {'exists': True, 'running': True}
        with fake_owner_stat(), patch('engine.os.chown', create=True), \
                patch('engine.docker_api',
                      side_effect=self.docker(calls, container, refuse=str(self.new_download))), \
                patch('engine.tr_rpc_probe', return_value=True):
            with self.assertRaises(Error) as caught:
                self.engine.reconfigure(self.new_paths())
        self.assertIn('已恢复原来的目录设置', str(caught.exception))
        self.assertIn('容器已按原设置启动', str(caught.exception))
        self.assertEqual(self.engine.config['download'], str(self.download))
        self.assertEqual(self.engine.config['download_root'], str(self.pool))
        self.assertEqual(json.loads(self.engine.cfgfile.read_text(encoding='utf-8'))['download'],
                         str(self.download))
        # 先按新目录创建失败，回滚后又把原容器建了回来
        sources = [mounts['/downloads'] for mounts in self.created_mounts(calls)]
        self.assertEqual(sources, [str(self.new_download), str(self.download)])
        self.assertTrue(container['exists'])

    def test_reconfigure_can_change_the_webui_password(self):
        self.configure()
        self.engine.credentialfile.write_text(
            json.dumps({'password': 'OldPass123', 'username': 'admin'}), encoding='utf-8')
        calls, container = [], {'exists': True, 'running': True}
        with fake_owner_stat(), patch('engine.os.chown', create=True), \
                patch('engine.docker_api', side_effect=self.docker(calls, container)), \
                patch('engine.tr_rpc_probe', return_value=True):
            self.engine.reconfigure(self.new_paths(password='NewPass456'))
        self.assertEqual(self.engine.saved_password(), 'NewPass456')
        env = next(body['Env'] for _, path, body in calls
                   if path.startswith('/containers/create') and body)
        self.assertIn('PASS=NewPass456', env)               # 重建容器时就带上新密码
        self.assertIn('USER=admin', env)

    def test_failed_reconfigure_restores_the_previous_password(self):
        self.configure()
        self.engine.credentialfile.write_text(
            json.dumps({'password': 'OldPass123', 'username': 'admin'}), encoding='utf-8')
        calls, container = [], {'exists': True, 'running': True}
        with fake_owner_stat(), patch('engine.os.chown', create=True), \
                patch('engine.docker_api',
                      side_effect=self.docker(calls, container, refuse=str(self.new_download))), \
                patch('engine.tr_rpc_probe', return_value=True):
            with self.assertRaises(Error):
                self.engine.reconfigure(self.new_paths(password='NewPass456'))
        self.assertEqual(self.engine.saved_password(), 'OldPass123')

    def test_reconfigure_keeps_the_new_directory_when_the_web_is_slow(self):
        """容器已按新目录重建、只是 Web 还没就绪：不能把用户刚选的目录回滚掉。"""
        self.configure()
        calls, container = [], {'exists': True, 'running': True}
        with fake_owner_stat(), patch('engine.os.chown', create=True), \
                patch('engine.docker_api', side_effect=self.docker(calls, container)), \
                patch('engine.tr_rpc_probe', side_effect=Error('Transmission 未运行或尚未就绪')), \
                patch('engine.time.sleep'):
            state = self.engine.reconfigure(self.new_paths())
        self.assertEqual(self.engine.config['download'], str(self.new_download))
        self.assertEqual(state['download_abs'], str(self.new_download))
        self.assertEqual([mounts['/downloads'] for mounts in self.created_mounts(calls)],
                         [str(self.new_download)])

    def test_reconfigure_rejects_unchanged_directories(self):
        self.configure()
        calls, container = [], {'exists': True, 'running': True}
        with fake_owner_stat(), patch('engine.docker_api', side_effect=self.docker(calls, container)):
            with self.assertRaises(Error) as caught:
                self.engine.reconfigure({'download': '下载', 'config': '配置', 'watch': '监控'})
        self.assertIn('无需重新配置', str(caught.exception))
        self.assertEqual(calls, [])                        # 一次 Docker 调用都不该发生
        self.assertEqual(self.engine.config['download'], str(self.download))

    def test_reconfigure_validates_before_touching_anything(self):
        self.configure()
        outside = self.base / 'elsewhere'
        outside.mkdir()
        calls, container = [], {'exists': True, 'running': True}
        with fake_owner_stat(), patch('engine.docker_api', side_effect=self.docker(calls, container)):
            with self.assertRaises(Error) as caught:
                self.engine.reconfigure({'download': str(outside), 'config': str(self.new_config),
                                         'watch': str(self.new_watch)})
        self.assertIn('必须位于已挂载的存储位置内', str(caught.exception))
        self.assertEqual(calls, [])
        self.assertEqual(self.engine.config['download'], str(self.download))
        self.assertFalse(list(self.engine.data.glob('settings.json.bak-*')))

    def test_reconfigure_requires_setup(self):
        with self.assertRaises(Error) as caught:
            self.engine.reconfigure(self.new_paths())
        self.assertIn('请先初始化', str(caught.exception))

    def test_reconfigure_and_reset_are_refused_while_another_operation_runs(self):
        for action in ('reconfigure', 'reset'):
            with self.subTest(action=action):
                engine = Engine(self.base / ('busy-' + action), self.pool,
                                roots=[self.pool, self.usb])
                engine.config = {'owner': self.owner, 'username': 'admin'}
                engine.busy = True
                with self.assertRaises(Error) as caught:
                    engine.launch(action, dict(self.new_paths(), confirm=True))
                self.assertIn('当前有操作正在进行，请稍后再试', str(caught.exception))
                engine.busy = False
                self.assertTrue(engine.config)             # 什么都没动

    def test_reset_needs_explicit_confirmation(self):
        self.configure()
        calls, container = [], {'exists': True, 'running': True}
        with patch('engine.docker_api', side_effect=self.docker(calls, container)):
            with self.assertRaises(Error) as caught:
                self.engine.launch('reset', {}, wait=True)
        self.assertIn('请确认重新初始化', str(caught.exception))
        self.assertTrue(self.engine.config)                 # 配置与容器都没动
        self.assertTrue(self.engine.cfgfile.exists())
        self.assertEqual(calls, [])

    def test_reset_clears_the_config_and_never_touches_the_user_directories(self):
        sentinels = []
        for folder in (self.download, self.configdir, self.watch):
            keep = folder / '哨兵-不要动.txt'
            keep.write_text('重要数据', encoding='utf-8')
            sentinels.append(keep)
        (self.download / 'sub').mkdir()
        (self.download / 'sub' / 'nested.txt').write_text('nested', encoding='utf-8')
        self.configure()
        (self.configdir / 'settings.json').write_text(
            json.dumps({'cache-size-mb': 256}), encoding='utf-8')
        self.engine.credentialfile.write_text(
            json.dumps({'password': 'OldPass123', 'username': 'admin'}), encoding='utf-8')
        calls, container = [], {'exists': True, 'running': True}
        with patch('engine.docker_api', side_effect=self.docker(calls, container)):
            state = self.engine.reset(confirm=True)
        self.assertFalse(state['configured'])
        self.assertFalse(state['running'])
        self.assertIsNone(self.engine.config)
        self.assertEqual(self.container_calls(calls), (True, True))
        # 插件配置挪成备份：页面回到初始化表单
        self.assertFalse(self.engine.cfgfile.exists())
        self.assertTrue(list(self.engine.data.glob('settings.json.bak-*')))
        self.assertFalse(self.engine.credentialfile.exists())
        # daemon 的 settings.json 只另存一份备份，原文件留在原处（用户调过的参数属于用户目录）
        self.assertTrue((self.configdir / 'settings.json').is_file())
        self.assertTrue(list(self.configdir.glob('settings.json.bak-*')))
        # 用户目录里的哨兵文件与子目录一个都没被动过
        for keep in sentinels:
            with self.subTest(keep=str(keep)):
                self.assertTrue(keep.is_file())
                self.assertEqual(keep.read_text(encoding='utf-8'), '重要数据')
        self.assertEqual((self.download / 'sub' / 'nested.txt').read_text(encoding='utf-8'), 'nested')
        self.assertEqual(sorted(item.name for item in self.download.iterdir()),
                         sorted(['哨兵-不要动.txt', 'sub']))
        self.assertEqual(sorted(item.name for item in self.configdir.iterdir()),
                         sorted(['settings.json', '哨兵-不要动.txt', 'webui'] + [p.name for p in
                                 self.configdir.glob('settings.json.bak-*')]))

    def test_reset_removes_the_router_mapping(self):
        self.configure()
        self.engine.forward_state = {'ok': True, 'method': 'UPnP', 'at': int(time.time()),
                                     'lease': 0, 'gateway': '192.168.1.1',
                                     'control': 'http://192.168.1.1:5000/ctl',
                                     'service': 'urn:schemas-upnp-org:service:WANIPConnection:1'}
        calls, container = [], {'exists': True, 'running': True}
        with patch('engine.docker_api', side_effect=self.docker(calls, container)), \
                patch.object(engine.upnp, 'remove_forward',
                             return_value={'ok': True, 'detail': '已移除'}) as removed:
            state = self.engine.reset(confirm=True)
        self.assertTrue(removed.called)                     # 路由器上不留垃圾映射
        self.assertTrue(self.engine.forward_snapshot()['removed'])
        self.assertFalse(state['configured'])

    def test_reset_archives_the_credential_file_instead_of_deleting_it(self):
        """凭据只能改名归档：直接删掉会让用户既起不来容器、又没法重新初始化。"""
        self.configure()
        self.engine.credentialfile.write_text(
            json.dumps({'password': 'OldPass123', 'username': 'admin'}), encoding='utf-8')
        calls, container = [], {'exists': True, 'running': True}
        with patch('engine.docker_api', side_effect=self.docker(calls, container)):
            state = self.engine.reset(confirm=True)
        archived = list(self.engine.data.glob('credential.json.bak-*'))
        self.assertEqual(len(archived), 1)
        self.assertEqual(json.loads(archived[0].read_text(encoding='utf-8'))['password'],
                         'OldPass123')                      # 内容与原来一致
        self.assertFalse(self.engine.credentialfile.exists())    # 是改名，不是多留一份
        self.assertFalse(state['configured'])

    def test_archived_credential_restores_the_service(self):
        """恢复路径的直接验证：把归档的凭据与配置备份改名回去，重启后能正常起来。"""
        self.configure()
        self.engine.credentialfile.write_text(
            json.dumps({'password': 'OldPass123', 'username': 'admin'}), encoding='utf-8')
        calls, container = [], {'exists': True, 'running': True}
        with patch('engine.docker_api', side_effect=self.docker(calls, container)):
            self.engine.reset(confirm=True)
        # 用户按 README 恢复：凭据改名回原名，插件配置从备份还原
        archived = list(self.engine.data.glob('credential.json.bak-*'))[0]
        archived.replace(self.engine.credentialfile)
        config_backup = list(self.engine.data.glob('settings.json.bak-*'))[0]
        self.engine.cfgfile.write_bytes(config_backup.read_bytes())
        # 新进程（相当于重启插件服务）从文件里读回状态
        fresh = Engine(self.engine.data, self.pool, roots=[self.pool, self.usb])
        self.assertEqual(fresh.saved_password(), 'OldPass123')
        with patch.object(fresh, 'inspect', return_value=None):
            self.assertFalse(fresh.snapshot()['credentialMissing'])
        calls, container = [], {'exists': False, 'running': False}
        with fake_owner_stat(), patch('engine.os.chown', create=True), \
                patch.object(fresh, 'ensure_published_ports', return_value=[]), \
                patch('engine.docker_api', side_effect=self.docker(calls, container)), \
                patch('engine.tr_rpc_probe', return_value=True):
            fresh.start()
        self.assertTrue(container['exists'])
        self.assertTrue(container['running'])
        env = next(body['Env'] for _, path, body in calls
                   if path.startswith('/containers/create') and body)
        self.assertIn('PASS=OldPass123', env)              # 老密码原样回到容器里

    def test_missing_credential_is_reported_with_the_way_out(self):
        """配置还在、凭据丢了：报错要说清恢复入口，状态里也要有标记。"""
        self.configure()
        self.engine.credentialfile.unlink()
        with patch.object(self.engine, 'inspect', return_value=None):
            self.assertTrue(self.engine.snapshot()['credentialMissing'])
        calls, container = [], {'exists': False, 'running': False}
        with fake_owner_stat(), patch('engine.os.chown', create=True), \
                patch('engine.docker_api', side_effect=self.docker(calls, container)):
            with self.assertRaises(Error) as caught:
                self.engine.start()
        message = str(caught.exception)
        self.assertIn('缺少 WebUI 密码', message)
        self.assertIn('修改目录', message)                  # 指到能重设密码的入口
        self.assertIn('重新初始化', message)
        # 凭据回来了，状态就不再报缺失
        self.engine.credentialfile.write_text(
            json.dumps({'password': 'Example123!'}), encoding='utf-8')
        with patch.object(self.engine, 'inspect', return_value=None):
            self.assertFalse(self.engine.snapshot()['credentialMissing'])

    def test_reconfigure_asks_for_a_new_password_when_the_credential_is_missing(self):
        self.configure()
        self.engine.credentialfile.unlink()
        calls, container = [], {'exists': True, 'running': True}
        with fake_owner_stat(), patch('engine.docker_api', side_effect=self.docker(calls, container)):
            with self.assertRaises(Error) as caught:
                self.engine.reconfigure(self.new_paths())
        self.assertIn('凭据文件缺失', str(caught.exception))
        self.assertIn('新的 WebUI 密码', str(caught.exception))
        self.assertEqual(calls, [])                        # 说清之前不动任何东西
        self.assertEqual(self.engine.config['download'], str(self.download))

    def test_reconfigure_recovers_a_missing_credential_with_a_new_password(self):
        """凭据缺失时，「修改目录」填个新密码（目录可以不动）就能把服务救回来。"""
        self.configure()
        self.engine.credentialfile.unlink()
        same = {'download': str(self.download), 'config': str(self.configdir),
                'watch': str(self.watch), 'password': 'NewPass456'}
        calls, container = [], {'exists': True, 'running': True}
        with fake_owner_stat(), patch('engine.os.chown', create=True), \
                patch('engine.docker_api', side_effect=self.docker(calls, container)), \
                patch('engine.tr_rpc_probe', return_value=True):
            state = self.engine.reconfigure(same)
        self.assertEqual(self.engine.saved_password(), 'NewPass456')
        self.assertFalse(state['credentialMissing'])
        self.assertEqual(self.engine.config['download'], str(self.download))
        env = next(body['Env'] for _, path, body in calls
                   if path.startswith('/containers/create') and body)
        self.assertIn('PASS=NewPass456', env)

    def test_setup_still_refuses_when_config_and_credential_exist(self):
        self.configure()
        with self.assertRaises(Error) as caught:
            self.engine.setup(self.new_paths(), 'admin', 'Example123!')
        self.assertIn('已完成初始化', str(caught.exception))
        self.assertEqual(self.engine.config['download'], str(self.download))

    def test_reset_can_be_followed_by_a_fresh_setup(self):
        """重新初始化之后必须还能再初始化一次。"""
        self.configure()
        calls, container = [], {'exists': True, 'running': True}
        with patch('engine.docker_api', side_effect=self.docker(calls, container)):
            self.engine.reset(confirm=True)
        with fake_owner_stat(), patch('engine.os.chown', create=True), \
                patch('engine.docker_api', side_effect=self.docker(calls, container)), \
                patch('engine.tr_rpc_probe', return_value=True):
            state = self.engine.launch('setup', dict(self.new_paths(), username='admin',
                                                     password='Example123!'), wait=True)
        config = json.loads(self.engine.cfgfile.read_text(encoding='utf-8'))
        self.assertEqual(config['download_root'], str(self.usb))
        self.assertEqual(config['download_relative'], '下载')
        self.assertEqual(state['download_abs'], str(self.new_download))


class DirectoryHttpTests(DirectoryHelpers, unittest.TestCase):
    """接口层：/api/service/reconfigure 与 /api/service/reset。

    Server 的 dev 只关掉鉴权，engine.dev 保持 False，好让 launch 真的跑（Docker 打桩）。
    """

    def setUp(self):
        self.setUpStorage()
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
            conn.request(method, route, json.dumps(data) if data is not None else None,
                         headers or {})
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def auth(self):
        return {'X-TR-Session': self.token, 'X-CSRF-Token': self.csrf,
                'Content-Type': 'application/json'}

    def test_reconfigure_returns_the_updated_snapshot(self):
        self.configure()
        calls, container = [], {'exists': True, 'running': True}
        with fake_owner_stat(), patch('engine.os.chown', create=True), \
                patch('engine.docker_api', side_effect=self.docker(calls, container)), \
                patch('engine.tr_rpc_probe', return_value=True):
            code, body = self.request('POST', '/api/service/reconfigure',
                                      self.new_paths(password=''), self.auth())
        self.assertEqual(code, 200)
        data = json.loads(body)
        self.assertTrue(data['ok'])
        self.assertEqual(data['state']['download_abs'], str(self.new_download))
        self.assertEqual(data['state']['config_abs'], str(self.new_config))
        # 状态既在 state 里，也平铺在响应的顶层
        self.assertEqual(data['download_abs'], str(self.new_download))
        self.assertEqual(data['configured'], True)
        self.assertEqual(self.engine.config['download_root'], str(self.usb))
        self.assertEqual([mounts['/downloads'] for mounts in self.created_mounts(calls)],
                         [str(self.new_download)])

    def test_reconfigure_failure_reports_the_reason(self):
        self.configure()
        calls, container = [], {'exists': True, 'running': True}
        with fake_owner_stat(), patch('engine.os.chown', create=True), \
                patch('engine.docker_api',
                      side_effect=self.docker(calls, container, refuse=str(self.new_download))), \
                patch('engine.tr_rpc_probe', return_value=True):
            code, body = self.request('POST', '/api/service/reconfigure',
                                      self.new_paths(), self.auth())
        self.assertEqual(code, 400)
        self.assertIn('已恢复原来的目录设置', json.loads(body)['error'])
        self.assertEqual(self.engine.config['download'], str(self.download))

    def test_reset_over_http_needs_confirmation(self):
        self.configure()
        code, body = self.request('POST', '/api/service/reset', {}, self.auth())
        self.assertEqual(code, 400)
        self.assertIn('确认', json.loads(body)['error'])
        self.assertTrue(self.engine.cfgfile.exists())

    def test_reset_over_http_reports_unconfigured(self):
        keep = self.download / '哨兵.txt'
        keep.write_text('x', encoding='utf-8')
        self.configure()
        calls, container = [], {'exists': True, 'running': True}
        with patch('engine.docker_api', side_effect=self.docker(calls, container)):
            code, body = self.request('POST', '/api/service/reset', {'confirm': True}, self.auth())
        self.assertEqual(code, 200)
        data = json.loads(body)
        self.assertFalse(data['state']['configured'])
        self.assertFalse(data['state']['running'])
        self.assertFalse(self.engine.cfgfile.exists())
        self.assertTrue(keep.is_file())                     # 用户目录不受影响


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

    def test_browse_selects_the_storage_root(self):
        """/api/browse?root=<n> 选存储位置；缺省仍是第 0 个，越界编号报错而不是悄悄换盘。"""
        usb = Path(self.tmp.name) / 'usb'
        (usb / 'Movies').mkdir(parents=True)
        self.engine.roots = [self.engine.root, usb]
        code, body = self.request('GET', '/api/browse?root=1', headers=self.auth())
        self.assertEqual(code, 200)
        self.assertEqual([item['name'] for item in json.loads(body)['items']], ['Movies'])
        code, body = self.request('GET', '/api/browse', headers=self.auth())
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)['items'], [])
        code, body = self.request('GET', '/api/browse?root=9', headers=self.auth())
        self.assertEqual(code, 400)
        self.assertIn('存储位置无效', json.loads(body)['error'])

    def test_status_carries_schedule_settings(self):
        code, body = self.request('GET', '/api/status', headers=self.auth())
        self.assertEqual(code, 200)
        self.assertIn('schedule', json.loads(body))

    def test_all_start_stop_hit_transmission_without_ids(self):
        """控制台的「全部开始 / 全部暂停」：不带 ids 就是全部任务。"""
        self.engine.config = {'owner': 'tok', 'config': '/tmp/config', 'username': 'admin'}
        with patch('server.Handler.rpc_creds', return_value=('admin', 'Secret123!')), \
                patch('server.tr_call') as call:
            code, _ = self.request('POST', '/api/all-start', {}, self.auth())
            self.assertEqual(code, 200)
            self.assertEqual(call.call_args.args[:3], ('torrent-start', 'admin', 'Secret123!'))
            self.assertEqual(len(call.call_args.args), 3)          # 没有 ids
            code, _ = self.request('POST', '/api/all-stop', {}, self.auth())
            self.assertEqual(code, 200)
            self.assertEqual(call.call_args.args[:3], ('torrent-stop', 'admin', 'Secret123!'))

    def test_all_start_needs_csrf(self):
        self.assertEqual(self.request('POST', '/api/all-start', {}, {'X-TR-Session': self.token})[0], 403)

    def test_schedule_endpoint_round_trip(self):
        self.engine.config = {'owner': 'tok', 'config': '/tmp/config'}
        code, body = self.request('POST', '/api/schedule', {'kind': 'start', 'value': '3'}, self.auth())
        self.assertEqual(code, 200)
        data = json.loads(body)
        self.assertEqual(data['schedule']['start']['value'], '3')
        self.assertGreater(data['schedule']['start']['next'], 0)
        # 关掉再确认回到 off
        code, body = self.request('POST', '/api/schedule', {'kind': 'start', 'value': 'off'}, self.auth())
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)['schedule']['start'], {'value': 'off', 'next': 0})

    def test_schedule_endpoint_rejects_bad_value(self):
        self.engine.config = {'owner': 'tok', 'config': '/tmp/config'}
        for value in ('24h', '24', 'x'):
            with self.subTest(value=value):
                code, _ = self.request('POST', '/api/schedule', {'kind': 'start', 'value': value}, self.auth())
                self.assertEqual(code, 400)

    def test_schedule_endpoint_needs_csrf(self):
        self.assertEqual(
            self.request('POST', '/api/schedule', {'kind': 'start', 'value': '3'},
                         {'X-TR-Session': self.token})[0], 403)

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
        with self.assertRaises(KeyboardInterrupt):
            forward_keeper(stub, sleep=fake_sleep)
        self.assertGreaterEqual(len(ticks), 3)

    def test_port_keeper_survives_errors(self):
        """端口巡检这条腿也不能因为一次异常就停摆，否则端口掉了没人补。"""
        from server import port_keeper

        ticks = []

        def fake_sleep(_seconds):
            ticks.append(1)
            if len(ticks) > 2:
                raise KeyboardInterrupt

        def boom():
            raise RuntimeError('Docker 抽风')

        stub = types.SimpleNamespace(keep_published_ports_alive=boom)
        with self.assertRaises(KeyboardInterrupt):
            port_keeper(stub, sleep=fake_sleep)
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
        with patch('engine.docker_api', return_value=(204, b'{}')) as api:
            missing = self.engine.ensure_published_ports()
        self.assertEqual(missing, [])
        self.assertFalse(self.engine.ports_state['repaired'])
        self.assertEqual(api.call_count, 0)

    def test_dev_mode_never_restarts(self):
        self.engine.dev = True
        self.write_net(tcp_listen=[9091], udp=[51413])
        with patch('engine.docker_api', return_value=(204, b'{}')) as api:
            self.engine.ensure_published_ports()
        self.assertEqual(api.call_count, 0)

    def test_running_watchdog_restarts_when_proxy_dies(self):
        """运行期 proxy 掉线也要补：启动时的自检管不到这一刻（实测 20 分钟内就掉了）。"""
        self.engine.config = {'enabled': True, 'owner': 'owner-token'}
        self.write_net(tcp_listen=[9091], udp=[51413])           # 51413/tcp 没了
        calls = []

        def fake_api(method, path, body=None, timeout=30):
            calls.append((method, path))
            if path.startswith('/containers/' + NAME + '/restart'):
                self.write_net(tcp_listen=[9091, 51413], udp=[51413])
            return 204, b'{}'

        with patch('engine.docker_api', side_effect=fake_api):
            missing = self.engine.keep_published_ports_alive(repair_interval=0)
        self.assertEqual(missing, '')
        self.assertEqual(calls, [('POST', '/containers/' + NAME + '/restart?t=15')])
        self.assertTrue(self.engine.ports_state['repaired'])

    def test_running_watchdog_leaves_stopped_service_alone(self):
        """用户停掉的服务不能被定时器拉起来。"""
        self.engine.config = {'enabled': False}
        self.write_net(tcp_listen=[], udp=[])
        with patch('engine.docker_api', return_value=(204, b'{}')) as api:
            self.assertEqual(self.engine.keep_published_ports_alive(repair_interval=0), '')
        self.assertEqual(api.call_count, 0)

    def test_running_watchdog_throttles_repairs(self):
        """修不好也不能每分钟重启一次容器，那会把下载打断。"""
        self.engine.config = {'enabled': True}
        self.write_net(tcp_listen=[9091], udp=[51413])
        with patch('engine.docker_api', return_value=(204, b'{}')) as api, \
                patch.object(self.engine, 'wait_published_ports', return_value=['51413/tcp']):
            self.engine.keep_published_ports_alive(repair_interval=600)
            self.engine.keep_published_ports_alive(repair_interval=600)
        self.assertEqual(api.call_count, 1)
        self.assertGreater(self.engine.last_port_repair, 0)

    def test_running_watchdog_yields_to_user_action(self):
        """手动启停进行中时，定时器不能同时去重启容器。"""
        self.engine.config = {'enabled': True}
        self.write_net(tcp_listen=[9091], udp=[51413])
        self.engine.lock.acquire()
        try:
            with patch('engine.docker_api', return_value=(204, b'{}')) as api:
                self.assertEqual(self.engine.keep_published_ports_alive(repair_interval=0), '')
            self.assertEqual(api.call_count, 0)
        finally:
            self.engine.lock.release()

    def test_running_watchdog_quiet_when_ports_are_published(self):
        """一切正常时不产生任何 Docker 调用。"""
        self.engine.config = {'enabled': True}
        self.write_net(tcp_listen=[9091, 51413], udp=[51413])
        with patch('engine.docker_api', return_value=(204, b'{}')) as api:
            self.assertEqual(self.engine.keep_published_ports_alive(repair_interval=0), '')
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


class ResourceLimitTests(unittest.TestCase):
    """内存上限 512 MiB → 2 GiB：太小会被内核 OOM 杀掉（见 MEMORY_LIMIT 的说明）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'root'
        self.root.mkdir()
        self.engine = Engine(Path(self.tmp.name) / 'data', self.root)

    def _config(self):
        return {'uid': 1, 'gid': 1, 'owner': 'tok', 'username': 'admin',
                'config': '/c', 'download': '/d', 'watch': '/w'}

    def test_container_config_asks_for_two_gib(self):
        cfg = container_config(self._config(), Path('/data'), 'Secret123!')
        self.assertEqual(MEMORY_LIMIT, 2 * 1024 ** 3)
        self.assertEqual(cfg['HostConfig']['Memory'], MEMORY_LIMIT)
        self.assertEqual(cfg['HostConfig']['MemorySwap'], MEMORY_LIMIT)
        self.assertEqual(cfg['HostConfig']['NanoCpus'], CPU_LIMIT)

    def test_old_memory_limit_is_detected_as_stale(self):
        ok = {'Memory': MEMORY_LIMIT, 'MemorySwap': MEMORY_LIMIT, 'NanoCpus': CPU_LIMIT}
        old = {'Memory': 512 * 1024 * 1024, 'MemorySwap': 512 * 1024 * 1024,
               'NanoCpus': CPU_LIMIT}
        cases = [
            ({'HostConfig': ok}, False),
            ({'HostConfig': old}, True),
            ({'HostConfig': {'Memory': MEMORY_LIMIT, 'MemorySwap': MEMORY_LIMIT,
                             'NanoCpus': 10 ** 9}}, True),
            ({'HostConfig': {}}, True),
            ({}, True),
        ]
        for item, expected in cases:
            with self.subTest(item=item):
                self.assertEqual(Engine.resources_stale(item), expected)

    def test_start_recreates_container_with_old_memory_limit(self):
        self.engine.config = {'owner': 'tok', 'username': 'admin', 'uid': 1, 'gid': 1,
                              'config': '/c', 'download': '/d', 'watch': '/w'}
        item = {'Config': {'Labels': {LABEL: 'tok'},
                           'Env': ['TRANSMISSION_WEB_HOME=' + WEBUI_HOME]},
                'HostConfig': {'Memory': 512 * 1024 * 1024, 'MemorySwap': 512 * 1024 * 1024,
                               'NanoCpus': CPU_LIMIT},
                'State': {'Running': True}}
        calls = []

        def fake_api(method, path, body=None, timeout=30):
            calls.append((method, path, body))
            return 200, b'{}'

        with patch.object(self.engine, 'check_directories'), \
                patch.object(self.engine, 'adopt_legacy_settings', return_value=False), \
                patch.object(self.engine, 'ensure_settings', return_value=False), \
                patch.object(self.engine, 'ensure_webui'), \
                patch.object(self.engine, 'owned', return_value=item), \
                patch.object(self.engine, 'pull'), \
                patch.object(self.engine, 'saved_password', return_value='Secret123!'), \
                patch.object(self.engine, 'ensure_published_ports'), \
                patch.object(self.engine, 'ensure_port_forward'), \
                patch('engine.docker_api', side_effect=fake_api), \
                patch('engine.tr_rpc_probe', return_value=True), \
                patch('engine.atomic_json'):
            self.engine.start()
        paths = [path for _, path, _ in calls]
        self.assertIn('/containers/' + NAME + '/stop?t=15', paths)
        self.assertIn('/containers/' + NAME + '?force=1&v=1', paths)      # DELETE
        self.assertIn('/containers/create?name=' + NAME, paths)
        create = next(body for _, path, body in calls if path.startswith('/containers/create'))
        self.assertEqual(create['HostConfig']['Memory'], MEMORY_LIMIT)
        # 必须先删再建，否则 create 会撞名
        self.assertLess(paths.index('/containers/' + NAME + '?force=1&v=1'),
                        paths.index('/containers/create?name=' + NAME))

    def test_start_keeps_container_with_matching_limits(self):
        self.engine.config = {'owner': 'tok', 'username': 'admin', 'uid': 1, 'gid': 1,
                              'config': '/c', 'download': '/d', 'watch': '/w'}
        item = {'Config': {'Labels': {LABEL: 'tok'},
                           'Env': ['TRANSMISSION_WEB_HOME=' + WEBUI_HOME]},
                'HostConfig': {'Memory': MEMORY_LIMIT, 'MemorySwap': MEMORY_LIMIT,
                               'NanoCpus': CPU_LIMIT, 'Mounts': []},
                'State': {'Running': True}}
        calls = []

        def fake_api(method, path, body=None, timeout=30):
            calls.append((method, path, body))
            return 200, b'{}'

        with patch.object(self.engine, 'check_directories'), \
                patch.object(self.engine, 'adopt_legacy_settings', return_value=False), \
                patch.object(self.engine, 'ensure_settings', return_value=False), \
                patch.object(self.engine, 'ensure_webui'), \
                patch.object(self.engine, 'owned', return_value=item), \
                patch.object(self.engine, 'ensure_published_ports'), \
                patch.object(self.engine, 'ensure_port_forward'), \
                patch('engine.docker_api', side_effect=fake_api), \
                patch('engine.tr_rpc_probe', return_value=True), \
                patch('engine.atomic_json'):
            self.engine.start()
        paths = [path for _, path, _ in calls]
        self.assertEqual(paths, [])                                       # 不该动容器
        self.assertTrue(self.engine.config['enabled'])                    # 只在内存里标记启用


class ScheduleTests(unittest.TestCase):
    """定时开启/关闭全部任务：off 或 '0'..'23'（钟点），每天到点执行一次。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'root'
        self.root.mkdir()
        self.engine = Engine(Path(self.tmp.name) / 'data', self.root)
        self.engine.config = {'owner': 'tok', 'username': 'admin'}

    @staticmethod
    def _moment():
        """测试用的固定时刻（用真实日期：Windows 上 1970 年前后 timestamp() 会报错）。"""
        return int(datetime.datetime(2026, 9, 28, 10, 30).timestamp())

    def test_options_are_off_plus_24_hours(self):
        self.assertEqual(len(SCHEDULE_VALUES), 25)
        self.assertEqual(SCHEDULE_VALUES[0], 'off')
        self.assertEqual(SCHEDULE_VALUES[1], '0')
        self.assertEqual(SCHEDULE_VALUES[-1], '23')

    def test_next_hour_is_today_or_tomorrow(self):
        moment = int(datetime.datetime(2026, 9, 28, 10, 30).timestamp())
        self.assertEqual(next_hour_epoch('23', moment),
                         int(datetime.datetime(2026, 9, 28, 23, 0).timestamp()))
        self.assertEqual(next_hour_epoch('3', moment),
                         int(datetime.datetime(2026, 9, 29, 3, 0).timestamp()))
        # 正好到点算明天，避免同一分钟内重复触发
        exact = int(datetime.datetime(2026, 9, 28, 3, 0).timestamp())
        self.assertEqual(next_hour_epoch('3', exact),
                         int(datetime.datetime(2026, 9, 29, 3, 0).timestamp()))

    def test_snapshot_defaults_to_off(self):
        self.assertEqual(self.engine.schedule_snapshot(),
                         {'start': {'value': 'off', 'next': 0}, 'stop': {'value': 'off', 'next': 0}})

    def test_enabling_sets_next_clock_time(self):
        moment = int(datetime.datetime(2026, 9, 28, 10, 30).timestamp())
        with patch('engine.time.time', return_value=moment), patch('engine.atomic_json'):
            snapshot = self.engine.set_schedule('start', '23')
            later = self.engine.set_schedule('stop', '3')
        self.assertEqual(snapshot['start']['value'], '23')
        self.assertEqual(snapshot['start']['next'], next_hour_epoch('23', moment))
        self.assertEqual(later['stop']['next'], next_hour_epoch('3', moment))
        self.assertEqual(self.engine.schedule_snapshot()['start']['value'], '23')

    def test_disabling_clears_next(self):
        with patch('engine.atomic_json'):
            self.engine.set_schedule('stop', '3')
            snapshot = self.engine.set_schedule('stop', 'off')
        self.assertEqual(snapshot['stop'], {'value': 'off', 'next': 0})

    def test_rejects_bad_input(self):
        for kind, value in (('start', '24h'), ('start', ''), ('start', '24'),
                            ('start', 'x'), ('both', '3'), ('', 'off')):
            with self.subTest(kind=kind, value=value), \
                    patch('engine.atomic_json'), self.assertRaises(Error):
                self.engine.set_schedule(kind, value)

    def test_requires_config(self):
        self.engine.config = None
        with self.assertRaises(Error):
            self.engine.set_schedule('start', '3')

    def test_runs_due_action_without_ids(self):
        moment = self._moment()
        self.engine.config['schedule'] = {'start': {'value': '3', 'next': moment - 60}}
        with patch.object(self.engine, 'saved_password', return_value='Secret123!'), \
                patch('engine.tr_call') as call, patch('engine.atomic_json'):
            done = self.engine.run_schedule_due(now=moment)
        self.assertEqual(done, ['start'])
        self.assertEqual(call.call_args.args[:3], ('torrent-start', 'admin', 'Secret123!'))
        self.assertEqual(len(call.call_args.args), 3)          # 不带 ids = 全部任务
        self.assertEqual(self.engine.config['schedule']['start']['next'],
                         next_hour_epoch('3', moment))

    def test_does_nothing_before_due(self):
        moment = self._moment()
        due = next_hour_epoch('3', moment)
        self.engine.config['schedule'] = {'stop': {'value': '3', 'next': due}}
        with patch('engine.tr_call') as call, patch('engine.atomic_json'):
            self.assertEqual(self.engine.run_schedule_due(now=due - 1), [])
        self.assertEqual(call.call_count, 0)

    def test_off_never_runs(self):
        self.engine.config['schedule'] = {'start': {'value': 'off', 'next': 0}}
        with patch('engine.tr_call') as call, patch('engine.atomic_json'):
            self.assertEqual(self.engine.run_schedule_due(now=self._moment()), [])
        self.assertEqual(call.call_count, 0)

    def test_legacy_interval_value_is_ignored(self):
        """"24h" 是上一版的写法，留在配置里也不该乱执行。"""
        self.engine.config['schedule'] = {'start': {'value': '24h', 'next': 1}}
        with patch('engine.tr_call') as call, patch('engine.atomic_json'):
            self.assertEqual(self.engine.run_schedule_due(now=self._moment()), [])
        self.assertEqual(call.call_count, 0)
        self.assertEqual(self.engine.schedule_snapshot()['start']['value'], 'off')

    def test_failure_still_reschedules(self):
        """服务没在跑时 RPC 会失败：只记录，并把下一次挪到明天同一钟点，别每分钟重试。"""
        moment = self._moment()
        self.engine.config['schedule'] = {'stop': {'value': '3', 'next': moment - 60}}
        with patch.object(self.engine, 'saved_password', return_value='Secret123!'), \
                patch('engine.tr_call', side_effect=Error('RPC 失败')), patch('engine.atomic_json'):
            done = self.engine.run_schedule_due(now=moment)
        self.assertEqual(done, [])
        self.assertEqual(self.engine.config['schedule']['stop']['next'],
                         next_hour_epoch('3', moment))

    def test_missing_credentials_skips(self):
        moment = self._moment()
        self.engine.config['schedule'] = {'start': {'value': '3', 'next': moment - 60}}
        with patch.object(self.engine, 'saved_password', return_value=''), patch('engine.tr_call') as call, \
                patch('engine.atomic_json'):
            self.assertEqual(self.engine.run_schedule_due(now=moment), [])
        self.assertEqual(call.call_count, 0)

    def test_snapshot_carries_schedule(self):
        self.engine.config['config'] = '/tmp/config'
        self.engine.config['schedule'] = {'start': {'value': '3', 'next': 42}}
        stopped = {'Config': {'Labels': {LABEL: 'tok'}}, 'State': {'Running': False}}
        with patch.object(self.engine, 'owned', return_value=stopped):
            data = self.engine.snapshot()
        self.assertEqual(data['schedule']['start'], {'value': '3', 'next': 42})


class ScheduleKeeperTests(unittest.TestCase):
    def test_keeper_survives_errors(self):
        from server import schedule_keeper

        ticks = []

        def fake_sleep(_seconds):
            ticks.append(1)
            if len(ticks) > 2:
                raise KeyboardInterrupt

        def boom():
            raise RuntimeError('transmission 抽风')

        stub = types.SimpleNamespace(run_schedule_due=boom)
        with self.assertRaises(KeyboardInterrupt):
            schedule_keeper(stub, sleep=fake_sleep)
        self.assertGreaterEqual(len(ticks), 3)


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
        with patch.object(upnp, '_udp_socket', return_value=sock):
            ok, detail, lease = upnp.natpmp_map('192.168.1.1', 51413, 51413, 'tcp')
        self.assertTrue(ok)
        self.assertIn('51413', detail)
        self.assertEqual(lease, 3600)
        self.assertEqual(sock.sent[0][1], ('192.168.1.1', 5351))
        self.assertEqual(sock.sent[0][0][:2], bytes([0, 2]))       # 版本 0，MAP TCP

    def test_natpmp_success_without_lease_field(self):
        reply = struct.pack('!BBHIHH', 0, 130, 0, 100, 51413, 51413)
        with patch.object(upnp, '_udp_socket', return_value=_FakeSocket([reply])):
            ok, _, lease = upnp.natpmp_map('192.168.1.1', 51413, 51413, 'tcp')
        self.assertTrue(ok)
        self.assertEqual(lease, 7200)                              # 应答没带租期就用请求值

    def test_natpmp_refused(self):
        reply = struct.pack('!BBHIHH', 0, 130, 3, 100, 51413, 0)
        with patch.object(upnp, '_udp_socket', return_value=_FakeSocket([reply])):
            ok, detail, lease = upnp.natpmp_map('192.168.1.1', 51413, 51413, 'tcp')
        self.assertFalse(ok)
        self.assertIn('网络故障', detail)
        self.assertEqual(lease, 0)

    def test_natpmp_timeout(self):
        with patch.object(upnp, '_udp_socket', return_value=_FakeSocket([])):
            ok, detail, lease = upnp.natpmp_map('192.168.1.1', 51413, 51413, 'udp')
        self.assertFalse(ok)
        self.assertIn('没有响应', detail)
        self.assertEqual(lease, 0)

    def test_natpmp_delete_success(self):
        reply = struct.pack('!BBHIHH', 0, 132, 0, 100, 51413, 51413)
        sock = _FakeSocket([reply])
        with patch.object(upnp, '_udp_socket', return_value=sock):
            ok, detail = upnp.natpmp_delete('192.168.1.1', 51413, 51413, 'tcp')
        self.assertTrue(ok)
        self.assertIn('已删除', detail)
        self.assertEqual(sock.sent[0][0][:2], bytes([0, 4]))       # opcode 4 = 删 TCP

    def test_natpmp_delete_refused(self):
        reply = struct.pack('!BBHIHH', 0, 131, 3, 100, 51413, 0)
        with patch.object(upnp, '_udp_socket', return_value=_FakeSocket([reply])):
            ok, detail = upnp.natpmp_delete('192.168.1.1', 51413, 51413, 'udp')
        self.assertFalse(ok)
        self.assertIn('网络故障', detail)

    def test_remove_forward_uses_recorded_control_point(self):
        seen = []

        def fake_request(url, data=None, headers=None, timeout=None):
            seen.append((url, (headers or {}).get('SOAPAction', '')))
            return 200, '<ok/>'

        with patch.object(upnp, '_request', side_effect=fake_request):
            result = upnp.remove_forward(51413, 51413, method='UPnP',
                                         control='http://192.168.1.1:41795/ctl/IPConn',
                                         service_type='urn:x:WANIPConnection:2')
        self.assertTrue(result['ok'])
        self.assertIn('已移除', result['detail'])
        actions = [action for _, action in seen]
        self.assertTrue(all('DeletePortMapping' in action for action in actions))
        self.assertEqual(len(actions), 2)                          # TCP + UDP
        # 记下了控制地址就不该再去 SSDP 发现一次
        self.assertEqual({url for url, _ in seen}, {'http://192.168.1.1:41795/ctl/IPConn'})

    def test_remove_forward_without_upnp_falls_back_to_natpmp(self):
        with patch.object(upnp, 'ssdp_location', side_effect=upnp.UpnpError('没响应')), \
                patch.object(upnp, 'gateway', return_value='192.168.1.1'), \
                patch.object(upnp, 'natpmp_delete', return_value=(True, '已删除')):
            result = upnp.remove_forward(51413, 51413)
        self.assertTrue(result['ok'])
        self.assertIn('NAT-PMP', result['detail'])

    def test_remove_forward_rediscovers_stale_control_point(self):
        """路由器重启会换 UPnP 临时端口（实测 41795 → 33299），存下来的地址会失效。"""
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
            result = upnp.remove_forward(51413, 51413, method='UPnP',
                                         control='http://192.168.1.1:41795/ctl/IPConn',
                                         service_type='urn:x:WANIPConnection:2')
        self.assertTrue(result['ok'])
        self.assertIn('http://192.168.1.1:33299/ctl/IPConn', calls)
        # 重新发现成功后不该把第一次的失败也写进结果里
        self.assertNotIn('UPnP：', result['detail'])
        self.assertIn('UPnP TCP', result['detail'])
        self.assertIn('UPnP UDP', result['detail'])

    def test_delete_mapping_tolerates_missing_entry(self):
        fault = ('<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
                 '<s:Fault><detail><UPnPError xmlns="urn:schemas-upnp-org:control-1-0">'
                 '<errorCode>714</errorCode></UPnPError></detail></s:Fault></s:Body></s:Envelope>')
        with patch.object(upnp, '_request', return_value=(500, fault)):
            self.assertFalse(upnp.delete_mapping('http://192.168.1.1/ctl',
                                                 'urn:x:WANIPConnection:2', 51413, 'TCP'))

    def test_remove_forward_reports_failure(self):
        with patch.object(upnp, 'ssdp_location', side_effect=upnp.UpnpError('没响应')), \
                patch.object(upnp, 'gateway', return_value=''), \
                patch.object(upnp, 'natpmp_delete', return_value=(False, '失败')):
            result = upnp.remove_forward(51413, 51413)
        self.assertFalse(result['ok'])
        self.assertIn('没响应', result['detail'])

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
        self.engine.config = {'enabled': True}
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

    def test_removed_state_is_not_re_added(self):
        self.engine.forward_state = {'ok': False, 'removed': True, 'at': 1000,
                                     'detail': '已移除路由器映射'}
        self.assertFalse(self.engine.forward_due(now=1000 + 86400))

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
        self.assertEqual(remove.call_args.kwargs['method'], 'UPnP')
        self.assertIn('/stop', call.call_args.args[1])              # 容器也停了
        snapshot = self.engine.forward_snapshot()
        self.assertTrue(snapshot['removed'])
        self.assertFalse(snapshot['ok'])

    def test_remove_is_skipped_when_never_mapped(self):
        with patch.object(engine.upnp, 'remove_forward') as remove:
            result = self.engine.remove_port_forward()
        self.assertFalse(result['ok'])
        self.assertEqual(remove.call_count, 0)

    def test_keep_forward_alive_skips_when_service_disabled(self):
        self.engine.config = {'enabled': False}
        self.engine.forward_state = {'ok': True, 'method': 'NAT-PMP', 'lease': 60,
                                     'at': int(time.time()) - 120}
        with patch.object(self.engine, 'ensure_port_forward') as call:
            self.assertFalse(self.engine.keep_forward_alive())
        self.assertEqual(call.call_count, 0)

    def test_forward_state_survives_a_new_process(self):
        """停止服务时 systemd 的 ExecStopPost 是另一个进程，得靠这个文件才知道删哪条。"""
        self.engine.forward_state = {'ok': True, 'method': 'UPnP', 'at': 1000,
                                     'control': 'http://192.168.1.1:41795/ctl/IPConn',
                                     'service': 'urn:x:WANIPConnection:2',
                                     'protocols': ['TCP', 'UDP'],
                                     'detail': 'UPnP 映射成功'}
        self.engine.save_forward()
        fresh = Engine(Path(self.engine.data), Path(self.engine.root))
        self.assertTrue(fresh.forward_snapshot()['ok'])
        self.assertEqual(fresh.forward_state['control'], 'http://192.168.1.1:41795/ctl/IPConn')
        self.assertEqual(fresh.forward_state['method'], 'UPnP')

    def test_removal_works_from_a_fresh_process(self):
        self.engine.forward_state = {'ok': True, 'method': 'UPnP', 'at': 1000,
                                     'control': 'http://192.168.1.1:41795/ctl/IPConn',
                                     'service': 'urn:x:WANIPConnection:2',
                                     'protocols': ['TCP', 'UDP']}
        self.engine.save_forward()
        fresh = Engine(Path(self.engine.data), Path(self.engine.root))
        with patch.object(engine.upnp, 'remove_forward',
                          return_value={'ok': True, 'detail': '已移除路由器映射'}) as remove:
            result = fresh.remove_port_forward()
        self.assertTrue(result['ok'])
        self.assertEqual(remove.call_count, 1)
        self.assertEqual(remove.call_args.kwargs['control'], 'http://192.168.1.1:41795/ctl/IPConn')

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
        for name in ('forwardDot', 'forwardPort'):
            with self.subTest(name=name):
                self.assertIn('id="%s"' % name, html)
        for name in ('app.js', 'app.bundle.js'):
            with self.subTest(name=name):
                script = (self.web / name).read_text(encoding='utf-8')
                self.assertIn('function renderForward(current)', script)
                self.assertIn("$('#forwardPort')", script)
                self.assertIn('未能转发', script)

    def test_status_is_shown_as_a_dot_not_text(self):
        """测试端口 / 映射端口前面的文字状态换成绿/红圆点，两个按钮挤在一行。"""
        html = (self.web / 'index.html').read_text(encoding='utf-8')
        self.assertIn('class="check-row"', html)
        self.assertNotIn('id="portState"', html)
        self.assertNotIn('id="forwardState"', html)
        row = html.split('class="check-row"', 1)[1].split('</div>', 1)[0]
        for name in ('portDot', 'testPort', 'forwardDot', 'forwardPort'):
            with self.subTest(name=name):
                self.assertIn('id="%s"' % name, row)
        # 映射端口排在测试端口前面
        self.assertLess(row.index('id="forwardPort"'), row.index('id="testPort"'))
        for name in ('app.js', 'app.bundle.js'):
            with self.subTest(name=name):
                script = (self.web / name).read_text(encoding='utf-8')
                self.assertIn('function setDot(', script)
                self.assertIn("state = 'bad'", script)
                self.assertIn('toast(portText(state))', script)
                # 端口测试是后台跑的，必须等 testedAt 变了再报结果
                self.assertIn('attempt < 20', script)
                self.assertIn('测试超时', script)

    def test_open_console_sits_next_to_port_buttons(self):
        """「打开控制台」移到测试端口后面；「复制」和地址栏同一行。"""
        html = (self.web / 'index.html').read_text(encoding='utf-8')
        row = html.split('class="check-row"', 1)[1].split('</div>', 1)[0]
        self.assertIn('id="openConsole"', row)
        self.assertLess(row.index('id="testPort"'), row.index('id="openConsole"'))
        # 配色沿用蓝色，且和两个状态按钮同高、靠右对齐
        self.assertIn('id="openConsole" type="button" class="btn primary"', row)
        address = html.split('class="address-row"', 1)[1].split('</div>', 1)[0]
        self.assertIn('id="copyAddress"', address)
        self.assertNotIn('openConsole', address)
        css = (self.web / 'styles.css').read_text(encoding='utf-8')
        # 手机上不再把地址挤成独占一行
        self.assertNotIn('.address-row code { flex: 1 1 100%; }', css)
        self.assertIn('.check-row .btn { min-height: 40px;', css)
        self.assertIn('.check-row > .btn { margin-left: auto; }', css)
        for name in ('app.js', 'app.bundle.js'):
            with self.subTest(name=name):
                script = (self.web / name).read_text(encoding='utf-8')
                self.assertIn("consoleEntry.disabled = !live", script)

    def test_console_has_start_all_and_pause_all(self):
        html = (self.web / 'index.html').read_text(encoding='utf-8')
        head = html.split('class="console-head"', 1)[1].split('</div>', 1)[0]
        for name in ('backToStatus', 'startAll', 'pauseAll'):
            with self.subTest(name=name):
                self.assertIn('id="%s"' % name, head)
        self.assertIn('全部开始', html)
        self.assertIn('全部暂停', html)
        for name in ('app.js', 'app.bundle.js'):
            with self.subTest(name=name):
                script = (self.web / name).read_text(encoding='utf-8')
                self.assertIn("api('all-start'", script)
                self.assertIn("api('all-stop'", script)

    def test_task_progress_colors_and_active_first(self):
        """进度条按状态配色（暂停灰/下载中蓝/完成绿/报错红），活动中的任务排最前面。"""
        css = (self.web / 'styles.css').read_text(encoding='utf-8')
        for state, color in (('paused', '#9aa4b2'), ('active', '#2f7be0'),
                             ('done', 'var(--green)'), ('error', '#e5484d')):
            with self.subTest(state=state):
                self.assertIn('progress.p-%s::-webkit-progress-value' % state, css)
                self.assertIn(color, css)
        for name in ('app.js', 'app.bundle.js'):
            with self.subTest(name=name):
                script = (self.web / name).read_text(encoding='utf-8')
                self.assertIn('function progressState(item)', script)
                self.assertIn("if (item.error) return 'error';", script)
                self.assertIn("if (isStopped(Number(item.status))) return 'paused';", script)
                self.assertIn("if (Number(item.progress || 0) >= 1) return 'done';", script)
                self.assertIn("return 'active';", script)
                # 判定顺序：报错 > 暂停 > 完成 > 下载中（下完再停止的要显示灰色）
                body = script.split('function progressState(item)', 1)[1].split('return \'active\';', 1)[0]
                self.assertLess(body.index('item.error'), body.index('isStopped'))
                self.assertLess(body.index('isStopped'), body.index('progress || 0) >= 1'))
                self.assertIn('class="p-${progressState(item)}"', script)
                # 活动中的排最前面，其余保持服务端顺序（两段拼接）
                self.assertIn('const isActive = (item) => !isStopped(Number(item.status));', script)
                self.assertIn('.concat(visible.filter(item => !isActive(item)))', script)
                self.assertNotIn("$('#tasks').innerHTML = visible.map", script)

    def test_console_shows_uploaded_beside_downloaded(self):
        """控制台「本次已下载」后面再加一个「本次已上传」，四格并排。"""
        html = (self.web / 'index.html').read_text(encoding='utf-8')
        card = html.split('class="card stats"', 1)[1].split('</section>', 1)[0]
        self.assertIn('<small>本次已下载</small>', card)
        self.assertIn('<small>本次已上传</small>', card)
        for name in ('downloaded', 'uploaded'):
            with self.subTest(name=name):
                self.assertIn('id="%s"' % name, card)
        # 上传紧跟在下载后面
        self.assertLess(card.index('id="downloaded"'), card.index('id="uploaded"'))
        css = (self.web / 'styles.css').read_text(encoding='utf-8')
        self.assertIn('grid-template-columns: repeat(4, minmax(0, 1fr));', css)
        for name in ('app.js', 'app.bundle.js'):
            with self.subTest(name=name):
                script = (self.web / name).read_text(encoding='utf-8')
                self.assertIn("$('#downloaded').textContent", script)
                self.assertIn("$('#uploaded').textContent = bytes(t.uploaded);", script)

    def test_page_has_schedule_controls(self):
        """第二个卡片底部原来的「WebUI 9091 · BT 51413」换成两个定时选择框。"""
        html = (self.web / 'index.html').read_text(encoding='utf-8')
        self.assertNotIn('peerPortHint', html)
        self.assertNotIn('24h', html)                     # 旧的"24 小时"选项不该还在
        for name in ('scheduleStart', 'scheduleStop'):
            with self.subTest(name=name):
                self.assertIn('id="%s"' % name, html)
        self.assertIn('定时开启全部任务', html)
        self.assertIn('定时关闭全部任务', html)
        for name in ('app.js', 'app.bundle.js'):
            with self.subTest(name=name):
                script = (self.web / name).read_text(encoding='utf-8')
                self.assertIn('function renderSchedule(current)', script)
                self.assertIn('function fillScheduleOptions(select)', script)
                self.assertIn('hour < 24', script)        # 0 点…23 点共 24 项 + 关闭
                self.assertIn("off.textContent = '关闭'", script)
                self.assertIn("saveSchedule('start'", script)
                self.assertIn("saveSchedule('stop'", script)

    def test_page_shows_glance_stats(self):
        html = (self.web / 'index.html').read_text(encoding='utf-8')
        for name in ('statusStats', 'statsDown', 'statsUp', 'statsSeeding', 'statsDownloading'):
            with self.subTest(name=name):
                self.assertIn('id="%s"' % name, html)
        # 「本次运行」已按需求去掉
        self.assertNotIn('statsSessionDown', html)
        self.assertNotIn('本次运行', html)
        for name in ('app.js', 'app.bundle.js'):
            with self.subTest(name=name):
                script = (self.web / name).read_text(encoding='utf-8')
                self.assertIn('function renderStats(current)', script)
                self.assertNotIn('statsSessionDown', script)

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
        """窄屏下状态卡/地址行的按钮不能被拉满整行，否则会变成一条很长的按钮。"""
        css = (self.web / 'styles.css').read_text(encoding='utf-8')
        marker = '@media (max-width: 430px)'
        start = css.find(marker)
        self.assertGreaterEqual(start, 0)
        narrow = css[start:start + 500]
        self.assertNotIn('width: 100%', narrow)
        self.assertIn('.status-actions', narrow)
        self.assertIn('.status-port', narrow)
        # 地址和「复制」保持一行：地址可伸缩但不再独占整行
        self.assertIn('.address-row code', narrow)
        self.assertIn('flex: 1 1 auto', narrow)
        self.assertNotIn('flex: 1 1 100%', narrow)
        # 三个按钮（映射/测试/控制台）在窄屏下也压在一行
        self.assertIn('.check-row', narrow)

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

    def test_ui_switches_storage_roots_and_shows_absolute_paths(self):
        """目录选择弹窗能切换存储位置，表单与状态卡片都显示完整绝对路径。"""
        html = (self.web / 'index.html').read_text(encoding='utf-8')
        self.assertIn('id="browseRoots"', html)
        # 目录输入框改成 textarea：长绝对路径在 input 里换不了行
        for field in ('download', 'config', 'watch'):
            with self.subTest(field=field):
                self.assertIn('textarea name="%s" id="%sPath"' % (field, field), html)
        for name in ('app.js', 'app.bundle.js'):
            with self.subTest(name=name):
                script = (self.web / name).read_text(encoding='utf-8')
                self.assertIn('function renderRoots()', script)
                self.assertIn('data-root="${index}"', script)
                self.assertIn("api('browse?root=' + browseRoot + '&path='", script)
                self.assertIn("value = absolutePath(browsePath)", script)
                self.assertIn('function locateValue(value)', script)
                self.assertIn('current.download_abs', script)
                self.assertIn("node.title = value || ''", script)
        css = (self.web / 'styles.css').read_text(encoding='utf-8')
        self.assertIn('.roots button.active', css)
        self.assertIn('.field-row textarea', css)

    def test_ui_has_reconfigure_and_reset_entry_points(self):
        """配置完成后要能改目录 / 重新初始化，两者都要确认并写清后果。"""
        html = (self.web / 'index.html').read_text(encoding='utf-8')
        for name in ('reconfigure', 'reset', 'reconfigureDialog', 'resetDialog', 'reconfigureForm',
                     'reDownloadPath', 'reConfigPath', 'reWatchPath', 'currentDirectories',
                     'confirmReset'):
            with self.subTest(name=name):
                self.assertIn('id="%s"' % name, html)
        # 两个入口都在服务卡片里，改目录排在重新初始化前面
        card = html.split('id="serviceActions"', 1)[1].split('</section>', 1)[0]
        for name in ('reconfigure', 'reset'):
            with self.subTest(entry=name):
                self.assertIn('id="%s"' % name, card)
        self.assertLess(card.index('id="reconfigure"'), card.index('id="reset"'))
        # 后果必须写清楚：只换位置 + 重建容器、原数据不删；重新初始化不动用户目录
        self.assertIn('不会被删除或移动', html)
        self.assertIn('不受影响', html)
        for name in ('app.js', 'app.bundle.js'):
            with self.subTest(name=name):
                script = (self.web / name).read_text(encoding='utf-8')
                self.assertIn("api('service/reconfigure'", script)
                self.assertIn("api('service/reset'", script)
                self.assertIn('{ confirm: true }', script)
                self.assertIn('reDownloadPath', script)
        css = (self.web / 'styles.css').read_text(encoding='utf-8')
        self.assertIn('.btn.danger', css)
        self.assertIn('.setting-row', css)

    def test_ui_guides_the_user_when_the_credential_file_is_missing(self):
        """凭据缺失（配置还在）时要指到「修改目录」重设密码，并说明凭据只归档不删除。"""
        html = (self.web / 'index.html').read_text(encoding='utf-8')
        self.assertIn('id="credentialWarning"', html)
        self.assertIn('credential.json.bak-', html)        # 重置弹窗写明凭据会被归档
        for name in ('app.js', 'app.bundle.js'):
            with self.subTest(name=name):
                script = (self.web / name).read_text(encoding='utf-8')
                self.assertIn('function renderCredentialWarning(current)', script)
                self.assertIn('current.credentialMissing', script)
                self.assertIn('凭据缺失，必须设置新的 WebUI 密码', script)   # 缺失时密码必填
                self.assertIn('password.required = missing', script)

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
