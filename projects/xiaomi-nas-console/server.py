#!/usr/bin/env python3
"""小米智能存储「控制台」——只读 HTTP 服务。

两个入口共用这一个服务（都只监听回环，由 nginx 反代）：

* 浏览器入口：安装时写入 `/etc/nginx/conf.d/xiaomi-nas-console-lan.conf`，
  nginx 在 8085 上反代到本服务；请求带 `X-Console-Entry: lan`，
  需要先用在安装时生成的 admin-token 登录（HMAC 签名的会话 Cookie）。
* 小米客户端入口：`/plugin/<用户>/nasconsole/`（443，客户端证书 + 令牌），
  请求带 `X-Console-Entry: xiaomi`；nginx 已经完成鉴权，控制台本身是只读的，
  因此不再要求二次登录。

只读：除登录/登出外全部是 GET，不写任何 NAS 配置。
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import posixpath
import secrets
import socket
import sys
import threading
import time
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import engine

COOKIE_NAME = 'xiaomi_nas_console_session'
SESSION_TTL = 30 * 24 * 60 * 60
LOGIN_FAILURE_LIMIT = 5
LOGIN_COOLDOWN = 30.0

CONTENT_TYPES = {
    '.html': 'text/html; charset=utf-8',
    '.js': 'application/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.json': 'application/json; charset=utf-8',
    '.svg': 'image/svg+xml',
    '.png': 'image/png',
    '.ico': 'image/x-icon',
    '.woff2': 'font/woff2',
    '.txt': 'text/plain; charset=utf-8',
}

CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
       "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'self'")

_login_lock = threading.Lock()
_login_failures: dict[str, list[float]] = {}


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')


class ConsoleServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler],
                 static_dir: Path, admin_token: str, sampler: engine.Sampler,
                 dev: bool = False, cookie_secure: str = 'auto',
                 token_file: Path | None = None) -> None:
        super().__init__(address, handler)
        self.static_dir = static_dir
        self.admin_token = admin_token
        self.session_key = hashlib.sha256(admin_token.encode('utf-8')).digest()
        self.sampler = sampler
        self.dev = dev
        self.cookie_secure = cookie_secure if cookie_secure in ('auto', 'always', 'never') else 'auto'
        self.token_file = token_file or engine.ADMIN_TOKEN_FILE
        self.started_at = int(time.time())

    def rotate_token(self, token: str) -> None:
        """换令牌：先落盘再换内存里的会话密钥，旧会话立即失效。"""
        engine.save_admin_token(self.token_file, token)
        self.admin_token = token
        self.session_key = hashlib.sha256(token.encode('utf-8')).digest()


class Handler(BaseHTTPRequestHandler):
    server_version = f'XiaomiNasConsole/{engine.VERSION or "unknown"}'
    protocol_version = 'HTTP/1.1'

    @property
    def app(self) -> ConsoleServer:
        return self.server  # type: ignore[return-value]

    # -- 日志：只记异常与鉴权，避免 2 秒一次的轮询刷爆 journal ---------------
    def log_message(self, format_string: str, *args: object) -> None:
        return

    def log_event(self, message: str) -> None:
        engine.log(f'{self._remote_ip()} {message}')

    def _remote_ip(self) -> str:
        for header in ('X-Real-IP', 'X-Forwarded-For'):
            value = (self.headers.get(header) or '').split(',')[0].strip()
            if value:
                return value
        return self.client_address[0] if self.client_address else '-'

    # -- 响应工具 ---------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str,
              headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', CSP)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def _json(self, status: int, value: object, headers: dict[str, str] | None = None) -> None:
        self._send(status, json_bytes(value), CONTENT_TYPES['.json'], headers)

    def _error(self, status: int, message: str) -> None:
        self._json(status, {'error': message})

    # -- 会话 -------------------------------------------------------------
    @property
    def entry(self) -> str:
        """这个请求来自哪个入口：xiaomi（客户端）/ lan（浏览器）/ local（本机直连）。"""
        header = (self.headers.get('X-Console-Entry') or '').strip().lower()
        if header in ('xiaomi', 'lan'):
            return header
        address = self._remote_ip()
        if address in ('127.0.0.1', '::1'):
            return 'local'
        return 'lan'

    def _session_id(self) -> str | None:
        cookie = SimpleCookie(self.headers.get('Cookie', ''))
        morsel = cookie.get(COOKIE_NAME)
        return morsel.value if morsel else None

    def _session(self) -> dict[str, object] | None:
        session_id = self._session_id()
        if not session_id:
            return None
        try:
            payload, provided = session_id.rsplit('.', 1)
            expiry_text, _random = payload.split('.', 1)
            expiry = int(expiry_text)
        except (ValueError, TypeError):
            return None
        expected = hmac.new(self.app.session_key, payload.encode('ascii'), hashlib.sha256).hexdigest()
        if expiry < int(time.time()) or not secrets.compare_digest(expected, provided):
            return None
        csrf = hmac.new(self.app.session_key, f'csrf:{session_id}'.encode('ascii'), hashlib.sha256).hexdigest()
        return {'csrf': csrf, 'expires': expiry}

    def _new_session(self) -> tuple[str, dict[str, object]]:
        expiry = int(time.time()) + SESSION_TTL
        payload = f'{expiry}.{secrets.token_urlsafe(24)}'
        signature = hmac.new(self.app.session_key, payload.encode('ascii'), hashlib.sha256).hexdigest()
        session_id = f'{payload}.{signature}'
        csrf = hmac.new(self.app.session_key, f'csrf:{session_id}'.encode('ascii'), hashlib.sha256).hexdigest()
        return session_id, {'csrf': csrf, 'expires': expiry}

    def _session_cookie(self, session_id: str) -> str:
        # 浏览器入口是明文 HTTP，带 Secure 的 Cookie 会被浏览器直接丢掉（表现为
        # 登录成功但下一页还是未登录）。所以按请求实际协议决定：经 nginx 的 https
        # 入口（X-Forwarded-Proto: https）才加 Secure。
        scheme = (self.headers.get('X-Forwarded-Proto') or '').split(',')[0].strip().lower()
        secure = self.app.cookie_secure == 'always' or (
            self.app.cookie_secure == 'auto' and scheme == 'https')
        parts = [f'{COOKIE_NAME}={session_id}', 'Path=/', 'HttpOnly', 'SameSite=Strict']
        if secure:
            parts.append('Secure')
        parts.append(f'Max-Age={SESSION_TTL}')
        return '; '.join(parts)

    def _trusted(self) -> bool:
        return self.entry in ('xiaomi', 'local') or self._session() is not None

    def _require_auth(self) -> bool:
        if self._trusted():
            return True
        self._error(HTTPStatus.UNAUTHORIZED, '需要先登录控制台')
        return False

    def _require_write(self) -> bool:
        """写操作的守卫：客户端入口/本机直连可信；浏览器入口要会话 + CSRF 令牌。"""
        if self.entry in ('xiaomi', 'local'):
            return True
        session = self._session()
        if session is None:
            self._error(HTTPStatus.UNAUTHORIZED, '需要先登录控制台')
            return False
        provided = self.headers.get('X-CSRF-Token', '')
        if not secrets.compare_digest(str(session['csrf']), provided):
            self._error(HTTPStatus.FORBIDDEN, 'CSRF 校验失败')
            return False
        return True

    def _login_blocked(self) -> bool:
        now = time.time()
        with _login_lock:
            failures = [stamp for stamp in _login_failures.get(self._remote_ip(), []) if now - stamp < LOGIN_COOLDOWN]
            _login_failures[self._remote_ip()] = failures
            return len(failures) >= LOGIN_FAILURE_LIMIT

    def _note_failure(self) -> None:
        with _login_lock:
            _login_failures.setdefault(self._remote_ip(), []).append(time.time())

    def _clear_failures(self) -> None:
        with _login_lock:
            _login_failures.pop(self._remote_ip(), None)

    # -- 路由 -------------------------------------------------------------
    def do_GET(self) -> None:                                               # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path == '/healthz':
            self._json(HTTPStatus.OK, {'ok': True, 'version': engine.VERSION,
                                       'uptime': int(time.time()) - self.app.started_at})
            return
        if path == '/api/session':
            self._handle_session()
            return
        if path.startswith('/api/files/') and path.split('/')[-1] in ('raw', 'download'):
            if not self._require_auth():
                return
            self._handle_file_stream(path, parse_qs(parsed.query))
            return
        if path.startswith('/api/'):
            if not self._require_auth():
                return
            self._handle_api(path, parse_qs(parsed.query))
            return
        self._serve_static(path)

    def do_HEAD(self) -> None:                                              # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:                                              # noqa: N802
        parsed = urlparse(self.path)
        # 上传是原始字节流，不能按 JSON 解析；未授权时直接断开，免得客户端白传一遍
        if parsed.path == '/api/files/upload':
            if not self._require_write():
                self.close_connection = True
                return
            self._handle_upload(parse_qs(parsed.query))
            return
        length = int(self.headers.get('Content-Length') or 0)
        if length > 65536:
            self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, '请求体过大')
            return
        body = self.rfile.read(length) if length else b''
        try:
            payload = json.loads(body.decode('utf-8')) if body else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._error(HTTPStatus.BAD_REQUEST, '请求体不是合法 JSON')
            return
        if not isinstance(payload, dict):
            self._error(HTTPStatus.BAD_REQUEST, '请求体必须是 JSON 对象')
            return
        if parsed.path == '/api/login':
            self._handle_login(payload)
            return
        if parsed.path == '/api/control':
            if not self._require_write():
                return
            self._handle_control(payload)
            return
        if parsed.path == '/api/control/port':
            if not self._require_write():
                return
            self._handle_control_port(payload)
            return
        if parsed.path == '/api/control/token':
            if not self._require_write():
                return
            self._handle_control_token()
            return
        if parsed.path.startswith('/api/files/'):
            if not self._require_write():
                return
            self._handle_file_write(parsed.path, payload)
            return
        if parsed.path == '/api/logout':
            self._send(HTTPStatus.OK, json_bytes({'ok': True}), CONTENT_TYPES['.json'],
                       {'Set-Cookie': f'{COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0'})
            return
        self._error(HTTPStatus.NOT_FOUND, '控制台是只读的：没有这个写接口')

    # -- 处理函数 ---------------------------------------------------------
    def _handle_session(self) -> None:
        entry = self.entry
        authed = self._trusted()
        self._json(HTTPStatus.OK, {
            'entry': entry,
            'authed': authed,
            'authenticated': authed,
            'needsLogin': entry == 'lan' and not authed,
            'csrf': (self._session() or {}).get('csrf', ''),
            'version': engine.VERSION,
            'hostname': socket.gethostname(),
        })

    def _handle_login(self, payload: dict[str, object]) -> None:
        if self._login_blocked():
            self.log_event('登录被限流')
            self._error(HTTPStatus.TOO_MANY_REQUESTS, '失败次数过多，请稍后再试')
            return
        token = str(payload.get('token') or '').strip()
        if not token:
            header = (self.headers.get('Authorization') or '').removeprefix('Bearer ').strip()
            token = header
        expected = self.app.admin_token
        if not token or not expected or not secrets.compare_digest(token, expected):
            self._note_failure()
            self.log_event('登录失败')
            self._error(HTTPStatus.UNAUTHORIZED, '访问令牌不正确')
            return
        self._clear_failures()
        session_id, session = self._new_session()
        self.log_event('登录成功')
        self._json(HTTPStatus.OK, {'ok': True, 'csrf': session['csrf']},
                   {'Set-Cookie': self._session_cookie(session_id)})

    def _handle_api(self, path: str, query: dict[str, list[str]]) -> None:
        sampler = self.app.sampler
        try:
            if path == '/api/overview':
                self._json(HTTPStatus.OK, engine.overview_payload(sampler))
            elif path == '/api/history':
                self._json(HTTPStatus.OK, sampler.history_payload())
            elif path == '/api/storage':
                self._json(HTTPStatus.OK, engine.storage_payload(sampler))
            elif path == '/api/docker':
                with_stats = (query.get('stats', ['1'])[0] != '0')
                self._json(HTTPStatus.OK, engine.docker_payload(with_stats=with_stats))
            elif path == '/api/services':
                self._json(HTTPStatus.OK, engine.services_payload(sampler))
            elif path == '/api/control':
                self._json(HTTPStatus.OK, self._control_payload())
            elif path == '/api/plugins':
                self._json(HTTPStatus.OK, engine.plugin_catalog())
            elif path.startswith('/api/files'):
                self._handle_files(path, query)
            elif path == '/api/all':
                self._json(HTTPStatus.OK, {
                    'overview': engine.overview_payload(sampler),
                    'storage': engine.storage_payload(sampler),
                    'docker': engine.docker_payload(with_stats=True),
                    'services': engine.services_payload(sampler),
                    'history': sampler.history_payload(),
                })
            else:
                self._error(HTTPStatus.NOT_FOUND, '未知接口')
        except Exception as error:                                          # noqa: BLE001
            self.log_event(f'{path} 采集失败：{error!r}')
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f'采集失败：{error}')

    # -- 桌面 web 入口的开关 ----------------------------------------------
    def _control_payload(self, extra: dict[str, object] | None = None) -> dict[str, object]:
        payload: dict[str, object] = {
            'enabled': engine.lan_enabled(),
            'url': engine.desktop_url(),
            'lan_port': engine.lan_port(),
            'token': self.app.admin_token,
            'template_ready': engine.LAN_TEMPLATE.is_file(),
            'version': engine.VERSION,
            'hostname': socket.gethostname(),
            'model': engine.read_text('/proc/device-tree/model').strip('\x00').strip(),
            'entry': self.entry,
        }
        if extra:
            payload.update(extra)
        return payload

    def _handle_control(self, payload: dict[str, object]) -> None:
        if 'enabled' not in payload:
            self._error(HTTPStatus.BAD_REQUEST, '请求体需要 enabled 字段')
            return
        enabled = bool(payload.get('enabled'))
        try:
            result = engine.set_lan_enabled(enabled)
        except RuntimeError as error:
            self.log_event(f'切换桌面入口失败：{error}')
            self._json(HTTPStatus.CONFLICT, self._control_payload({'error': str(error)}))
            return
        self.log_event(f'桌面入口已{"启用" if enabled else "停用"}')
        self._json(HTTPStatus.OK, self._control_payload({'changed': result['changed']}))

    def _handle_control_port(self, payload: dict[str, object]) -> None:
        try:
            result = engine.set_lan_port(payload.get('port'))
        except RuntimeError as error:
            self.log_event(f'修改桌面端口失败：{error}')
            self._json(HTTPStatus.CONFLICT, self._control_payload({'error': str(error)}))
            return
        self.log_event(f'桌面端口已改为 {result["port"]}')
        self._json(HTTPStatus.OK, self._control_payload({'changed': result['changed']}))

    def _handle_control_token(self) -> None:
        token = engine.new_admin_token()
        try:
            self.app.rotate_token(token)
        except OSError as error:
            self.log_event(f'重新生成令牌失败：{error}')
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {'error': f'无法写入令牌文件：{error}'})
            return
        self.log_event('已重新生成访问令牌（旧会话全部失效）')
        self._json(HTTPStatus.OK, self._control_payload({'rotated': True}))

    # -- 文件浏览（只读）--------------------------------------------------
    def _handle_files(self, path: str, query: dict[str, list[str]]) -> None:
        raw = (query.get('path', ['']) or [''])[0]
        try:
            if path == '/api/files/roots':
                self._json(HTTPStatus.OK, {'roots': engine.file_shortcuts()})
                return
            if path == '/api/files/trash':
                self._json(HTTPStatus.OK, engine.trash_payload())
                return
            if path.startswith('/api/files/op/'):
                task = engine.file_task(path.rsplit('/', 1)[-1])
                if task is None:
                    self._json(HTTPStatus.OK, {'state': 'expired', 'message': '任务不存在或已过期'})
                    return
                self._json(HTTPStatus.OK, task.payload())
                return
            if path == '/api/files' and not raw:
                self._json(HTTPStatus.OK, {
                    'path': '', 'name': '', 'parent': None, 'entries': [],
                    'truncated': False, 'limit': engine.FILE_ENTRY_LIMIT,
                    'readonly': True, 'roots': engine.file_shortcuts(),
                    'shortcuts': engine.file_shortcuts(), 'at': int(time.time()),
                })
                return
            target = engine.resolve_user_path(raw)
            if path == '/api/files':
                self._json(HTTPStatus.OK, engine.list_directory(target))
            elif path == '/api/files/text':
                self._json(HTTPStatus.OK, engine.read_text_preview(target))
            else:
                self._error(HTTPStatus.NOT_FOUND, '未知接口')
        except RuntimeError as error:
            self._error(HTTPStatus.BAD_REQUEST, str(error))
        except OSError as error:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f'读取失败：{error}')

    def _handle_file_stream(self, path: str, query: dict[str, list[str]]) -> None:
        raw = (query.get('path', ['']) or [''])[0]
        try:
            target = engine.resolve_user_path(raw)
        except RuntimeError as error:
            self._error(HTTPStatus.BAD_REQUEST, str(error))
            return
        if not target.is_file():
            self._error(HTTPStatus.BAD_REQUEST, '不是普通文件')
            return
        try:
            size = target.stat().st_size
        except OSError as error:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f'无法读取文件：{error}')
            return
        download = path.endswith('/download')
        if download:
            content_type, disposition = engine.download_headers_for(target)
        else:
            content_type, disposition = engine.inline_headers_for(target)
            if disposition == 'inline' and size > engine.FILE_INLINE_LIMIT:
                self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                            '文件太大，不适合在页面里预览，请下载后再看')
                return
        self.send_response(HTTPStatus.OK)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(size))
        self.send_header('Content-Disposition',
                         f"{disposition}; filename*=UTF-8''{quote(target.name)}")
        self.send_header('Cache-Control', 'private, max-age=30')
        self.send_header('X-Content-Type-Options', 'nosniff')
        # 内联响应也带上最严 CSP：万一被当成文档打开，脚本也不会执行
        self.send_header('Content-Security-Policy', "default-src 'none'; sandbox")
        self.end_headers()
        if self.command == 'HEAD':
            return
        try:
            with target.open('rb') as handle:
                while True:
                    chunk = handle.read(256 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # -- 文件浏览的写操作（上传 / 新建目录 / 重命名 / 删除 / 回收站）--------
    def _handle_upload(self, query: dict[str, list[str]]) -> None:
        raw = (query.get('path', ['']) or [''])[0]
        name = (query.get('name', ['']) or [''])[0]
        overwrite = (query.get('overwrite', ['0']) or ['0'])[0] == '1'
        try:
            directory = engine.resolve_user_path(raw)
            length = int(self.headers.get('Content-Length') or 0)
            if length <= 0:
                raise RuntimeError('缺少文件内容')
            result = engine.save_upload(directory, name, self.rfile, length, overwrite)
        except (RuntimeError, OSError) as error:
            self.log_event(f'上传失败：{error}')
            status = (HTTPStatus.CONFLICT if '已存在' in str(error)
                      else HTTPStatus.BAD_REQUEST)
            self._error(status, str(error))
            return
        self._json(HTTPStatus.OK, result)

    def _handle_file_write(self, path: str, payload: dict[str, object]) -> None:
        try:
            if path == '/api/files/mkdir':
                parent = engine.resolve_user_path(str(payload.get('path') or ''))
                result = engine.create_folder(parent, str(payload.get('name') or ''))
            elif path == '/api/files/rename':
                target = engine.resolve_user_path(str(payload.get('path') or ''))
                result = engine.rename_entry(target, str(payload.get('name') or ''))
            elif path == '/api/files/delete':
                result = self._delete_many(payload)
            elif path == '/api/files/restore':
                ids = payload.get('ids')
                if not isinstance(ids, list) or not ids:
                    raise RuntimeError('缺少要恢复的条目')
                restored = []
                for entry_id in ids[:50]:
                    restored.append(engine.restore_from_trash(str(entry_id)))
                result = {'ok': True, 'restored': len(restored)}
            elif path == '/api/files/trash/empty':
                ids = payload.get('ids')
                if ids is not None and not isinstance(ids, list):
                    raise RuntimeError('ids 必须是数组')
                result = engine.purge_trash([str(item) for item in ids] if ids else None)
            elif path == '/api/files/op':
                paths = payload.get('paths')
                if not isinstance(paths, list):
                    raise RuntimeError('paths 必须是数组')
                result = engine.start_file_task(
                    str(payload.get('mode') or 'copy'),
                    [str(item) for item in paths],
                    str(payload.get('target') or ''),
                    str(payload.get('conflict') or 'rename'))
            elif path == '/api/files/op/cancel':
                task = engine.file_task(str(payload.get('id') or ''))
                if task is None:
                    raise RuntimeError('任务不存在或已过期')
                task.request_cancel()
                result = {'ok': True, 'task': task.payload()}
            else:
                self._error(HTTPStatus.NOT_FOUND, '未知接口')
                return
        except RuntimeError as error:
            self.log_event(f'{path} 失败：{error}')
            self._error(HTTPStatus.BAD_REQUEST, str(error))
            return
        except OSError as error:
            self.log_event(f'{path} 失败：{error}')
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f'操作失败：{error}')
            return
        self._json(HTTPStatus.OK, result)

    def _delete_many(self, payload: dict[str, object]) -> dict[str, object]:
        raw_paths = payload.get('paths')
        if not isinstance(raw_paths, list) or not raw_paths:
            raise RuntimeError('缺少要删除的路径')
        if len(raw_paths) > 50:
            raise RuntimeError('一次最多删除 50 项')
        moved: list[str] = []
        failed: list[dict[str, str]] = []
        for item in raw_paths:
            try:
                target = engine.resolve_user_path(str(item))
                engine.move_to_trash(target)
                moved.append(str(target))
            except (RuntimeError, OSError) as error:
                failed.append({'path': str(item), 'error': str(error)})
        if not moved and failed:
            raise RuntimeError(failed[0]['error'])
        return {'ok': True, 'moved': moved, 'failed': failed}

    # -- 静态文件 ---------------------------------------------------------
    def _serve_static(self, path: str) -> None:
        relative = unquote(path).lstrip('/') or 'index.html'
        if relative.endswith('/'):
            relative += 'index.html'
        normalised = posixpath.normpath(relative)
        if normalised.startswith('..') or normalised.startswith('/'):
            self._error(HTTPStatus.NOT_FOUND, '路径不合法')
            return
        candidate = (self.app.static_dir / normalised).resolve()
        try:
            candidate.relative_to(self.app.static_dir.resolve())
        except ValueError:
            self._error(HTTPStatus.NOT_FOUND, '路径不合法')
            return
        if not candidate.is_file():
            if '.' not in normalised.rsplit('/', 1)[-1]:
                candidate = (self.app.static_dir / 'index.html').resolve()
            if not candidate.is_file():
                self._error(HTTPStatus.NOT_FOUND, '资源不存在')
                return
        body = candidate.read_bytes()
        content_type = CONTENT_TYPES.get(candidate.suffix.lower(), 'application/octet-stream')
        self._send(HTTPStatus.OK, body, content_type)


def load_admin_token(path: Path, dev: bool) -> str:
    """读取安装时生成的访问令牌；缺失时生成一个并尽量落盘。"""
    try:
        token = path.read_text(encoding='utf-8').strip()
    except OSError:
        token = ''
    if token:
        return token
    token = secrets.token_hex(16)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(token + '\n', encoding='utf-8')
        os.chmod(path, 0o600)
        engine.log(f'已生成访问令牌：{path}')
    except OSError as error:
        engine.log(f'无法写入令牌文件（{error}）；本次运行使用临时令牌，重启后失效')
    return token


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='小米智能存储只读控制台')
    parser.add_argument('--host', default=os.environ.get('HOST', '127.0.0.1'))
    parser.add_argument('--port', type=int, default=int(os.environ.get('PORT', '18100')))
    parser.add_argument('--static-dir', type=Path,
                        default=Path(os.environ.get('STATIC_DIR', Path(__file__).parent / 'web')))
    parser.add_argument('--admin-token-file', type=Path,
                        default=Path(os.environ.get('ADMIN_TOKEN_FILE', '/data/plugin/xiaomi-nas-console/admin-token')))
    parser.add_argument('--dev', action='store_true', help='本地开发：自动生成令牌并允许无令牌访问')
    parser.add_argument('--cookie-secure', choices=('auto', 'always', 'never'),
                        default=os.environ.get('COOKIE_SECURE', 'auto'),
                        help='会话 Cookie 的 Secure 属性（auto：按 X-Forwarded-Proto 判断）')
    args = parser.parse_args(argv)

    static_dir = args.static_dir
    if not (static_dir / 'index.html').is_file():
        print(f'找不到前端页面：{static_dir}/index.html', file=sys.stderr)
        return 2

    sampler = engine.Sampler()
    sampler.start()
    token = load_admin_token(args.admin_token_file, args.dev)
    server = ConsoleServer((args.host, args.port), Handler, static_dir, token, sampler,
                           dev=args.dev, cookie_secure=args.cookie_secure,
                           token_file=args.admin_token_file)
    engine.log(f'控制台已启动：http://{args.host}:{args.port}（前端 {static_dir}）')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        sampler.stop()
        server.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
