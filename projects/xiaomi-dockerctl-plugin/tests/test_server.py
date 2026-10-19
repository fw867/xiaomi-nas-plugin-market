"""HTTP 路由单测：鉴权、静态资源、参数校验、错误分支、配置读写、二进制分流。"""
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import quote

from engine import Engine
from server import Server

CERT = {"X-Xiaomi-Client-Verify": "SUCCESS", "X-Xiaomi-Client-DN": "CN=nas.123456.abc,O=test"}


class FakeDocker:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []
        self.last_input = None
        self.configured = "/usr/bin/docker"
        self.bin = "/usr/bin/docker"

    def _reply(self, args):
        key = " ".join(args)
        for prefix, reply in self.responses:
            if key.startswith(prefix):
                return reply
        return 0, "", ""

    def run(self, args, timeout=12):
        self.calls.append(list(args))
        return self._reply(args)

    def run_input(self, args, data, timeout=30):
        self.calls.append(list(args))
        self.last_input = data
        return self._reply(args)

    def stream(self, args, on_line, timeout=1800, env=None, binary=None, on_start=None):
        self.calls.append(list(args))
        code, out, error = self._reply(args)
        for line in out.splitlines():
            on_line(line)
        return code, error


class HTTPBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.server = None

    def serve(self, dev=True, responses=None):
        engine = Engine(self.root / "data", dev=dev, docker=FakeDocker(responses))
        self.server = Server(("127.0.0.1", 0), engine, "u123456", dev)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()

        def stop():
            self.server.shutdown()
            self.server.server_close()
            thread.join()

        self.addCleanup(stop)
        return self.server

    def request(self, method, route, body=None, headers=None, raw=None,
                content_type="application/json"):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        try:
            data = raw if raw is not None else (
                json.dumps(body) if body is not None else None)
            head = dict(headers or {})
            if data is not None and "Content-Type" not in head:
                head["Content-Type"] = content_type
            connection.request(method, route, body=data, headers=head)
            response = connection.getresponse()
            return response.status, response.read(), response.getheader("Content-Type")
        finally:
            connection.close()


class AuthTests(HTTPBase):
    def test_health_is_public(self):
        self.serve(dev=False)
        status, body, _ = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertTrue(data["ok"])
        self.assertEqual(data["service"], "dockerctl")

    def test_api_requires_mtls(self):
        self.serve(dev=False)
        status, body, _ = self.request("GET", "/api/overview")
        self.assertEqual(status, 401)
        self.assertFalse(json.loads(body)["ok"])

    def test_client_certificate_allows(self):
        self.serve(dev=False, responses=[("ps -a --format {{.State}}", (0, "running\n", ""))])
        status, body, _ = self.request("GET", "/api/overview", headers=CERT)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["containers_running"], 1)

    def test_certificate_for_other_user_denied(self):
        self.serve(dev=False)
        headers = dict(CERT)
        headers["X-Xiaomi-Client-DN"] = "CN=nas.999999.abc"
        self.assertEqual(self.request("GET", "/api/overview", headers=headers)[0], 401)

    def test_unverified_certificate_denied(self):
        self.serve(dev=False)
        headers = dict(CERT)
        headers["X-Xiaomi-Client-Verify"] = "NONE"
        self.assertEqual(self.request("GET", "/api/overview", headers=headers)[0], 401)

    def test_loopback_x_real_ip_allowed(self):
        self.serve(dev=False)
        self.assertEqual(
            self.request("GET", "/api/settings", headers={"X-Real-IP": "127.0.0.1"})[0], 200)

    def test_post_also_requires_mtls(self):
        self.serve(dev=False)
        status, _, _ = self.request("POST", "/api/logs/clear", {})
        self.assertEqual(status, 401)


