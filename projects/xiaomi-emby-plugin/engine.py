"""Bounded Emby container lifecycle for the Xiaomi plugin market.

只做固定几件事：按锁定镜像创建/启停一个容器，挂载私有配置目录和用户
选择的媒体目录。不提供通用 Docker 接口，也不转发 Emby 的 API。媒体目录
以读写方式挂载，Emby 才能把元数据、字幕写到媒体文件夹。
"""
from __future__ import annotations

import http.client
import json
import os
import re
import secrets
import subprocess
import threading
import time
from pathlib import Path

VERSION = '0.1.0'
# Emby 官方镜像的多架构 manifest（含 amd64 / arm64v8 / arm32v7），
# 与 `latest` 当前指向同一 digest。用 tag + digest 固定，避免被上游重新推送影响。
IMAGE = 'emby/embyserver:4.10.0.40@sha256:3aafff933d3f28d23ed0bc201022abe71c0aa80deb17177566c726b9bbc686c6'
NAME = 'xiaomi-plugin-emby'
LABEL = 'io.xiaomi-plugin.emby.owner'
# Emby 容器内外都用 8096（Emby 默认 Web/API 端口），并发布到 NAS 局域网，
# 否则电视、手机等 Emby 客户端无法连接。
PORT = 8096


class Error(RuntimeError):
    pass


def confined(root, relative):
    """把相对路径限制在用户存储根目录内，拒绝穿越、隐藏目录和符号链接。"""
    if not isinstance(relative, str) or relative.startswith('/') or '\\' in relative:
        raise Error('目录路径无效')
    current = Path(root)
    if current.is_symlink() or not current.is_dir():
        raise Error('用户存储不可用')
    for part in relative.split('/') if relative else []:
        if part in ('', '.', '..') or part.startswith('.') or any(ord(c) < 32 for c in part):
            raise Error('目录路径无效')
        current /= part
        if current.is_symlink() or not current.is_dir():
            raise Error('目录不存在或为符号链接')
    current.resolve().relative_to(Path(root).resolve())
    return current


def atomic_json(path, value):
    tmp = path.with_suffix('.tmp')
    with tmp.open('w', encoding='utf-8') as stream:
        os.chmod(tmp, 0o600)
        json.dump(value, stream)
    tmp.replace(path)


def emby_info():
    """读取 Emby 无需认证的公开信息；任何 HTTP 响应都算服务已就绪。"""
    connection = http.client.HTTPConnection('127.0.0.1', PORT, timeout=6)
    try:
        connection.request('GET', '/System/Info/Public')
        response = connection.getresponse()
        body = response.read(65536)
        if response.status == 200:
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                return {}
            return data if isinstance(data, dict) else {}
        return {}
    except (OSError, http.client.HTTPException) as exc:
        raise Error('Emby 未运行或尚未就绪') from exc
    finally:
        connection.close()


def container_args(config, data):
    """固定参数，不接受调用方传入镜像、端口、挂载或命令。"""
    return ['run', '-d', '--name', NAME, '--label', LABEL + '=' + config['owner'],
            '--restart', 'no', '--memory', '1024m', '--memory-swap', '1024m', '--cpus', '2',
            '--pids-limit', '512', '--security-opt', 'no-new-privileges:true',
            '--log-opt', 'max-size=5m', '--log-opt', 'max-file=2',
            '-e', 'UID=' + str(config['uid']), '-e', 'GID=' + str(config['gid']),
            '-e', 'GIDLIST=' + str(config['gid']), '-e', 'TZ=Asia/Shanghai',
            '-p', '0.0.0.0:' + str(PORT) + ':' + str(PORT),
            '--mount', 'type=bind,src=' + str(data / 'config') + ',dst=/config',
            '--mount', 'type=bind,src=' + config['media'] + ',dst=/mnt/media', IMAGE]


