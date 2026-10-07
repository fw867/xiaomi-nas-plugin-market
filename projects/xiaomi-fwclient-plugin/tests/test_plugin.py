import http.client
import json
import os
import re
import signal
import stat
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from engine import Engine, Error, validate_server, validate_token, installed_version, VERSION
from server import Server

# Windows 没有 SIGKILL（线上是 Linux）：补一个等效编号，便于本地跑通回退流程
SIGKILL = getattr(signal, 'SIGKILL', 9)

# 二进制 `-h` 帮助输出的三种形态：老版本（无 -k/-dir）、过渡版本（有 -k）、新版本（有 -k/-dir）
HELP_OLD = ('用法:\n  fwclient -s <服务器> -t <令牌> [选项]\n\n选项:\n'
            '  -s string   服务器域名或 IP\n  -t string   访问令牌\n'
            '  -d          以守护进程在后台运行\n  -u          检查并升级到最新版本\n'
            '  -v          显示版本号\n  -h          显示帮助\n')
HELP_MID = HELP_OLD.replace('  -h          显示帮助\n',
                            '  -k          优雅退出并等待隧道排空\n  -h          显示帮助\n')
HELP_NEW = HELP_MID.replace('  -v          显示版本号\n',
                            '  -dir string 运行数据目录（pid / 日志 / 设备标识）\n'
                            '  -v          显示版本号\n')


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


