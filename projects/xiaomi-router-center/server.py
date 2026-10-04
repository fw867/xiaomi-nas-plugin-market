"""路由器软件中心插件的服务端：只做三件事

  1. 提供前端页面（src/ui 下的静态文件，含被 iframe 嵌入的真实软件中心）；
  2. 提供 /api/status、/api/settings，让插件页显示路由器状态、保存目标地址与令牌；
  3. /healthz 给厂商框架与商店做健康检查。

写操作只有一处：换目标地址时重新渲染自己的那一个 nginx 配置文件（nginx -t 通过才 reload）。
"""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import sys
import threading
import time
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import engine                                                            # noqa: E402

STATIC_DIR = Path(os.environ.get('STATIC_DIR', str(engine.RELEASE_DIR / 'web')))
CONTENT_TYPES = {
    '.html': 'text/html; charset=utf-8',
    '.js': 'text/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.json': 'application/json; charset=utf-8',
    '.png': 'image/png',
    '.svg': 'image/svg+xml',
    '.ico': 'image/x-icon',
    '.woff2': 'font/woff2',
}
CLIENT_ENTRY = 'xiaomi'          # 由 nginx 打 X-Console-Entry: xiaomi
LAN_ENTRY = 'lan'
LOCAL_ENTRY = 'local'


class RouterCenter:
    """进程级共享状态：当前入口、端口（渲染 nginx 模板要用）。"""

    def __init__(self, port: int) -> None:
        self.port = port


