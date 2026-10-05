"""fwclient 内网穿透客户端生命周期：配置、启停、版本、升级。

二进制随包分发到 bin/，首次初始化拷贝到插件数据目录后运行，
这样 `fwclient -u` 自升级能写回同一路径。优先以 `-d` 守护方式启动。
"""
from __future__ import annotations

import logging
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path

VERSION = '0.1.0'
BUNDLE_NAME = 'fwclient-linux-arm64'
PLUGIN_PORT = 18180
LOG = logging.getLogger('fwclient-plugin')


class Error(RuntimeError):
    pass


def installed_version():
    parts = Path(__file__).resolve().parent.name.split('-')
    if len(parts) > 2 and parts[-1].isdigit() and parts[-2].isdigit():
        return '-'.join(parts[:-2])
    return VERSION


def atomic_json(path, value):
    tmp = path.with_suffix('.tmp')
    with tmp.open('w', encoding='utf-8') as stream:
        os.chmod(tmp, 0o600)
        json_dump = __import__('json').dump
        json_dump(value, stream)
    tmp.replace(path)


def load_json(path):
    try:
        return __import__('json').loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def validate_server(value):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= 253:
        raise Error('请填写服务器域名或 IP')
    host = value.strip()
    if any(ord(c) < 33 or ord(c) > 126 for c in host) or any(c in host for c in ' /\\@'):
        raise Error('服务器域名含无效字符')
    return host


def validate_token(value):
    if not isinstance(value, str) or not 8 <= len(value) <= 256 or any(ord(c) < 32 for c in value):
        raise Error('访问令牌须为 8 至 256 个可见字符')
    return value


def validate_username_like_unused():
    return None


