from __future__ import annotations

import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('DATA_DIR', tempfile.mkdtemp(prefix='disk-sleep-test-'))

import engine  # noqa: E402


class DiskSleepEngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.state_file = root / 'state.json'
        self.event_file = root / 'events.jsonl'
        self.dropin = root / 'hdidle.service.d' / '10-plugin-timeout.conf'
        patcher = patch.multiple(
            engine,
            STATE_FILE=self.state_file,
            EVENT_FILE=self.event_file,
            DROPIN_DIR=self.dropin.parent,
            DROPIN_FILE=self.dropin,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        # hdidle 状态有 15 秒缓存：测试之间要清掉，否则用例之间会互相影响
        engine._hdidle_state_cache['at'] = 0
        engine._hdidle_state_cache['states'] = {}

    # -- 官方开关 ---------------------------------------------------------
    def test_app_switch_reads_uci_hibernate(self) -> None:
        with patch.object(engine, 'run', return_value=type('R', (), {'returncode': 0, 'stdout': '1\n'})()):
            self.assertTrue(engine.app_switch())
        with patch.object(engine, 'run', return_value=type('R', (), {'returncode': 0, 'stdout': '0\n'})()):
            self.assertFalse(engine.app_switch())

    def test_set_app_switch_writes_uci_and_toggles_unit(self) -> None:
        calls: list[list[str]] = []

        def fake_run(command, timeout=15):
            calls.append(command)
            return type('R', (), {'returncode': 0, 'stdout': ''})()

        with patch.object(engine, 'run', side_effect=fake_run):
            engine.set_app_switch(True)
            engine.set_app_switch(False)

        self.assertIn([engine.UCI, 'set', 'system.disk.hibernate=1'], calls)
        self.assertIn([engine.UCI, 'commit', 'system'], calls)
        self.assertIn(['systemctl', 'start', engine.HDIDLE_UNIT], calls)
        self.assertIn(['systemctl', 'stop', engine.HDIDLE_UNIT], calls)

    # -- 休眠时间 ---------------------------------------------------------
    def test_dropin_overrides_official_1800(self) -> None:
        calls: list[list[str]] = []

        def fake_run(command, timeout=15):
            calls.append(command)
            if command[:2] == [engine.UCI, 'get']:
                return type('R', (), {'returncode': 0, 'stdout': '1\n'})()
            return type('R', (), {'returncode': 0, 'stdout': ''})()

        with patch.object(engine, 'run', side_effect=fake_run):
            engine.apply_timeout(45)

        text = self.dropin.read_text(encoding='utf-8')
        self.assertIn('ExecStart=', text)
        self.assertIn(f'{engine.HDIDLE} -n -i 2700', text)
        self.assertIn('system.disk.hibernate', text)          # 仍由官方开关控制启停
        self.assertIn(['systemctl', 'daemon-reload'], calls)
        self.assertIn(['systemctl', 'restart', engine.HDIDLE_UNIT], calls)

    def test_apply_timeout_keeps_unit_stopped_when_switch_off(self) -> None:
        calls: list[list[str]] = []

        def fake_run(command, timeout=15):
            calls.append(command)
            if command[:2] == [engine.UCI, 'get']:
                return type('R', (), {'returncode': 0, 'stdout': '0\n'})()
            return type('R', (), {'returncode': 0, 'stdout': ''})()

        with patch.object(engine, 'run', side_effect=fake_run):
            engine.apply_timeout(60)

        self.assertNotIn(['systemctl', 'restart', engine.HDIDLE_UNIT], calls)

    def test_minutes_bounds(self) -> None:
        with patch.object(engine, 'run', return_value=type('R', (), {'returncode': 0, 'stdout': ''})()):
            with self.assertRaises(engine.Error):
                engine.set_minutes(1)
            with self.assertRaises(engine.Error):
                engine.set_minutes(engine.MAX_MINUTES + 1)
            with self.assertRaises(engine.Error):
                engine.set_minutes(True)
            engine.set_minutes(engine.MIN_MINUTES)

    def test_restore_official_removes_dropin(self) -> None:
        calls: list[list[str]] = []

        def fake_run(command, timeout=15):
            calls.append(command)
            if command[:2] == [engine.UCI, 'get']:
                return type('R', (), {'returncode': 0, 'stdout': '1\n'})()
            return type('R', (), {'returncode': 0, 'stdout': ''})()

        self.dropin.parent.mkdir(parents=True, exist_ok=True)
        self.dropin.write_text('[Service]\n', encoding='utf-8')
        with patch.object(engine, 'run', side_effect=fake_run):
            engine.restore_official()

        self.assertFalse(self.dropin.exists())
        self.assertFalse(self.dropin.parent.exists())          # 空目录也清掉
        self.assertIn(['systemctl', 'daemon-reload'], calls)

    def test_configured_minutes_defaults_to_official(self) -> None:
        self.assertEqual(engine.configured_minutes(), engine.OFFICIAL_SECONDS // 60)

    def test_effective_seconds_parses_argv(self) -> None:
        show = ('ExecStart={ path=/bin/sh ; argv[]=/bin/sh -c ... ; '
                'argv[]=/usr/bin/hdidle -n -i 2700 ; ignore_errors=no ; }')
        with patch.object(engine, 'run', return_value=type('R', (), {'returncode': 0, 'stdout': show})()):
            self.assertEqual(engine.effective_seconds(), 2700)

    # -- 事件与状态 -------------------------------------------------------
    def test_sample_once_logs_standby_and_wake(self) -> None:
        states = iter(['active/idle', 'standby', 'standby', 'active/idle'])

        def fake_run(command, timeout=15):
            if command[0] == engine.HDPARM:
                return type('R', (), {'returncode': 0, 'stdout': f'drive state is:  {next(states)}\n'})()
            return type('R', (), {'returncode': 0, 'stdout': ''})()

        with patch.object(engine, 'disk_devices', return_value=['sda']), \
                patch.object(engine, 'run', side_effect=fake_run):
            last = engine.sample_once({})           # 首次采样只记录基线
            self.assertEqual(last, {'sda': 'active/idle'})
            last = engine.sample_once(last)         # 进入休眠
            last = engine.sample_once(last)         # 无变化
            last = engine.sample_once(last)         # 被唤醒

        events = engine.recent_events()
        kinds = [item['kind'] for item in events]
        self.assertEqual(kinds, ['wake', 'standby'])            # 最新在最前
        self.assertEqual(events[0]['device'], 'sda')
        self.assertEqual(events[1]['detail'], 'active/idle → standby')

    # -- USB 硬盘盒与读不到的状态 -------------------------------------------
    def test_usb_bridge_is_not_reported_as_sleeping(self) -> None:
        """USB 硬盘盒不实现 ATA 电源状态：不许当成休眠，也不许写事件。

        实测（ASM2464 硬盘盒 + SSD，挂着 /mnt/usb-… 在用）：hdparm -C 会先回一段
        坏掉的 sense data 再瞎报 `drive state is: standby`，smartctl -n standby 也会
        误报 SLEEP——结果一块在读写的外接盘被一直显示成「休眠中」。
        """
        lying = ('SG_IO: bad/missing sense data, sb[]:  70 00 05 00 00 00 00 0a\n'
                 '\n/dev/sdc:\n drive state is:  standby\n')
        with patch.object(engine, 'is_usb_device', return_value=True), \
                patch.object(engine, 'run', return_value=type('R', (), {'returncode': 0,
                                                                       'stdout': lying,
                                                                       'stderr': ''})()):
            self.assertEqual(engine.disk_state('sdc'), 'unsupported')
            self.assertFalse(engine.is_standby(engine.disk_state('sdc')))
            with patch.object(engine, 'disk_devices', return_value=['sdc']):
                last = engine.sample_once({})
                last = engine.sample_once(last)
        self.assertEqual(last, {})                       # 不进入基线
        self.assertEqual(engine.recent_events(), [])     # 没有假休眠事件

    def test_bogus_sense_data_is_unknown_not_standby(self) -> None:
        """非 USB 路径上，带 bad/missing sense data 的输出同样不能当 standby。"""
        text = ('SG_IO: bad/missing sense data, sb[]:  70 00 05 00\n'
                '/dev/sdx:\n drive state is:  standby\n')
        with patch.object(engine, 'is_usb_device', return_value=False), \
                patch.object(engine, 'STATE_RETRY_DELAY', 0), \
                patch.object(engine, 'run', return_value=type('R', (), {'returncode': 0,
                                                                       'stdout': text,
                                                                       'stderr': ''})()):
            self.assertEqual(engine.disk_state('sdx'), 'unknown')

    def test_unknown_reading_keeps_previous_state(self) -> None:
        """读不到时不要改写基线：真实跃迁应该是 active/idle → standby，而不是带 unknown 的噪声。"""
        # 第二次采样读不到（原查询 + 重试都空），第三次才读到 standby
        replies = ['drive state is:  active/idle\n', '', '', 'drive state is:  standby\n']
        engine.add_event('sda', 'config', '旧事件')

        def fake_run(command, timeout=15):
            if command[0] != engine.HDPARM:
                return type('R', (), {'returncode': 0, 'stdout': '', 'stderr': ''})()
            stdout = replies.pop(0) if replies else 'drive state is:  standby\n'
            return type('R', (), {'returncode': 0, 'stdout': stdout, 'stderr': ''})()

        with patch.object(engine, 'is_usb_device', return_value=False), \
                patch.object(engine, 'STATE_RETRY_DELAY', 0), \
                patch.object(engine, 'disk_devices', return_value=['sda']), \
                patch.object(engine, 'run', side_effect=fake_run):
            last = engine.sample_once({})           # active/idle 基线
            self.assertEqual(last, {'sda': 'active/idle'})
            last = engine.sample_once(last)         # 读不到 → 保留基线，不写事件
            self.assertEqual(last, {'sda': 'active/idle'})
            last = engine.sample_once(last)         # 真的休眠

        events = [item for item in engine.recent_events() if item['kind'] in ('standby', 'wake')]
        self.assertEqual([item['kind'] for item in events], ['standby'])
        self.assertEqual(events[0]['detail'], 'active/idle → standby')
        self.assertNotIn('unknown', events[0]['detail'])

    def test_state_retry_recovers_from_transient_failure(self) -> None:
        """完全读不到（例如 WD200EDGZ 偶发 ATA softreset 失败）时重试一次。"""
        replies = [type('R', (), {'returncode': 0, 'stdout': '', 'stderr': ''})(),
                   type('R', (), {'returncode': 0, 'stdout': 'drive state is:  standby\n',
                                  'stderr': ''})()]
        with patch.object(engine, 'is_usb_device', return_value=False), \
                patch.object(engine, 'STATE_RETRY_DELAY', 0), \
                patch.object(engine, 'run', side_effect=replies) as call:
            self.assertEqual(engine.disk_state('sda'), 'standby')
        self.assertEqual(call.call_count, 2)

    def test_disk_summary_flags_usb_and_unknown(self) -> None:
        with patch.object(engine, 'disk_devices', return_value=['sda', 'sdc']), \
                patch.object(engine, 'is_usb_device', side_effect=lambda name: name == 'sdc'), \
                patch.object(engine, 'disk_state', side_effect=lambda name: 'unsupported' if name == 'sdc' else 'unknown'), \
                patch.object(engine, 'disk_model', return_value='MODEL'):
            items = engine.disk_summary()
        by_name = {item['device']: item for item in items}
        self.assertTrue(by_name['sdc']['usb'])
        self.assertFalse(by_name['sdc']['powerStateSupported'])
        self.assertFalse(by_name['sdc']['standby'])
        self.assertFalse(by_name['sda']['usb'])
        self.assertTrue(by_name['sda']['powerStateSupported'])
        self.assertFalse(by_name['sda']['standby'])      # unknown 不是休眠

    def test_hdidle_log_covers_drives_that_never_report_state(self) -> None:
        """WD200EDGZ 这类盘对 hdparm -C 永远回 unknown，就用 hdidle 的 spindown/spinup 判。

        实测：/dev/sda 连跑 5 次都是 `drive state is:  unknown`（rc=0），但
        hdidle 日志里明确写着 `disk sda: spindown` / `disk sda: spinup (...)`。
        """
        messages = [
            {'at': 200, 'message': 'disk sda: spindown'},
            {'at': 150, 'message': 'disk sda: spinup (running: 1502, stopped: 543)'},
            {'at': 100, 'message': 'disk sda: standby timeout 600 seconds'},
            {'at': 90, 'message': 'disk sdb: spindown'},
        ]
        with patch.object(engine, 'disk_devices', return_value=['sda', 'sdb']), \
                patch.object(engine, 'is_usb_device', return_value=False), \
                patch.object(engine, 'disk_state', side_effect=lambda name: 'unknown' if name == 'sda' else 'idle'), \
                patch.object(engine, 'hdidle_log', return_value=messages), \
                patch.object(engine, 'HDIDLE_STATE_TTL', 0):
            states = engine.cached_hdidle_states()
            self.assertEqual(states.get('sda'), 'standby')      # 最新一条是 spindown
            self.assertEqual(states.get('sdb'), 'standby')

            item = {disk['device']: disk for disk in engine.disk_summary()}['sda']
            self.assertEqual(item['state'], 'standby')
            self.assertEqual(item['source'], 'hdidle')
            self.assertTrue(item['standby'])

            last = engine.sample_once({})                        # 基线：sda=standby
            self.assertEqual(last.get('sda'), 'standby')

            messages.insert(0, {'at': 300, 'message': 'disk sda: spinup (running: 1, stopped: 2)'})
            engine._hdidle_state_cache['at'] = 0
            last = engine.sample_once(last)                      # hdidle 说醒了

        events = [item for item in engine.recent_events() if item['kind'] in ('standby', 'wake')]
        self.assertEqual([item['kind'] for item in events], ['wake'])
        self.assertEqual(events[0]['detail'], 'standby → active/idle（hdidle 日志）')

    def test_unknown_without_hdidle_record_stays_out_of_events(self) -> None:
        """两边都读不到时：不写事件、不改基线。"""
        with patch.object(engine, 'disk_devices', return_value=['sda']), \
                patch.object(engine, 'is_usb_device', return_value=False), \
                patch.object(engine, 'disk_state', return_value='unknown'), \
                patch.object(engine, 'hdidle_log', return_value=[]), \
                patch.object(engine, 'HDIDLE_STATE_TTL', 0):
            last = engine.sample_once({'sda': 'active/idle'})
        self.assertEqual(last, {'sda': 'active/idle'})
        self.assertEqual([item for item in engine.recent_events()
                          if item['kind'] in ('standby', 'wake')], [])

    def test_events_are_trimmed(self) -> None:
        with patch.object(engine, 'MAX_EVENTS', 3):
            for index in range(5):
                engine.add_event('sda', 'standby', str(index))
            events = engine.recent_events(10)
        self.assertEqual([item['detail'] for item in events], ['4', '3', '2'])

    def test_hdidle_log_is_newest_first(self) -> None:
        lines = '\n'.join(json.dumps({'MESSAGE': f'line{i}', '__REALTIME_TIMESTAMP': str(1000 + i)})
                          for i in range(3))
        with patch.object(engine, 'run', return_value=type('R', (), {'returncode': 0, 'stdout': lines})()):
            entries = engine.hdidle_log(10)
        self.assertEqual([item['message'] for item in entries], ['line2', 'line1', 'line0'])

    def test_snapshot_reports_link_state(self) -> None:
        show = ('ExecStart={ path=/bin/sh ; argv[]=/usr/bin/hdidle -n -i 1200 ; ignore_errors=no ; }')
        responses = {
            (engine.UCI, 'get'): ('1\n', 0),
            ('systemctl', 'is-active'): ('', 0),
            ('systemctl', 'show'): (show, 0),
        }

        def fake_run(command, timeout=15):
            key = (command[0], command[1])
            stdout, code = responses.get(key, ('', 1))
            return type('R', (), {'returncode': code, 'stdout': stdout})()

        self.dropin.parent.mkdir(parents=True, exist_ok=True)
        self.dropin.write_text('[Service]\n', encoding='utf-8')
        with patch.object(engine, 'run', side_effect=fake_run), \
                patch.object(engine, 'disk_summary', return_value=[]):
            data = engine.snapshot()

        self.assertTrue(data['appSwitch'])
        self.assertTrue(data['hdidleActive'])
        self.assertTrue(data['managed'])
        self.assertEqual(data['effectiveMinutes'], 20)
        self.assertEqual(data['officialMinutes'], 30)


    # -- 接管开关 ---------------------------------------------------------
    def _uci_run(self, calls, initial='1'):
        """模拟 uci：记住 hibernate 的值，这样 app_switch() 会跟着变。"""
        state = {'hibernate': initial}

        def fake_run(command, timeout=15):
            calls.append(command)
            if command[:2] == [engine.UCI, 'get']:
                return type('R', (), {'returncode': 0, 'stdout': state['hibernate'] + '\n'})()
            if command[:2] == [engine.UCI, 'set'] and '=' in command[2]:
                state['hibernate'] = command[2].split('=', 1)[1]
            return type('R', (), {'returncode': 0, 'stdout': ''})()

        return fake_run

    def test_set_minutes_alone_does_not_take_over(self) -> None:
        """保存时间不应该顺手把接管打开。"""
        calls: list[list[str]] = []
        with patch.object(engine, 'run', side_effect=self._uci_run(calls, initial='0')):
            engine.set_minutes(45)
        self.assertFalse(self.dropin.exists())
        self.assertEqual(json.loads(self.state_file.read_text(encoding='utf-8'))['minutes'], 45)

    def test_set_minutes_updates_dropin_when_already_taken_over(self) -> None:
        self.dropin.parent.mkdir(parents=True, exist_ok=True)
        self.dropin.write_text('[Service]\n', encoding='utf-8')
        calls: list[list[str]] = []
        with patch.object(engine, 'run', side_effect=self._uci_run(calls, initial='1')):
            engine.set_minutes(90)
        self.assertIn(f'{engine.HDIDLE} -n -i 5400', self.dropin.read_text(encoding='utf-8'))
        self.assertIn(['systemctl', 'restart', engine.HDIDLE_UNIT], calls)

    def test_set_takeover_on_writes_dropin_before_switch(self) -> None:
        calls: list[list[str]] = []
        with patch.object(engine, 'run', side_effect=self._uci_run(calls, initial='0')):
            engine.set_takeover(True)
        text = self.dropin.read_text(encoding='utf-8')
        self.assertIn('system.disk.hibernate', text)          # 仍由官方开关控制启停
        self.assertIn([engine.UCI, 'set', 'system.disk.hibernate=1'], calls)
        self.assertIn(['systemctl', 'start', engine.HDIDLE_UNIT], calls)
        # 先落地 drop-in，再启动守护；反了会有一瞬间按官方 30 分钟跑
        self.assertLess(calls.index([engine.UCI, 'set', 'system.disk.hibernate=1']),
                        calls.index(['systemctl', 'start', engine.HDIDLE_UNIT]))

    def test_set_takeover_off_stops_then_restores(self) -> None:
        self.dropin.parent.mkdir(parents=True, exist_ok=True)
        self.dropin.write_text('[Service]\n', encoding='utf-8')
        calls: list[list[str]] = []
        with patch.object(engine, 'run', side_effect=self._uci_run(calls, initial='1')):
            engine.set_takeover(False)
        self.assertFalse(self.dropin.exists())
        self.assertIn([engine.UCI, 'set', 'system.disk.hibernate=0'], calls)
        self.assertIn(['systemctl', 'stop', engine.HDIDLE_UNIT], calls)
        # 先停守护，再撤 drop-in；停完开关后不该再重启服务
        self.assertLess(calls.index(['systemctl', 'stop', engine.HDIDLE_UNIT]),
                        calls.index(['systemctl', 'daemon-reload']))
        self.assertNotIn(['systemctl', 'restart', engine.HDIDLE_UNIT], calls)

    def test_set_takeover_rejects_non_boolean(self) -> None:
        with self.assertRaises(engine.Error):
            engine.set_takeover('yes')

    def test_snapshot_active_needs_switch_and_dropin(self) -> None:
        show = 'ExecStart={ argv[]=/usr/bin/hdidle -n -i 600 ; }'

        def fake_run(command, timeout=15):
            if command[:2] == [engine.UCI, 'get']:
                return type('R', (), {'returncode': 0, 'stdout': '1\n'})()
            if command[:2] == ['systemctl', 'show']:
                return type('R', (), {'returncode': 0, 'stdout': show})()
            return type('R', (), {'returncode': 0, 'stdout': ''})()

        with patch.object(engine, 'run', side_effect=fake_run), \
                patch.object(engine, 'disk_summary', return_value=[]):
            data = engine.snapshot()
            self.assertTrue(data['appSwitch'])
            self.assertFalse(data['managed'])
            self.assertFalse(data['active'])                  # 开关开但没接管
            self.dropin.parent.mkdir(parents=True, exist_ok=True)
            self.dropin.write_text('[Service]\n', encoding='utf-8')
            data = engine.snapshot()
        self.assertTrue(data['managed'])
        self.assertTrue(data['active'])


if __name__ == '__main__':
    unittest.main()
