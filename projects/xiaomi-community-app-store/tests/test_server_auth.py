from __future__ import annotations

import http.client
import json
import threading
import unittest
from unittest import mock

from server import StoreServer


class ServerAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = StoreServer(("127.0.0.1", 0), False, None, "a" * 32)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method: str, path: str, body: dict | None = None, headers: dict | None = None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        encoded = json.dumps(body).encode("utf-8") if body is not None else None
        request_headers = dict(headers or {})
        if encoded is not None:
            request_headers["Content-Type"] = "application/json"
            request_headers["Content-Length"] = str(len(encoded))
        connection.request(method, path, body=encoded, headers=request_headers)
        response = connection.getresponse()
        payload = response.read()
        response_headers = dict(response.getheaders())
        connection.close()
        return response.status, response_headers, payload

    def test_untrusted_request_cannot_open_catalog(self) -> None:
        status, _, _ = self.request("GET", "/api/catalog")
        self.assertEqual(401, status)
        status, headers, payload = self.request("POST", "/api/unlock", {"code": "a" * 32})
        self.assertEqual(404, status)

    def _index_session(self, headers: dict) -> str:
        status, _, payload = self.request("GET", "/index.html", headers=headers)
        self.assertEqual(200, status)
        html = payload.decode("utf-8")
        marker = '<meta name="session-token" content="'
        return html.split(marker, 1)[1].split('"', 1)[0]

    def test_windows_local_proxy_private_ip_gets_session(self) -> None:
        """Windows 客户端本地代理：无客户端证书（Verify=NONE），X-Real-IP 是局域网地址。

        回归：此前只认 SUCCESS/回环，导致页面 csrf/session 为空，
        /api/catalog 恒 401，前端再把 HTML 当 JSON 解析成
        Unexpected token '<'。
        """
        session = self._index_session({
            "X-Xiaomi-Client-Verify": "NONE",
            "X-Real-IP": "192.168.31.50",
        })
        self.assertTrue(session)
        status, _, _ = self.request(
            "GET", "/api/catalog",
            headers={"X-Community-Session": session, "X-Real-IP": "192.168.31.50"},
        )
        self.assertIn(status, (200, 500))

    def _assert_no_session(self, payload: bytes) -> None:
        html = payload.decode("utf-8")
        marker = '<meta name="session-token" content="'
        self.assertEqual("", html.split(marker, 1)[1].split('"', 1)[0])

    def test_public_ip_without_cert_gets_no_session(self) -> None:
        status, _, payload = self.request("GET", "/index.html", headers={
            "X-Xiaomi-Client-Verify": "NONE",
            "X-Real-IP": "8.8.8.8",
        })
        self.assertEqual(200, status)
        self._assert_no_session(payload)

    def test_bootstrap_token_gets_session(self) -> None:
        session = self._index_session({"X-Xiaomi-Bootstrap-Token": "a" * 32})
        self.assertTrue(session)
        session = self._index_session({"Authorization": "Bearer " + "a" * 32})
        self.assertTrue(session)

    def test_wrong_bootstrap_token_gets_no_session(self) -> None:
        status, _, payload = self.request("GET", "/index.html", headers={
            "X-Real-IP": "8.8.8.8",
            "X-Xiaomi-Bootstrap-Token": "b" * 32,
        })
        self.assertEqual(200, status)
        self._assert_no_session(payload)

    def test_backend_tcp_peer_is_not_used_when_forwarded_present(self) -> None:
        """nginx 反代时 TCP 对端恒为 127.0.0.1；有 X-Real-IP 时不得据此放行。"""
        status, _, payload = self.request("GET", "/index.html", headers={
            "X-Real-IP": "8.8.8.8",
            "X-Xiaomi-Client-Verify": "NONE",
        })
        self.assertEqual(200, status)
        self._assert_no_session(payload)

    def test_local_catalog_and_bundles_are_not_served(self) -> None:
        # 旧的本地 catalog/icons 路径已移除，不再提供
        status, _, _ = self.request("GET", "/catalog/icons/devicemanager.png")
        self.assertEqual(404, status)
        status, _, _ = self.request("GET", "/catalog/bundles/devicemanager-0.4.1.bundle.zip")
        self.assertEqual(404, status)

    def test_verified_xiaomi_client_gets_session_from_index(self) -> None:
        status, headers, payload = self.request(
            "GET",
            "/index.html",
            headers={"X-Xiaomi-Client-Verify": "SUCCESS"},
        )
        self.assertEqual(200, status)
        html = payload.decode("utf-8")
        marker = '<meta name="session-token" content="'
        session = html.split(marker, 1)[1].split('"', 1)[0]
        self.assertTrue(session)
        cookie = headers["Set-Cookie"].split(";", 1)[0]

        # catalog 从 GitHub 拉取，可能因网络失败；只验证鉴权不验证内容数量
        status, _, _ = self.request("GET", "/api/catalog", headers={"X-Community-Session": session})
        self.assertIn(status, (200, 500))  # 200=成功, 500=GitHub 不可达

        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.server = StoreServer(("127.0.0.1", 0), False, None, "a" * 32)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        status, _, _ = self.request("GET", "/api/catalog", headers={"Cookie": cookie})
        self.assertIn(status, (200, 500))

    def test_xiaomi_relay_gets_session_without_client_cookie(self) -> None:
        status, _, payload = self.request("GET", "/index.html", headers={"X-Real-IP": "127.1.0.110"})
        self.assertEqual(200, status)
        self.assertNotIn(b"__SESSION_TOKEN__", payload)
        self.assertRegex(payload.decode("utf-8"), r'<meta name="session-token" content="[^\"]+"')

    def session_token(self) -> str:
        _, _, payload = self.request(
            "GET", "/index.html", headers={"X-Xiaomi-Client-Verify": "SUCCESS"}
        )
        html = payload.decode("utf-8")
        marker = '<meta name="session-token" content="'
        return html.split(marker, 1)[1].split('"', 1)[0]

    def test_static_assets_are_never_cached(self) -> None:
        """页面脚本不能长缓存：商店升级后客户端必须拿到新的 app.js。"""
        status, headers, _ = self.request("GET", "/app.js")
        self.assertEqual(200, status)
        self.assertEqual("no-store", headers["Cache-Control"])

    def test_catalog_reachable_via_static_extension_and_page_query(self) -> None:
        """Windows 本地代理可能把 /api/* SPA 回退成 HTML；catalog.json / ?api=catalog 要可用。"""
        session = self.session_token()

        def fake_catalog(*, cache_path=None, force_refresh=False):
            return {"apps": [], "store": {}}

        with mock.patch("server.load_apps_catalog", side_effect=fake_catalog):
            for path in (
                "/api/catalog",
                "/catalog.json",
                "/api/catalog.json",
                "/index.html?api=catalog",
            ):
                status, headers, body = self.request(
                    "GET", path, headers={"X-Community-Session": session}
                )
                self.assertEqual(200, status, path)
                self.assertIn("json", headers.get("Content-Type", ""), path)
                self.assertTrue(json.loads(body.decode("utf-8")).get("ok"), path)

    def test_catalog_refresh_bypasses_ttl_cache(self) -> None:
        """刷新按钮带 refresh=1，要跳过后端 TTL 缓存直接重拉远程。"""
        session = self.session_token()
        calls: list[bool] = []

        def fake_catalog(*, cache_path=None, force_refresh=False):
            calls.append(force_refresh)
            return {"apps": [], "store": {}}

        with mock.patch("server.load_apps_catalog", side_effect=fake_catalog):
            for path in ("/api/catalog", "/api/catalog?refresh=1"):
                status, _, _ = self.request("GET", path, headers={"X-Community-Session": session})
                self.assertEqual(200, status)
        self.assertEqual([False, True], calls)


if __name__ == "__main__":
    unittest.main()
