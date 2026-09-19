"""Owner-authenticated, same-origin Emby console.

只暴露插件自己的状态页和固定操作，不代理 Emby 的 Web/API，也不提供通用
Docker 接口。Emby 本体由 NAS 局域网上的 8096 端口直接对外提供。
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from engine import Engine, Error, PORT, installed_version

WEB = Path(__file__).resolve().parent / 'web'
TTL = 86400


def lan_ip():
    """取本机在局域网里的地址；取不到返回空串。

    用「连一个外部地址但不真发包」的办法让内核挑默认出口对应的网卡地址，
    比 gethostbyname(gethostname()) 可靠——后者在没有 hosts 记录时常常
    返回 127.0.1.1。UDP connect 不会产生任何流量。
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(('223.5.5.5', 53))
        address = probe.getsockname()[0]
        return address if address and not address.startswith('127.') else ''
    except OSError:
        return ''
    finally:
        probe.close()


STATIC = {
    '/app.js': ('app.js', 'application/javascript; charset=utf-8'),
    '/styles.css': ('styles.css', 'text/css; charset=utf-8'),
    '/assets/emby.png': ('assets/emby.png', 'image/png'),
}


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, engine, user, dev=False):
        self.engine, self.user, self.dev = engine, user, dev
        keyfile = engine.data / 'session.key'
        if not keyfile.exists():
            with keyfile.open('xb') as stream:
                os.chmod(keyfile, 0o600)
                stream.write(secrets.token_bytes(32))
        self.key = keyfile.read_bytes()
        self.request_slots = threading.BoundedSemaphore(8)
        super().__init__(addr, Handler)


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(25)

    def log_message(self, *args):
        pass

    def send(self, code, data, mime='application/json; charset=utf-8'):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy',
                         "default-src 'self'; img-src 'self' data:; style-src 'self'; "
                         "script-src 'self'; connect-src 'self'; frame-ancestors 'self'; base-uri 'none'")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def sign(self, text):
        return hmac.new(self.server.key, text.encode(), hashlib.sha256).hexdigest()

    def session(self):
        token = self.headers.get('X-Emby-Session', '')
        try:
            payload, signature = token.rsplit('.', 1)
            if len(token) > 200 or int(payload.split('.')[0]) < time.time():
                return None
            return token if secrets.compare_digest(signature, self.sign(payload)) else None
        except (ValueError, UnicodeError):
            return None

    def trusted(self):
        """沿用生态约定：设备所有者客户端的客户端证书，或 NAS 本机回环访问。"""
        if self.server.dev:
            return True
        dn = self.headers.get('X-Xiaomi-Client-DN', '')
        if self.headers.get('X-Xiaomi-Client-Verify') == 'SUCCESS' and \
                re.search(r'CN=nas\.' + re.escape(self.server.user.lstrip('u')) + r'\.', dn):
            return True
        try:
            return ipaddress.ip_address(self.headers.get('X-Real-IP', '')).is_loopback
        except ValueError:
            return False

    def require(self, write=False):
        token = self.session()
        if not token:
            self.send(401, {'ok': False, 'error': '请从设备所有者的小米客户端重新打开插件'})
        elif write and not secrets.compare_digest(self.headers.get('X-CSRF-Token', ''), self.sign('csrf:' + token)):
            self.send(403, {'ok': False, 'error': '操作令牌失效，请重新打开插件'})
        else:
            return token
        return None

    def address(self):
        """拼出局域网里能直接打开的 Emby 地址。

        不能靠请求的 Host：小米客户端是经客户端自己的隧道访问 NAS 的，
        到这里时 Host 已经被改写成 127.0.0.1，拼出的地址在电视/手机上打不开。
        本机网卡地址取不到时才退回 Host。
        """
        host = lan_ip()
        if not host:
            host = re.sub(r':\d+$', '', self.headers.get('Host', '').strip())
            if not re.fullmatch(r'[A-Za-z0-9.\-]{1,253}', host):
                return ''
        return host + ':' + str(PORT)

    def do_GET(self):
        route = urlsplit(self.path)
        if route.path == '/healthz':
            return self.send(200, {'ok': True, 'version': installed_version()})
        if route.path in ('/', '/index.html'):
            token = ''
            if self.trusted():
                payload = str(int(time.time()) + TTL) + '.' + secrets.token_hex(16)
                token = payload + '.' + self.sign(payload)
            html = (WEB / 'index.html').read_text(encoding='utf-8')
            html = html.replace('__SESSION_TOKEN__', token)
            html = html.replace('__CSRF_TOKEN__', self.sign('csrf:' + token) if token else '')
            html = html.replace('__PLUGIN_VERSION__', installed_version())
            return self.send(200, html.encode(), 'text/html; charset=utf-8')
        if route.path in STATIC:
            name, mime = STATIC[route.path]
            return self.send(200, (WEB / name).read_bytes(), mime)
        token = self.require()
        if not token:
            return
        if not self.server.request_slots.acquire(False):
            return self.send(429, {'ok': False, 'error': '请求较多，请稍后重试'})
        try:
            if route.path == '/api/status':
                result = self.server.engine.snapshot()
                result['address'] = self.address()
            elif route.path == '/api/browse':
                result = {'items': self.server.engine.browse(parse_qs(route.query).get('path', [''])[0])}
            else:
                return self.send(404, {'ok': False, 'error': 'not found'})
            self.send(200, {'ok': True, **result})
        except (Error, ValueError, OSError) as exc:
            self.send(400, {'ok': False, 'error': str(exc) if isinstance(exc, Error) else '读取失败，请检查存储或服务状态'})
        finally:
            self.server.request_slots.release()

    def do_POST(self):
        token = self.require(True)
        if not token:
            return
        if not self.server.request_slots.acquire(False):
            return self.send(429, {'ok': False, 'error': '请求较多，请稍后重试'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 64 * 1024 or self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                raise Error('请求格式无效或超过大小限制')
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise Error('请求格式无效')
            action = urlsplit(self.path).path.removeprefix('/api/')
            if action.startswith('service/'):
                self.server.engine.launch(action.split('/')[1], data)
                return self.send(202, {'ok': True})
            if action == 'setup':
                value = data.get('path', '')
                if not isinstance(value, str) or len(value) > 1024:
                    raise Error('目录路径无效')
                self.server.engine.launch('setup', {'path': value})
                return self.send(202, {'ok': True})
            self.send(404, {'ok': False, 'error': 'not found'})
        except (Error, ValueError, OSError) as exc:
            self.send(400, {'ok': False, 'error': str(exc) if isinstance(exc, Error) else '操作失败，请检查输入'})
        finally:
            self.server.request_slots.release()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dev', action='store_true')
    parser.add_argument('--stop-owned', action='store_true')
    args = parser.parse_args()
    data = os.environ.get('DATA_DIR') or (tempfile.mkdtemp(prefix='emby-preview-') if args.dev else '/data/plugin/emby/data')
    root = os.environ.get('LOCAL_ROOT', data if args.dev else '')
    user = os.environ.get('NAS_USER_ID', 'u123456' if args.dev else '')
    if not root or not re.fullmatch(r'u[0-9]+', user):
        raise SystemExit('LOCAL_ROOT and NAS_USER_ID are required')
    engine = Engine(data, root, args.dev)
    if args.stop_owned:
        engine.stop(remember=False)
        return
    server = Server(('127.0.0.1', int(os.environ.get('PORT', 18150))), engine, user, args.dev)
    if engine.config and engine.config.get('enabled') and not args.dev:
        engine.launch('start', {})
    print('Emby plugin listening on http://127.0.0.1:' + str(server.server_port), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
