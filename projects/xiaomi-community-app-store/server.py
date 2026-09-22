#!/usr/bin/env python3
"""Local-only web service for the Xiaomi NAS community application store."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import ipaddress
import json
import mimetypes
import os
import secrets
import threading
import time
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from storelib import (
    InstallManager,
    StoreError,
    fetch_store_latest_version,
    is_newer_version,
    load_apps_catalog,
    self_update_store,
    RAW_BASE,
    GITHUB_API_LATEST,
)


PROJECT = Path(__file__).resolve().parent
WEB = PROJECT / "web"


def _read_store_version() -> str:
    """从 VERSION 文件读取版本号（构建时写入）。"""
    version_file = PROJECT / "VERSION"
    if version_file.is_file():
        text = version_file.read_text(encoding="ascii").strip()
        if text:
            return text
    # 开发环境回退：从 build_release.py 读取
    script = PROJECT / "scripts" / "build_release.py"
    if script.is_file():
        for line in script.read_text(encoding="utf-8").splitlines():
            if line.startswith("VERSION"):
                return line.split("=", 1)[1].strip().strip("\"'")
    return "0.0.0"


STORE_VERSION = _read_store_version()
COOKIE_NAME = "xiaomi_community_store_session"
SESSION_TTL = 30 * 24 * 60 * 60
BASE_PATH = os.environ.get("BASE_PATH", "/")
ACTION_LOCK = threading.Lock()
# apps.json 本地缓存（减少 GitHub API 请求）
CATALOG_CACHE = Path(os.environ.get("CATALOG_CACHE", "/data/plugin/community-store/state/apps-cache.json"))


def json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


class StoreHandler(BaseHTTPRequestHandler):
    server_version = "XiaomiCommunityStore/0.2"

    @property
    def app(self) -> "StoreServer":
        return self.server  # type: ignore[return-value]

    def log_message(self, format_string: str, *args: object) -> None:
        print(f"{self.address_string()} [{self.log_date_time_string()}] {format_string % args}")

    def _send(self, status: int, body: bytes, content_type: str, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data: https://raw.githubusercontent.com; style-src 'self'; script-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'self'")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, value: object, headers: dict[str, str] | None = None) -> None:
        self._send(status, json_bytes(value), "application/json; charset=utf-8", headers)

    def _session_id(self) -> str | None:
        header_session = self.headers.get("X-Community-Session", "").strip()
        if header_session:
            return header_session
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        morsel = cookie.get(COOKIE_NAME)
        return morsel.value if morsel else None

    def _session(self) -> dict[str, object] | None:
        session_id = self._session_id()
        if not session_id:
            return None
        try:
            payload, provided = session_id.rsplit(".", 1)
            expiry_text, _random = payload.split(".", 1)
            expiry = int(expiry_text)
        except (ValueError, TypeError):
            return None
        expected = hmac.new(self.app.session_key, payload.encode("ascii"), hashlib.sha256).hexdigest()
        if expiry < int(time.time()) or not secrets.compare_digest(expected, provided):
            return None
        csrf = hmac.new(self.app.session_key, f"csrf:{session_id}".encode("ascii"), hashlib.sha256).hexdigest()
        return {"csrf": csrf, "expires": expiry}

    def _new_session(self) -> tuple[str, dict[str, object]]:
        expiry = int(time.time()) + SESSION_TTL
        payload = f"{expiry}.{secrets.token_urlsafe(24)}"
        signature = hmac.new(self.app.session_key, payload.encode("ascii"), hashlib.sha256).hexdigest()
        session_id = f"{payload}.{signature}"
        csrf = hmac.new(self.app.session_key, f"csrf:{session_id}".encode("ascii"), hashlib.sha256).hexdigest()
        session = {"csrf": csrf, "expires": expiry}
        return session_id, session

    def _session_cookie(self, session_id: str) -> str:
        secure = "" if self.app.dev else "; Secure"
        cookie_path = "/" if self.app.dev else BASE_PATH
        return f"{COOKIE_NAME}={session_id}; Path={cookie_path}; HttpOnly; SameSite=Strict{secure}; Max-Age={SESSION_TTL}"

    def _ip_candidates(self) -> list[str]:
        """真实客户端地址。

        商店只监听 127.0.0.1，生产流量都经 nginx 反代，TCP 对端永远是
        回环——绝不能把 self.client_address 当成用户来源。优先用 nginx
        写入的 X-Real-IP / X-Forwarded-For；两者都没有时（运维直连
        18119）才退回 TCP 对端。
        """
        forwarded = [
            self.headers.get("X-Real-IP", "").strip(),
            self.headers.get("X-Forwarded-For", "").split(",")[0].strip(),
        ]
        forwarded = [item for item in forwarded if item]
        if forwarded:
            return forwarded
        if self.client_address:
            return [str(self.client_address[0])]
        return []

    def _is_loopback_source(self) -> bool:
        for raw in self._ip_candidates():
            try:
                if ipaddress.ip_address(raw).is_loopback:
                    return True
            except ValueError:
                continue
        return False

    def _is_private_source(self) -> bool:
        for raw in self._ip_candidates():
            try:
                address = ipaddress.ip_address(raw)
            except ValueError:
                continue
            if address.is_loopback or address.is_private:
                return True
        return False

    def _bootstrap_token_ok(self) -> bool:
        """安装器 / Windows 本地代理可用的引导密钥（与 admin-token 相同）。"""
        secret = getattr(self.app, "admin_token", "")
        if not secret:
            return False
        candidates = []
        token = self.headers.get("X-Xiaomi-Bootstrap-Token", "").strip()
        if token:
            candidates.append(token)
        for header in ("X-Xiaomi-Bootstrap-Authorization", "Authorization"):
            value = self.headers.get(header, "")
            if value.lower().startswith("bearer "):
                candidates.append(value[7:].strip())
        return any(secrets.compare_digest(item, secret) for item in candidates if item)

    def _trusted_xiaomi_client(self) -> bool:
        """签发会话前的身份判断。

        1. 设备客户端证书（手机 App 完整证书通道）→ SUCCESS。
        2. 回环：nginx 的 X-Real-IP，或 TCP 对端本身是 127.0.0.1
           （Windows 客户端本地代理把 NAS 映射到本机时会走到这里）。
        3. Bootstrap token：nginx 转发的 ?token= / Authorization: Bearer，
           与安装时生成的 admin-token 比对。
        4. 私网来源：Windows 客户端经本地代理访问时通常**不带**设备客户端
           证书（$ssl_client_verify=NONE），X-Real-IP 是电脑的局域网地址。
           此时放行会话签发；安装/卸载仍依赖会话 + CSRF，且商店只安装
           验签通过的包。不要把商店管理端口暴露到公网。
        """
        if self.headers.get("X-Xiaomi-Client-Verify", "").upper() == "SUCCESS":
            return True
        if self._is_loopback_source():
            return True
        if self._bootstrap_token_ok():
            return True
        # Windows 小米客户端本地代理：无私网证书时的私网接入
        verify = self.headers.get("X-Xiaomi-Client-Verify", "").upper()
        if verify in ("NONE", "FAILED", "EXPIRED") and self._is_private_source():
            return True
        # 本地代理可能完全不经过带 ssl_client_verify 的 nginx 变量路径
        if self._is_private_source() and self.headers.get("X-Real-IP"):
            return True
        return False

    def _require_session(self, write: bool = False) -> dict[str, object] | None:
        session = self._session()
        if not session:
            self._json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "请从小米智能存储客户端重新打开插件市场"})
            return None
        if write and not secrets.compare_digest(str(session["csrf"]), self.headers.get("X-CSRF-Token", "")):
            self._json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "操作令牌已失效，请重新打开页面"})
            return None
        return session

    def _safe_file(self, root: Path, relative: str) -> Path | None:
        candidate = (root / unquote(relative).lstrip("/")).resolve()
        try:
            candidate.relative_to(root.resolve())
        except ValueError:
            return None
        return candidate if candidate.is_file() else None

    def _serve_file(self, path: Path) -> None:
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        # 不要给静态资源长缓存：商店升级后 app.js/styles.css 变了，
        # 客户端 WebView 拿着旧脚本会出现「按钮点了没反应」这类怪象。
        self._send(HTTPStatus.OK, path.read_bytes(), mime, {"Cache-Control": "no-store"})

    def _serve_index(self) -> None:
        session = self._session()
        session_id: str | None = None
        if not session and (self.app.dev or self._trusted_xiaomi_client()):
            session_id, session = self._new_session()
        csrf = str(session["csrf"]) if session else ""
        html = (WEB / "index.html").read_text(encoding="utf-8")
        html = html.replace("__CSRF_TOKEN__", csrf).replace("__SESSION_TOKEN__", session_id or self._session_id() or "")
        headers = {}
        if session_id:
            headers["Set-Cookie"] = self._session_cookie(session_id)
        self._send(HTTPStatus.OK, html.encode("utf-8"), "text/html; charset=utf-8", headers)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._serve_index()
            return
        if path == "/api/status":
            if not self._require_session():
                return
            self._json(HTTPStatus.OK, {
                "ok": True,
                "version": STORE_VERSION,
                "mode": "preview" if self.app.dev else "active",
            })
            return
        if path == "/healthz":
            self._json(HTTPStatus.OK, {"ok": True})
            return
        if path == "/api/update-check":
            if not self._require_session():
                return
            self._handle_update_check()
            return
        if path == "/api/catalog":
            if not self._require_session():
                return
            query = parse_qs(urlparse(self.path).query)
            self._handle_catalog(force_refresh=query.get("refresh") == ["1"])
            return
        # 静态资源（web/ 目录下的 JS/CSS/HTML）
        file_path = self._safe_file(WEB, path)
        if file_path:
            self._serve_file(file_path)
        else:
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})

    def _handle_update_check(self) -> None:
        try:
            latest = fetch_store_latest_version()
            has_update = is_newer_version(latest["version"], STORE_VERSION)
            self._json(HTTPStatus.OK, {
                "ok": True,
                "current": STORE_VERSION,
                "latest": latest["version"],
                "hasUpdate": has_update,
                "url": latest.get("url", ""),
            })
        except StoreError as error:
            self._json(HTTPStatus.OK, {
                "ok": False,
                "current": STORE_VERSION,
                "error": str(error),
            })

    def _handle_catalog(self, force_refresh: bool = False) -> None:
        """从 GitHub 拉取 apps.json（带本地缓存），图标转为 raw URL。

        带 refresh=1 时跳过 TTL 缓存，直接重新拉远程——页面上的刷新按钮用它。
        """
        try:
            catalog = load_apps_catalog(cache_path=CATALOG_CACHE, force_refresh=force_refresh)
            packages = catalog.get("apps", [])
            for package in packages:
                icon = str(package.get("icon", ""))
                if icon and not icon.startswith("http"):
                    package["iconUrl"] = f"{RAW_BASE}/{icon.lstrip('/')}"
                elif icon:
                    package["iconUrl"] = icon
                else:
                    package["iconUrl"] = ""
                inventory = (self.app.manager.inventory() if self.app.manager else {}).get(package["id"], {})
                package["installedVersion"] = inventory.get("version")
                package["managed"] = bool(inventory.get("managed"))
            self._json(HTTPStatus.OK, {
                "ok": True,
                "catalog": {"schemaVersion": 2, "packages": packages, "store": catalog.get("store", {})},
                "preview": self.app.dev,
            })
        except StoreError as error:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(error)})

    def _request_json(self) -> dict[str, object]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise StoreError("Invalid request length") from error
        if length <= 0 or length > 4096:
            raise StoreError("Invalid request body")
        try:
            value = json.loads(self.rfile.read(length))
        except json.JSONDecodeError as error:
            raise StoreError("Request body must be JSON") from error
        if not isinstance(value, dict):
            raise StoreError("Request body must be an object")
        return value

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path not in ("/api/install", "/api/uninstall", "/api/self-update"):
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})
            return
        if not self._require_session(write=True):
            return
        if self.app.dev:
            self._json(HTTPStatus.CONFLICT, {"ok": False, "error": "本地预览模式不会修改 NAS"})
            return

        # 自更新不需要 InstallManager
        if path == "/api/self-update":
            self._handle_self_update()
            return

        if not self.app.manager:
            self._json(HTTPStatus.CONFLICT, {"ok": False, "error": "安装服务未就绪"})
            return
        try:
            body = self._request_json()
            package_id = str(body.get("id", ""))
            if not ACTION_LOCK.acquire(blocking=False):
                raise StoreError("另一个安装任务正在执行")
            try:
                result = self.app.manager.install(package_id) if path == "/api/install" else self.app.manager.uninstall(package_id)
            finally:
                ACTION_LOCK.release()
            self._json(HTTPStatus.OK, result)
        except StoreError as error:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(error)})
        except Exception as error:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": f"操作失败：{error}"})

    def _handle_self_update(self) -> None:
        if not ACTION_LOCK.acquire(blocking=False):
            self._json(HTTPStatus.CONFLICT, {"ok": False, "error": "另一个任务正在执行，请稍后再试"})
            return
        try:
            result = self_update_store(STORE_VERSION)
            self._json(HTTPStatus.OK, result)
        except StoreError as error:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(error)})
        except Exception as error:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": f"更新失败：{error}"})
        finally:
            ACTION_LOCK.release()


class StoreServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], dev: bool, manager: InstallManager | None, admin_token: str):
        super().__init__(address, StoreHandler)
        self.dev = dev
        self.manager = manager
        self.admin_token = admin_token
        self.session_key = hashlib.sha256(admin_token.encode("utf-8")).digest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dev", action="store_true")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "18119")))
    parser.add_argument("--user-id", default=os.environ.get("NAS_USER_ID", ""))
    parser.add_argument("--admin-token-file", type=Path, default=Path(os.environ.get("ADMIN_TOKEN_FILE", "/data/plugin/community-store/admin-token")))
    args = parser.parse_args()
    manager = None
    if not args.dev:
        if not args.user_id:
            raise SystemExit("NAS_USER_ID is required outside preview mode")
        manager = InstallManager(
            catalog_dir=None,
            public_key=None,
            user_id=args.user_id,
            remote_apps=True,
        )
        try:
            admin_token = args.admin_token_file.read_text(encoding="utf-8").strip()
        except OSError as error:
            raise SystemExit(f"Cannot read admin token: {error}") from error
        if len(admin_token) < 20:
            raise SystemExit("Admin token is too short")
    else:
        admin_token = "preview-only-token-not-for-production"
    server = StoreServer((args.host, args.port), args.dev, manager, admin_token)
    print(f"Xiaomi community store v{STORE_VERSION} on http://{args.host}:{args.port} ({'preview' if args.dev else 'active'})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
