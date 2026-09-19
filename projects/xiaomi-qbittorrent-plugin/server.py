"""Owner-authenticated, same-origin qB download UI; no generic Docker proxy."""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
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
from http.cookies import SimpleCookie, CookieError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from engine import (Engine, Error, PORT, SESSION_TIMEOUT, qb_request, mutation,
                    torrent_hash, installed_version, atomic_json)

WEB = Path(__file__).resolve().parent / 'web'
TTL = 86400


def lan_ip():
    """取本机在局域网里的地址；取不到返回空串。

    用「连一个外部地址但不真发包」的办法让内核挑默认出口对应的网卡地址，
    比 gethostbyname(gethostname()) 可靠——后者在没有 hosts 记录时常常返回
    127.0.1.1。UDP connect 不会产生任何流量。
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


def accepted(body):
    """判断 torrents/add 是否被接受。

    qB 4.x 成功时返回纯文本 "Ok."；5.x 改成返回
    {"added_torrent_ids": [...]}。两种都要认，否则正常添加也会被判成失败。
    """
    text = body.strip()
    if text == b'Ok.':
        return True
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return False
    return isinstance(data, dict) and bool(data.get('added_torrent_ids'))


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
        # qB 的登录会话是服务级共享的，并持久化到磁盘：这样从状态页进入下载
        # 列表时不必每次重输密码，插件重启后也能续上。
        self.qb_cookie = ''
        self.qb_expiry = 0.0
        self.qb_lock = threading.Lock()
        self.qb_session_file = engine.data / 'qb-session.json'
        self.load_qb_session()
        self.request_slots = threading.BoundedSemaphore(12)
        super().__init__(addr, Handler)

    def load_qb_session(self):
        try:
            saved = json.loads(self.qb_session_file.read_text(encoding='utf-8'))
            cookie = saved.get('cookie')
            expiry = float(saved.get('expiry', 0))
        except (OSError, ValueError, TypeError):
            return
        if isinstance(cookie, str) and cookie and expiry > time.time():
            self.qb_cookie, self.qb_expiry = cookie, expiry

    def save_qb_session(self, cookie):
        expiry = time.time() + SESSION_TIMEOUT
        with self.qb_lock:
            self.qb_cookie, self.qb_expiry = cookie, expiry
        try:
            atomic_json(self.qb_session_file, {'cookie': cookie, 'expiry': expiry})
        except OSError:
            pass

    def qb_session(self):
        with self.qb_lock:
            return self.qb_cookie if self.qb_cookie and self.qb_expiry > time.time() else ''

    def clear_qb_session(self):
        with self.qb_lock:
            self.qb_cookie, self.qb_expiry = '', 0.0
        try:
            self.qb_session_file.unlink()
        except OSError:
            pass


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
        self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'self'; base-uri 'none'")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def sign(self, text):
        return hmac.new(self.server.key, text.encode(), hashlib.sha256).hexdigest()

    def session(self):
        token = self.headers.get('X-QB-Session', '')
        try:
            payload, signature = token.rsplit('.', 1)
            if len(token) > 200 or int(payload.split('.')[0]) < time.time():
                return None
            return token if secrets.compare_digest(signature, self.sign(payload)) else None
        except (ValueError, UnicodeError):
            return None

    def trusted(self):
        if self.server.dev:
            return True
        dn = self.headers.get('X-Xiaomi-Client-DN', '')
        if self.headers.get('X-Xiaomi-Client-Verify') == 'SUCCESS' and re.search(r'CN=nas\.' + re.escape(self.server.user.lstrip('u')) + r'\.', dn):
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
        """拼出局域网里能直接打开的 qB WebUI 地址。

        不能靠请求的 Host：小米客户端是经客户端自己的隧道访问 NAS 的，到这里
        时 Host 已被改写成 127.0.0.1，拼出来的地址在手机/电脑上打不开。本机
        网卡地址取不到时才退回 Host。
        """
        host = lan_ip()
        if not host:
            host = re.sub(r':\d+$', '', self.headers.get('Host', '').strip())
            if not re.fullmatch(r'[A-Za-z0-9.\-]{1,253}', host):
                return ''
        return 'http://' + host + ':' + str(PORT)

    def call_qb(self, route, params=None, **kwargs):
        cookie = self.server.qb_session()
        if not cookie:
            raise Error('请先登录 qBittorrent')
        code, body, _ = qb_request(route, params, cookie=cookie, **kwargs)
        if code in (401, 403):
            self.server.clear_qb_session()
            raise Error('qBittorrent 登录已过期，请重新登录')
        # qB 5.x 的部分操作成功时返回 204 No Content，不再一律是 200。
        if code not in (200, 204):
            raise Error('qBittorrent 拒绝此操作（HTTP ' + str(code) + '）')
        return body

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
        if route.path == '/assets/qb.png':
            return self.send(200, (WEB / 'assets/qb.png').read_bytes(), 'image/png')
        token = self.require()
        if not token:
            return
        if not self.server.request_slots.acquire(False):
            return self.send(429, {'ok': False, 'error': '请求较多，请稍后重试'})
        try:
            query = parse_qs(route.query)
            if route.path == '/api/status':
                result = self.server.engine.snapshot()
                result['loggedIn'] = bool(self.server.qb_session())
                result['address'] = self.address()
            elif route.path == '/api/browse':
                result = {'items': self.server.engine.browse(query.get('path', [''])[0])}
            elif route.path == '/api/torrents':
                result = {'items': json.loads(self.call_qb('torrents/info?limit=500&sort=added_on&reverse=true')),
                          'transfer': json.loads(self.call_qb('transfer/info'))}
            elif route.path == '/api/detail':
                key = torrent_hash(query.get('hash', [''])[0])
                result = {'files': json.loads(self.call_qb('torrents/files?hash=' + key)),
                          'properties': json.loads(self.call_qb('torrents/properties?hash=' + key))}
            elif route.path == '/api/limits':
                prefs = json.loads(self.call_qb('app/preferences'))
                result = {'download': prefs['dl_limit'] // 1024, 'upload': prefs['up_limit'] // 1024,
                          'active': prefs['max_active_downloads']}
            else:
                return self.send(404, {'ok': False, 'error': 'not found'})
            self.send(200, {'ok': True, **result})
        except (Error, ValueError, OSError, KeyError) as exc:
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
            if not 0 < length <= 3 * 1024 * 1024 or self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                raise Error('请求格式无效或超过大小限制')
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise Error('请求格式无效')
            action = urlsplit(self.path).path.removeprefix('/api/')
            if action.startswith('service/'):
                self.server.engine.launch(action.split('/')[1], data)
                return self.send(202, {'ok': True})
            if action == 'login':
                password = data.get('password')
                if not isinstance(password, str) or not 1 <= len(password) <= 200:
                    raise Error('请输入 qBittorrent 密码')
                code, body, header = qb_request('auth/login', {'username': 'admin', 'password': password})
                # qB 5.x 登录成功是 204 空响应、cookie 名为 QBT_SID_<端口>；
                # 4.x 是 200 + "Ok." + SID。按 4.x 严格校验会让密码正确也报失败。
                jar = SimpleCookie()
                try:
                    jar.load(header)
                except CookieError:
                    jar = SimpleCookie()
                name = next((n for n in ('QBT_SID_' + str(PORT), 'SID') if n in jar), '')
                sid = jar[name].value if name else ''
                if code not in (200, 204) or not sid or not re.fullmatch(r'[A-Za-z0-9_-]{8,256}', sid):
                    raise Error('登录失败，密码错误或登录次数过多')
                self.server.save_qb_session(name + '=' + sid)
            elif action == 'logout':
                self.server.clear_qb_session()
            elif action == 'torrent':
                value = data.get('content', '')
                if not isinstance(value, str):
                    raise Error('种子文件格式无效')
                try:
                    raw = base64.b64decode(value, validate=True)
                except ValueError as exc:
                    raise Error('种子文件格式无效') from exc
                if not 1 <= len(raw) <= 2 * 1024 * 1024 or not raw.startswith(b'd'):
                    raise Error('请选择不超过 2 MiB 的 torrent 文件')
                boundary = 'nasqb' + secrets.token_hex(24)
                body = ('--' + boundary + '\r\nContent-Disposition: form-data; name="torrents"; filename="upload.torrent"\r\nContent-Type: application/x-bittorrent\r\n\r\n').encode() + raw
                for key, value in [('savepath', '/downloads'), ('autoTMM', 'false'), ('stopped', 'false')]:
                    body += ('\r\n--' + boundary + '\r\nContent-Disposition: form-data; name="' + key + '"\r\n\r\n' + value).encode()
                body += ('\r\n--' + boundary + '--\r\n').encode()
                if not accepted(self.call_qb('torrents/add', raw=body, content_type='multipart/form-data; boundary=' + boundary)):
                    raise Error('种子未被接受，请检查文件内容')
            else:
                target, params = mutation(action, data)
                body = self.call_qb(target, params)
                if action == 'magnet' and not accepted(body):
                    raise Error('磁力链接未被接受')
            self.send(200, {'ok': True})
        except (Error, ValueError, OSError) as exc:
            self.send(400, {'ok': False, 'error': str(exc) if isinstance(exc, Error) else '操作失败，请检查输入'})
        finally:
            self.server.request_slots.release()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dev', action='store_true')
    parser.add_argument('--stop-owned', action='store_true')
    args = parser.parse_args()
    data = os.environ.get('DATA_DIR') or (tempfile.mkdtemp(prefix='qb-preview-') if args.dev else '/data/plugin/qbittorrent/data')
    root = os.environ.get('LOCAL_ROOT', data if args.dev else '')
    user = os.environ.get('NAS_USER_ID', 'u123456' if args.dev else '')
    if not root or not re.fullmatch(r'u[0-9]+', user):
        raise SystemExit('LOCAL_ROOT and NAS_USER_ID are required')
    engine = Engine(data, root, args.dev)
    if args.stop_owned:
        engine.stop(remember=False)
        return
    server = Server(('127.0.0.1', int(os.environ.get('PORT', 18122))), engine, user, args.dev)
    if engine.config and engine.config.get('enabled') and not args.dev:
        engine.launch('start', {})
    print('qB plugin listening on http://127.0.0.1:' + str(server.server_port), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