class StaticTests(HTTPBase):
    def setUp(self):
        super().setUp()
        self.serve(dev=True)

    def test_index(self):
        status, body, content_type = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", content_type)
        self.assertIn("Docker 控制台".encode("utf-8"), body)

    def test_app_and_style(self):
        for route, marker in (("/app.js", b"function apiUrl"),
                              ("/style.css", b"--accent")):
            with self.subTest(route=route):
                status, body, content_type = self.request("GET", route)
                self.assertEqual(status, 200)
                self.assertIn(marker, body)

    def test_png_is_served_as_binary_untouched(self):
        status, body, content_type = self.request("GET", "/assets/dockerctl.png")
        self.assertEqual(status, 200)
        self.assertEqual(content_type, "image/png")
        self.assertTrue(body.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertNotIn(b"\r\r\n", body)

    def test_missing_static(self):
        self.assertEqual(self.request("GET", "/nope.js")[0], 404)


class RouteTests(HTTPBase):
    def test_containers_list(self):
        self.serve(responses=[
            ("ps -a --no-trunc --format {{json .}}",
             (0, '{"ID":"c1","Names":"web"}\n{"ID":"c2","Names":"db"}\n', "")),
        ])
        status, body, _ = self.request("GET", "/api/containers?all=1")
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(body)["items"]), 2)

    def test_container_detail(self):
        self.serve(responses=[("inspect c1", (0, '[{"Id":"c1"}]\n', ""))])
        status, body, _ = self.request("GET", "/api/containers/c1")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["detail"]["Id"], "c1")

    def test_invalid_container_id(self):
        self.serve()
        status, body, _ = self.request("GET", "/api/containers/..%2Fetc")
        self.assertEqual(status, 400)
        self.assertIn("invalid id", json.loads(body)["error"])

    def test_images_pull_get_is_405(self):
        self.serve()
        status, body, _ = self.request("GET", "/api/images/pull")
        self.assertEqual(status, 405)
        self.assertEqual(json.loads(body)["error"], "use POST")

    def test_pull_status_empty(self):
        self.serve()
        status, body, _ = self.request("GET", "/api/images/pull/status")
        self.assertEqual(status, 200)
        self.assertIsNone(json.loads(body)["task"])

    def test_unknown_route(self):
        self.serve()
        self.assertEqual(self.request("GET", "/api/unknown")[0], 404)

    def test_logs_route_serves_ops_log(self):
        self.serve()
        self.server.engine.ops.log("容器操作:start", "web", True)
        status, body, _ = self.request("GET", "/api/logs?tail=10")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["items"][0]["target"], "web")


class PostTests(HTTPBase):
    def test_post_requires_json_content_type(self):
        self.serve()
        status, body, _ = self.request(
            "POST", "/api/logs/clear", raw="a=1", content_type="text/plain")
        self.assertEqual(status, 415)
        self.assertFalse(json.loads(body)["ok"])

    def test_malformed_json(self):
        self.serve()
        status, _, _ = self.request(
            "POST", "/api/logs/clear", raw="{", content_type="application/json")
        self.assertEqual(status, 400)

    def test_post_without_body_is_empty_object(self):
        self.serve(responses=[("stop c1", (0, "c1\n", ""))])
        status, body, _ = self.request(
            "POST", "/api/containers/c1/stop", headers={"Content-Type": "application/json"})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_deploy_run(self):
        self.serve(responses=[("run -d", (0, "cid123\n", ""))])
        status, body, _ = self.request("POST", "/api/deploy/run", {
            "image": "nginx:latest", "name": "web",
            "ports": [{"host": 8080, "container": 80}]})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        self.assertIn("8080:80/tcp", self.server.engine.docker.calls[-1])

    def test_deploy_invalid_payload_is_400(self):
        self.serve()
        status, body, _ = self.request("POST", "/api/deploy/run", {"image": "-bad"})
        self.assertEqual(status, 400)
        self.assertIn("镜像名不合法", json.loads(body)["error"])

    def test_remove_image_by_id(self):
        # 无标签镜像（<none>:<none>）只能按 ID 删除，前端会把 data-id 编码进路径
        image_id = "sha256:" + "c" * 64
        self.serve(responses=[("rmi " + image_id, (0, "Untagged: " + image_id + "\n", ""))])
        status, body, _ = self.request(
            "POST", "/api/images/" + quote(image_id, safe="") + "/remove", {})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        self.assertEqual(self.server.engine.docker.calls[-1], ["rmi", image_id])

    def test_clear_logs(self):
        self.serve()
        self.server.engine.ops.log("操作", "x", True)
        status, body, _ = self.request("POST", "/api/logs/clear", {})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])
        self.assertEqual(self.server.engine.ops.tail(10), [])

    def test_options_has_no_cors(self):
        self.serve()
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port)
        try:
            connection.request("OPTIONS", "/api/overview")
            response = connection.getresponse()
            self.assertEqual(response.status, 204)
            self.assertIsNone(response.getheader("Access-Control-Allow-Origin"))
        finally:
            connection.close()