class StopTests(unittest.TestCase):
    """停止服务优先执行规范关闭命令 `fwclient -k`，不支持或未生效才回退到信号终止。"""

    # 二进制 `-h` 帮助输出：新版列出 -k，包内老版本没有
    HELP_WITH_K = HELP_NEW
    HELP_WITHOUT_K = HELP_OLD

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / 'data'
        runtime = Path(__file__).resolve().parents[1]
        self.engine = Engine(self.data, runtime)
        self.engine.config = {
            'server': 'fw867.com', 'token': 'tk_' + 'f' * 20, 'enabled': True,
        }

    def fake_run(self, calls, help_output, kill_code=0, kill_output='', on_kill=None):
        """打桩 subprocess.run：`-h` 返回 help_output（None 表示执行失败），`-k` 返回给定退出码。"""

        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            if '-h' in argv:
                if help_output is None:
                    raise OSError(8, 'Exec format error')
                return subprocess.CompletedProcess(argv, 0, help_output, '')
            if on_kill is not None:
                on_kill()
            return subprocess.CompletedProcess(argv, kill_code, kill_output, '')

        return run

    def test_stop_uses_shutdown_command_without_signals(self):
        """③ 探测到支持 `-k`、`-k` 生效且无残留：不发任何信号。"""
        running, calls = [4321], []
        run = self.fake_run(calls, self.HELP_WITH_K, on_kill=running.clear)

        with self.assertNoLogs('fwclient-plugin', level='WARNING'), \
                patch('engine.Engine.find_pids', side_effect=lambda: list(running)), \
                patch('engine.subprocess.run', side_effect=run), \
                patch('engine.os.kill') as kill:
            self.assertIsNone(self.engine.stop())
        self.assertFalse(kill.called, '规范关闭成功后不应再发 SIGTERM/SIGKILL')
        self.assertEqual([argv for argv, _ in calls],
                         [[str(self.engine.binary), '-h'], [str(self.engine.binary), '-k']])
        for _, kwargs in calls:
            self.assertEqual(kwargs['cwd'], str(self.engine.data))
            self.assertNotIn('env', kwargs)  # 不传 env = 继承当前环境，与 start() 相同
        self.assertEqual(calls[0][1]['timeout'], Engine.PROBE_TIMEOUT)
        self.assertEqual(calls[1][1]['timeout'], Engine.SHUTDOWN_TIMEOUT)
        self.assertEqual(self.engine.error, '')
        self.assertFalse(self.engine.config['enabled'])
        self.assertFalse(json.loads(self.engine.cfgfile.read_text(encoding='utf-8'))['enabled'])

    def test_stop_skips_shutdown_command_when_unsupported(self):
        """① 老二进制（帮助里没有 `-k`）：不调用 -k，直接信号停止且停干净。"""
        running, calls, killed = [4321], [], []
        run = self.fake_run(calls, self.HELP_WITHOUT_K)

        def kill(pid, sig):
            killed.append(sig)
            if sig == signal.SIGTERM:
                running.clear()

        with self.assertLogs('fwclient-plugin', level='WARNING') as logs, \
                patch('engine.Engine.find_pids', side_effect=lambda: list(running)), \
                patch('engine.subprocess.run', side_effect=run), \
                patch('engine.os.kill', side_effect=kill):
            self.engine.stop()
        self.assertEqual([argv for argv, _ in calls], [[str(self.engine.binary), '-h']])
        self.assertEqual(killed, [signal.SIGTERM])
        self.assertEqual(running, [], '回退后不应残留 fwclient 进程')
        # 预期路径：老版本还不支持 -k，只记日志；页面提示条留给真正异常
        self.assertEqual(self.engine.error, '')
        self.assertIn('不支持 -k', logs.output[0])
        self.assertFalse(self.engine.config['enabled'])

    def test_stop_tries_shutdown_command_when_probe_inconclusive(self):
        """探测不确定（`-h` 执行失败）：退化成先试 `-k`。"""
        running, calls = [4321], []
        run = self.fake_run(calls, None, on_kill=running.clear)

        with patch('engine.Engine.find_pids', side_effect=lambda: list(running)), \
                patch('engine.subprocess.run', side_effect=run), \
                patch('engine.os.kill') as kill:
            self.engine.stop()
        self.assertFalse(kill.called)
        self.assertEqual([argv for argv, _ in calls],
                         [[str(self.engine.binary), '-h'], [str(self.engine.binary), '-k']])
        self.assertEqual(self.engine.error, '')

    def test_stop_waits_for_shutdown_command_process_to_exit(self):
        """`-k` 成功、进程稍后才退出：轮询等待即可，仍不发信号。"""
        remaining, calls = [3], []

        def find():
            if remaining[0] > 0:
                remaining[0] -= 1
                return [4321]
            return []

        run = self.fake_run(calls, self.HELP_WITH_K)

        with patch.object(Engine, 'STOP_POLL_INTERVAL', 0.001), \
                patch('engine.Engine.find_pids', side_effect=find), \
                patch('engine.subprocess.run', side_effect=run), \
                patch('engine.os.kill') as kill:
            self.engine.stop()
        self.assertFalse(kill.called)
        self.assertEqual(self.engine.error, '')

    def test_stop_falls_back_when_shutdown_command_fails(self):
        """② `-k` 非 0 退出：如实记录原因并回退到 SIGTERM → SIGKILL。"""
        running, killed, calls = [4321], [], []
        run = self.fake_run(calls, self.HELP_WITH_K, kill_code=2,
                            kill_output='flag provided but not defined: -k')

        def kill(pid, sig):
            killed.append(sig)
            if sig == signal.SIGTERM:
                running.clear()

        with self.assertLogs('fwclient-plugin', level='WARNING') as logs, \
                patch('engine.Engine.find_pids', side_effect=lambda: list(running)), \
                patch('engine.subprocess.run', side_effect=run), \
                patch('engine.os.kill', side_effect=kill):
            self.engine.stop()
        self.assertEqual([argv for argv, _ in calls],
                         [[str(self.engine.binary), '-h'], [str(self.engine.binary), '-k']])
        self.assertEqual(killed, [signal.SIGTERM])
        self.assertIn('flag provided but not defined', self.engine.error)
        self.assertIn('fwclient -k 未生效', logs.output[0])
        self.assertFalse(self.engine.config['enabled'])

    def test_stop_falls_back_when_shutdown_command_cannot_run(self):
        """② `-k` 抛异常（二进制不存在/执行失败）：记录原因后仍能停掉进程。"""
        running, killed, calls = [4321], [], []

        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            if '-h' in argv:
                return subprocess.CompletedProcess(argv, 0, self.HELP_WITH_K, '')
            raise FileNotFoundError(2, 'No such file or directory')

        def kill(pid, sig):
            killed.append(sig)
            if sig == signal.SIGTERM:
                running.clear()

        with self.assertLogs('fwclient-plugin', level='WARNING') as logs, \
                patch('engine.Engine.find_pids', side_effect=lambda: list(running)), \
                patch('engine.subprocess.run', side_effect=run), \
                patch('engine.os.kill', side_effect=kill):
            self.engine.stop()
        self.assertEqual(killed, [signal.SIGTERM])
        self.assertIn('fwclient 执行失败或超时', self.engine.error)
        self.assertIn('fwclient 执行失败或超时', logs.output[0])

    def test_stop_falls_back_when_processes_survive_shutdown_command(self):
        """②③ `-k` 返回 0 但仍有 fwclient 进程（含被多拉起的重复实例）：全部清掉。"""
        pids, killed, calls, polls = [4321], [], [], []

        def find():
            polls.append(1)
            return list(pids)

        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            if '-h' in argv:
                return subprocess.CompletedProcess(argv, 0, self.HELP_WITH_K, '')
            # 模拟老二进制遇到未知参数：不报错，反而又拉起一个实例，且都赖着不退
            pids.append(8765)
            return subprocess.CompletedProcess(argv, 0, '', '')

        def kill(pid, sig):
            killed.append((pid, sig))
            if sig == SIGKILL and pid in pids:
                pids.remove(pid)

        with self.assertLogs('fwclient-plugin', level='WARNING') as logs, \
                patch.object(signal, 'SIGKILL', SIGKILL, create=True), \
                patch.object(Engine, 'STOP_POLL_INTERVAL', 0.01), \
                patch('engine.Engine.find_pids', side_effect=find), \
                patch('engine.subprocess.run', side_effect=run), \
                patch('engine.os.kill', side_effect=kill):
            self.engine.stop()
        self.assertEqual(killed, [(4321, signal.SIGTERM), (8765, signal.SIGTERM),
                                  (4321, SIGKILL), (8765, SIGKILL)])
        self.assertEqual(pids, [], '回退必须清掉所有 fwclient 进程')
        self.assertGreater(len(polls), 5, '回退前应按现有节奏轮询等待超时')
        self.assertIn('进程未在等待时间内退出', self.engine.error)
        self.assertIn('已回退到信号终止', logs.output[0])
        self.assertFalse(self.engine.config['enabled'])

    def test_stop_without_process_is_unchanged(self):
        """④ 未在运行：不探测、不执行 -k、不发信号、不报错，返回结构与 remember 语义不变。"""
        with patch('engine.Engine.find_pids', return_value=[]), \
                patch('engine.subprocess.run') as run, \
                patch('engine.os.kill') as kill:
            self.assertIsNone(self.engine.stop())
        self.assertFalse(run.called, '进程未运行时不必探测或执行 -k')
        self.assertFalse(kill.called)
        self.assertEqual(self.engine.error, '')
        self.assertFalse(self.engine.config['enabled'])

    def test_stop_without_process_keeps_config_when_not_remembering(self):
        """④ remember=False 时只停不记：不动 enabled。"""
        with patch('engine.Engine.find_pids', return_value=[]), \
                patch('engine.subprocess.run') as run, \
                patch('engine.os.kill') as kill:
            self.engine.stop(remember=False)
        self.assertFalse(run.called)
        self.assertFalse(kill.called)
        self.assertTrue(self.engine.config['enabled'])


