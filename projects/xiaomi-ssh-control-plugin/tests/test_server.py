from __future__ import annotations

import importlib
import json
import os
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest import mock


class SshControlTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.state = root / "state.json"
        self.web = root / "web"
        self.web.mkdir()
        (self.web / "index.html").write_text("<html>ssh</html>", encoding="utf-8")

        os.environ["STATE_FILE"] = str(self.state)
        os.environ["WEB_DIR"] = str(self.web)
        os.environ["PORT"] = "0"
        import server  # noqa: PLC0415
        importlib.reload(server)
        self.server_module = server

        self.calls: list[list[str]] = []

        def fake_run(command, **kwargs):
            self.calls.append(list(command))
            if command[:2] == ["systemctl", "is-active"]:
                return mock.Mock(returncode=1, stdout="", stderr="")
            if command[:2] == ["crontab", "-l"]:
                return mock.Mock(returncode=1, stdout="", stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")

        self.patcher = mock.patch.object(server, "_run", side_effect=fake_run)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def tearDown(self) -> None:
        for key in ("STATE_FILE", "WEB_DIR", "PORT"):
            os.environ.pop(key, None)
        self.temp.cleanup()

    # ---- 状态读写 ----

    def test_state_roundtrip(self) -> None:
        module = self.server_module
        self.assertFalse(module.autostart_enabled())
        module.write_state({"autostart": True})
        self.assertTrue(module.autostart_enabled())
        self.assertEqual({"autostart": True}, json.loads(self.state.read_text(encoding="utf-8")))

    def test_keepalive_script_matches_state_file(self) -> None:
        """keepalive.sh 用 grep 判断开关，格式必须与 write_state 一致。"""
        module = self.server_module
        module.write_state({"autostart": True})
        content = self.state.read_text(encoding="utf-8")
        self.assertRegex(content, r'"autostart"\s*:\s*true')

        module.write_state({"autostart": False})
        content = self.state.read_text(encoding="utf-8")
        self.assertNotRegex(content, r'"autostart"\s*:\s*true')

    # ---- 启停 ----

    def test_start_action_calls_systemctl(self) -> None:
        module = self.server_module
        self.calls.clear()
        ok, error = module.start_ssh()
        self.assertTrue(ok, error)
        self.assertIn(["systemctl", "start", "dropbear.socket"], self.calls)

    def test_stop_action_calls_systemctl(self) -> None:
        module = self.server_module
        self.calls.clear()
        ok, error = module.stop_ssh()
        self.assertTrue(ok, error)
        self.assertIn(["systemctl", "stop", "dropbear.socket"], self.calls)

    # ---- 自启开关 ----

    def test_set_autostart_writes_state_and_cron(self) -> None:
        module = self.server_module
        written: list[str] = []

        def capture_crontab(command, **kwargs):
            if command == ["crontab", "-"]:
                written.append(kwargs.get("input", ""))
                return mock.Mock(returncode=0, stdout="", stderr="")
            if command[:2] == ["crontab", "-l"]:
                return mock.Mock(returncode=1, stdout="", stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(module, "_run", side_effect=capture_crontab), \
                mock.patch("subprocess.run", side_effect=capture_crontab):
            ok, error = module.set_autostart(True)
        self.assertTrue(ok, error)
        self.assertTrue(module.autostart_enabled())
        self.assertTrue(written and "#@sshcontrol" in written[-1])

    def test_set_autostart_false_removes_cron(self) -> None:
        module = self.server_module
        existing = "* * * * * /x/keepalive.sh #@sshcontrol"
        written: list[str] = []

        def capture_crontab(command, **kwargs):
            if command == ["crontab", "-"]:
                written.append(kwargs.get("input", ""))
                return mock.Mock(returncode=0, stdout="", stderr="")
            if command[:2] == ["crontab", "-l"]:
                return mock.Mock(returncode=0, stdout=existing + "\n", stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(module, "_run", side_effect=capture_crontab), \
                mock.patch("subprocess.run", side_effect=capture_crontab):
            ok, error = module.set_autostart(False)
        self.assertTrue(ok, error)
        self.assertFalse(module.autostart_enabled())
        self.assertTrue(written)
        self.assertNotIn("#@sshcontrol", written[-1])

    # ---- HTTP ----

    def _serve(self):
        module = self.server_module
        server = module.ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def _request(self, port: int, method: str, path: str, body: dict | None = None):
        connection = HTTPConnection("127.0.0.1", port, timeout=5)
        payload = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if payload else {}
        connection.request(method, path, body=payload, headers=headers)
        response = connection.getresponse()
        data = response.read()
        connection.close()
        return response.status, data

    def test_status_endpoint(self) -> None:
        port = self._serve()
        status, data = self._request(port, "GET", "/api/status")
        self.assertEqual(200, status)
        payload = json.loads(data)
        self.assertTrue(payload["ok"])
        self.assertIn("sshRunning", payload)
        self.assertIn("autostart", payload)

    def test_index_served(self) -> None:
        port = self._serve()
        status, data = self._request(port, "GET", "/index.html")
        self.assertEqual(200, status)
        self.assertIn(b"ssh", data)

    def test_action_rejects_unknown(self) -> None:
        port = self._serve()
        status, data = self._request(port, "POST", "/api/action", {"action": "nope"})
        self.assertEqual(400, status)
        self.assertFalse(json.loads(data)["ok"])


if __name__ == "__main__":
    unittest.main()
