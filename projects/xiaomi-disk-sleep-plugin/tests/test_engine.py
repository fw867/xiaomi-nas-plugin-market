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


if __name__ == '__main__':
    unittest.main()