class PathTests(unittest.TestCase):
    """新版客户端的 `-dir` 启动参数与运行日志候选路径。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / 'data'
        runtime = Path(__file__).resolve().parents[1]
        self.engine = Engine(self.data, runtime)
        self.engine.config = {
            'server': 'fw867.com', 'token': 'tk_' + 'f' * 20, 'insecure': True, 'enabled': False,
        }

    def start_argv(self, help_output):
        """跑一次 start() 并返回实际 argv：find_pids 首次为空、之后返回一个 pid。"""
        calls, seen = [], []

        def find():
            seen.append(1)
            return [] if len(seen) == 1 else [4321]

        def run(argv, **kwargs):
            calls.append(argv)
            if '-h' in argv:
                if help_output is None:
                    raise OSError(8, 'Exec format error')
                return subprocess.CompletedProcess(argv, 0, help_output, '')
            return subprocess.CompletedProcess(argv, 0, '', '')

        with patch('engine.Engine.find_pids', side_effect=find), \
                patch('engine.subprocess.run', side_effect=run):
            self.engine.start()
        return calls[-1]

    def test_start_adds_dir_when_supported(self):
        """① 探测支持 -dir：启动 argv 带上 -dir <插件运行目录>，目录按 0700 创建。"""
        argv = self.start_argv(HELP_NEW)
        self.assertEqual(argv, [str(self.engine.binary), '-s', 'fw867.com', '-t', 'tk_' + 'f' * 20,
                                '-insecure', '-dir', str(self.engine.run_dir), '-d'])
        self.assertTrue(self.engine.run_dir.is_dir())
        if os.name == 'posix':
            self.assertEqual(stat.S_IMODE(os.stat(self.engine.run_dir).st_mode), 0o700)

    def test_start_keeps_old_argv_when_dir_unsupported(self):
        """② 老二进制（帮助里没有 -dir）：argv 与旧行为完全一致，另不创建运行目录。"""
        argv = self.start_argv(HELP_OLD)
        self.assertEqual(argv, [str(self.engine.binary), '-s', 'fw867.com', '-t', 'tk_' + 'f' * 20,
                                '-insecure', '-d'])
        self.assertFalse(self.engine.run_dir.exists())

    def test_start_keeps_old_argv_when_probe_inconclusive(self):
        """② 探测失败（`-h` 执行失败）：同样按旧方式启动，一个新增参数都不加。"""
        argv = self.start_argv(None)
        self.assertEqual(argv, [str(self.engine.binary), '-s', 'fw867.com', '-t', 'tk_' + 'f' * 20,
                                '-insecure', '-d'])
        self.assertFalse(self.engine.run_dir.exists())

    def test_start_keeps_dir_before_daemon_flag(self):
        """-d 始终在末尾（前台托管分支用 argv[:-1]，-dir 不能被截掉）。"""
        self.engine.config['insecure'] = False
        argv = self.start_argv(HELP_NEW)
        self.assertEqual(argv, [str(self.engine.binary), '-s', 'fw867.com', '-t', 'tk_' + 'f' * 20,
                                '-dir', str(self.engine.run_dir), '-d'])

    def test_help_probe_is_cached(self):
        """能力探测结果缓存：多次判断参数支持情况只跑一次 `-h`。"""
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, HELP_NEW, '')

        with patch('engine.subprocess.run', side_effect=run):
            self.assertTrue(self.engine.supports_flag('-dir'))
            self.assertTrue(self.engine.supports_flag('-k'))
        self.assertEqual(len(calls), 1)

    def test_log_candidate_paths(self):
        """候选顺序：插件 -dir → 新默认 /var/log/fwclient → 旧位置（二进制同目录）。"""
        candidates = [str(p) for p in self.engine.log_candidates()]
        self.assertEqual(candidates[0], str(self.engine.run_dir / 'fwclient.log'))
        self.assertEqual(candidates[1], os.path.join(os.sep, 'var', 'log', 'fwclient', 'fwclient.log'))
        self.assertEqual(candidates[2], str(self.engine.data / 'fwclient.log'))

    def test_log_reads_first_existing_candidate(self):
        """③ 三个候选里只有其中一个存在时都能读到，都不存在时返回中文提示。"""
        root = Path(self.tmp.name)
        first, second, third = root / '1.log', root / '2.log', root / '3.log'
        with patch.object(Engine, 'log_candidates', lambda self: [first, second, third]):
            self.assertEqual(self.engine.log_tail(), Engine.LOG_MISSING)
            third.write_text('第三候选\n', encoding='utf-8')
            self.assertEqual(self.engine.log_tail(), '第三候选')
            second.write_text('第二候选\n', encoding='utf-8')
            self.assertEqual(self.engine.log_tail(), '第二候选')
            first.write_text('第一候选\n', encoding='utf-8')
            self.assertEqual(self.engine.log_tail(), '第一候选')

    def test_log_prefers_plugin_dir_then_legacy_location(self):
        """真实候选链：-dir 下的日志优先；只有旧位置存在时读旧位置（兼容老版本）。"""
        with patch.object(Engine, 'DEFAULT_LOG_FILE', Path(self.tmp.name) / 'missing.log'):
            legacy = self.engine.data / 'fwclient.log'
            legacy.write_text('旧位置日志\n', encoding='utf-8')
            self.assertEqual(self.engine.log_tail(), '旧位置日志')
            run_log = self.engine.run_dir / 'fwclient.log'
            run_log.parent.mkdir(parents=True, exist_ok=True)
            run_log.write_text('运行目录日志\n第一行\n第二行\n', encoding='utf-8')
            self.assertEqual(self.engine.log_tail(), '运行目录日志\n第一行\n第二行')

    def test_log_reads_new_default_location(self):
        """只有新默认位置（/var/log/fwclient）存在时也能读到。"""
        default = Path(self.tmp.name) / 'var-log-fwclient.log'
        default.write_text('新默认位置日志\n', encoding='utf-8')
        with patch.object(Engine, 'DEFAULT_LOG_FILE', default):
            self.assertEqual(self.engine.log_tail(), '新默认位置日志')

    def test_log_empty_missing_or_unreadable_is_safe(self):
        """④ 日志不存在 / 为空 / 读不到（正在轮转）都不抛异常。"""
        with patch.object(Engine, 'DEFAULT_LOG_FILE', Path(self.tmp.name) / 'missing.log'):
            self.assertEqual(self.engine.log_tail(), Engine.LOG_MISSING)
            legacy = self.engine.data / 'fwclient.log'
            legacy.write_text('', encoding='utf-8')
            self.assertEqual(self.engine.log_tail(), '')  # 第一个存在的候选为空 → 页面显示「暂无」
            legacy.unlink()
            legacy.mkdir()  # 目录冒充日志文件：读取失败，换下一个候选
            self.assertEqual(self.engine.log_tail(), Engine.LOG_MISSING)
            legacy.rmdir()
            run_log = self.engine.run_dir / 'fwclient.log'
            run_log.parent.mkdir(parents=True, exist_ok=True)
            run_log.write_text('', encoding='utf-8')
            legacy.write_text('旧位置日志\n', encoding='utf-8')
            self.assertEqual(self.engine.log_tail(), '')  # 取第一个存在的候选，不回退到旧位置


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

    def test_log_endpoint_reports_missing_log(self):
        """日志候选全不存在时 /api/log 仍返回 200 与中文提示，不是 500。"""
        missing = Path(self.tmp.name) / 'none.log'
        with patch.object(Engine, 'log_candidates', lambda self: [missing]):
            code, body = self.request('GET', '/api/log', headers=self.auth())
        self.assertEqual(code, 200)
        data = json.loads(body.decode())
        self.assertTrue(data['ok'])
        self.assertEqual(data['log'], Engine.LOG_MISSING)
        self.assertIn('未找到运行日志', data['log'])

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
        # 以 script 自身 URL 为基址拼相对路径（Windows 客户端下 location 可能带盘符）
        self.assertIn("fetch(assetUrl('api' + path", script)


if __name__ == '__main__':
    unittest.main()
