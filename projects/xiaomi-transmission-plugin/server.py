"""Owner-authenticated Transmission Docker plugin UI."""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import http.client
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import socket
import tempfile
import threading
import time
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from engine import Engine, Error, PORT, installed_version, tr_rpc_probe, webui_path

WEB = Path(__file__).resolve().parent / 'web'
TTL = 86400

# 同源控制台：插件页点「打开控制台」→ /console/?t=<插件会话令牌> 换一个签名 Cookie
# → twc 静态页（装在 <配置目录>/webui/）→ 它的 rpcpath='../rpc' 打到 /rpc，由插件转发给容器。
# 走同源路径而不是直连 9091，才能在局域网之外（客户端的远程通道）也能打开。
CONSOLE_PATH = '/console'
CONSOLE_COOKIE = 'tr_console'
CONSOLE_RPC_PATH = '/rpc'
CONSOLE_TOKEN_TTL = 12 * 3600
MAX_RPC_BYTES = 4 * 1024 * 1024
# 控制台是第三方页面（twc），允许它自己的内联脚本与字体，其余仍限同源
CONSOLE_CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
               "script-src 'self' 'unsafe-inline'; connect-src 'self'; "
               "font-src 'self' data:; frame-ancestors 'self'; base-uri 'none'")
# twc 里 json/字体等扩展名在部分系统 mimetypes 里认不出来；配合 nosniff 会直接加载失败，
# 表现就是「页面元素都在、文字全丢」。这里显式给全。
CONSOLE_MIME = {
    '.json': 'application/json; charset=utf-8',
    '.js': 'application/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.html': 'text/html; charset=utf-8',
    '.svg': 'image/svg+xml',
    '.png': 'image/png',
    '.gif': 'image/gif',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.ico': 'image/x-icon',
    '.woff': 'font/woff',
    '.woff2': 'font/woff2',
    '.ttf': 'font/ttf',
    '.eot': 'application/vnd.ms-fontobject',
    '.map': 'application/json; charset=utf-8',
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
        self.request_slots = threading.BoundedSemaphore(12)
        super().__init__(addr, Handler)


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(25)

    def log_message(self, *args):
        pass

    def send(self, code, data, mime='application/json; charset=utf-8', headers=None, csp=None):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header(
            'Content-Security-Policy',
            csp or "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'self'; base-uri 'none'")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def sign(self, text):
        return hmac.new(self.server.key, text.encode(), hashlib.sha256).hexdigest()

    def token_valid(self, token):
        """校验插件签发的令牌（<到期时间>.<随机串>.<签名>）。"""
        try:
            payload, signature = token.rsplit('.', 1)
            if len(token) > 200 or int(payload.split('.')[0]) < time.time():
                return False
            return secrets.compare_digest(signature, self.sign(payload))
        except (ValueError, UnicodeError, AttributeError):
            return False

    def mint_token(self, seconds):
        payload = str(int(time.time()) + seconds) + '.' + secrets.token_hex(16)
        return payload + '.' + self.sign(payload)

    def session(self):
        token = self.headers.get('X-TR-Session', '')
        return token if self.token_valid(token) else None

    def console_allowed(self):
        """控制台的闸门：设备所有者，或插件页令牌/控制台 Cookie。

        控制台是第三方页面（twc），它自己的 XHR 带不上插件的自定义头，所以静态页与
        RPC 都靠签名 Cookie；同时接受 X-TR-Session 头，便于插件页直接用 fetch 进入。
        """
        if self.trusted():
            return True
        if self.session():
            return True
        jar = SimpleCookie()
        try:
            jar.load(self.headers.get('Cookie', ''))
        except CookieError:
            return False
        morsel = jar.get(CONSOLE_COOKIE)
        return bool(morsel and self.token_valid(morsel.value))

    def serve_console(self, relative):
        """把 <配置目录>/webui/ 下的控制台文件按同源路径发出去。"""
        folder = (self.server.engine.config or {}).get('config')
        if not folder:
            return self.send(409, {'ok': False, 'error': '请先在插件页完成初始化，再打开控制台'})
        root = webui_path(Path(folder)).resolve()
        if not (root / 'index.html').is_file():
            return self.send(409, {'ok': False, 'error': '控制台文件缺失，应位于 ' + str(root)})
        candidate = (root / (relative or 'index.html')).resolve()
        if candidate != root and root not in candidate.parents:
            return self.send(403, {'ok': False, 'error': 'forbidden'})
        if candidate.is_dir():
            candidate = candidate / 'index.html'
        if not candidate.is_file():
            return self.send(404, {'ok': False, 'error': 'not found'})
        mime = CONSOLE_MIME.get(candidate.suffix.lower()) or \
            mimetypes.guess_type(candidate.name)[0] or 'application/octet-stream'
        headers = {}
        cookie = getattr(self, '_console_cookie', '')
        if cookie:
            headers['Set-Cookie'] = cookie
        return self.send(200, candidate.read_bytes(), mime, headers, csp=CONSOLE_CSP)

    def proxy_console_rpc(self, payload):
        """把控制台的 RPC 请求转发给容器里的 transmission，并代填 WebUI 账号。"""
        engine = self.server.engine
        username = (engine.config or {}).get('username', '')
        password = engine.saved_password()
        headers = {'Content-Type': 'application/json'}
        if username and password:
            token = base64.b64encode((username + ':' + password).encode('utf-8')).decode('ascii')
            headers['Authorization'] = 'Basic ' + token
        sid = self.headers.get('X-Transmission-Session-Id')
        if sid:
            headers['X-Transmission-Session-Id'] = sid
        connection = http.client.HTTPConnection('127.0.0.1', PORT, timeout=15)
        try:
            connection.request('POST', '/transmission/rpc', body=payload, headers=headers)
            response = connection.getresponse()
            body = response.read()
            status = response.status
            out = {}
            session = response.getheader('X-Transmission-Session-Id')
            if session:
                out['X-Transmission-Session-Id'] = session
            mime = response.getheader('Content-Type') or 'application/json'
        except (OSError, http.client.HTTPException) as error:
            return self.send(502, {'ok': False, 'error': 'transmission 未就绪：' + str(error)})
        finally:
            connection.close()
        self.send(status, body, mime, out, csp=CONSOLE_CSP)

    def trusted(self):
        """设备所有者判定：证书、回环，或私网来源（Windows 本地代理不带证书）。"""
        if self.server.dev:
            return True
        dn = self.headers.get('X-Xiaomi-Client-DN', '')
        verify = self.headers.get('X-Xiaomi-Client-Verify', '').upper()
        if verify == 'SUCCESS' and re.search(
                r'CN=nas\.' + re.escape(self.server.user.lstrip('u')) + r'\.', dn):
            return True
        try:
            address = ipaddress.ip_address(self.headers.get('X-Real-IP', ''))
        except ValueError:
            return False
        if address.is_loopback:
            return True
        # Windows 客户端经本机代理访问时没有设备证书（ssl_client_verify=NONE），
        # 来源是电脑的局域网地址；此时仍签发会话，否则插件页与控制台都打不开。
        return address.is_private and verify in ('NONE', 'FAILED', 'EXPIRED', 'SUCCESS', '')

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
        host = lan_ip()
        if not host:
            host = re.sub(r':\d+$', '', self.headers.get('Host', '').strip())
            if not re.fullmatch(r'[A-Za-z0-9.\-]{1,253}', host):
                return ''
        return 'http://' + host + ':' + str(PORT)

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
        if route.path in ('/app.bundle.js', '/styles.css'):
            file = WEB / route.path[1:]
            return self.send(200, file.read_bytes(), mimetypes.guess_type(file.name)[0])
        if route.path.startswith('/assets/'):
            name = Path(route.path).name
            if not re.fullmatch(r'[A-Za-z0-9._-]+\.png', name):
                return self.send(404, {'ok': False, 'error': 'not found'})
            file = WEB / 'assets' / name
            if not file.is_file():
                return self.send(404, {'ok': False, 'error': 'not found'})
            return self.send(200, file.read_bytes(), 'image/png')
        if route.path == CONSOLE_PATH or route.path.startswith(CONSOLE_PATH + '/'):
            return self.handle_console(route)
        token = self.require()
        if not token:
            return
        if not self.server.request_slots.acquire(False):
            return self.send(429, {'ok': False, 'error': '请求较多，请稍后重试'})
        try:
            query = parse_qs(route.query)
            if route.path == '/api/status':
                result = self.server.engine.snapshot()
                result['address'] = self.address()
            elif route.path == '/api/browse':
                result = {'items': self.server.engine.browse(query.get('path', [''])[0])}
            elif route.path == '/api/health':
                password = self.server.engine.saved_password()
                username = (self.server.engine.config or {}).get('username', '')
                try:
                    tr_rpc_probe(username or None, password or None)
                    result = {'rpc': True}
                except Error:
                    result = {'rpc': False}
            else:
                return self.send(404, {'ok': False, 'error': 'not found'})
            self.send(200, {'ok': True, **result})
        except (Error, ValueError, OSError, KeyError) as exc:
            self.send(400, {'ok': False, 'error': str(exc) if isinstance(exc, Error) else '读取失败，请检查存储或服务状态'})
        finally:
            self.server.request_slots.release()

    def handle_console(self, route):
        """控制台入口与静态文件。

        两种进入方式都支持：
        1. `/console`（或 `/console/`）带 `?t=<插件会话令牌>` → 302 到不带令牌的地址并种 Cookie；
        2. 任意 console 路径直接带 `?t=`（如 `/console/index.html?t=…`）→ 直接发文件并种 Cookie。
        Windows 客户端经本机代理时相对 Location/Set-Cookie 容易丢，第 2 种最稳。
        """
        entry = route.path.rstrip('/') == CONSOLE_PATH
        query = parse_qs(route.query)
        query_token = query.get('t', [''])[0]
        if query_token and self.token_valid(query_token):
            cookie = (CONSOLE_COOKIE + '=' + self.mint_token(CONSOLE_TOKEN_TTL)
                      + '; Path=/; HttpOnly; SameSite=Strict')
            if entry:
                # 令牌只出现在首次跳转的地址上，随后 302 到不带令牌的地址（相对路径才不丢 nginx 前缀）
                target = 'console/' if route.path == CONSOLE_PATH else './'
                return self.send(302, b'', 'text/plain; charset=utf-8', {
                    'Location': target,
                    'Set-Cookie': cookie,
                })
            self._console_cookie = cookie
        if not self.console_allowed():
            return self.send(403, {'ok': False, 'error': '请从插件页点「打开控制台」进入'})
        if entry:
            return self.serve_console('index.html')
        return self.serve_console(route.path[len(CONSOLE_PATH) + 1:])

    def do_POST(self):
        route = urlsplit(self.path)
        if route.path == CONSOLE_RPC_PATH:
            if not self.console_allowed():
                return self.send(403, {'ok': False, 'error': '请从插件页点「打开控制台」进入'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
            except ValueError:
                length = 0
            if not 0 < length <= MAX_RPC_BYTES:
                return self.send(413, {'ok': False, 'error': '请求体过大'})
            return self.proxy_console_rpc(self.rfile.read(length))
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
            if not action.startswith('service/'):
                raise Error('不支持此操作')
            self.server.engine.launch(action.split('/')[1], data)
            self.send(202, {'ok': True})
        except (Error, ValueError, OSError) as exc:
            self.send(400, {'ok': False, 'error': str(exc) if isinstance(exc, Error) else '操作失败，请检查输入'})
        finally:
            self.server.request_slots.release()


def lan_ip():
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(('223.5.5.5', 53))
        address = probe.getsockname()[0]
        return address if address and not address.startswith('127.') else ''
    except OSError:
        return ''
    finally:
        probe.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dev', action='store_true')
    parser.add_argument('--stop-owned', action='store_true')
    args = parser.parse_args()
    data = os.environ.get('DATA_DIR') or (
        tempfile.mkdtemp(prefix='tr-preview-') if args.dev else '/data/plugin/transmission/data')
    root = os.environ.get('LOCAL_ROOT', data if args.dev else '')
    user = os.environ.get('NAS_USER_ID', 'u123456' if args.dev else '')
    if not root or not re.fullmatch(r'u[0-9]+', user):
        raise SystemExit('LOCAL_ROOT and NAS_USER_ID are required')
    engine = Engine(data, root, args.dev)
    if args.stop_owned:
        engine.stop(remember=False)
        return
    server = Server(('127.0.0.1', int(os.environ.get('PORT', 18140))), engine, user, args.dev)
    if engine.config and engine.config.get('enabled') and not args.dev:
        engine.launch('start', {})
    print('Transmission plugin listening on http://127.0.0.1:' + str(server.server_port), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
