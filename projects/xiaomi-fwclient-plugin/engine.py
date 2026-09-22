"""fwclient 内网穿透客户端生命周期：配置、启停、版本、升级。

二进制随包分发到 bin/，首次初始化拷贝到插件数据目录后运行，
这样 `fwclient -u` 自升级能写回同一路径。优先以 `-d` 守护方式启动。
"""
from __future__ import annotations

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

    def stop(self, remember=True):
        if self.dev:
            raise Error('预览模式不会停止穿透客户端')
        pids = self.find_pids()
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                continue
        for _ in range(20):
            if not self.find_pids():
                break
            time.sleep(0.15)
        for pid in self.find_pids():
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                continue
        if remember and self.config:
            self.config['enabled'] = False
            atomic_json(self.cfgfile, self.config)

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
        if action not in ('setup', 'start', 'stop', 'upgrade'):
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