class SettingsTests(HTTPBase):
    def setUp(self):
        super().setUp()
        self.serve()

    def test_get_settings(self):
        status, body, _ = self.request("GET", "/api/settings")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertIn("docker_bin", data)
        self.assertIn("compose_dir", data)
        self.assertEqual(data["version"], self.server.engine.health()["version"])

    def test_save_settings(self):
        binary = self.root / "docker"
        binary.write_bytes(b"#!/bin/sh\n")
        compose = self.root / "compose"
        status, body, _ = self.request("POST", "/api/settings", {
            "docker_bin": str(binary), "compose_dir": str(compose)})
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertEqual(data["docker_bin"], str(binary))
        self.assertTrue(compose.is_dir())
        self.assertTrue((self.root / "data" / "config.json").is_file())
        # 服务端内存里的引擎也换了路径
        self.assertEqual(self.server.engine.docker.configured, str(binary))

    def test_save_settings_rejects_relative(self):
        status, body, _ = self.request("POST", "/api/settings", {
            "docker_bin": "relative/docker"})
        self.assertEqual(status, 400)
        self.assertIn("绝对路径", json.loads(body)["error"])

    def test_save_settings_rejects_missing_binary(self):
        status, body, _ = self.request("POST", "/api/settings", {
            "docker_bin": str(self.root / "missing")})
        self.assertEqual(status, 400)
        self.assertIn("不存在", json.loads(body)["error"])


class UiTests(unittest.TestCase):
    def setUp(self):
        self.web = Path(__file__).resolve().parents[1] / "web"

    def test_index_has_settings_tab_and_fields(self):
        html = (self.web / "index.html").read_text(encoding="utf-8")
        self.assertIn('data-view="settings"', html)
        self.assertIn('id="set-docker-bin"', html)
        self.assertIn('id="set-compose-dir"', html)
        self.assertIn('id="btn-save-settings"', html)

    def test_app_uses_relative_api_and_json_posts(self):
        script = (self.web / "app.js").read_text(encoding="utf-8")
        self.assertIn("function apiUrl", script)
        self.assertIn('"Content-Type":"application/json"', script)
        # 不把后端地址写死在前端
        self.assertNotIn("127.0.0.1", script)
        self.assertNotIn(":9302", script)
        self.assertIn("loadSettings", script)

    def test_app_deletes_untagged_image_by_id(self):
        script = (self.web / "app.js").read_text(encoding="utf-8")
        # 无标签镜像不再被前端拦下，而是自动改用镜像 ID 调用删除接口
        self.assertNotIn("无标签镜像请用 ID 删除", script)
        self.assertIn("var target=dangling?imgID:imgRef;", script)
        self.assertIn("removeImageRequest(target,false)", script)

    def test_index_references_existing_files(self):
        html = (self.web / "index.html").read_text(encoding="utf-8")
        for name in ("style.css", "app.js"):
            self.assertIn(name, html)
            self.assertTrue((self.web / name).is_file(), name)

    def test_icon_is_256x256_png(self):
        data = (self.web / "assets" / "dockerctl.png").read_bytes()
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        # IHDR：宽高各 4 字节大端；256×256 与市场其它插件图标一致
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
        self.assertEqual((width, height), (256, 256))


if __name__ == "__main__":
    unittest.main()
