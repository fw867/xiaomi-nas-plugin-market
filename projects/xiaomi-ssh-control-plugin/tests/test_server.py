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
        self.runtime = Path(__file__).resolve().parents[1]
        self.hotplug_dir = root / "syshotplug" / "pool"
        self.hotplug_upper = root / "upper" / "syshotplug" / "pool"

        os.environ["STATE_FILE"] = str(self.state)
        os.environ["WEB_DIR"] = str(self.web)
        os.environ["HOTPLUG_DIR"] = str(self.hotplug_dir)
        os.environ["HOTPLUG_UPPER_DIR"] = str(self.hotplug_upper)
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
        for key in ("STATE_FILE", "WEB_DIR", "PORT", "HOTPLUG_DIR", "HOTPLUG_UPPER_DIR"):
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


    # ---- 存储池挂载钩子（syshotplug） ----

    def test_hotplug_install_writes_executable_hook(self) -> None:
        """syshotplug 只执行带可执行位的文件，所以安装时必须 chmod 755。"""
        module = self.server_module
        self.assertFalse(module.hotplug_installed())
        ok, error = module.install_hotplug()
        self.assertTrue(ok, error)
        target = module.hotplug_path()
        self.assertTrue(target.is_file())
        self.assertTrue(os.access(target, os.X_OK), "钩子必须可执行")
        self.assertEqual(target.read_bytes(), module.HOTPLUG_SCRIPT.read_bytes())
        self.assertEqual(target.name, "98.ssh-control")
        self.assertTrue(module.hotplug_installed())
        self.assertTrue(module.hotplug_up_to_date())

    def test_hotplug_remove(self) -> None:
        module = self.server_module
        module.install_hotplug()
        ok, error = module.remove_hotplug()
        self.assertTrue(ok, error)
        self.assertFalse(module.hotplug_installed())
        # 幂等：再删一次也不报错
        ok, error = module.remove_hotplug()
        self.assertTrue(ok, error)

    def test_hotplug_script_gates_on_mounted_and_autostart(self) -> None:
        """钩子必须只认 ACTION=mounted、尊重开关，并且是后台延迟复查（不能立刻 start）。"""
        script = (self.runtime / "hotplug.sh").read_text(encoding="utf-8")
        self.assertIn('[ "${ACTION:-}" = "mounted" ] || exit 0', script)
        self.assertIn('/data/plugin/ssh-control/state.json', script)
        self.assertIn('"autostart"', script)
        self.assertIn("sleep 20", script)
        self.assertIn(") &", script)          # fork 到后台，别拖慢 syshotplug 其它钩子
        self.assertIn("systemctl start", script)

    def test_method_cron_installs_cron_only(self) -> None:
        module = self.server_module
        written: list[str] = []

        def capture(command, **kwargs):
            if command == ["crontab", "-"]:
                written.append(kwargs.get("input", ""))
                return mock.Mock(returncode=0, stdout="", stderr="")
            if command[:2] == ["crontab", "-l"]:
                return mock.Mock(returncode=1, stdout="", stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(module, "_run", side_effect=capture), \
                mock.patch("subprocess.run", side_effect=capture):
            ok, error = module.set_method("cron")
            self.assertTrue(ok, error)
            ok, error = module.set_autostart(True)
        self.assertTrue(ok, error)
        self.assertTrue(written and "#@sshcontrol" in written[-1])
        self.assertFalse(module.hotplug_installed())

    def test_method_hotplug_installs_hook_without_cron(self) -> None:
        module = self.server_module
        written: list[str] = []

        def capture(command, **kwargs):
            if command == ["crontab", "-"]:
                written.append(kwargs.get("input", ""))
                return mock.Mock(returncode=0, stdout="", stderr="")
            if command[:2] == ["crontab", "-l"]:
                return mock.Mock(returncode=0, stdout="* * * * * /x #@sshcontrol\n", stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(module, "_run", side_effect=capture), \
                mock.patch("subprocess.run", side_effect=capture):
            ok, error = module.set_method("hotplug")
            self.assertTrue(ok, error)
            ok, error = module.set_autostart(True)
        self.assertTrue(ok, error)
        self.assertTrue(module.hotplug_installed())
        self.assertTrue(written and "#@sshcontrol" not in written[-1])   # cron 条目被移除

    def test_method_both_installs_both(self) -> None:
        module = self.server_module
        written: list[str] = []

        def capture(command, **kwargs):
            if command == ["crontab", "-"]:
                written.append(kwargs.get("input", ""))
                return mock.Mock(returncode=0, stdout="", stderr="")
            if command[:2] == ["crontab", "-l"]:
                return mock.Mock(returncode=1, stdout="", stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(module, "_run", side_effect=capture), \
                mock.patch("subprocess.run", side_effect=capture):
            ok, error = module.set_method("both")
            self.assertTrue(ok, error)
            ok, error = module.set_autostart(True)
        self.assertTrue(ok, error)
        self.assertTrue(module.hotplug_installed())
        self.assertTrue(written and "#@sshcontrol" in written[-1])

    def test_autostart_off_removes_hook_and_cron(self) -> None:
        module = self.server_module
        written: list[str] = []

        def capture(command, **kwargs):
            if command == ["crontab", "-"]:
                written.append(kwargs.get("input", ""))
                return mock.Mock(returncode=0, stdout="", stderr="")
            if command[:2] == ["crontab", "-l"]:
                return mock.Mock(returncode=0, stdout="* * * * * /x #@sshcontrol\n", stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")

        with mock.patch.object(module, "_run", side_effect=capture), \
                mock.patch("subprocess.run", side_effect=capture):
            module.set_method("both")
            module.set_autostart(True)
            self.assertTrue(module.hotplug_installed())
            ok, error = module.set_autostart(False)
        self.assertTrue(ok, error)
        self.assertFalse(module.hotplug_installed())
        self.assertTrue(written and "#@sshcontrol" not in written[-1])

    def test_method_endpoint_switches_and_reports(self) -> None:
        module = self.server_module
        port = self._serve()
        status, data = self._request(port, "POST", "/api/method", {"method": "hotplug"})
        self.assertEqual(200, status)
        payload = json.loads(data)
        self.assertEqual("hotplug", payload["method"])
        self.assertIn("hotplug", payload)
        status, data = self._request(port, "POST", "/api/method", {"method": "nope"})
        self.assertEqual(400, status)

    def test_status_reports_hotplug_state(self) -> None:
        module = self.server_module
        module.install_hotplug()
        port = self._serve()
        status, data = self._request(port, "GET", "/api/status")
        self.assertEqual(200, status)
        payload = json.loads(data)
        self.assertIn("method", payload)
        self.assertTrue(payload["hotplug"]["installed"])
        self.assertTrue(payload["hotplug"]["upToDate"])
        self.assertEqual("98.ssh-control", payload["hotplug"]["name"])

    def test_index_has_method_selector(self) -> None:
        html = (self.runtime / "web" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="methodSelect"', html)
        for value in ("cron", "hotplug", "both"):
            with self.subTest(value=value):
                self.assertIn('value="%s"' % value, html)


class PackagingTests(unittest.TestCase):
    """打包清单是显式列文件的：插件目录里新增的脚本必须同步进去。

    回归用例：hotplug.sh（存储池挂载钩子）加进来时忘了写进 scripts/build_apps.py 的
    runtime，商店包就没有这个文件，界面上选「存储池挂载时」会直接报"读取钩子脚本失败"
    （手工拷文件部署时看不出来）。
    """

    def setUp(self) -> None:
        self.plugin = Path(__file__).resolve().parents[1]
        self.build = self.plugin.parents[1] / "scripts" / "build_apps.py"

    def _spec(self) -> dict:
        import ast

        tree = ast.parse(self.build.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "PACKAGE_SPECS":
                for spec in ast.literal_eval(node.value):
                    if spec.get("project") == self.plugin.name:
                        return spec
        self.fail("build_apps.py 里找不到本插件的 PACKAGE_SPECS 条目")

    def test_every_script_is_packaged(self) -> None:
        runtime = self._spec()["runtime"]
        packaged = set(runtime.keys()) | set(runtime.values())
        for pattern in ("*.py", "*.sh"):
            for path in sorted(self.plugin.glob(pattern)):
                with self.subTest(script=path.name):
                    self.assertIn(path.name, packaged)


if __name__ == "__main__":
    unittest.main()