class Handler(BaseHTTPRequestHandler):
    server_version = 'XiaomiNasRouterCenter/' + (engine.VERSION or 'unknown')
    protocol_version = 'HTTP/1.1'
    app: RouterCenter

    # -- 基础工具 ---------------------------------------------------------
    def log_message(self, fmt: str, *args) -> None:                       # noqa: A003
        pass

    def log_event(self, message: str) -> None:
        engine.log(f'{self.client_address[0]} {message}')

    def entry(self) -> str:
        return (self.headers.get('X-Console-Entry') or '').strip().lower() or LOCAL_ENTRY

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, status: HTTPStatus, payload: object) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode('utf-8'),
                   'application/json; charset=utf-8')

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._json(status, {'ok': False, 'error': message})

    # -- 路由 -------------------------------------------------------------
    def do_GET(self) -> None:                                             # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path == '/healthz':
            settings = engine.load_settings()
            self._json(HTTPStatus.OK, {'ok': True, 'version': engine.VERSION,
                                       'target': settings['target'], 'uptime': int(time.time() - STARTED),
                                       'assets': engine.assets_present(STATIC_DIR)})
            return
        if path == '/api/status':
            self._json(HTTPStatus.OK, engine.status_payload(self.app.port))
            return
        if path == '/api/settings':
            settings = engine.load_settings()
            self._json(HTTPStatus.OK, {'ok': True, 'target': settings['target'],
                                       'default_target': engine.DEFAULT_TARGET,
                                       'token_set': bool(engine.router_token()),
                                       'token': engine.router_token(),
                                       'assets': engine.assets_present(STATIC_DIR),
                                       'entry': self.entry()})
            return
        if path.startswith('/view'):
            self._proxy_view(path[len('/view'):], parsed.query)
            return
        if path == '/root' or path.startswith('/root/'):
            self._serve_router_page(path[len('/root'):], parsed.query)
            return
        if path.startswith('/api/github/'):
            self._proxy_github(path[len('/api/'):], parsed.query)
            return
        if path.startswith('/api/'):
            self._error(HTTPStatus.NOT_FOUND, '未知接口')
            return
        self._serve_static(path)

    def do_POST(self) -> None:                                            # noqa: N802
        parsed = urlparse(self.path)
        length = int(self.headers.get('Content-Length') or 0)
        if parsed.path.startswith('/view') or parsed.path.startswith('/api/github/'):
            body = self.rfile.read(length) if length else None
            if parsed.path.startswith('/api/github/'):
                self._proxy_github(parsed.path[len('/api/'):], parsed.query)
            else:
                self._proxy_view(parsed.path[len('/view'):], parsed.query, body=body)
            return
        if length > 32768:
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
        if parsed.path == '/api/settings':
            self._save_settings(payload)
            return
        self._error(HTTPStatus.NOT_FOUND, '未知接口')

    # -- 软件中心的本地化反代 --------------------------------------------
    def _serve_router_page(self, rest: str, query: str) -> None:
        """插件根路径（App 打开的就是这里）：直接返回本地化后的软件中心页面。

        不做 iframe 套壳 —— 厂商客户端里没有一个插件用 iframe，直接给页面最稳。
        """
        try:
            html = engine.fetch_router_page()
        except Exception as error:                                     # noqa: BLE001
            self.log_event(f'取软件中心页面失败：{error}')
            self._error(HTTPStatus.BAD_GATEWAY, f'无法访问软件中心：{error}')
            return
        prefix = (self.headers.get('X-Plugin-Prefix') or '').rstrip('/')
        if not prefix:
            prefix = '/plugin'
        rewritten, unknown = engine.prepare_page(
            html, prefix, service_base=f'{prefix}/ctl', token=engine.router_token())
        # 常驻入口：右下/右上角一个小按钮，进插件自己的设置页（目标地址、状态）。
        # 令牌校验那层门由软件中心自己负责（电脑端与移动端同一套），插件不再重复。
        scripts = (f'<script src="{prefix}/assets/close.js" defer></script>'
                   f'<script src="{prefix}/assets/overlay.js" defer></script>')
        rewritten = rewritten.replace('</body>', scripts + '</body>', 1)
        if unknown:
            self.log_event(f'页面里仍有外部脚本未本地化：{unknown[:3]}')
        missing = [name for name, present in engine.assets_present(STATIC_DIR).items() if not present]
        if missing:
            self.log_event(f'本地资源缺失 {missing}，页面可能显示不全')
        self._send(HTTPStatus.OK, rewritten.encode('utf-8'), 'text/html; charset=utf-8')

    def _proxy_github(self, rest: str, query: str) -> None:
        """把页面里对 GitHub 的直连改为经 NAS 转发（App 内 webview 访问不了外网）。

        注意：绝不能把客户端的 Authorization 带过去，否则 GitHub 会判成无效凭据返回 401。
        """
        upstream = engine.github_upstream(rest)
        if not upstream:
            self._error(HTTPStatus.NOT_FOUND, '不是 GitHub 转发路径')
            return
        url = upstream + (f'?{query}' if query else '')
        try:
            request = urllib.request.Request(url, headers={
                'User-Agent': 'XiaomiNasRouterCenter/' + (engine.VERSION or '?'),
                'Accept': self.headers.get('Accept') or 'application/json',
            }, method='GET')
            with urllib.request.urlopen(request, timeout=120) as response:
                self._send(HTTPStatus(response.status), response.read(),
                           response.headers.get('Content-Type') or 'application/json; charset=utf-8')
        except urllib.error.HTTPError as error:
            self._send(HTTPStatus(error.code), error.read() or b'',
                       error.headers.get('Content-Type') or 'application/json; charset=utf-8')
        except Exception as error:                                     # noqa: BLE001
            self.log_event(f'GitHub 转发失败 {rest}: {error}')
            self._error(HTTPStatus.BAD_GATEWAY, f'GitHub 转发失败：{error}')

    def _proxy_view(self, rest: str, query: str, body: bytes | None = None) -> None:
        """`/view/...` 是软件中心在客户端里的入口：

        * `/view/`（首页）—— 取回来把外部 CDN 换成本地副本再发出去；
        * `/view/api/...` —— 原样转发到路由器（含 Authorization），不缓冲；
        * 其它（favicon 等）—— 直接转发。
        """
        suffix = rest.lstrip('/')
        target = engine.load_settings()['target'].rstrip('/')
        try:
            if suffix in ('', 'index.html') and self.command == 'GET':
                self._serve_router_page(suffix, query)
                return
            if suffix.startswith('github/'):
                self._proxy_github(suffix, query)
                return
            url = f'{target}/{suffix}'
            if query:
                url += f'?{query}'
            headers = {
                'User-Agent': self.headers.get('User-Agent') or 'XiaomiNasRouterCenter',
                'Accept': self.headers.get('Accept') or '*/*',
            }
            for name in ('Authorization', 'Content-Type', 'Accept-Language'):
                value = self.headers.get(name)
                if value:
                    headers[name] = value
            request = urllib.request.Request(url, data=body, headers=headers, method=self.command)
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = response.read()
                content_type = response.headers.get('Content-Type') or 'application/octet-stream'
                self._send(HTTPStatus(response.status), payload, content_type)
        except urllib.error.HTTPError as error:
            # 401/404 这些要原样回给页面（软件中心靠 401 判断令牌错误）
            payload = error.read() or b''
            self._send(HTTPStatus(error.code), payload,
                       error.headers.get('Content-Type') or 'application/json; charset=utf-8')
        except Exception as error:                                     # noqa: BLE001
            self.log_event(f'转发失败 {suffix}: {error}')
            self._error(HTTPStatus.BAD_GATEWAY, f'无法访问软件中心：{error}')

    def _save_settings(self, payload: dict[str, object]) -> None:
        user = os.environ.get('NAS_USER_ID', '')
        target = str(payload.get('target') or engine.load_settings()['target'])
        try:
            if 'token' in payload:
                engine.save_router_token(str(payload.get('token') or ''))
            engine.apply_target(target, user, self.app.port)
        except RuntimeError as error:
            self.log_event(f'保存设置失败：{error}')
            self._json(HTTPStatus.CONFLICT, {'ok': False, 'error': str(error),
                                             **engine.status_payload(self.app.port)})
            return
        except OSError as error:
            self.log_event(f'保存设置失败：{error}')
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f'写入失败：{error}')
            return
        self.log_event(f'设置已更新：目标 {target}')
        self._json(HTTPStatus.OK, engine.status_payload(self.app.port))

    # -- 静态文件 ---------------------------------------------------------
    def _serve_static(self, path: str) -> None:
        relative = unquote(path).lstrip('/') or 'index.html'
        if relative.endswith('/'):
            relative += 'index.html'
        candidate = (STATIC_DIR / posixpath.normpath(relative)).resolve()
        try:
            candidate.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self._error(HTTPStatus.FORBIDDEN, '拒绝访问')
            return
        if candidate.is_dir():
            candidate = candidate / 'index.html'
        if not candidate.is_file():
            self._error(HTTPStatus.NOT_FOUND, '文件不存在')
            return
        content_type = CONTENT_TYPES.get(candidate.suffix.lower(), 'application/octet-stream')
        self._send(HTTPStatus.OK, candidate.read_bytes(), content_type)


STARTED = time.time()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='小米存储 · 路由器软件中心')
    parser.add_argument('--host', default=os.environ.get('HOST', '127.0.0.1'))
    parser.add_argument('--port', type=int, default=int(os.environ.get('PORT', '18101')))
    args = parser.parse_args(argv)

    handler = type('BoundHandler', (Handler,), {'app': RouterCenter(args.port)})
    server = ThreadingHTTPServer((args.host, args.port), handler)
    engine.log(f'路由器软件中心已启动：http://{args.host}:{args.port}'
               f'（目标 {engine.load_settings()["target"]}，前端 {STATIC_DIR}）')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
