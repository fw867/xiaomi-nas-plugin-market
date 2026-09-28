#!/usr/bin/env python3
"""网络邻居插件的本地 HTTP 服务：只监听 127.0.0.1，由 nginx 代理 /plugin/<user>/netneighbor/。

鉴权沿用仓库既有插件的约定：插件页由 nginx 直接发静态文件，令牌通过页面里的
`X-NN-Session` / `csrf-token` 两个 meta 传给 app.js；写操作额外校验 CSRF。
令牌只在回环来源（nginx 的 X-Real-IP 或对端 127.0.0.1）被签发。

    python3 server.py                  # 正常启动
    python3 server.py --restore-wsdd   # 只把官方 wsdd 放回去，然后退出（早退路径）
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import http.client  # noqa: F401  （保持与其它插件一致的导入面）
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import engine
from engine import Engine, Error

WEB = Path(__file__).resolve().parent / 'web'
TTL = 12 * 3600
MAX_BODY = 64 * 1024

HOST = os.environ.get('HOST', '127.0.0.1')
PORT = int(os.environ.get('PORT', '18190'))
DATA_DIR = Path(os.environ.get('DATA_DIR', '/data/plugin/netneighbor/data'))
WEB_DIR = Path(os.environ.get('WEB_DIR', str(WEB)))

SESSION_HEADER = 'X-NN-Session'
CSRF_HEADER = 'X-CSRF-Token'
COOKIE_NAME = 'nn_session'

# app.js 里 path → 处理函数，避免一长串 if
STATIC_FILES = ('/styles.css', '/app.js', '/favicon.ico')


def engine_factory() -> Engine:
    """单独拿出来，测试里可以整体替换（永不真的启动 3702 回应器）。"""
    return Engine(data_dir=DATA_DIR, start_responder=True)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, engine_instance: Engine, dev: bool = False):
        self.engine = engine_instance
        self.dev = dev
        self.slots = threading.BoundedSemaphore(8)
        keyfile = Path(engine_instance.data_dir) / 'session.key'
        try:
            keyfile.parent.mkdir(parents=True, exist_ok=True)
            if not keyfile.exists():
                with keyfile.open('xb') as stream:
                    os.chmod(keyfile, 0o600)
                    stream.write(secrets.token_bytes(32))
            self.key = keyfile.read_bytes()
        except OSError:
            # 拿不到持久密钥也不能拒绝服务：退化成进程内随机密钥
            self.key = secrets.token_bytes(32)
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = 'XiaomiNetNeighbor/0.1'

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, fmt: str, *args) -> None:
        sys.stdout.write('%s [%s] %s\n' % (self.address_string(),
                                           self.log_date_time_string(), fmt % args))
        sys.stdout.flush()

    # ---- 输出 -----------------------------------------------------------
    def send_payload(self, status: int, body: bytes, content_type: str,
                     cache: str = 'no-store', headers=None) -> None:
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', cache)
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'SAMEORIGIN')
        self.send_header('Referrer-Policy', 'no-referrer')
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def json_out(self, status: int, value) -> None:
        body = (json.dumps(value, ensure_ascii=False) + '\n').encode('utf-8')
        self.send_payload(status, body, 'application/json; charset=utf-8')

    def fail(self, status: int, message: str) -> None:
        self.json_out(status, {'ok': False, 'error': message})

    # ---- 鉴权 -----------------------------------------------------------
    def sign(self, text: str) -> str:
        return hmac.new(self.server.key, text.encode('utf-8'), hashlib.sha256).hexdigest()

    def token_valid(self, token: str) -> bool:
        """令牌格式：`<到期时间>.<随机串>.<签名>`。"""
        try:
            payload, signature = str(token).rsplit('.', 1)
            if len(token) > 200 or int(payload.split('.')[0]) < time.time():
                return False
            return secrets.compare_digest(signature, self.sign(payload))
        except (ValueError, UnicodeError, AttributeError):
            return False

    def mint_token(self, seconds: int = TTL) -> str:
        payload = '%d.%s' % (int(time.time()) + seconds, secrets.token_hex(16))
        return payload + '.' + self.sign(payload)

    def cookie_token(self) -> str:
        for chunk in (self.headers.get('Cookie', '') or '').split(';'):
            name, _, value = chunk.strip().partition('=')
            if name == COOKIE_NAME:
                return value
        return ''

    def session(self) -> str:
        token = self.headers.get(SESSION_HEADER, '') or self.cookie_token()
        return token if self.token_valid(token) else ''

    def trusted(self) -> bool:
        """谁能拿到令牌：设备所有者（客户端证书）或回环来源（本机/nginx）。"""
        if self.server.dev:
            return True
        if self.headers.get('X-Xiaomi-Client-Verify') == 'SUCCESS':
            return True
        try:
            return ipaddress.ip_address(self.headers.get('X-Real-IP', '')).is_loopback
        except ValueError:
            return False

    def require(self, write: bool = False) -> str:
        token = self.session()
        if not token:
            self.fail(401, '请从小米智能存储客户端重新打开插件')
            return ''
        if write and not secrets.compare_digest(self.headers.get(CSRF_HEADER, ''),
                                                self.sign('csrf:' + token)):
            self.fail(403, '操作令牌失效，请重新打开插件')
            return ''
        return token

    # ---- 路由 -----------------------------------------------------------
    def do_GET(self) -> None:                                   # noqa: N802
        route = urlsplit(self.path)
        path = route.path
        if path == '/healthz':
            self.json_out(200, {'ok': True, 'version': engine.installed_version()})
            return
        if path in ('/', '/index.html'):
            return self.serve_page()
        if path in STATIC_FILES:
            return self.serve_static(path.lstrip('/'))
        if path.startswith('/api/'):
            if not self.require():
                return
            return self.api_get(path)
        self.fail(404, 'not found')

    def serve_page(self) -> None:
        page = WEB_DIR / 'index.html'
        if not page.is_file():
            return self.fail(500, '页面文件缺失：%s' % page)
        token = self.mint_token() if self.trusted() else ''
        html = page.read_text(encoding='utf-8')
        html = html.replace('__SESSION_TOKEN__', token)
        html = html.replace('__CSRF_TOKEN__', self.sign('csrf:' + token) if token else '')
        html = html.replace('__PLUGIN_VERSION__', engine.installed_version())
        headers = {}
        if token:
            base = '/' if self.server.dev else '/plugin/%s/netneighbor/' % (
                os.environ.get('NAS_USER_ID', '').lstrip('u') or '_')
            headers['Set-Cookie'] = '%s=%s; Path=%s; HttpOnly; SameSite=Strict; Max-Age=%d%s' % (
                COOKIE_NAME, token, base, TTL, '' if self.server.dev else '; Secure')
        self.send_payload(200, html.encode('utf-8'), 'text/html; charset=utf-8', headers=headers)

    def serve_static(self, relative: str) -> None:
        root = WEB_DIR.resolve()
        candidate = (root / relative).resolve()
        if candidate != root and root not in candidate.parents:
            return self.fail(403, 'forbidden')
        if not candidate.is_file():
            return self.fail(404, 'not found')
        mime = mimetypes.guess_type(candidate.name)[0] or 'application/octet-stream'
        self.send_payload(200, candidate.read_bytes(), mime)

    def api_get(self, path: str) -> None:
        route = urlsplit(self.path)
        try:
            if path == '/api/status':
                self.json_out(200, self.server.engine.snapshot())
                return
            if path == '/api/log':
                self.json_out(200, {'ok': True, 'lines': self.server.engine.recent_log(80)})
                return
            if path == '/api/dirs':
                # 弹窗里选目录用：账号数据根目录下的子目录（含是否已共享）
                account = (parse_qs(route.query).get('account') or [''])[0]
                self.json_out(200, self.server.engine.browse_account_dirs(account))
                return
        except Error as error:
            return self.fail(400, str(error))
        except OSError as error:
            return self.fail(500, '读取状态失败：%s' % error)
        self.fail(404, 'not found')

    def do_POST(self) -> None:                                  # noqa: N802
        path = urlsplit(self.path).path
        token = self.require(True)
        if not token:
            return
        try:
            length = int(self.headers.get('Content-Length', '0') or '0')
        except ValueError:
            return self.fail(400, '无效请求长度')
        if not 0 < length <= MAX_BODY:
            return self.fail(400, '无效请求长度')
        try:
            body = json.loads(self.rfile.read(length).decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            return self.fail(400, '请求体必须是 JSON')
        if not isinstance(body, dict):
            return self.fail(400, '请求体必须是 JSON 对象')

        if not self.server.slots.acquire(blocking=False):
            return self.fail(409, '上一个操作还在进行，请稍候')
        try:
            if path == '/api/share/add':
                # 兼容旧的单个 `{"account","path","sharePoint"}`，以及新的
                # `{"account","paths":[...]}`（弹窗里一次勾多个目录）
                paths = body.get('paths')
                if isinstance(paths, (list, tuple)):
                    result = self.server.engine.add_shares(
                        body.get('account', ''), list(paths),
                        share_point=body.get('sharePoint', ''),
                        force_user=body.get('forceUser', ''),
                        user_list=body.get('users', ''))
                else:
                    result = self.server.engine.add_share(
                        body.get('account', ''), body.get('path', ''),
                        share_point=body.get('sharePoint', ''),
                        force_user=body.get('forceUser', ''),
                        user_list=body.get('users', ''))
                self.json_out(200, {'ok': True, 'result': result,
                                    'status': self.server.engine.snapshot()})
            elif path == '/api/share/delete':
                # 兼容旧的单条 `{"shareName":"fw867_nb_1"}`，以及新的
                # `{"shareNames":[...]}`（弹窗里一次取消勾选多个目录）
                names = body.get('shareNames')
                if isinstance(names, (list, tuple)):
                    result = self.server.engine.delete_shares(list(names))
                else:
                    result = self.server.engine.delete_share(body.get('shareName', ''))
                self.json_out(200, {'ok': True, 'result': result,
                                    'status': self.server.engine.snapshot()})
            elif path == '/api/discovery':
                # 页面上的「网络发现」开关：开 = 接管 + 起回应器，关 = 还原官方 wsdd
                snapshot = self.server.engine.set_discovery_enabled(body.get('enabled'))
                self.json_out(200, {'ok': True, 'status': snapshot})
            elif path == '/api/hostname':
                # 改 Windows「网络」里显示的名字：落盘 + 重建回应器 + 重发 Hello；
                # {"reset": true} 是恢复默认（清掉落盘值，回退到 samba 配置里的名字）
                if body.get('reset'):
                    snapshot = self.server.engine.set_hostname(reset=True)
                else:
                    snapshot = self.server.engine.set_hostname(body.get('hostname', ''))
                self.json_out(200, {'ok': True, 'status': snapshot})
            elif path == '/api/detect/restart':
                # 重新宣告：重建回应器 + 重发 Hello，同步做完再回（几百毫秒量级）
                snapshot = self.server.engine.restart_responder(times=2)
                self.json_out(200, {'ok': True, 'status': snapshot})
            else:
                self.fail(404, 'not found')
        except Error as error:
            self.fail(400, str(error))
        except (OSError, ValueError) as error:
            self.fail(500, '操作失败：%s' % error)
        finally:
            self.server.slots.release()


def restore_path() -> int:
    """`--restore-wsdd`：服务停止后由 systemd 的 ExecStopPost 调用。

    只做「删掉我们的 drop-in + daemon-reload + 启动官方 wsdd」，不启动回应器、
    不开 HTTP 端口，然后早退。
    """
    message = engine.restore_wsdd(data_dir=DATA_DIR)
    print('[netneighbor] --restore-wsdd：%s' % message, flush=True)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description='网络邻居插件服务')
    parser.add_argument('--restore-wsdd', action='store_true',
                        help='只恢复官方 wsdd 并退出（ExecStopPost 用）')
    parser.add_argument('--dev', action='store_true', help='预览模式：跳过鉴权')
    args = parser.parse_args(argv)

    if args.restore_wsdd:
        return restore_path()

    instance = engine_factory()
    server = Server((HOST, PORT), instance, dev=args.dev)

    def shutdown(*_args):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    print('Xiaomi netneighbor plugin on http://%s:%d' % (HOST, PORT), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        # 正常退出也要发 Bye、把官方 wsdd 放回去（ExecStopPost 是第二道保险）
        try:
            instance.shutdown()
        except Exception as error:                              # noqa: BLE001
            print('[netneighbor] 退出清理出错：%s' % error, flush=True)
        server.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
