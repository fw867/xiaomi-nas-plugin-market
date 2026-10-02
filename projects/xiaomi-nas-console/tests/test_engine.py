#!/usr/bin/env python3
"""只读采集层的单元测试（纯解析函数，不需要 NAS）。

在仓库里执行：
    cd projects/xiaomi-nas-console && python3 -m unittest discover -s tests
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import engine  # noqa: E402


class CpuTests(unittest.TestCase):
    def test_parse_cpu_times_and_percent(self) -> None:
        first = engine.parse_cpu_times('cpu  100 0 100 800 0 0 0 0 0 0\ncpu0 1 0 1 8 0 0 0 0 0 0\n')
        second = engine.parse_cpu_times('cpu  150 0 150 900 0 0 0 0 0 0\n')
        self.assertEqual(first, {'total': 1000, 'idle': 800})
        self.assertEqual(second, {'total': 1200, 'idle': 900})
        # 总增量 200，其中空闲增量 100 → 50%
        self.assertEqual(engine.cpu_percent(first, second), 50.0)

    def test_cpu_percent_handles_missing(self) -> None:
        self.assertIsNone(engine.cpu_percent(None, {'total': 1, 'idle': 1}))
        self.assertIsNone(engine.cpu_percent({'total': 5, 'idle': 1}, {'total': 5, 'idle': 1}))

    def test_iowait_counts_as_idle(self) -> None:
        first = {'total': 1000, 'idle': 900}
        second = {'total': 1200, 'idle': 1100}
        self.assertEqual(engine.cpu_percent(first, second), 0.0)


class MemTests(unittest.TestCase):
    MEMINFO = (
        'MemTotal:        3906472 kB\n'
        'MemFree:         1982116 kB\n'
        'MemAvailable:    2405448 kB\n'
        'Buffers:           20000 kB\n'
        'Cached:          1000000 kB\n'
        'SReclaimable:      50000 kB\n'
        'Shmem:             10000 kB\n'
        'SwapTotal:             0 kB\n'
        'SwapFree:              0 kB\n'
    )

    def test_mem_summary(self) -> None:
        values = engine.parse_meminfo(self.MEMINFO)
        self.assertEqual(values['MemTotal'], 3906472 * 1024)
        summary = engine.mem_summary(values)
        self.assertEqual(summary['total'], 3906472 * 1024)
        self.assertEqual(summary['used'], (3906472 - 2405448) * 1024)
        self.assertEqual(summary['cached'], (1000000 + 50000 - 10000) * 1024)
        self.assertEqual(summary['swap_total'], 0)


class NetDiskTests(unittest.TestCase):
    NETDEV = (
        'Inter-|   Receive                                                |  Transmit\n'
        ' face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop\n'
        '    lo: 3966618603 19856039 0 0 0 0 0 0 3966618603 19856039 0 0 0\n'
        'enu1u3: 3462903318 31671848 0 0 0 0 0 0 55207456142 49531232 0 0 0\n'
        'docker0: 49549246767 23705000 0 0 0 0 0 0 1308891633 14552688 0 0 0\n'
    )

    DISKSTATS = (
        '   8       0 sda 12345 0 500000 1000 6789 0 250000 500 0 0 0 0 0 0\n'
        '   8       1 sda1 100 0 4000 10 50 0 2000 5 0 0 0 0 0 0\n'
        '   9     127 md127 10 0 4000 1 5 0 2000 1 0 0 0 0 0 0\n'
        ' 179       0 mmcblk0 1 0 8 0 2 0 16 0 0 0 0 0 0 0\n'
        ' 252       0 dm-0 7 0 7 0 7 0 7 0 0 0 0 0 0 0\n'
    )

    def test_parse_net_dev_skips_loopback(self) -> None:
        counters = engine.parse_net_dev(self.NETDEV)
        self.assertNotIn('lo', counters)
        self.assertEqual(counters['enu1u3'], (3462903318, 55207456142))

    def test_parse_diskstats_keeps_partitions_and_arrays(self) -> None:
        counters = engine.parse_diskstats(self.DISKSTATS)
        self.assertEqual(counters['sda'], (500000, 250000))
        self.assertEqual(counters['sda1'], (4000, 2000))
        self.assertEqual(counters['md127'], (4000, 2000))
        self.assertEqual(counters['mmcblk0'], (8, 16))
        self.assertEqual(counters['dm-0'], (7, 7))

    def test_rates_between_snapshots(self) -> None:
        first = {'sda': (0, 0), 'sdb': (0, 0)}
        second = {'sda': (2048, 1024), 'sdb': (0, 0)}
        result = engine.rates(first, second, 2.0, scale=512.0)
        self.assertEqual(result['sda']['in'], 524288.0)     # 2048 扇区 × 512 / 2 秒
        self.assertEqual(result['sda']['out'], 262144.0)
        self.assertEqual(result['sdb']['in'], 0.0)

    def test_rates_needs_elapsed(self) -> None:
        self.assertEqual(engine.rates({'sda': (0, 0)}, {'sda': (1, 1)}, 0), {})


class MountTests(unittest.TestCase):
    MOUNTS = (
        '/dev/mmcblk0p11 /log ext4 rw,relatime 0 0\n'
        '/dev/md127 /nas/sys btrfs rw,relatime,space_cache=v2 0 0\n'
        '/dev/sda2 /nas/mnt/pa0 btrfs rw,relatime 0 0\n'
        'tmpfs /tmp tmpfs rw,nosuid,nodev 0 0\n'
        'proc /proc proc rw 0 0\n'
        '/dev/root / erofs ro,relatime 0 0\n'
        'overlay /etc overlay rw,relatime 0 0\n'
        'cfs /nas/pool0 fuse.cfs rw 0 0\n'
        '/dev/sdb1 /mnt/with\\040space btrfs rw 0 0\n'
    )

    def test_only_real_storage_survives(self) -> None:
        rows = engine.parse_mounts(self.MOUNTS)
        points = [row['point'] for row in rows]
        self.assertIn('/log', points)
        self.assertIn('/nas/sys', points)
        self.assertIn('/mnt/with space', points)
        for excluded in ('/tmp', '/proc', '/etc', '/nas/pool0'):
            self.assertNotIn(excluded, points)

    def test_mount_options_are_kept(self) -> None:
        rows = {row['point']: row for row in engine.parse_mounts(self.MOUNTS)}
        self.assertTrue(rows['/log']['readonly'] is False)
        self.assertEqual(rows['/nas/sys']['fstype'], 'btrfs')

    def test_own_bind_mount_is_ignored(self) -> None:
        # systemd 的 ReadWritePaths= 会在服务的 mount namespace 里造一个 bind mount
        self.assertTrue(engine.is_own_mount(engine.OWN_MOUNT))
        self.assertTrue(engine.is_own_mount(engine.OWN_MOUNT + '/current'))
        self.assertFalse(engine.is_own_mount('/nas/sys'))
        self.assertFalse(engine.is_own_mount(engine.OWN_MOUNT + '-other'))


class MdstatTests(unittest.TestCase):
    MDSTAT = (
        'Personalities : [raid1]\n'
        'md127 : active raid1 sdb1[1] sda1[0]\n'
        '      19535134 blocks super 1.2 [2/2] [UU]\n'
        '\n'
        'unused devices: <none>\n'
    )

    def test_parse_mdstat(self) -> None:
        arrays = engine.parse_mdstat(self.MDSTAT)
        self.assertEqual(len(arrays), 1)
        array = arrays[0]
        self.assertEqual(array['name'], 'md127')
        self.assertEqual(array['level'], 'raid1')
        self.assertEqual(array['state'], 'active')
        self.assertEqual(sorted(array['members']), ['sda1', 'sdb1'])
        self.assertEqual(array['blocks'], 19535134)
        self.assertEqual(array['health'], '2/2 [UU]')

    def test_parse_mdstat_resync(self) -> None:
        arrays = engine.parse_mdstat('md0 : active raid1 sda1[0] sdb1[1]\n'
                                     '      1000 blocks [2/1] [U_]\n'
                                     '      [>....................]  recovery =  1.0% (10/900)\n')
        self.assertIn('recovery', arrays[0]['sync'])


class SystemdTests(unittest.TestCase):
    UNITS = (
        '  album.service      loaded active running album daemon\n'
        '  hdidle.service     loaded active running minas standby disk\n'
        '  systemd-journald.service loaded active running Journal Service\n'
        '  not-a-service.target loaded active active some target\n'
    )

    def test_parse_systemctl_units(self) -> None:
        units = engine.parse_systemctl_units(self.UNITS)
        self.assertEqual(len(units), 3)
        self.assertEqual(units[0]['unit'], 'album.service')
        self.assertEqual(units[0]['sub'], 'running')
        self.assertEqual(units[0]['description'], 'album daemon')


class CrontabTests(unittest.TestCase):
    CRON = (
        '31 11 * * * systemctl kill --kill-who=main -s USR1 miio_fcgi #@miio_fcgi\n'
        '0 2-22/4 * * * nas call dsync 1 #@dsync\n'
        '0 15 */3 * * nas storage smart all short 1 #@nasadm\n'
        '# 注释行\n'
        '*/5 * * * * echo hi\n'
    )

    def test_only_storage_tasks(self) -> None:
        rows = engine.parse_crontab(self.CRON)
        commands = [row['command'] for row in rows]
        self.assertEqual(len(rows), 2)
        self.assertTrue(any('smart all short' in item for item in commands))
        self.assertTrue(any('dsync' in item for item in commands))
        self.assertEqual(rows[0]['schedule'], '0 2-22/4 * * *')
        self.assertEqual(rows[0]['tag'], 'dsync')


class HdidleTests(unittest.TestCase):
    def test_timeout_from_dropin(self) -> None:
        drop_in = ("[Service]\nExecStart=\nExecStart=/bin/sh -c 'exec /usr/bin/hdidle -n -i 2700'\n")
        self.assertEqual(engine.parse_hdidle_timeout(drop_in), 2700)

    def test_timeout_from_official_unit(self) -> None:
        unit = "ExecStart=/bin/sh -c '... then exec /usr/bin/hdidle -n -i 1800; fi'\n"
        self.assertEqual(engine.parse_hdidle_timeout(unit), 1800)
        self.assertIsNone(engine.parse_hdidle_timeout('ExecStart=/usr/bin/hdidle -n'))


class SmartTests(unittest.TestCase):
    PAYLOAD = {
        'model_name': 'TOSHIBA MG08ACA14TE',
        'serial_number': '71N0A0BQFRVH',
        'firmware_version': '4303',
        'rotation_rate': 7200,
        'user_capacity': {'bytes': 14000519643136},
        'smart_status': {'passed': True},
        'temperature': {'current': 38},
        'ata_smart_attributes': {
            'table': [
                {'id': 9, 'name': 'Power_On_Hours', 'value': 99, 'worst': 99, 'thresh': 0,
                 'raw': {'value': 12345}},
                {'id': 5, 'name': 'Reallocated_Sector_Ct', 'value': 100, 'worst': 100, 'thresh': 10,
                 'raw': {'value': 0}},
                # Toshiba 的 194 号属性 raw 是打包值，必须被忽略
                {'id': 194, 'name': 'Temperature_Celsius', 'value': 100, 'worst': 100, 'thresh': 0,
                 'raw': {'value': 201864052776, 'string': '38 Min/Max 25/45'}},
            ]
        },
    }

    def test_parse_smart_json(self) -> None:
        parsed = engine.parse_smart_json(self.PAYLOAD)
        self.assertEqual(parsed['model'], 'TOSHIBA MG08ACA14TE')
        self.assertEqual(parsed['health'], 'PASSED')
        self.assertEqual(parsed['power_on_hours'], 12345)
        self.assertEqual(parsed['reallocated'], 0)
        self.assertEqual(parsed['temperature'], 38)
        self.assertEqual(len(parsed['attributes']), 3)

    def test_plausible_temperature(self) -> None:
        self.assertEqual(engine.plausible_temperature(38), 38)
        self.assertEqual(engine.plausible_temperature('38 Min/Max 25/45'), 38)
        self.assertIsNone(engine.plausible_temperature(201864052776))
        self.assertIsNone(engine.plausible_temperature(None))

    def test_parse_smart_text_fallback(self) -> None:
        text = (
            'Device Model:     TOSHIBA MG08ACA14TE\n'
            'Serial Number:    71N0A0BQFRVH\n'
            'SMART overall-health self-assessment test result: PASSED\n'
            '  9 Power_On_Hours          0x0032   099   099   000    Old_age   Always       -       12345\n'
            '  5 Reallocated_Sector_Ct   0x0033   100   100   010    Pre-fail  Always       -       0\n'
            '194 Temperature_Celsius     0x0022   100   100   000    Old_age   Always       -       38\n'
        )
        parsed = engine.parse_smart_text(text)
        self.assertEqual(parsed['health'], 'PASSED')
        self.assertEqual(parsed['power_on_hours'], '12345')
        self.assertEqual(parsed['reallocated'], '0')
        self.assertEqual(parsed['temperature'], 38)
        self.assertEqual(parsed['model'], 'TOSHIBA MG08ACA14TE')

    def test_parse_container_stats(self) -> None:
        payload = {
            'cpu_stats': {'cpu_usage': {'total_usage': 300}, 'system_cpu_usage': 2000,
                          'online_cpus': 4},
            'precpu_stats': {'cpu_usage': {'total_usage': 100}, 'system_cpu_usage': 1000},
            'memory_stats': {'usage': 104857600, 'limit': 1073741824},
            'networks': {'eth0': {'rx_bytes': 1000, 'tx_bytes': 2000}},
            'blkio_stats': {'io_service_bytes_recursive': [
                {'op': 'Read', 'value': 4096}, {'op': 'Write', 'value': 8192}]},
            'pids_stats': {'current': 12},
        }
        stats = engine.parse_container_stats(payload)
        self.assertEqual(stats['cpu'], 80.0)      # (200/1000) × 4 核
        self.assertEqual(stats['mem'], 104857600)
        self.assertEqual(stats['net_rx'], 1000)
        self.assertEqual(stats['block_write'], 8192)
        self.assertEqual(stats['pids'], 12)


class TokenTests(unittest.TestCase):
    def test_load_admin_token_reads_file(self) -> None:
        import server
        import tempfile

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'admin-token'
            path.write_text('deadbeef\n', encoding='utf-8')
            self.assertEqual(server.load_admin_token(path, dev=True), 'deadbeef')

    def test_load_admin_token_creates_missing_file(self) -> None:
        import server
        import tempfile

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'nested' / 'admin-token'
            token = server.load_admin_token(path, dev=True)
            self.assertEqual(len(token), 32)
            self.assertEqual(path.read_text(encoding='utf-8').strip(), token)


class RegistryShapeTests(unittest.TestCase):
    def test_plugin_meta_matches_registration(self) -> None:
        project = Path(__file__).resolve().parent.parent
        meta = json.loads((project / 'deploy' / 'plugin-meta.json').read_text(encoding='utf-8'))
        self.assertEqual(meta['plugin'], 'nasconsole')
        self.assertEqual(meta['service'], 'xiaomi-nas-console.service')
        self.assertIsInstance(meta['id'], int)

    def test_version_file(self) -> None:
        project = Path(__file__).resolve().parent.parent
        version = (project / 'VERSION').read_text(encoding='utf-8').strip()
        self.assertRegex(version, r'^\d+\.\d+\.\d+$')
        self.assertEqual(version, engine.VERSION)

    def test_version_is_read_not_hardcoded(self) -> None:
        """版本号必须来自安装文件：改 VERSION 文件内容，读出来的就跟着变。"""
        import json
        import tempfile
        from unittest import mock

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            version_file = root / 'VERSION'
            with mock.patch.object(engine, 'VERSION_FILE', version_file), \
                 mock.patch.object(engine, 'HOME_ROOT', root / 'home'):
                version_file.write_text('9.9.9\n', encoding='utf-8')
                self.assertEqual(engine.load_version(), '9.9.9')

                # 文件不在时退回框架 INFO 里的登记版本
                version_file.unlink()
                info = root / 'home' / 'u1' / 'plugin' / 'nasconsole' / 'INFO'
                info.parent.mkdir(parents=True)
                info.write_text(json.dumps({'version': '7.7.7'}), encoding='utf-8')
                with mock.patch.dict(os.environ, {'NAS_USER_ID': 'u1'}):
                    self.assertEqual(engine.load_version(), '7.7.7')

                # 两处都读不到：返回空串，不编造版本号
                info.unlink()
                self.assertEqual(engine.load_version(), '')

    def test_version_comes_from_release_directory(self) -> None:
        """商店安装的目录名是 0.1.1-<时间>，脚本安装是 v0.1.0-<时间>：都要能取出版本号。"""
        cases = {
            '/data/plugin/xiaomi-nas-console/releases/0.1.1-1790567139': '0.1.1',
            '/data/plugin/xiaomi-nas-console/releases/v0.1.0-20261003004804': '0.1.0',
            '/data/plugin/xiaomi-nas-console/releases/0.2.4-rc5-1790567139': '0.2.4-rc5',
            '/data/plugin/xiaomi-nas-console/current': '',
            '/data/plugin/xiaomi-nas-console/releases/latest': '',
            '/data/plugin/xiaomi-nas-console/releases/0.1.1': '',
            '/tmp/project': '',
        }
        for raw, expected in cases.items():
            with self.subTest(path=raw):
                self.assertEqual(engine.version_from_release_dir(Path(raw)), expected)

    def test_release_dir_wins_over_stale_version_file(self) -> None:
        """商店会把版本抬到 0.1.1，而包里的 VERSION 文件可能还是 0.1.0：以目录名为准。"""
        import tempfile
        from unittest import mock

        with tempfile.TemporaryDirectory() as temp:
            release = Path(temp) / 'releases' / '0.1.1-1790567139'
            release.mkdir(parents=True)
            (release / 'VERSION').write_text('0.1.0\n', encoding='utf-8')
            with mock.patch.object(engine, 'VERSION_FILE', release / 'VERSION'), \
                 mock.patch.object(engine, 'RELEASE_DIR', release):
                self.assertEqual(engine.load_version(), '0.1.1')

    def test_module_imports_with_nas_user_set(self) -> None:
        """NAS 上服务是带 NAS_USER_ID 启的：模块导入不能依赖尚未定义的常量。

        这条回归测试是必要的：曾经把 VERSION = load_version() 放在 HOME_ROOT 之前，
        而且三个候选值写成"立即求值的元组"，结果在 NAS 上 import engine 直接 NameError。
        """
        import subprocess

        project = Path(__file__).resolve().parent.parent
        environment = dict(os.environ, NAS_USER_ID='u1')
        result = subprocess.run([sys.executable, '-c', 'import engine; print(engine.VERSION)'],
                                cwd=str(project), env=environment, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout.strip(), r'^\d+\.\d+\.\d+$')

    def test_running_version_matches_release_file(self) -> None:
        """当前进程用的版本应当能对上安装位置：开发目录看 VERSION 文件。"""
        self.assertEqual(engine.VERSION, engine.load_version())


class LanToggleTests(unittest.TestCase):
    """桌面 web 入口的开关：这是唯一的写操作，必须可回滚、可预测。"""

    def setUp(self) -> None:
        import tempfile
        from unittest import mock

        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.conf = root / 'conf' / 'xiaomi-nas-console-lan.conf'
        self.template = root / 'lan' / 'xiaomi-nas-console-lan.conf'
        self.template.parent.mkdir(parents=True, exist_ok=True)
        self.template.write_text('server {\n    listen 8085;\n}\n', encoding='utf-8')
        self.mock = mock
        self.patchers = [
            mock.patch.object(engine, 'LAN_CONF', self.conf),
            mock.patch.object(engine, 'LAN_TEMPLATE', self.template),
            mock.patch.object(engine, 'nginx_test', lambda: (True, 'ok')),
            mock.patch.object(engine, 'nginx_reload', lambda: True),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in self.patchers:
            patcher.stop()
        self.temp.cleanup()

    def test_enable_then_disable(self) -> None:
        self.assertFalse(engine.lan_enabled())
        result = engine.set_lan_enabled(True)
        self.assertTrue(result['enabled'])
        self.assertTrue(result['changed'])
        self.assertTrue(self.conf.is_file())
        self.assertIn(f'listen {engine.lan_port()}', self.conf.read_text(encoding='utf-8'))
        if os.name != 'nt':                                # Windows 只能表达只读位
            self.assertEqual(self.conf.stat().st_mode & 0o777, 0o644)

        again = engine.set_lan_enabled(True)
        self.assertFalse(again['changed'])                 # 已经是启用状态，不再动文件

        engine.set_lan_enabled(False)
        self.assertFalse(self.conf.is_file())
        self.assertFalse(engine.lan_enabled())

    def test_invalid_nginx_config_rolls_back(self) -> None:
        with self.mock.patch.object(engine, 'nginx_test', lambda: (False, 'syntax error')):
            with self.assertRaises(RuntimeError) as context:
                engine.set_lan_enabled(True)
        self.assertIn('回滚', str(context.exception))
        self.assertFalse(self.conf.is_file())              # 失败必须回到停用状态

    def test_failed_reload_rolls_back(self) -> None:
        with self.mock.patch.object(engine, 'nginx_reload', lambda: False):
            with self.assertRaises(RuntimeError):
                engine.set_lan_enabled(True)
        self.assertFalse(self.conf.is_file())

    def test_missing_template_is_refused(self) -> None:
        self.template.unlink()
        with self.assertRaises(RuntimeError):
            engine.set_lan_enabled(True)
        self.assertFalse(self.conf.is_file())

    def test_desktop_url_uses_probed_address(self) -> None:
        with self.mock.patch.object(engine, 'primary_ipv4', lambda: '192.168.1.8'):
            self.assertEqual(engine.desktop_url(), f'http://192.168.1.8:{engine.lan_port()}/')
        with self.mock.patch.object(engine, 'primary_ipv4', lambda: ''):
            self.assertIn('NAS-IP', engine.desktop_url())


class PortProbeTests(unittest.TestCase):
    """真实测一次端口探测：正在 LISTEN 的端口要判为占用，关掉后要判为可用。"""

    def test_port_available_really_checks(self) -> None:
        import socket as socket_module

        holder = socket_module.socket()
        holder.setsockopt(socket_module.SOL_SOCKET, socket_module.SO_REUSEADDR, 1)
        holder.bind(('0.0.0.0', 0))
        port = holder.getsockname()[1]
        holder.listen(1)
        try:
            if os.name != 'nt':
                # Linux：SO_REUSEADDR 不允许抢一个正在 LISTEN 的端口（Windows 语义不同）
                self.assertFalse(engine.port_available(port), '有进程在 LISTEN，应判为占用')
        finally:
            holder.close()
        # 关闭后立刻可用（SO_REUSEADDR 让 TIME_WAIT 不再造成假占用）
        self.assertTrue(engine.port_available(port))


class LanPortTests(unittest.TestCase):
    """端口可改：默认 5001，改动要经过 nginx -t 并回滚。"""

    def setUp(self) -> None:
        import tempfile
        from unittest import mock

        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.conf = root / 'conf' / 'lan.conf'
        self.template = root / 'lan' / 'template.conf'
        self.state = root / 'state.json'
        self.template.parent.mkdir(parents=True, exist_ok=True)
        self.template.write_text('server {\n    listen __LAN_PORT__;\n    listen [::]:__LAN_PORT__;\n}\n',
                                 encoding='utf-8')
        self.mock = mock
        self.patchers = [
            mock.patch.object(engine, 'LAN_CONF', self.conf),
            mock.patch.object(engine, 'LAN_TEMPLATE', self.template),
            mock.patch.object(engine, 'STATE_FILE', self.state),
            mock.patch.object(engine, 'LAN_PORT', 5001),
            mock.patch.object(engine, 'nginx_test', lambda: (True, 'ok')),
            mock.patch.object(engine, 'nginx_reload', lambda: True),
            mock.patch.object(engine, 'port_available', lambda port: True),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in self.patchers:
            patcher.stop()
        self.temp.cleanup()

    def test_validate_port(self) -> None:
        self.assertEqual(engine.validate_port('5001'), 5001)
        self.assertEqual(engine.validate_port(65535), 65535)
        for bad in (0, 80, 1023, 65536, 'abc', None):
            with self.assertRaises(RuntimeError):
                engine.validate_port(bad)

    def test_render_replaces_placeholder_and_hardcoded_listen(self) -> None:
        rendered = engine.render_lan_conf(6000)
        self.assertIn('listen 6000;', rendered)
        self.assertIn('listen [::]:6000;', rendered)
        self.template.write_text('server {\n    listen 5001;\n    listen [::]:5001;\n}\n', encoding='utf-8')
        legacy = engine.render_lan_conf(7000)
        self.assertIn('listen 7000;', legacy)
        self.assertIn('listen [::]:7000;', legacy)

    def test_default_port_is_5001(self) -> None:
        self.assertEqual(engine.lan_port(), 5001)          # 没有状态文件时用默认值

    def test_change_port_writes_conf_and_state(self) -> None:
        engine.set_lan_enabled(True)
        result = engine.set_lan_port(6001)
        self.assertTrue(result['changed'])
        self.assertEqual(result['port'], 6001)
        self.assertIn('listen 6001;', self.conf.read_text(encoding='utf-8'))
        self.assertEqual(engine.lan_port(), 6001)
        again = engine.set_lan_port(6001)
        self.assertFalse(again['changed'])                  # 同端口不做任何事

    def test_change_port_rolls_back_on_bad_config(self) -> None:
        engine.set_lan_enabled(True)
        before = self.conf.read_text(encoding='utf-8')
        with self.mock.patch.object(engine, 'nginx_test', lambda: (False, 'syntax error')):
            with self.assertRaises(RuntimeError):
                engine.set_lan_port(6002)
        self.assertEqual(self.conf.read_text(encoding='utf-8'), before)
        self.assertEqual(engine.lan_port(), 5001, '端口不该被写进状态')

    def test_change_port_refuses_busy_port(self) -> None:
        with self.mock.patch.object(engine, 'port_available', lambda port: False):
            with self.assertRaises(RuntimeError) as context:
                engine.set_lan_port(6003)
        self.assertIn('占用', str(context.exception))

    def test_change_port_while_disabled_only_saves_state(self) -> None:
        self.assertFalse(engine.lan_enabled())
        result = engine.set_lan_port(6004)
        self.assertFalse(result['enabled'])
        self.assertFalse(self.conf.is_file())
        self.assertEqual(engine.lan_port(), 6004)           # 下次启用就用新端口

    def test_save_admin_token(self) -> None:
        target = Path(self.temp.name) / 'admin-token'
        engine.save_admin_token(target, 'abcd1234')
        self.assertEqual(target.read_text(encoding='utf-8').strip(), 'abcd1234')
        if os.name != 'nt':
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(len(engine.new_admin_token()), 32)


class FileBrowserTests(unittest.TestCase):
    """文件浏览：只读、根目录白名单、符号链接逃逸必须被挡住。"""

    def setUp(self) -> None:
        import tempfile
        from unittest import mock

        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'root'
        (self.root / 'docs').mkdir(parents=True)
        (self.root / 'docs' / 'note.txt').write_text('hello world', encoding='utf-8')
        (self.root / 'photo.jpg').write_bytes(b'\xff\xd8\xff\xe0' + b'x' * 20)
        (self.root / '.hidden').write_text('secret', encoding='utf-8')
        self.outside = tempfile.TemporaryDirectory()
        (Path(self.outside.name) / 'target.txt').write_text('outside', encoding='utf-8')
        self.mock = mock
        self.patch = mock.patch.object(engine, 'FILE_ROOTS', [(str(self.root), '测试根')])
        self.patch.start()

    def tearDown(self) -> None:
        self.patch.stop()
        self.temp.cleanup()
        self.outside.cleanup()

    def test_listing_puts_directories_first_and_types_files(self) -> None:
        data = engine.list_directory(self.root)
        names = [entry['name'] for entry in data['entries']]
        self.assertEqual(names[0], 'docs')
        self.assertIn('.hidden', names)
        kinds = {entry['name']: entry for entry in data['entries']}
        self.assertEqual(kinds['docs']['kind'], 'dir')
        self.assertEqual(kinds['photo.jpg']['kind'], 'file')
        self.assertEqual(kinds['photo.jpg']['type'], 'image')
        self.assertEqual(kinds['photo.jpg']['size'], 24)
        self.assertEqual(kinds['docs']['size'], None)
        self.assertEqual(kinds['.hidden']['hidden'], True)
        self.assertTrue(data['readonly'])
        self.assertEqual(data['parent'], None)          # 根目录没有更上一级

    def test_truncation_flag(self) -> None:
        with self.mock.patch.object(engine, 'FILE_ENTRY_LIMIT', 1):
            data = engine.list_directory(self.root)
        self.assertTrue(data['truncated'])
        self.assertEqual(len(data['entries']), 1)

    def test_resolve_rejects_relative_missing_and_outside(self) -> None:
        with self.assertRaises(RuntimeError):
            engine.resolve_user_path('docs/note.txt')
        with self.assertRaises(RuntimeError):
            engine.resolve_user_path(str(self.root / 'nope.txt'))
        with self.assertRaises(RuntimeError):
            engine.resolve_user_path('/etc/passwd')
        self.assertEqual(engine.resolve_user_path(str(self.root / 'docs')), (self.root / 'docs').resolve())

    def test_symlink_escape_is_rejected(self) -> None:
        link = self.root / 'escape'
        try:
            link.symlink_to(Path(self.outside.name) / 'target.txt')
        except (OSError, NotImplementedError):
            self.skipTest('当前环境不支持创建符号链接')
        with self.assertRaises(RuntimeError):
            engine.resolve_user_path(str(link))

    def test_text_preview_truncates(self) -> None:
        payload = engine.read_text_preview(self.root / 'docs' / 'note.txt', limit=5)
        self.assertEqual(payload['text'], 'hello')
        self.assertTrue(payload['truncated'])
        self.assertEqual(payload['size'], 11)
        full = engine.read_text_preview(self.root / 'docs' / 'note.txt')
        self.assertFalse(full['truncated'])

    def test_inline_is_restricted_to_images(self) -> None:
        self.assertEqual(engine.inline_headers_for(self.root / 'photo.jpg')[1], 'inline')
        self.assertEqual(engine.inline_headers_for(self.root / 'docs' / 'note.txt')[1], 'attachment')
        self.assertEqual(engine.inline_headers_for(Path('/tmp/x.html'))[1], 'attachment')
        self.assertEqual(engine.inline_headers_for(Path('/tmp/x.svg'))[1], 'attachment')
        self.assertEqual(engine.download_headers_for(Path('/tmp/x.html'))[0], 'application/octet-stream')

    def test_shortcuts_only_report_real_directories(self) -> None:
        rows = {row['path']: row for row in engine.file_shortcuts()}
        self.assertTrue(rows[str(self.root)]['exists'])


class PluginCatalogTests(unittest.TestCase):
    """桌面只摆能在网页里打开的插件：客户端专属应用（影视/中枢/…）不出图标。"""

    def setUp(self) -> None:
        import tempfile
        from unittest import mock

        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        (root / 'home').mkdir()
        self.icons = root / 'icons'
        self.icons.mkdir()
        (self.icons / 'jellyfin.icon').write_bytes(b'\x89PNG')
        self.mock = mock
        self.patchers = [
            mock.patch.object(engine, 'HOME_ROOT', root / 'home'),
            mock.patch.object(engine, 'PLUGIN_ICON_DIR', self.icons),
            mock.patch.object(engine, '_plugin_cache', {'at': 0.0, 'payload': None}),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in self.patchers:
            patcher.stop()
        self.temp.cleanup()

    def _records(self) -> list[dict[str, object]]:
        return [
            {'key': 'jellyfin', 'name': 'Jellyfin', 'id': 11009, 'version': '0.1.9',
             'users': ['u1'], 'icon': '/icon/jellyfin.icon?v=1', 'unit_active': 'active'},
            {'key': 'central', 'name': '中枢功能', 'id': 14, 'version': '1.0.0',
             'users': ['u1'], 'icon': '/icon/central.icon'},
            {'key': 'baidupan', 'name': '百度网盘', 'id': 7, 'version': '2.16',
             'users': ['u1'], 'icon': '/icon/baidupan.icon'},
            {'key': 'nasconsole', 'name': '控制台', 'id': 11020, 'version': '0.1.0',
             'users': ['u1'], 'icon': ''},
        ]

    def test_only_openable_plugins_are_listed(self) -> None:
        ui = engine.HOME_ROOT / 'u1' / 'plugin' / 'jellyfin' / 'src' / 'ui'
        ui.mkdir(parents=True)
        (ui / 'index.html').write_text('<html></html>', encoding='utf-8')
        central_ui = engine.HOME_ROOT / 'u1' / 'plugin' / 'central' / 'src' / 'ui'
        central_ui.mkdir(parents=True)
        (central_ui / 'index.html').write_text('<html></html>', encoding='utf-8')

        def probe(path: str) -> int:
            return 200 if 'jellyfin' in path else 404

        with self.mock.patch.object(engine, 'plugin_records', self._records), \
             self.mock.patch.object(engine, '_probe_plugin', probe):
            payload = engine.plugin_catalog()

        keys = [item['key'] for item in payload['plugins']]
        self.assertEqual(keys, ['jellyfin'])
        self.assertEqual(payload['web_ready'], 1)
        item = payload['plugins'][0]
        self.assertEqual(item['icon'], 'jellyfin.icon')
        self.assertEqual(item['web_path'], '/plugin/u1/jellyfin/index.html')
        self.assertTrue(item['web_ready'])

    def test_result_is_cached(self) -> None:
        calls: list[str] = []
        ui = engine.HOME_ROOT / 'u1' / 'plugin' / 'jellyfin' / 'src' / 'ui'
        ui.mkdir(parents=True)
        (ui / 'index.html').write_text('<html></html>', encoding='utf-8')

        def probe(path: str) -> int:
            calls.append(path)
            return 200

        with self.mock.patch.object(engine, 'plugin_records', self._records), \
             self.mock.patch.object(engine, '_probe_plugin', probe):
            engine.plugin_catalog()
            first = len(calls)
            engine.plugin_catalog()          # 命中缓存，不再探测
        self.assertEqual(first, 1)
        self.assertEqual(len(calls), first)


class WriteOperationTests(unittest.TestCase):
    """上传 / 新建目录 / 重命名 / 删除（进回收站）：全部限制在白名单根目录内。"""

    def setUp(self) -> None:
        import tempfile
        from unittest import mock

        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / 'share'
        (self.root / 'docs').mkdir(parents=True)
        (self.root / 'docs' / 'note.txt').write_text('hello', encoding='utf-8')
        (self.root / 'top.txt').write_text('top', encoding='utf-8')
        self.mock = mock
        self.patch = mock.patch.object(engine, 'FILE_ROOTS', [(str(self.root), '测试根')])
        self.patch.start()

    def tearDown(self) -> None:
        self.patch.stop()
        self.temp.cleanup()

    def test_validate_name(self) -> None:
        self.assertEqual(engine.validate_name('  a b.txt '), 'a b.txt')
        for bad in ('', '   ', '.', '..', 'a/b', 'a\\b', None, 'x' * 300):
            with self.assertRaises(RuntimeError):
                engine.validate_name(bad)

    def test_create_folder(self) -> None:
        result = engine.create_folder(self.root, '新目录')
        self.assertTrue((self.root / '新目录').is_dir())
        self.assertEqual(result['name'], '新目录')
        with self.assertRaises(RuntimeError):
            engine.create_folder(self.root, '新目录')          # 重名

    def test_rename(self) -> None:
        source = self.root / 'top.txt'
        result = engine.rename_entry(source, 'renamed.txt')
        self.assertFalse(source.exists())
        self.assertTrue((self.root / 'renamed.txt').is_file())
        self.assertEqual(result['name'], 'renamed.txt')
        with self.assertRaises(RuntimeError):
            engine.rename_entry(self.root / 'renamed.txt', 'docs')   # 目标已存在
        with self.assertRaises(RuntimeError):
            engine.rename_entry(self.root, 'other')                   # 根目录不能改名

    def test_move_to_trash_and_restore(self) -> None:
        source = self.root / 'docs' / 'note.txt'
        item = engine.move_to_trash(source)
        self.assertFalse(source.exists())
        self.assertEqual(item['kind'], 'file')
        self.assertTrue(Path(item['trashed']).is_file())
        self.assertTrue((self.root / engine.TRASH_DIR_NAME / 'index.json').is_file())

        payload = engine.trash_payload()
        self.assertEqual([entry['id'] for entry in payload['items']], [item['id']])

        engine.restore_from_trash(item['id'])
        self.assertTrue(source.is_file())
        self.assertEqual(engine.trash_payload()['items'], [])

    def test_move_directory_to_trash_and_purge(self) -> None:
        target = self.root / 'docs'
        item = engine.move_to_trash(target)
        self.assertEqual(item['kind'], 'dir')
        self.assertFalse(target.exists())
        result = engine.purge_trash([item['id']])
        self.assertEqual(result['removed'], [item['id']])
        self.assertFalse(Path(item['trashed']).exists())
        self.assertEqual(engine.trash_payload()['items'], [])

    def test_trash_refuses_roots_and_trash_itself(self) -> None:
        with self.assertRaises(RuntimeError):
            engine.move_to_trash(self.root)
        item = engine.move_to_trash(self.root / 'top.txt')
        with self.assertRaises(RuntimeError):
            engine.move_to_trash(Path(item['trashed']))

    def test_restore_conflict_is_reported(self) -> None:
        source = self.root / 'top.txt'
        item = engine.move_to_trash(source)
        source.write_text('new file in the same name', encoding='utf-8')
        with self.assertRaises(RuntimeError):
            engine.restore_from_trash(item['id'])

    def test_upload_streams_body_and_refuses_overwrite(self) -> None:
        import io

        payload = 'uploaded-body'.encode('utf-8')
        result = engine.save_upload(self.root, 'up.txt', io.BytesIO(payload), len(payload))
        self.assertEqual(result['size'], len(payload))
        self.assertEqual((self.root / 'up.txt').read_bytes(), payload)
        with self.assertRaises(RuntimeError):
            engine.save_upload(self.root, 'up.txt', io.BytesIO(b'x'), 1)
        again = engine.save_upload(self.root, 'up.txt', io.BytesIO(b'yy'), 2, overwrite=True)
        self.assertEqual(again['size'], 2)
        self.assertEqual((self.root / 'up.txt').read_bytes(), b'yy')

    def test_upload_rejects_short_body(self) -> None:
        import io

        with self.assertRaises(RuntimeError):
            engine.save_upload(self.root, 'broken.bin', io.BytesIO(b'abc'), 10)
        self.assertFalse((self.root / 'broken.bin').exists())
        leftovers = [name for name in os.listdir(self.root) if name.startswith('.broken.bin')]
        self.assertEqual(leftovers, [])

    def test_writes_stay_inside_roots(self) -> None:
        with self.assertRaises(RuntimeError):
            engine.rename_entry(Path('/etc/hosts'), 'x')
        with self.assertRaises(RuntimeError):
            engine.create_folder(Path('/etc'), 'x')


class ServerHttpTests(unittest.TestCase):
    """真实起一个 HTTP 服务打一遍关键路由。

    教训：曾经因为把 `def _serve_static` 误并进上一行的注释里，静态文件路由整个消失，
    但服务照常启动、/healthz 照常 200 —— 只有真正请求页面才会暴露。
    """

    def setUp(self) -> None:
        import tempfile
        import threading

        import server

        self.server_module = server
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        static = root / 'web'
        static.mkdir()
        (static / 'index.html').write_text('<html>desktop-console</html>', encoding='utf-8')
        (static / 'control.html').write_text('<html>mobile-control</html>', encoding='utf-8')
        (static / 'app.js').write_text('void 0;', encoding='utf-8')

        self.token_file = root / 'admin-token'
        self.token_file.write_text('secret-token\n', encoding='utf-8')
        sampler = engine.Sampler(interval=3600)
        self.httpd = server.ConsoleServer(('127.0.0.1', 0), server.Handler, static,
                                          'secret-token', sampler, dev=True,
                                          token_file=self.token_file)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.temp.cleanup()

    def request(self, path: str, method: str = 'GET', body: bytes | None = None,
                headers: dict[str, str] | None = None) -> tuple[int, bytes]:
        import urllib.error
        import urllib.request

        url = f'http://127.0.0.1:{self.port}{path}'
        request = urllib.request.Request(url, data=body, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def test_healthz(self) -> None:
        status, body = self.request('/healthz')
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)['ok'])

    def test_static_pages_are_served(self) -> None:
        for path, marker in (('/', b'desktop-console'), ('/control.html', b'mobile-control'),
                             ('/app.js', b'void 0;')):
            status, body = self.request(path)
            self.assertEqual(status, 200, path)
            self.assertIn(marker, body)

    def test_unknown_static_path_falls_back_to_index(self) -> None:
        status, body = self.request('/some/deep/route')
        self.assertEqual(status, 200)
        self.assertIn(b'desktop-console', body)

    def test_path_traversal_is_refused(self) -> None:
        # urllib 会把明文 .. 规范化掉，所以用百分号编码送进去
        status, _ = self.request('/%2e%2e/server.py')
        self.assertEqual(status, 404)

    def test_session_and_unknown_api(self) -> None:
        status, body = self.request('/api/session')
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload['entry'], 'local')        # 回环直连视为可信
        self.assertTrue(payload['authed'])

        status, _ = self.request('/api/does-not-exist')
        self.assertEqual(status, 404)

    def test_control_get_and_post(self) -> None:
        from unittest import mock

        template = Path(self.temp.name) / 'lan.conf'
        template.write_text('server { listen 8085; }\n', encoding='utf-8')
        box = {'enabled': True}
        with mock.patch.object(engine, 'lan_enabled', lambda: box['enabled']), \
             mock.patch.object(engine, 'desktop_url', lambda: 'http://192.168.1.8:8085/'), \
             mock.patch.object(engine, 'LAN_TEMPLATE', template):
            status, body = self.request('/api/control')
            self.assertEqual(status, 200)
            payload = json.loads(body)
            self.assertTrue(payload['enabled'])
            self.assertEqual(payload['url'], 'http://192.168.1.8:8085/')
            self.assertEqual(payload['token'], 'secret-token')
            self.assertEqual(payload['entry'], 'local')
            self.assertTrue(payload['template_ready'])

            captured = {}

            def fake_toggle(enabled: bool) -> dict[str, object]:
                captured['enabled'] = enabled
                box['enabled'] = enabled
                return {'enabled': enabled, 'changed': True}

            with mock.patch.object(engine, 'set_lan_enabled', fake_toggle):
                status, body = self.request(
                    '/api/control', method='POST',
                    body=json.dumps({'enabled': False}).encode('utf-8'),
                    headers={'Content-Type': 'application/json'})
                self.assertEqual(status, 200)
                self.assertFalse(captured['enabled'])
                self.assertFalse(json.loads(body)['enabled'])

    def test_control_reports_engine_failure(self) -> None:
        from unittest import mock

        def failing(enabled: bool) -> dict[str, object]:
            raise RuntimeError('nginx 配置校验失败，已回滚为停用')

        with mock.patch.object(engine, 'set_lan_enabled', failing), \
             mock.patch.object(engine, 'lan_enabled', lambda: False):
            status, body = self.request('/api/control', method='POST',
                                        body=json.dumps({'enabled': True}).encode('utf-8'),
                                        headers={'Content-Type': 'application/json'})
            self.assertEqual(status, 409)
            self.assertIn('回滚', json.loads(body)['error'])

    def test_control_requires_enabled_field(self) -> None:
        status, _ = self.request('/api/control', method='POST', body=b'{}',
                                 headers={'Content-Type': 'application/json'})
        self.assertEqual(status, 400)

    def test_file_write_endpoints(self) -> None:
        from unittest import mock
        from urllib.parse import quote

        root = Path(self.temp.name) / 'files'
        root.mkdir()
        (root / 'a.txt').write_text('hello', encoding='utf-8')

        with mock.patch.object(engine, 'FILE_ROOTS', [(str(root), '测试根')]):
            # 新建目录
            status, body = self.request('/api/files/mkdir', method='POST',
                                        body=json.dumps({'path': str(root), 'name': '新建'}).encode(),
                                        headers={'Content-Type': 'application/json'})
            self.assertEqual(status, 200)
            self.assertTrue((root / '新建').is_dir())

            # 重命名
            status, _ = self.request('/api/files/rename', method='POST',
                                     body=json.dumps({'path': str(root / 'a.txt'), 'name': 'b.txt'}).encode(),
                                     headers={'Content-Type': 'application/json'})
            self.assertEqual(status, 200)
            self.assertTrue((root / 'b.txt').is_file())

            # 重名要报错
            status, body = self.request('/api/files/rename', method='POST',
                                        body=json.dumps({'path': str(root / 'b.txt'), 'name': '新建'}).encode(),
                                        headers={'Content-Type': 'application/json'})
            self.assertEqual(status, 400)

            # 上传（原始字节流）
            status, body = self.request(
                '/api/files/upload?path=' + quote(str(root)) + '&name=up.bin', method='POST',
                body=b'0123456789', headers={'Content-Type': 'application/octet-stream'})
            self.assertEqual(status, 200)
            self.assertEqual((root / 'up.bin').read_bytes(), b'0123456789')

            # 同名上传 → 409
            status, _ = self.request(
                '/api/files/upload?path=' + quote(str(root)) + '&name=up.bin', method='POST',
                body=b'xx', headers={'Content-Type': 'application/octet-stream'})
            self.assertEqual(status, 409)

            # 删除 → 回收站
            status, body = self.request('/api/files/delete', method='POST',
                                        body=json.dumps({'paths': [str(root / 'b.txt')]}).encode(),
                                        headers={'Content-Type': 'application/json'})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)['moved'], [str(root / 'b.txt')])
            self.assertFalse((root / 'b.txt').exists())

            status, body = self.request('/api/files/trash')
            self.assertEqual(status, 200)
            items = json.loads(body)['items']
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]['name'], 'b.txt')

            # 恢复
            status, _ = self.request('/api/files/restore', method='POST',
                                     body=json.dumps({'ids': [items[0]['id']]}).encode(),
                                     headers={'Content-Type': 'application/json'})
            self.assertEqual(status, 200)
            self.assertTrue((root / 'b.txt').is_file())

            # 越界写入被拒
            status, _ = self.request('/api/files/mkdir', method='POST',
                                     body=json.dumps({'path': '/etc', 'name': 'x'}).encode(),
                                     headers={'Content-Type': 'application/json'})
            self.assertEqual(status, 400)

            # 根目录不能删
            status, _ = self.request('/api/files/delete', method='POST',
                                     body=json.dumps({'paths': [str(root)]}).encode(),
                                     headers={'Content-Type': 'application/json'})
            self.assertEqual(status, 400)

    def test_control_port_and_token_endpoints(self) -> None:
        from unittest import mock

        template = Path(self.temp.name) / 'lan-template.conf'
        template.write_text('server {\n    listen __LAN_PORT__;\n}\n', encoding='utf-8')
        state = Path(self.temp.name) / 'state.json'
        token_file = Path(self.temp.name) / 'admin-token'
        token_file.write_text('secret-token\n', encoding='utf-8')

        with mock.patch.object(engine, 'LAN_TEMPLATE', template), \
             mock.patch.object(engine, 'STATE_FILE', state), \
             mock.patch.object(engine, 'LAN_PORT', 5001), \
             mock.patch.object(engine, 'LAN_CONF', Path(self.temp.name) / 'lan.conf'), \
             mock.patch.object(engine, 'port_available', lambda port: True), \
             mock.patch.object(engine, 'nginx_test', lambda: (True, 'ok')), \
             mock.patch.object(engine, 'nginx_reload', lambda: True):
            status, body = self.request('/api/control/port', method='POST',
                                        body=json.dumps({'port': 6005}).encode(),
                                        headers={'Content-Type': 'application/json'})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)['lan_port'], 6005)

            status, body = self.request('/api/control/port', method='POST',
                                        body=json.dumps({'port': 80}).encode(),
                                        headers={'Content-Type': 'application/json'})
            self.assertEqual(status, 409)

            # 先登录拿一个会话，再轮换令牌，旧会话必须失效
            import http.cookiejar
            import urllib.request

            jar = http.cookiejar.CookieJar()
            opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
            login = urllib.request.Request(
                f'http://127.0.0.1:{self.port}/api/login', method='POST',
                data=json.dumps({'token': 'secret-token'}).encode(),
                headers={'Content-Type': 'application/json'})
            with opener.open(login, timeout=10):
                pass
            cookie = '; '.join(f'{item.name}={item.value}' for item in jar)

            def session_state() -> dict:
                request = urllib.request.Request(
                    f'http://127.0.0.1:{self.port}/api/session',
                    headers={'X-Console-Entry': 'lan', 'Cookie': cookie})
                with urllib.request.urlopen(request, timeout=10) as response:
                    return json.loads(response.read())

            self.assertTrue(session_state()['authed'])

            status, body = self.request('/api/control/token', method='POST', body=b'{}',
                                        headers={'Content-Type': 'application/json'})
            self.assertEqual(status, 200)
            new_token = json.loads(body)['token']
            self.assertNotEqual(new_token, 'secret-token')
            self.assertEqual(self.token_file.read_text(encoding='utf-8').strip(), new_token)

            self.assertFalse(session_state()['authed'], '轮换令牌后旧会话必须失效')

            # 新令牌能登录，旧令牌不能
            status, _ = self.request('/api/login', method='POST',
                                     body=json.dumps({'token': new_token}).encode(),
                                     headers={'Content-Type': 'application/json'})
            self.assertEqual(status, 200)
            status, _ = self.request('/api/login', method='POST',
                                     body=json.dumps({'token': 'secret-token'}).encode(),
                                     headers={'Content-Type': 'application/json'})
            self.assertEqual(status, 401)

    def test_handler_routes_exist(self) -> None:
        handler = self.server_module.Handler
        for name in ('do_GET', 'do_POST', '_serve_static', '_handle_api', '_handle_login',
                     '_handle_control', '_control_payload', '_require_write',
                     '_handle_files', '_handle_file_stream'):
            self.assertTrue(callable(getattr(handler, name, None)), f'Handler 缺少 {name}')

    def test_file_endpoints(self) -> None:
        from unittest import mock
        from urllib.parse import quote

        root = Path(self.temp.name) / 'files'
        (root / 'sub').mkdir(parents=True)
        (root / 'a.txt').write_text('hello', encoding='utf-8')
        (root / 'b.html').write_text('<script>alert(1)</script>', encoding='utf-8')

        with mock.patch.object(engine, 'FILE_ROOTS', [(str(root), '测试根')]):
            status, body = self.request('/api/files?path=' + quote(str(root)))
            self.assertEqual(status, 200)
            payload = json.loads(body)
            self.assertEqual(payload['path'], str(root))
            self.assertEqual([entry['name'] for entry in payload['entries']], ['sub', 'a.txt', 'b.html'])
            self.assertTrue(payload['readonly'])

            status, _ = self.request('/api/files?path=' + quote('/etc'))
            self.assertEqual(status, 400)

            status, _ = self.request('/api/files?path=' + quote(str(root / 'missing')))
            self.assertEqual(status, 400)

            status, body = self.request('/api/files/text?path=' + quote(str(root / 'a.txt')))
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)['text'], 'hello')

            status, body = self.request('/api/files/download?path=' + quote(str(root / 'a.txt')))
            self.assertEqual(status, 200)
            self.assertEqual(body, b'hello')

            # HTML 不能内联返回（否则同源 XSS），必须走 attachment
            status, _ = self.request('/api/files/raw?path=' + quote(str(root / 'b.html')))
            self.assertEqual(status, 200)

            status, _ = self.request('/api/files/raw')
            self.assertEqual(status, 400)


class FrontendSyntaxTests(unittest.TestCase):
    """前端没有构建链，语法错误只会在浏览器里爆掉——这里用 node --check 兜住。

    教训：曾经因为少了两个右括号，整个 app.js 都没执行，页面只剩骨架。
    """

    def test_app_js_parses(self) -> None:
        import shutil
        import subprocess

        node = shutil.which('node')
        if not node:
            self.skipTest('没有 node，跳过前端语法检查')
        project = Path(__file__).resolve().parent.parent
        for script in ('app.js', 'control.js'):
            result = subprocess.run([node, '--check', str(project / 'web' / script)],
                                    capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, f'{script}: {result.stderr}')

    def test_index_references_local_assets(self) -> None:
        project = Path(__file__).resolve().parent.parent
        html = (project / 'web' / 'index.html').read_text(encoding='utf-8')
        self.assertIn('styles.css', html)
        self.assertIn('app.js', html)
        self.assertNotIn('https://', html)

    def test_control_page_references_local_assets(self) -> None:
        project = Path(__file__).resolve().parent.parent
        html = (project / 'web' / 'control.html').read_text(encoding='utf-8')
        for asset in ('styles.css', 'control.css', 'control.js'):
            self.assertIn(asset, html)
        self.assertNotIn('http://', html)
        self.assertNotIn('https://', html)

    def test_no_inline_style_attributes(self) -> None:
        """页面 CSP 是 style-src 'self'，任何内联 style 属性都会被浏览器丢弃。"""
        project = Path(__file__).resolve().parent.parent
        for name in ('index.html', 'control.html'):
            html = (project / 'web' / name).read_text(encoding='utf-8')
            self.assertNotIn(' style="', html, f'{name} 里有内联 style 属性（会被 CSP 拦掉）')


if __name__ == '__main__':
    unittest.main()
