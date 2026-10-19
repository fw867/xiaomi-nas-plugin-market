"""Docker 控制台 HTTP 服务 — 小米智能存储社区插件。

移植自 lvmaple/minas_plugin 的 `xiaomi-nas-plugin-dockerctl`（上游 `src/files/httpd.py`
的 Handler 与 `src/ui/dockerctl.cgi` 的静态文件分发）。与上游的差异：

* 静态页面由 Python 直接提供（上游靠 nginx alias / CGI），少一层路径约定；
* 只监听回环，并且除 `/api/health` 外的请求必须是「小米客户端证书入口」或带
  回环 `X-Real-IP`；这是 Docker 级权限，不能像上游那样只靠 nginx 不配鉴权；
* POST 必须 `Content-Type: application/json`，挡住跨站表单伪造的简单请求；
* 日志落到 systemd journal（print 到 stdout），不再写 `/tmp/dockerctl/server.log`。
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import tempfile
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from engine import DEFAULT_PORT, Engine, Error, installed_version

WEB = Path(os.environ.get("STATIC_DIR") or Path(__file__).resolve().parent / "web")
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/assets/dockerctl.png": ("assets/dockerctl.png", "image/png"),
}
MAX_BODY = 1_000_000


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, engine: Engine, user: str = "", dev: bool = False):
        self.engine = engine
        self.user = user
        self.dev = dev
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    timeout = 30  # 每连接 socket 超时（慢速攻击保护）
    server_version = "dockerctl"
    sys_version = ""

    def log_message(self, fmt, *args):
        pass

    # ---- 响应 ----
    def _send(self, code, body, content_type="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else str(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _error(self, error: Error):
        self._json({"ok": False, "error": str(error)}, error.status)

    def _static(self, path: str):
        name, content_type = STATIC[path]
        try:
            # 二进制资源（图标）按字节读，绝不经过文本换行处理。
            data = (WEB / name).read_bytes()
        except OSError:
            self._json({"error": "not found"}, 404)
            return
        self._send(200, data, content_type)

    # ---- 鉴权 ----
    def trusted(self) -> bool:
        """只信任小米客户端证书入口与回环请求。

        nginx 的客户端入口会带上 `X-Xiaomi-Client-Verify` / `X-Xiaomi-Client-DN`；
        CN 里嵌着当前 NAS 用户 id（`nas.<uid>`）。其它来源一律拒绝——本插件能操作
        宿主机全部容器，权限等价于 root。
        """
        if self.server.dev:
            return True
        verify = self.headers.get("X-Xiaomi-Client-Verify", "")
        dn = self.headers.get("X-Xiaomi-Client-DN", "")
        user = (self.server.user or "").lstrip("u")
        if verify == "SUCCESS" and user and re.search(
                r"CN=nas\." + re.escape(user) + r"\.", dn):
            return True
        try:
            return ipaddress.ip_address(self.headers.get("X-Real-IP", "")).is_loopback
        except ValueError:
            return False

    # ---- 请求体 ----
    def _body(self) -> dict:
        content_type = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if content_type != "application/json":
            raise Error("请求须为 application/json", 415)
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError as exc:
            raise Error("请求长度无效") from exc
        if length < 0 or length > MAX_BODY:
            raise Error("请求体过大", 413)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise Error("JSON 格式无效") from exc
        if not isinstance(data, dict):
            raise Error("JSON 需为对象")
        return data

    # ---- GET ----
    def do_GET(self):
        route = urlsplit(self.path)
        path = route.path.rstrip("/") or "/"
        query = parse_qs(route.query)

        if path in STATIC:
            return self._static(path)
        if path == "/api/health":
            return self._json(self.server.engine.health())
        if not self.trusted():
            return self._json({"ok": False, "error": "请从小米客户端打开本插件"}, 401)

        engine = self.server.engine
        try:
            if path == "/api/info":
                return self._json(engine.info())
            if path == "/api/overview":
                return self._json(engine.overview())
            if path == "/api/settings":
                return self._json(engine.settings())

            if path == "/api/containers":
                show_all = query.get("all", ["0"])[0] != "0"
                return self._json(engine.containers(show_all))

            match = re.fullmatch(r"/api/containers/([^/]+)/logs", path)
            if match:
                return self._json(engine.container_logs(
                    unquote(match.group(1)), query.get("tail", ["200"])[0]))
            match = re.fullmatch(r"/api/containers/([^/]+)/stats", path)
            if match:
                return self._json(engine.container_stats(unquote(match.group(1))))
            match = re.fullmatch(r"/api/containers/([^/]+)", path)
            if match:
                return self._json(engine.container_detail(unquote(match.group(1))))

            if path == "/api/images":
                return self._json(engine.images())
            if path == "/api/images/pull":
                return self._json({"error": "use POST"}, 405)
            if path == "/api/images/pull/status":
                return self._json(engine.pull_status())
            match = re.fullmatch(r"/api/images/([^/]+)", path)
            if match:
                return self._json(engine.image_detail(unquote(match.group(1))))

            if path == "/api/volumes":
                return self._json(engine.volumes())
            match = re.fullmatch(r"/api/volumes/([^/]+)", path)
            if match:
                return self._json(engine.volume_detail(unquote(match.group(1))))

            if path == "/api/networks":
                return self._json(engine.networks())
            match = re.fullmatch(r"/api/networks/([^/]+)", path)
            if match:
                return self._json(engine.network_detail(unquote(match.group(1))))

            if path == "/api/compose":
                return self._json(engine.list_compose())
            if path == "/api/registry/auth":
                return self._json(engine.registry_auths())
            if path == "/api/logs":
                return self._json(engine.logs(query.get("tail", ["200"])[0]))
        except Error as error:
            return self._error(error)
        except Exception:  # noqa: BLE001 - 任何异常都不能带出堆栈细节
            traceback.print_exc()
            return self._json({"ok": False, "error": "内部错误"}, 500)

        self._json({"error": "not found"}, 404)

    # ---- POST ----
    def do_POST(self):
        route = urlsplit(self.path)
        path = route.path.rstrip("/")
        if not self.trusted():
            return self._json({"ok": False, "error": "请从小米客户端打开本插件"}, 401)
        engine = self.server.engine
        try:
            body = self._body()

            match = re.fullmatch(
                r"/api/containers/([^/]+)/(start|stop|restart|pause|unpause|kill|remove)",
                path)
            if match:
                return self._json(engine.container_action(
                    unquote(match.group(1)), match.group(2), body))

            if path == "/api/deploy/run":
                return self._json(engine.deploy_run(body))
            if path == "/api/deploy/compose":
                return self._json(engine.deploy_compose(body))

            if path == "/api/logs/clear":
                return self._json(engine.clear_logs())

            if path == "/api/images/pull":
                return self._json(engine.start_pull(body))
            if path == "/api/images/pull/cancel":
                return self._json(engine.cancel_pull())

            if path == "/api/registry/login":
                return self._json(engine.registry_login(body))
            if path == "/api/registry/logout":
                return self._json(engine.registry_logout(body))

            if path == "/api/settings":
                return self._json(engine.save_settings(body))

            match = re.fullmatch(r"/api/images/([^/]+)/remove", path)
            if match:
                return self._json(engine.remove_image(unquote(match.group(1)), body))

            if path == "/api/volumes":
                return self._json(engine.create_volume(body))
            match = re.fullmatch(r"/api/volumes/([^/]+)/remove", path)
            if match:
                return self._json(engine.remove_volume(unquote(match.group(1))))

            if path == "/api/networks":
                return self._json(engine.create_network(body))
            match = re.fullmatch(r"/api/networks/([^/]+)/remove", path)
            if match:
                return self._json(engine.remove_network(unquote(match.group(1))))

            match = re.fullmatch(r"/api/compose/([^/]+)/(down|remove)", path)
            if match:
                return self._json(engine.compose_action(
                    unquote(match.group(1)), match.group(2)))
        except Error as error:
            return self._error(error)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            return self._json({"ok": False, "error": "内部错误"}, 500)

        self._json({"error": "not found"}, 404)

    def do_OPTIONS(self):
        # 不返回 CORS 头：页面与接口同源（都经 nginx 客户端入口），跨源访问有意禁止。
        self.send_response(204)
        self.send_header("Allow", "GET, POST, OPTIONS")
        self.end_headers()


def main() -> int:
    parser = argparse.ArgumentParser(description="Xiaomi Docker Console plugin")
    parser.add_argument("--dev", action="store_true", help="本地预览：跳过客户端证书校验")
    parser.add_argument("--port", type=int, default=0, help="覆盖监听端口")
    args = parser.parse_args()

    if args.dev and not os.environ.get("DATA_DIR"):
        # 预览模式绝不写 /data 下的真实插件目录。
        data_dir = tempfile.mkdtemp(prefix="dockerctl-preview-")
    else:
        data_dir = os.environ.get("DATA_DIR") or "/data/plugin/dockerctl/data"
    user = os.environ.get("NAS_USER_ID", "u000000" if args.dev else "")
    host = os.environ.get("HOST", "127.0.0.1")
    port = args.port or int(os.environ.get("PORT", DEFAULT_PORT))

    engine = Engine(data_dir, dev=args.dev)
    server = Server((host, port), engine, user, args.dev)
    print(
        "dockerctl %s listening on http://%s:%s docker=%s data=%s"
        % (installed_version(), host, server.server_port, engine.docker.bin, data_dir),
        flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
