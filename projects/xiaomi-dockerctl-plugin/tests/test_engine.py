"""引擎层单测：参数拼装、配置读写、校验、错误分支、日志/拉取时间持久化。"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from engine import (
    DEFAULT_DOCKER_BIN,
    Engine,
    Error,
    OpsLog,
    PullTimes,
    build_run_args,
    load_config,
    parse_json_list,
    proxy_error,
    resolve_docker_bin,
    safe_addr,
    safe_id,
    safe_name,
    safe_registry,
    save_config,
)


class FakeDocker:
    """按 argv 前缀返回预设结果，并记录每次调用。"""

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


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / "data"

    def test_defaults_when_missing(self):
        config = load_config(self.data)
        self.assertEqual(config["docker_bin"], DEFAULT_DOCKER_BIN)
        self.assertEqual(config["compose_dir"], str(self.data / "compose"))

    def test_roundtrip(self):
        save_config(self.data, {"docker_bin": "/opt/docker", "compose_dir": "/srv/compose"})
        self.assertTrue((self.data / "config.json").is_file())
        loaded = load_config(self.data)
        self.assertEqual(loaded["docker_bin"], "/opt/docker")
        self.assertEqual(loaded["compose_dir"], "/srv/compose")
        # 0600 + LF：配置里有本机路径，不允许组/其他用户读，也不写 CRLF
        mode = (self.data / "config.json").stat().st_mode & 0o777
        if os.name == "posix":
            self.assertEqual(mode, 0o600)
        raw = (self.data / "config.json").read_bytes()
        self.assertNotIn(b"\r\n", raw)

    def test_corrupt_file_falls_back(self):
        self.data.mkdir(parents=True)
        (self.data / "config.json").write_text("{not json", encoding="utf-8")
        config = load_config(self.data)
        self.assertEqual(config["docker_bin"], DEFAULT_DOCKER_BIN)

    def test_wrong_types_ignored(self):
        self.data.mkdir(parents=True)
        (self.data / "config.json").write_text(
            json.dumps({"docker_bin": 42, "compose_dir": None}), encoding="utf-8")
        config = load_config(self.data)
        self.assertEqual(config["docker_bin"], DEFAULT_DOCKER_BIN)
        self.assertEqual(config["compose_dir"], str(self.data / "compose"))

    def test_relative_paths_in_file_ignored(self):
        self.data.mkdir(parents=True)
        (self.data / "config.json").write_text(
            json.dumps({"docker_bin": "docker", "compose_dir": "relative"}), encoding="utf-8")
        config = load_config(self.data)
        self.assertEqual(config["docker_bin"], DEFAULT_DOCKER_BIN)
        self.assertEqual(config["compose_dir"], str(self.data / "compose"))


class ValidationTests(unittest.TestCase):
    def test_build_run_args_minimal(self):
        args, error = build_run_args({"image": "nginx:latest"})
        self.assertIsNone(error)
        self.assertEqual(args, ["run", "-d", "nginx:latest"])

    def test_build_run_args_full(self):
        args, error = build_run_args({
            "image": "nginx:latest",
            "name": "web",
            "ports": [{"host": 8080, "container": 80, "protocol": "tcp"},
                      {"host": "5353", "container": "53", "protocol": "udp"}],
            "volumes": [{"host": "/srv/www", "container": "/usr/share/nginx/html",
                         "readonly": True}],
            "env": [{"key": "TZ", "value": "Asia/Shanghai"}],
            "restart": "unless-stopped",
            "network": "webnet",
            "extra": "--privileged",
            "command": "nginx -g 'daemon off;'",
        })
        self.assertIsNone(error)
        self.assertEqual(args[:4], ["run", "-d", "--name", "web"])
        self.assertIn("8080:80/tcp", args)
        self.assertIn("5353:53/udp", args)
        self.assertIn("/srv/www:/usr/share/nginx/html:ro", args)
        self.assertIn("TZ=Asia/Shanghai", args)
        self.assertIn("--restart", args)
        self.assertIn("webnet", args)
        self.assertIn("--privileged", args)
        self.assertEqual(args[-3:], ["nginx", "-g", "daemon off;"])

    def test_build_run_args_rejects(self):
        cases = [
            ({"image": ""}, "镜像名不能为空"),
            ({"image": "-bad"}, "镜像名不合法"),
            ({"image": "nginx", "name": "-bad"}, "容器名不合法"),
            ({"image": "nginx", "ports": [{"host": 0, "container": 80}]}, "端口须为"),
            ({"image": "nginx", "ports": [{"host": 70000, "container": 80}]}, "端口须为"),
        ]
        for body, expected in cases:
            with self.subTest(body=body):
                args, error = build_run_args(body)
                self.assertIsNone(args)
                self.assertIn(expected, error)

    def test_restart_policy_allowlist(self):
        args, _ = build_run_args({"image": "nginx", "restart": "always; rm -rf /"})
        self.assertNotIn("--restart", args)

    def test_extra_flags_ignore_bad_quoting(self):
        args, error = build_run_args({"image": "nginx", "extra": "--hostname 'unterminated"})
        self.assertIsNone(error)
        self.assertEqual(args, ["run", "-d", "nginx"])

    def test_safe_helpers(self):
        self.assertTrue(safe_name("web-1"))
        self.assertFalse(safe_name("-web"))
        self.assertFalse(safe_name("a/b"))
        self.assertTrue(safe_id("registry.local:5000/app:v1"))
        self.assertFalse(safe_id("app name"))
        self.assertTrue(safe_addr("192.168.1.0/24"))
        self.assertFalse(safe_addr("rm -rf"))
        self.assertTrue(safe_registry("harbor.local:5000"))
        self.assertFalse(safe_registry("http://harbor"))
        self.assertFalse(safe_registry("harbor/path"))

    def test_proxy(self):
        self.assertEqual(proxy_error("http://192.168.1.2:7890"), "")
        for bad in ("", "socks5://1.2.3.4:1080", "http://user:pass@1.2.3.4:80",
                    "http://1.2.3.4:7890/path", "not a url", "http://1.2.3.4"):
            with self.subTest(bad=bad):
                self.assertTrue(proxy_error(bad))

    def test_parse_json_list_skips_garbage(self):
        items = parse_json_list('{"a":1}\n\nnot-json\n{"b":2}\n')
        self.assertEqual(items, [{"a": 1}, {"b": 2}])

    def test_resolve_falls_back_to_path(self):
        with patch("engine.os.path.isfile", return_value=False), \
                patch("engine.shutil.which", return_value="/usr/bin/docker"):
            self.assertEqual(resolve_docker_bin("/nope/docker"), "/usr/bin/docker")

    def test_resolve_prefers_configured(self):
        with patch("engine.os.path.isfile", return_value=True), \
                patch("engine.os.access", return_value=True):
            self.assertEqual(resolve_docker_bin("/opt/docker"), "/opt/docker")


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name)

    def test_ops_log_append_reload_clear(self):
        log = OpsLog(self.data / "ops.log")
        log.log("容器操作:start", "web", True)
        log.log("删除镜像", "nginx:latest", False, "in use")
        reloaded = OpsLog(self.data / "ops.log")
        reloaded.load()
        self.assertEqual([entry["kind"] for entry in reloaded.items],
                         ["容器操作:start", "删除镜像"])
        self.assertFalse(reloaded.items[1]["ok"])
        self.assertEqual(reloaded.tail(1)[0]["detail"], "in use")
        reloaded.clear()
        self.assertEqual(reloaded.items, [])
        self.assertEqual((self.data / "ops.log").read_text(encoding="utf-8"), "")

    def test_ops_log_ring_buffer(self):
        log = OpsLog(self.data / "ops.log")
        for index in range(210):
            log.log("操作", str(index), True)
        self.assertEqual(len(log.items), 200)
        self.assertEqual(log.items[0]["target"], "10")

    def test_pull_times_record_attach_forget(self):
        docker = FakeDocker([("image inspect --format {{.Id}} nginx:latest",
                              (0, "sha256:abcdef\n", ""))])
        times = PullTimes(self.data / "pull-times.json")
        times.record(docker, "nginx:latest", "", 1700000000)
        items = times.attach([
            {"Repository": "nginx", "Tag": "latest", "ID": "sha256:abcdef"},
            {"Repository": "nginx", "Tag": "latest", "ID": "sha256:other"},
        ])
        self.assertEqual(items[0]["PulledAt"], 1700000000)
        self.assertEqual(items[1]["PulledAt"], 0)
        # 重新从磁盘读
        again = PullTimes(self.data / "pull-times.json")
        again.load(docker)
        self.assertTrue(again.attach([{"Repository": "nginx", "Tag": "latest",
                                       "ID": "sha256:abcdef"}])[0]["PulledAt"])
        again.forget("nginx:latest")
        self.assertEqual(again.attach([{"Repository": "nginx", "Tag": "latest",
                                        "ID": "sha256:abcdef"}])[0]["PulledAt"], 0)

    def test_pull_times_recover_from_ops_log(self):
        docker = FakeDocker([("image inspect", (0, "sha256:feed\n", ""))])
        times = PullTimes(self.data / "pull-times.json")
        times.load(docker, [{"kind": "拉取镜像", "target": "nginx", "ok": True, "ts": 123}])
        self.assertIn("nginx:latest", times.items)
        self.assertEqual(times.items["nginx:latest"]["at"], 123)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / "data"

    def engine(self, responses=None):
        return Engine(self.data, docker=FakeDocker(responses))

    def test_health_and_settings(self):
        engine = self.engine()
        self.assertTrue(engine.health()["ok"])
        settings = engine.settings()
        self.assertEqual(settings["compose_dir"], str(self.data / "compose"))
        self.assertIn("version", settings)

    def test_settings_roundtrip(self):
        binary = Path(self.tmp.name) / "docker"
        binary.write_bytes(b"#!/bin/sh\n")
        engine = self.engine()
        result = engine.save_settings({
            "docker_bin": str(binary),
            "compose_dir": str(Path(self.tmp.name) / "compose"),
        })
        self.assertEqual(result["docker_bin"], str(binary))
        self.assertTrue(Path(self.tmp.name, "compose").is_dir())
        loaded = load_config(self.data)
        self.assertEqual(loaded["docker_bin"], str(binary))
        # 新实例应继承保存的配置
        self.assertEqual(Engine(self.data, docker=FakeDocker()).config["docker_bin"], str(binary))

    def test_settings_rejects(self):
        engine = self.engine()
        bad_bodies = [
            {"docker_bin": "relative/docker"},
            {"docker_bin": "/does/not/exist"},
            {"docker_bin": "/tmp/x\nrm -rf"},
            {"compose_dir": "relative"},
            {"compose_dir": "/tmp/x\x00y"},
        ]
        for body in bad_bodies:
            with self.subTest(body=body), self.assertRaises(Error):
                engine.save_settings(body)

    def test_overview_counts_states(self):
        engine = self.engine([
            ("ps -a --format {{.State}}", (0, "running\nexited\nrunning\npaused\nweird\n", "")),
            ("images -q", (0, "a\nb\n", "")),
            ("volume ls -q", (0, "v1\n", "")),
            ("network ls -q", (0, "n1\nn2\n", "")),
        ])
        overview = engine.overview()
        self.assertEqual(overview["containers_total"], 5)
        self.assertEqual(overview["containers_running"], 2)
        self.assertEqual(overview["states"]["other"], 1)
        self.assertEqual(overview["images"], 2)
        self.assertEqual(overview["volumes"], 1)
        self.assertEqual(overview["networks"], 2)

    def test_containers_and_images(self):
        engine = self.engine([
            ("ps --no-trunc --format {{json .}}",
             (0, '{"ID":"c1","Names":"web"}\n{"ID":"c2","Names":"db"}\n', "")),
            ("images --no-trunc --format {{json .}}",
             (0, '{"Repository":"nginx","Tag":"latest","ID":"sha256:1"}\n', "")),
        ])
        self.assertEqual(len(engine.containers(False)["items"]), 2)
        images = engine.images()
        self.assertEqual(images["items"][0]["Repository"], "nginx")
        self.assertIn("PulledAt", images["items"][0])

    def test_container_action_logs_and_reports(self):
        engine = self.engine([
            ("stop c1", (0, "c1\n", "")),
            ("rm -f c1", (0, "c1\n", "")),
        ])
        result = engine.container_action("c1", "stop", {})
        self.assertTrue(result["ok"])
        self.assertEqual(engine.ops.tail(1)[0]["kind"], "容器操作:stop")
        result = engine.container_action("c1", "remove", {"force": True})
        self.assertEqual(result["ok"], True)

    def test_container_action_invalid_id(self):
        engine = self.engine()
        for bad in ("../etc", "-rf", "a b"):
            with self.subTest(bad=bad), self.assertRaises(Error):
                engine.container_action(bad, "stop", {})

    def test_deploy_run_builds_command(self):
        engine = self.engine([("run -d", (0, "cid123\n", ""))])
        result = engine.deploy_run({
            "image": "nginx:latest", "name": "web",
            "ports": [{"host": 8080, "container": 80}],
        })
        self.assertTrue(result["ok"])
        self.assertEqual(result["container_id"], "cid123")
        called = engine.docker.calls[-1]
        self.assertEqual(called[0], "run")
        self.assertIn("8080:80/tcp", called)
        self.assertEqual(engine.ops.tail(1)[0]["kind"], "部署容器")

    def test_deploy_run_error_not_raises(self):
        engine = self.engine([("run -d", (125, "", "port is already allocated"))])
        result = engine.deploy_run({"image": "nginx"})
        self.assertFalse(result["ok"])
        self.assertIn("port is already allocated", result["error"])

    def test_volume_and_network_validation(self):
        engine = self.engine()
        with self.assertRaises(Error):
            engine.create_volume({"name": "-bad"})
        with self.assertRaises(Error):
            engine.create_network({"name": "ok", "subnet": "rm -rf"})
        with self.assertRaises(Error):
            engine.remove_network("../x")

    def test_compose_deploy_writes_yaml_and_uses_project(self):
        engine = self.engine([("compose -p", (0, "started\n", ""))])
        result = engine.deploy_compose({"name": "stack", "yaml": "services:\n  web:\n    image: nginx\n"})
        self.assertTrue(result["ok"])
        yaml_path = Path(result["path"])
        self.assertEqual(yaml_path, self.data / "compose" / "stack" / "docker-compose.yml")
        self.assertIn("nginx", yaml_path.read_text(encoding="utf-8"))
        # 文本文件统一 LF，不写 CRLF
        self.assertNotIn(b"\r\n", yaml_path.read_bytes())

    def test_compose_rejects(self):
        engine = self.engine()
        with self.assertRaises(Error):
            engine.deploy_compose({"name": "../x", "yaml": "a: 1"})
        with self.assertRaises(Error):
            engine.deploy_compose({"name": "ok", "yaml": "  "})
        with self.assertRaises(Error) as caught:
            engine.compose_action("missing", "down")
        self.assertEqual(caught.exception.status, 404)

    def test_pull_validation(self):
        engine = self.engine()
        with self.assertRaises(Error):
            engine.start_pull({"image": "bad image"})
        with self.assertRaises(Error):
            engine.start_pull({"image": "nginx", "proxy": "socks5://1.2.3.4:1080"})
        with patch.dict(os.environ, {"SKOPEO_BIN": "definitely-not-installed"}):
            with self.assertRaises(Error):
                engine.start_pull({"image": "nginx", "proxy": "http://1.2.3.4:7890"})

    def test_pull_status_without_task(self):
        self.assertIsNone(self.engine().pull_status()["task"])

    def test_stream_pull_tracks_frames_and_process(self):
        class StreamDocker(FakeDocker):
            def stream(self, args, on_line, timeout=1800, env=None, binary=None, on_start=None):
                class Process:
                    def kill(self):
                        pass

                on_start(Process())
                on_line("Pulling from library/nginx")
                on_line("Status: Downloaded newer image for nginx:latest")
                return 0, ""

        engine = Engine(self.data, docker=StreamDocker())
        engine.pull.task = {"id": "t1", "image": "nginx", "status": "running",
                            "frames": [], "proc": None}
        code, output, error = engine._stream_pull(["pull", "nginx"], "t1")
        self.assertEqual(code, 0)
        self.assertEqual(error, "")
        self.assertIn("Pulling", output)
        self.assertEqual(len(engine.pull.task["frames"]), 2)
        # 执行结束后不再持有进程句柄
        self.assertIsNone(engine.pull.task["proc"])

    def test_cancel_pull_kills_process(self):
        engine = self.engine()
        killed = []

        class Process:
            def kill(self):
                killed.append(True)

        engine.pull.task = {"id": "t1", "image": "nginx", "status": "running",
                            "frames": [], "proc": Process()}
        result = engine.cancel_pull()
        self.assertTrue(result["ok"])
        self.assertEqual(killed, [True])
        self.assertEqual(engine.pull.task["status"], "error")
        self.assertEqual(engine.ops.tail(1)[0]["kind"], "取消拉取")

    def test_registry_validation(self):
        engine = self.engine()
        with self.assertRaises(Error):
            engine.registry_login({"server": "http://x", "username": "u", "password": "p"})
        with self.assertRaises(Error):
            engine.registry_login({"server": "harbor.local", "username": "u", "password": ""})
        with self.assertRaises(Error):
            engine.registry_logout({"server": "bad server"})

    def test_registry_login_passes_password_on_stdin(self):
        engine = self.engine([("login -u", (0, "Login Succeeded\n", ""))])
        result = engine.registry_login({
            "server": "harbor.local:5000", "username": "u", "password": "secret"})
        self.assertTrue(result["ok"])
        self.assertEqual(engine.docker.last_input, "secret\n")
        self.assertNotIn("secret", " ".join(engine.docker.calls[-1]))


if __name__ == "__main__":
    unittest.main()