class Engine:
    def __init__(self, data, runtime, dev=False):
        self.data, self.runtime, self.dev = Path(data), Path(runtime), dev
        self.data.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.data, 0o700)
        self.lock = threading.Lock()
        self.busy, self.error = False, ''
        self.worker = None
        self.cfgfile = self.data / 'settings.json'
        self.config = load_json(self.cfgfile) if self.cfgfile.exists() else None
        if isinstance(self.config, dict) and not self.config.get('server'):
            self.config = None

    @property
    def binary(self):
        return self.data / 'fwclient'

    @property
    def log_file(self):
        return self.data / 'fwclient.log'

    def ensure_binary(self):
        if self.binary.is_file() and os.access(self.binary, os.X_OK):
            return
        source = self.runtime / 'bin' / BUNDLE_NAME
        if not source.is_file():
            source = self.runtime / BUNDLE_NAME
        if not source.is_file():
            raise Error('未找到随包 fwclient 二进制')
        shutil.copy2(source, self.binary)
        os.chmod(self.binary, 0o755)

    def run_cli(self, *args, timeout=30):
        self.ensure_binary()
        try:
            result = subprocess.run(
                [str(self.binary), *args],
                capture_output=True, text=True, timeout=timeout,
                cwd=str(self.data),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Error('fwclient 执行失败或超时') from exc
        output = ((result.stdout or '') + (result.stderr or '')).strip()
        return result.returncode, output

    def client_version(self):
        try:
            code, output = self.run_cli('-v', timeout=10)
        except Error:
            return ''
        if code != 0:
            return ''
        text = output.strip()
        return re.sub(r'^fwclient\s+', '', text)[:80]

    def find_pids(self):
        """只认命令行里带本插件二进制绝对路径的进程。"""
        binary = str(self.binary)
        pids = []
        try:
            entries = os.listdir('/proc')
        except OSError:
            return pids
        for name in entries:
            if not name.isdigit():
                continue
            pid = int(name)
            try:
                raw = Path('/proc') / name / 'cmdline'
                cmdline = raw.read_bytes().replace(b'\x00', b' ').decode('utf-8', 'replace')
            except OSError:
                continue
            if binary in cmdline and 'fwclient' in cmdline:
                pids.append(pid)
        return pids

    def snapshot(self):
        running = bool(self.find_pids())
        version = ''
        error = self.error
        try:
            version = self.client_version()
        except Error as exc:
            if not error:
                error = str(exc)
        return {
            'version': installed_version(),
            'clientVersion': version,
            'configured': bool(self.config),
            'running': running,
            'busy': self.busy,
            'error': error,
            'preview': self.dev,
            'server': self.config.get('server', '') if self.config else '',
            'insecure': bool(self.config.get('insecure')) if self.config else False,
            'hasToken': bool(self.config.get('token')) if self.config else False,
        }

    def setup(self, server, token, insecure=False):
        if self.config:
            raise Error('已完成初始化；如需更换服务器或令牌请先卸载重装')
        server = validate_server(server)
        token = validate_token(token)
        self.ensure_binary()
        self.config = {
            'owner': secrets.token_hex(16),
            'server': server,
            'token': token,
            'insecure': bool(insecure),
            'enabled': True,
        }
        atomic_json(self.cfgfile, self.config)
        os.chmod(self.cfgfile, 0o600)

    def reconfigure(self, server, token, insecure=None):
        """已初始化后修改服务器域名或访问令牌，改完自动重启客户端。

        页面不回显令牌明文，所以留空表示「令牌保持不变」——只有用户真的填了
        新值才会覆盖。
        """
        if not self.config:
            raise Error('请先初始化')
        new_server = validate_server(server)
        if isinstance(token, str) and token:
            new_token = validate_token(token)
        else:
            new_token = self.config.get('token', '')
            if not new_token:
                raise Error('请填写访问令牌')
        new_insecure = bool(insecure) if insecure is not None else bool(self.config.get('insecure'))
        if (new_server == self.config.get('server') and new_token == self.config.get('token')
                and new_insecure == bool(self.config.get('insecure'))):
            raise Error('没有需要修改的内容')
        if self.find_pids():
            self.stop(remember=False)
        self.config['server'] = new_server
        self.config['token'] = new_token
        self.config['insecure'] = new_insecure
        self.config['enabled'] = True
        atomic_json(self.cfgfile, self.config)
        os.chmod(self.cfgfile, 0o600)
        self.start()

    def start(self):
        if not self.config:
            raise Error('请先填写服务器域名与访问令牌')
        if self.find_pids():
            return
        self.ensure_binary()
        argv = [str(self.binary), '-s', self.config['server'], '-t', self.config['token']]
        if self.config.get('insecure'):
            argv.append('-insecure')
        argv.append('-d')
        try:
            # 优先后台守护：父进程会立刻退出，子进程继续跑
            result = subprocess.run(
                argv, capture_output=True, text=True, timeout=15, cwd=str(self.data),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Error('启动 fwclient 失败') from exc
        if result.returncode != 0:
            message = ((result.stdout or '') + (result.stderr or '')).strip()[:200]
            raise Error(message or 'fwclient 启动失败')
        for _ in range(20):
            if self.find_pids():
                self.config['enabled'] = True
                atomic_json(self.cfgfile, self.config)
                return
            time.sleep(0.25)
        # -d 在部分环境未真正拉起时，再以前台方式托管一次
        try:
            subprocess.Popen(
                argv[:-1], cwd=str(self.data),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            raise Error('fwclient 启动失败') from exc
        for _ in range(20):
            if self.find_pids():
                self.config['enabled'] = True
                atomic_json(self.cfgfile, self.config)
                return
            time.sleep(0.25)
        raise Error('fwclient 已发出启动命令，但进程未就绪；请查看 fwclient.log')

    # 停止流程的轮询节奏与等待预算：沿用旧实现（20 × 0.15s ≈ 3s）
    STOP_POLL_ROUNDS = 20
    STOP_POLL_INTERVAL = 0.15
    # `fwclient -k` 与能力探测 `fwclient -h` 的最长执行时间，避免卡住拖住停止流程
    SHUTDOWN_TIMEOUT = 10
    PROBE_TIMEOUT = 5

    def supports_shutdown_command(self):
        """用本地 `fwclient -h` 探测是否支持规范关闭 `-k`。

        只跑本地帮助，不联网、不触发版本检查或自动升级；
        返回 True（帮助里列出 -k）/ False（帮助里没有 -k，明确不支持）/
        None（探测失败或没有输出，无法判断）。
        """
        try:
            _, output = self.run_cli('-h', timeout=self.PROBE_TIMEOUT)
        except Error:
            return None
        if not output:
            return None
        # 匹配独立的 -k 选项，如 "  -k          优雅退出" 或 "  -k, --kill"
        return bool(re.search(r'(?:^|\s)-k(?=\s|$|,|/|\))', output))

    def shutdown_command(self):
        """执行规范关闭命令 `fwclient -k`，返回 (是否成功, 失败原因)。

        走 run_cli，因此与 start() 使用同一二进制路径、同一 cwd（插件数据目录），
        且不传 env，运行身份与环境也一致；`-k` 不需要 `-s/-t` 配置文件参数。
        """
        try:
            code, output = self.run_cli('-k', timeout=self.SHUTDOWN_TIMEOUT)
        except Error as exc:
            return False, str(exc)
        if code != 0:
            return False, output[:200] or ('fwclient -k 退出码 ' + str(code))
        return True, ''

    def wait_exit(self, deadline):
        """按旧节奏轮询等待进程退出；返回截止时间内是否已全部退出。

        判据是「当前没有任何 fwclient 进程」，整个停止流程共用一个截止时间，
        所以总等待时长不超过旧实现。
        """
        while time.monotonic() < deadline:
            if not self.find_pids():
                return True
            time.sleep(self.STOP_POLL_INTERVAL)
        return not self.find_pids()

    def mark_stopped(self, remember):
        """停止结束后落盘 enabled=False（remember=False 时只停不记）。"""
        if remember and self.config:
            self.config['enabled'] = False
            atomic_json(self.cfgfile, self.config)

    def stop(self, remember=True):
        if self.dev:
            raise Error('预览模式不会停止穿透客户端')
        note = ''
        if self.find_pids():
            # 全流程共用一个等待预算，正常路径耗时与旧实现一致（≤3s）
            deadline = time.monotonic() + self.STOP_POLL_ROUNDS * self.STOP_POLL_INTERVAL
            # 一、先廉价探测 `-k` 是否受支持：老二进制遇到未知参数可能不报错、反而再拉起一个实例
            support = self.supports_shutdown_command()
            if support is False:
                # 预期路径：包内老版本还不支持 -k（下次启动会自动升级），只记日志、不占用页面提示条
                LOG.warning('停止服务：当前 fwclient 不支持 -k（帮助里没有该选项），改用信号终止')
            else:
                ok, reason = self.shutdown_command()
                # 成功判据：当前没有任何 fwclient 进程残留（含 -k 可能多拉起的重复实例）
                if ok and self.wait_exit(deadline):
                    self.mark_stopped(remember)
                    return
                if ok:
                    reason = '进程未在等待时间内退出'
                elif support is None:
                    reason = '未能确认是否支持 -k：' + reason
                note = ('fwclient -k 未生效（' + reason + '），已回退到信号终止')[:240]
                LOG.warning('停止服务：%s', note)
            # 二、回退到原有信号流程：对所有残留 pid 做 SIGTERM → 轮询 → SIGKILL
            for pid in self.find_pids():
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    continue
            self.wait_exit(deadline)
            for pid in self.find_pids():
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    continue
        self.mark_stopped(remember)
        if note:
            self.error = note

    def upgrade(self):
        if self.dev:
            raise Error('预览模式不会升级客户端')
        self.ensure_binary()
        code, output = self.run_cli('-u', timeout=180)
        if code != 0:
            raise Error(output[:300] or '升级失败')
        return output[:300] or '升级完成'

    def log_tail(self, lines=20):
        try:
            text = self.log_file.read_text(encoding='utf-8', errors='replace')
        except OSError:
            return ''
        parts = [line for line in text.splitlines() if line.strip()]
        return '\n'.join(parts[-lines:])[-4000:]

    def launch(self, action, data):
        if action not in ('setup', 'start', 'stop', 'upgrade', 'reconfigure'):
            raise Error('未知操作')
        if self.dev:
            raise Error('预览模式不会启动或修改穿透客户端')
        if not self.lock.acquire(False):
            raise Error('操作正在进行，请稍候')
        self.busy, self.error = True, ''

        def work():
            try:
                if action == 'setup':
                    self.setup(data.get('server', ''), data.get('token', ''), data.get('insecure', False))
                    self.start()
                elif action == 'reconfigure':
                    self.reconfigure(data.get('server', ''), data.get('token', ''), data.get('insecure'))
                elif action == 'start':
                    self.start()
                elif action == 'stop':
                    self.stop()
                elif action == 'upgrade':
                    self.error = ''
                    message = self.upgrade()
                    self.error = message
            except Error as exc:
                self.error = str(exc)
            except Exception:
                self.error = '操作失败；请检查目录权限与二进制是否完整'
            finally:
                self.busy = False
                self.lock.release()

        self.worker = threading.Thread(target=work, daemon=False)
        self.worker.start()
