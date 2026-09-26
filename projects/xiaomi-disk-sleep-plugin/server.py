#!/usr/bin/env python3
"""硬盘休眠插件的本地 HTTP 服务：只监听 127.0.0.1，由 nginx 的 /plugin 路径代理。"""

from __future__ import annotations

import json
import mimetypes
import os
import subprocess
import threading
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import engine
from engine import Error

HOST = os.environ.get('HOST', '127.0.0.1')
PORT = int(os.environ.get('PORT', '18160'))
WEB_DIR = Path(os.environ.get('WEB_DIR', Path(__file__).resolve().parent / 'web'))
ACTION_LOCK = threading.Lock()


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')


class Handler(BaseHTTPRequestHandler):
    server_version = 'XiaomiDiskSleep/0.1'

    def log_message(self, fmt: str, *args: object) -> None:
        print(f'{self.address_string()} [{self.log_date_time_string()}] {fmt % args}', flush=True)

    # -- 输出 -------------------------------------------------------------
    def send_payload(self, status: int, body: bytes, content_type: str, cache: str = 'no-store') -> None:
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', cache)
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'SAMEORIGIN')
        self.end_headers()
        self.wfile.write(body)

    def json_out(self, status: int, value: object) -> None:
        self.send_payload(status, json_bytes(value), 'application/json; charset=utf-8')

    def fail(self, status: int, message: str) -> None:
        self.json_out(status, {'ok': False, 'error': message})

    # -- 路由 -------------------------------------------------------------
    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
        if path == '/healthz':
            self.json_out(HTTPStatus.OK, {'ok': True, 'version': engine.VERSION})
            return
        if path == '/api/status':
            try:
                self.json_out(HTTPStatus.OK, engine.snapshot())
            except Error as error:
                self.fail(HTTPStatus.BAD_REQUEST, str(error))
            except (subprocess.SubprocessError, OSError) as error:
                self.fail(HTTPStatus.INTERNAL_SERVER_ERROR, f'读取状态失败：{error}')
            return
        if path == '/api/events':
            self.json_out(HTTPStatus.OK, {'ok': True, 'events': engine.recent_events(self._limit(query))})
            return
        if path == '/api/hdidle-log':
            self.json_out(HTTPStatus.OK, {'ok': True, 'entries': engine.hdidle_log(self._limit(query))})
            return
        if path == '/api/activity':
            # 「谁在写盘」：块设备计数一直在采；目录扫描与事件库只在有人打开
            # 本页时才做（见 engine.activity_tick）。面板的「重新扫描」带 force。
            try:
                force = (query.get('force') or [''])[0] == '1'
                self.json_out(HTTPStatus.OK, engine.activity_snapshot(force=force))
            except (subprocess.SubprocessError, OSError, ValueError) as error:
                self.fail(HTTPStatus.INTERNAL_SERVER_ERROR, f'读取写入活动失败：{error}')
            return
        if path in ('/', '/index.html'):
            self.serve_static('index.html')
            return
        self.serve_static(path.lstrip('/'))

    @staticmethod
    def _limit(query: dict[str, list[str]], default: int = 200) -> int:
        try:
            value = int((query.get('limit') or [default])[0])
        except (TypeError, ValueError):
            return default
        return max(1, min(500, value))

    def serve_static(self, relative: str) -> None:
        candidate = (WEB_DIR / relative).resolve()
        try:
            candidate.relative_to(WEB_DIR.resolve())
        except ValueError:
            self.fail(HTTPStatus.FORBIDDEN, 'forbidden')
            return
        if not candidate.is_file():
            self.fail(HTTPStatus.NOT_FOUND, 'not found')
            return
        mime = mimetypes.guess_type(candidate.name)[0] or 'application/octet-stream'
        cache = 'no-store' if candidate.suffix in ('.html', '.js', '.css') else 'public, max-age=300'
        if candidate.name == 'index.html':
            # 页脚显示真实安装版本，而不是占位符（安装目录名里带着版本号）
            html = candidate.read_text(encoding='utf-8').replace('__PLUGIN_VERSION__', engine.installed_version())
            self.send_payload(HTTPStatus.OK, html.encode('utf-8'), mime, cache)
            return
        self.send_payload(HTTPStatus.OK, candidate.read_bytes(), mime, cache)

    def do_POST(self) -> None:
        path = urllib.parse.urlparse(self.path).path
        try:
            length = int(self.headers.get('Content-Length', '0') or '0')
        except ValueError:
            self.fail(HTTPStatus.BAD_REQUEST, '无效请求长度')
            return
        if not 0 < length <= 8192:
            self.fail(HTTPStatus.BAD_REQUEST, '无效请求长度')
            return
        try:
            body = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError):
            self.fail(HTTPStatus.BAD_REQUEST, '请求体必须是 JSON')
            return
        if not isinstance(body, dict):
            self.fail(HTTPStatus.BAD_REQUEST, '请求体必须是对象')
            return

        if not ACTION_LOCK.acquire(blocking=False):
            self.fail(HTTPStatus.CONFLICT, '上一个操作还在进行，请稍候')
            return
        try:
            if path == '/api/switch':
                engine.set_app_switch(bool(body.get('enabled')))
                engine.add_event(None, 'switch', '开启休眠' if body.get('enabled') else '关闭休眠')
            elif path == '/api/takeover':
                # 页面上的主按钮：一个开关同时管系统开关与插件接管
                engine.set_takeover(bool(body.get('enabled')))
            elif path == '/api/timeout':
                engine.set_minutes(body.get('minutes'))
            elif path == '/api/restore':
                engine.restore_official()
                engine.add_event(None, 'config', '恢复官方 30 分钟')
            else:
                self.fail(HTTPStatus.NOT_FOUND, 'not found')
                return
            self.json_out(HTTPStatus.OK, engine.snapshot())
        except Error as error:
            self.fail(HTTPStatus.BAD_REQUEST, str(error))
        except (subprocess.SubprocessError, OSError) as error:
            self.fail(HTTPStatus.INTERNAL_SERVER_ERROR, f'操作失败：{error}')
        finally:
            ACTION_LOCK.release()


def prepare() -> None:
    """启动时把 drop-in 与已保存的设置对齐（升级、重装、重启都靠这一步）。"""
    engine.DATA_DIR.mkdir(parents=True, exist_ok=True)
    engine.RUN_DIR.mkdir(parents=True, exist_ok=True)
    if engine.managed():
        try:
            engine.apply_timeout(engine.configured_minutes())
        except Error:
            pass
    engine.start_sampler()


def main() -> int:
    prepare()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f'Xiaomi disk sleep plugin on http://{HOST}:{PORT}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        engine._stop_sampler.set()
        server.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