class Engine:
    def __init__(self, data, root, dev=False):
        self.data, self.root, self.dev = Path(data), Path(root), dev
        self.data.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.data, 0o700)
        self.lock = threading.Lock()
        self.busy, self.error = False, ''
        self.worker = None
        self.cfgfile = self.data / 'settings.json'
        self.config = json.loads(self.cfgfile.read_text(encoding='utf-8')) if self.cfgfile.exists() else None

    def docker(self, *args, timeout=30):
        if self.dev:
            raise Error('预览模式不会操作 Docker')
        try:
            result = subprocess.run(['docker', *args], capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Error('Docker 不可用或操作超时') from exc
        if result.returncode:
            raise Error('Docker 操作失败，请检查镜像网络、端口 8096 和可用资源；未修改其他容器')
        return result.stdout

    def owned(self):
        names = self.docker('ps', '-a', '--filter', 'name=^/' + NAME + '$', '--format', '{{.Names}}').splitlines()
        if NAME not in names:
            return None
        item = json.loads(self.docker('inspect', NAME))[0]
        if not self.config or item.get('Config', {}).get('Labels', {}).get(LABEL) != self.config['owner']:
            raise Error('同名容器不属于本插件，拒绝接管')
        return item

    def browse(self, relative):
        folder = confined(self.root, relative)
        return sorted([{'name': p.name, 'path': (relative + '/' if relative else '') + p.name}
                       for p in folder.iterdir()
                       if not p.name.startswith('.') and p.is_dir() and not p.is_symlink()],
                      key=lambda p: p['name'])[:1000]

    def snapshot(self):
        running, ready, error = False, False, self.error
        server_version, wizard = '', None
        if self.config and not self.busy:
            try:
                item = self.owned()
                running = bool(item and item.get('State', {}).get('Running'))
            except Error as exc:
                error = str(exc)
            if running:
                try:
                    info = emby_info()
                    ready = True
                    server_version = str(info.get('Version', ''))[:32]
                    if isinstance(info.get('StartupWizardCompleted'), bool):
                        wizard = info['StartupWizardCompleted']
                except Error:
                    ready = False
        return {'version': VERSION, 'configured': bool(self.config), 'running': running, 'ready': ready,
                'busy': self.busy, 'error': error, 'preview': self.dev, 'port': PORT,
                'directory': self.config['relative'] if self.config else '',
                'serverVersion': server_version, 'wizardCompleted': wizard,
                'imageVersion': '4.10.0.40'}

    def setup(self, relative):
        if self.config:
            raise Error('已完成初始化；现有媒体目录和 Emby 配置不会被覆盖')
        if not isinstance(relative, str) or len(relative) > 1024:
            raise Error('媒体目录无效')
        folder = confined(self.root, relative)
        if ',' in str(folder):
            raise Error('Docker 挂载目录不能包含逗号')
        uid, gid = folder.stat().st_uid, folder.stat().st_gid
        if not uid or not gid:
            raise Error('所选目录须由非 root 的 NAS 用户拥有')
        self.docker('info', '--format', '{{.Architecture}}')
        if NAME in self.docker('ps', '-a', '--format', '{{.Names}}').splitlines():
            raise Error('同名容器已存在，拒绝覆盖')
        cfgdir = self.data / 'config'
        cfgdir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chown(cfgdir, uid, gid)
        os.chmod(cfgdir, 0o700)
        stat = folder.stat()
        self.config = {'owner': secrets.token_hex(24), 'relative': relative, 'media': str(folder),
                       'uid': uid, 'gid': gid, 'device': stat.st_dev, 'inode': stat.st_ino,
                       'enabled': True}
        atomic_json(self.cfgfile, self.config)

    def check_directory(self):
        folder = confined(self.root, self.config['relative'])
        stat = folder.stat()
        if str(folder) != self.config['media'] or (stat.st_dev, stat.st_ino) != (self.config['device'], self.config['inode']):
            raise Error('媒体目录身份已变化，拒绝启动；请先检查存储挂载')

    def start(self):
        if not self.config:
            raise Error('请先初始化')
        self.check_directory()
        item = self.owned()
        if item:
            if not item.get('State', {}).get('Running'):
                self.docker('start', NAME)
        else:
            self.docker('pull', IMAGE, timeout=1800)
            self.check_directory()
            self.docker(*container_args(self.config, self.data), timeout=120)
        self.config['enabled'] = True
        atomic_json(self.cfgfile, self.config)
        # Emby 首次启动要初始化数据库，可能明显慢于 qB；超时只影响提示，容器仍在运行。
        for _ in range(150):
            try:
                emby_info()
                return
            except Error:
                time.sleep(2)
        raise Error('容器已启动，但 Emby 尚未就绪；可稍后刷新页面，首次初始化可能需要数分钟')

    def stop(self, remember=True):
        if not self.config:
            return
        item = self.owned()
        if item and item.get('State', {}).get('Running'):
            self.docker('stop', '--time', '30', NAME)
        if remember:
            self.config['enabled'] = False
            atomic_json(self.cfgfile, self.config)

    def launch(self, action, data):
        if self.dev:
            raise Error('预览模式不会启动服务或修改 NAS')
        if action not in ('setup', 'start', 'stop'):
            raise Error('未知服务操作')
        if not self.lock.acquire(False):
            raise Error('服务操作正在进行，请稍候')
        self.busy, self.error = True, ''

        def work():
            try:
                if action == 'setup':
                    self.setup(data.get('path', ''))
                if action in ('setup', 'start'):
                    self.start()
                else:
                    self.stop()
            except Error as exc:
                self.error = str(exc)
            except Exception:
                self.error = '操作失败；保留已有配置和文件，请检查目录权限与 Docker 状态'
            finally:
                self.busy = False
                self.lock.release()

        self.worker = threading.Thread(target=work, daemon=False)
        self.worker.start()
