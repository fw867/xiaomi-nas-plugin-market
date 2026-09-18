"""Transmission 插件的单元测试。

全部用 unittest.mock 打桩，不启动真正的 transmission-daemon，
也不要求设备上存在 /data 或 Entware。
"""

from __future__ import annotations

import importlib
import io
import json
import os
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from unittest import mock

ENVIRONMENT = {
    "HOST": "127.0.0.1",
    "PORT": "0",
    "RPC_PORT": "9091",
    "LOCAL_ROOT": "",
    "SETTINGS_KEY_STYLE": "auto",
}

# 8 类必需设置项 → transmission 4.x settings.json 的准确键名。
# 依据：transmission 4.0.6 的 docs/Editing-Configuration-Files.md。
REQUIRED_KEYS = {
    "下载目录": "download-dir",
    "同时上传数": "upload-slots-per-torrent",
    "同时下载数": "download-queue-size",
    "全局连接数": "peer-limit-global",
    "单种连接数": "peer-limit-per-torrent",
    "上传限速": "speed-limit-up",
    "下载限速": "speed-limit-down",
    "时段限速": "alt-speed-enabled",
}

# 1.4x 的旧字段名，不能出现在 4.x 的设置项里。
LEGACY_KEYS = {"max-peers-global", "max-peers-per-torrent", "max-peers"}


class TransmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)

        self.web = root / "web"
        self.web.mkdir()
        (self.web / "index.html").write_text("<html>transmission</html>", encoding="utf-8")
        self.twc = self.web / "twc"
        self.twc.mkdir()
        (self.twc / "index.html").write_text("<html>web-control</html>", encoding="utf-8")

        self.runtime = root / "runtime"
        self.bin = self.runtime / "bin"
        self.lib = self.runtime / "lib"
        self.bin.mkdir(parents=True)
        self.lib.mkdir()
        self.daemon = self.bin / "transmission-daemon"
        self.daemon.write_bytes(b"#!/bin/sh\necho transmission-daemon 4.0.6\n")
        # 打包脚本把归档里所有文件写成 0644，解压后二进制没有执行位。
        self.daemon.chmod(0o644)

        self.data = root / "data"
        self.downloads = root / "downloads"

        self.saved = {key: os.environ.get(key) for key in ENVIRONMENT}
        os.environ.update(ENVIRONMENT)
        os.environ["WEB_DIR"] = str(self.web)
        os.environ["RUNTIME_DIR"] = str(self.runtime)
        os.environ["DATA_DIR"] = str(self.data)

        import server  # noqa: PLC0415 - 必须在设置好环境变量之后再导入

        importlib.reload(server)
        self.module = server

    def tearDown(self) -> None:
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        for key in ("WEB_DIR", "RUNTIME_DIR", "DATA_DIR"):
            os.environ.pop(key, None)
        self.temp.cleanup()

    def reload_with(self, **environment: str):
        """按需改环境变量并重新导入模块（用于测试默认值/风格回退）。"""
        for key, value in environment.items():
            os.environ[key] = value
        import server  # noqa: PLC0415

        importlib.reload(server)
        self.module = server
        return server

    def write_raw_settings(self, payload: dict) -> None:
        self.data.mkdir(parents=True, exist_ok=True)
        self.module.SETTINGS_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=4) + "\n", encoding="utf-8"
        )

    # ------------------------------------------------------------------
    # 路径与默认值
    # ------------------------------------------------------------------

    def test_runtime_paths_are_derived_from_runtime_dir(self) -> None:
        module = self.module
        self.assertEqual(self.daemon, module.DAEMON)
        self.assertEqual(self.lib, module.LIB_DIR)
        self.assertEqual(self.data / "settings.json", module.SETTINGS_FILE)

    def test_default_download_dir_falls_back_to_data_dir(self) -> None:
        module = self.module
        self.assertEqual(str(self.data / "downloads"), module.DEFAULT_DOWNLOAD_DIR)

    def test_default_download_dir_prefers_local_root(self) -> None:
        module = self.module
        self.assertEqual(
            "/nas/pool0/u123456/data/TransmissionDownloads",
            module.default_download_dir("/nas/pool0/u123456/data/", self.data),
        )
        self.assertEqual(
            str(self.data / "downloads"), module.default_download_dir("", self.data)
        )

    def test_ensure_runtime_executables_sets_exec_bit(self) -> None:
        if os.name != "posix":
            self.skipTest("执行位只在 POSIX 上可验证")
        module = self.module
        loader = self.lib / "ld-linux-aarch64.so.1"
        loader.write_bytes(b"\x7fELF")
        loader.chmod(0o644)
        self.assertFalse(os.access(self.daemon, os.X_OK))
        self.assertFalse(os.access(loader, os.X_OK))
        module.ensure_runtime_executables()
        self.assertTrue(os.access(self.daemon, os.X_OK))
        # 随包加载器也必须可执行，否则所有二进制都起不来。
        self.assertTrue(os.access(loader, os.X_OK))

    # ------------------------------------------------------------------
    # 字段映射
    # ------------------------------------------------------------------

    def test_required_settings_map_to_transmission_4_keys(self) -> None:
        module = self.module
        for label, key in REQUIRED_KEYS.items():
            self.assertIn(key, module.FIELDS_BY_ID, f"{label} 缺少字段 {key}")

    def test_simultaneous_upload_limit_uses_upload_slots_not_legacy_peers(self) -> None:
        """4.x 已没有 max-peers-* 系列；「同时上传数」= upload-slots-per-torrent。"""
        module = self.module
        self.assertEqual(
            "upload-slots-per-torrent", module.FIELDS_BY_ID["upload-slots-per-torrent"]["kebab"]
        )
        self.assertEqual(
            "upload_slots_per_torrent", module.FIELDS_BY_ID["upload-slots-per-torrent"]["snake"]
        )
        for legacy in LEGACY_KEYS:
            self.assertNotIn(legacy, module.FIELDS_BY_ID)

    def test_alt_speed_family_is_complete(self) -> None:
        module = self.module
        for key in (
            "alt-speed-enabled",
            "alt-speed-up",
            "alt-speed-down",
            "alt-speed-time-enabled",
            "alt-speed-time-begin",
            "alt-speed-time-end",
            "alt-speed-time-day",
        ):
            self.assertIn(key, module.FIELDS_BY_ID)

    def test_every_field_has_label_unit_and_range(self) -> None:
        module = self.module
        for field in module.FIELDS:
            self.assertTrue(field["label"].strip(), field["id"])
            self.assertTrue(field["help"].strip(), field["id"])
            # 只有纯数值字段（int）带单位；clock/weekdays 是时间与星期位图，
            # 布尔与路径也没有单位。
            if field["kind"] == "int":
                self.assertTrue(str(field.get("unit", "")).strip(), field["id"])
            else:
                self.assertFalse(str(field.get("unit", "")).strip(), field["id"])
            if field["kind"] in ("int", "clock", "weekdays"):
                self.assertIsInstance(field["minimum"], int, field["id"])
                self.assertIsInstance(field["maximum"], int, field["id"])
                self.assertLess(field["minimum"], field["maximum"] + 1, field["id"])
            if field["kind"] == "bool":
                self.assertIsInstance(field["default"], bool, field["id"])

    def test_numeric_fields_declare_the_expected_unit(self) -> None:
        """界面上靠这个字符串显示单位（「个」「分钟」「KB/s」），一个都不能漏。

        通用断言只检查「int 字段非空」，这里把具体文案钉死，
        顺便确认 seed-queue-size 这类字段确实带单位。
        """
        module = self.module
        expected = {
            "upload-slots-per-torrent": "个",
            "seed-queue-size": "个",
            "download-queue-size": "个",
            "queue-stalled-minutes": "分钟",
            "peer-limit-global": "个",
            "peer-limit-per-torrent": "个",
            "speed-limit-up": "KB/s",
            "speed-limit-down": "KB/s",
            "alt-speed-up": "KB/s",
            "alt-speed-down": "KB/s",
        }
        for logical, unit in expected.items():
            field = module.FIELDS_BY_ID[logical]
            self.assertEqual("int", field["kind"], logical)
            self.assertEqual(unit, field["unit"], logical)
        # 这份清单覆盖了需求里 8 个设置项的全部数值型字段
        # （「每个 int 字段都要有单位」由上面的通用断言负责）。
        numeric = {field["id"] for field in module.FIELDS if field["kind"] == "int"}
        self.assertTrue(set(expected) <= numeric, sorted(numeric - set(expected)))

    def test_key_for_translates_between_styles(self) -> None:
        module = self.module
        self.assertEqual("download-dir", module.key_for("download-dir", "kebab"))
        self.assertEqual("download_dir", module.key_for("download-dir", "snake"))
        self.assertEqual("alt-speed-time-day", module.key_for("alt-speed-time-day", "kebab"))
        self.assertEqual("alt_speed_time_day", module.key_for("alt-speed-time-day", "snake"))

    # ------------------------------------------------------------------
    # 键名风格识别
    # ------------------------------------------------------------------

    def test_detect_style_kebab_and_snake(self) -> None:
        module = self.module
        self.assertEqual("kebab", module.detect_style({"download-dir": "/x"}))
        self.assertEqual("snake", module.detect_style({"download_dir": "/x"}))
        self.assertEqual("kebab", module.detect_style({}))
        # 只有无法归类的键时回落到 kebab（4.x 的默认风格）。
        self.assertEqual("kebab", module.detect_style({"totally": 1, "made_up": 2}))
        self.assertEqual("snake", module.detect_style({"rpc_port": 9091}))
        self.assertEqual("snake", module.detect_style({"rpc_enabled": True}))

    def test_style_override_from_environment(self) -> None:
        module = self.reload_with(SETTINGS_KEY_STYLE="snake")
        self.assertEqual("snake", module.detect_style({}))
        self.assertEqual("download_dir", module.key_for("download-dir", module.detect_style({})))

    # ------------------------------------------------------------------
    # settings.json 读写
    # ------------------------------------------------------------------

    def test_settings_roundtrip_is_valid_json(self) -> None:
        module = self.module
        self.assertEqual({}, module.read_settings())
        module.write_settings({"download-dir": str(self.downloads), "peer-limit-global": 200})
        self.assertEqual(
            {"download-dir": str(self.downloads), "peer-limit-global": 200},
            module.read_settings(),
        )
        self.assertEqual([], list(self.data.glob("*.tmp")))

    def test_corrupt_settings_file_is_treated_as_empty(self) -> None:
        module = self.module
        self.data.mkdir(parents=True, exist_ok=True)
        module.SETTINGS_FILE.write_text("{not json", encoding="utf-8")
        self.assertEqual({}, module.read_settings())

    def test_effective_values_use_defaults_for_missing_keys(self) -> None:
        module = self.module
        values = module.effective_values({}, "kebab")
        self.assertEqual(module.FIELDS_BY_ID["peer-limit-global"]["default"], values["peer-limit-global"])
        self.assertEqual(module.DEFAULT_DOWNLOAD_DIR, values["download-dir"])

    def test_merge_managed_only_writes_known_and_managed_keys(self) -> None:
        module = self.module
        merged = module.merge_managed({}, "kebab", {"peer-limit-global": 300})
        allowed = {field["id"] for field in module.FIELDS} | set(module.MANAGED_KEYS)
        self.assertLessEqual(set(merged), allowed)
        self.assertEqual(300, merged["peer-limit-global"])
        self.assertTrue(merged["rpc-enabled"])
        self.assertEqual(module.RPC_PORT, merged["rpc-port"])
        self.assertFalse(merged["rpc-host-whitelist-enabled"])
        # 白名单交给「凭据齐全才对外监听」的规则替代，绑定地址默认只监听本机
        self.assertFalse(merged["rpc-whitelist-enabled"])
        self.assertEqual("127.0.0.1", merged["rpc-bind-address"])
        self.assertFalse(merged["rpc-authentication-required"])
        self.assertEqual(module.DEFAULT_DOWNLOAD_DIR, merged["download-dir"])

    def test_rpc_bind_defaults_to_loopback(self) -> None:
        """没配凭据时绝不能对外监听。"""
        module = self.module
        self.assertEqual("127.0.0.1", module.FIELDS_BY_ID["rpc-bind-address"]["default"])

    # ------------------------------------------------------------------
    # 「0.0.0.0 必须配齐凭据」规则
    # ------------------------------------------------------------------

    def test_remote_access_requires_credentials(self) -> None:
        module = self.module
        with mock.patch.object(module, "read_credential", return_value={}):
            # 只要凭据不齐，请求 0.0.0.0 也会被压回 127.0.0.1
            for values in (
                {"rpc-bind-address": "0.0.0.0"},
                {"rpc-bind-address": "0.0.0.0", "rpc-username": "u"},
                {"rpc-bind-address": "0.0.0.0", "rpc-password": "p"},
                {"rpc-bind-address": "0.0.0.0", "rpc-username": "  "},
            ):
                result = module.normalize_remote_access(values)
                self.assertEqual("127.0.0.1", result["rpc-bind-address"], values)
                self.assertFalse(result["rpc-authentication-required"], values)

    def test_remote_access_allowed_with_credentials(self) -> None:
        module = self.module
        with mock.patch.object(module, "read_credential", return_value={}):
            result = module.normalize_remote_access({
                "rpc-bind-address": "0.0.0.0", "rpc-username": "u", "rpc-password": "p",
            })
        self.assertEqual("0.0.0.0", result["rpc-bind-address"])
        self.assertTrue(result["rpc-authentication-required"])

    def test_remote_access_reuses_saved_password(self) -> None:
        """界面留空表示不修改口令，此时应沿用已保存的密码。"""
        module = self.module
        with mock.patch.object(module, "read_credential",
                               return_value={"username": "u", "password": "saved"}):
            result = module.normalize_remote_access({
                "rpc-bind-address": "0.0.0.0", "rpc-username": "u", "rpc-password": "",
            })
        self.assertEqual("0.0.0.0", result["rpc-bind-address"])
        self.assertTrue(result["rpc-authentication-required"])

    def test_loopback_never_requires_auth(self) -> None:
        module = self.module
        with mock.patch.object(module, "read_credential", return_value={}):
            result = module.normalize_remote_access({
                "rpc-bind-address": "127.0.0.1", "rpc-username": "u", "rpc-password": "p",
            })
        self.assertEqual("127.0.0.1", result["rpc-bind-address"])
        self.assertFalse(result["rpc-authentication-required"])

    def test_auth_header_encodes_basic_credential(self) -> None:
        module = self.module
        with mock.patch.object(module, "read_credential",
                               return_value={"username": "u", "password": "p"}):
            header = module.auth_header()
        self.assertEqual("Basic dTpw", header["Authorization"])

    def test_auth_header_empty_without_credentials(self) -> None:
        module = self.module
        with mock.patch.object(module, "read_credential", return_value={}):
            self.assertEqual({}, module.auth_header())
        with mock.patch.object(module, "read_credential",
                               return_value={"username": "u", "password": ""}):
            self.assertEqual({}, module.auth_header())

    def test_merge_managed_keeps_user_choice_of_rpc_bind(self) -> None:
        """用户把绑定地址改成 127.0.0.1 时不能被默认值覆盖。"""
        module = self.module
        merged = module.merge_managed({}, "kebab", {"rpc-bind-address": "127.0.0.1"})
        self.assertEqual("127.0.0.1", merged["rpc-bind-address"])

    def test_merge_managed_never_writes_a_default_password(self) -> None:
        """默认值里不能有密码，否则每次保存都会把已设的密码清掉。"""
        module = self.module
        merged = module.merge_managed({}, "kebab", {})
        self.assertNotIn("rpc-password", merged)
        merged = module.merge_managed({"rpc-password": "hash"}, "kebab", {"rpc-username": "u"})
        self.assertEqual("hash", merged["rpc-password"])

    def test_merge_managed_keeps_snake_style(self) -> None:
        module = self.module
        merged = module.merge_managed({"rpc_port": 9091}, "snake", {"peer-limit-global": 300})
        self.assertEqual(300, merged["peer_limit_global"])
        self.assertNotIn("peer-limit-global", merged)

    # ------------------------------------------------------------------
    # 远程访问：监听地址与账号验证
    # ------------------------------------------------------------------

    def test_choice_field_rejects_unknown_address(self) -> None:
        module = self.module
        with self.assertRaises(module.SettingsError):
            module.validate_settings({"rpc-bind-address": "192.168.1.5"})

    def test_choice_field_accepts_both_addresses(self) -> None:
        module = self.module
        for value in ("0.0.0.0", "127.0.0.1"):
            clean = module.validate_settings({"rpc-bind-address": value})
            self.assertEqual(value, clean["rpc-bind-address"])

    def test_empty_password_means_keep_current(self) -> None:
        """留空不能覆盖已保存的密码哈希。"""
        module = self.module
        clean = module.validate_settings({"rpc-password": ""})
        self.assertNotIn("rpc-password", clean)

    def test_nonempty_password_is_written(self) -> None:
        module = self.module
        clean = module.validate_settings({"rpc-password": "hunter2"})
        self.assertEqual("hunter2", clean["rpc-password"])

    def test_password_with_newline_is_rejected(self) -> None:
        module = self.module
        with self.assertRaises(module.SettingsError):
            module.validate_settings({"rpc-password": "a\nb"})

    def test_username_accepts_empty_string(self) -> None:
        module = self.module
        clean = module.validate_settings({"rpc-username": ""})
        self.assertEqual("", clean["rpc-username"])

    def test_payload_never_exposes_stored_password(self) -> None:
        """settings.json 里是加盐哈希，接口不能回显。"""
        module = self.module
        with mock.patch.object(module, "read_settings",
                               return_value={"rpc-password": "abcdef0123456789"}):
            payload = module.settings_payload()
        field = next(f for f in payload["fields"] if f["id"] == "rpc-password")
        self.assertEqual("", field["value"])

    def test_auth_group_is_exposed_to_the_ui(self) -> None:
        module = self.module
        groups = {group["id"] for group in module.GROUPS}
        self.assertIn("auth", groups)
        for field_id in ("rpc-bind-address", "rpc-authentication-required",
                         "rpc-username", "rpc-password"):
            self.assertEqual("auth", module.FIELDS_BY_ID[field_id]["group"])

    def test_merge_managed_preserves_unmanaged_keys(self) -> None:
        module = self.module
        merged = module.merge_managed({"encryption": 1, "umask": "022"}, "kebab", {})
        self.assertEqual(1, merged["encryption"])
        self.assertEqual("022", merged["umask"])

    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------

    def test_validate_rejects_unknown_field(self) -> None:
        module = self.module
        with self.assertRaises(module.SettingsError) as caught:
            module.validate_settings({"totally-unknown": 1})
        self.assertIn("totally-unknown", caught.exception.fields)

    def test_validate_rejects_out_of_range(self) -> None:
        module = self.module
        with self.assertRaises(module.SettingsError) as caught:
            module.validate_settings({"peer-limit-global": 10 ** 7})
        self.assertIn("peer-limit-global", caught.exception.fields)

    def test_validate_rejects_bool_for_int_field(self) -> None:
        """bool 是 int 的子类，必须显式拒绝，否则 True 会被当成 1。"""
        module = self.module
        with self.assertRaises(module.SettingsError):
            module.validate_settings({"download-queue-size": True})

    def test_validate_rejects_int_for_bool_field(self) -> None:
        module = self.module
        with self.assertRaises(module.SettingsError):
            module.validate_settings({"speed-limit-up-enabled": 1})

    def test_validate_accepts_numeric_strings(self) -> None:
        module = self.module
        clean = module.validate_settings({"peer-limit-global": "512"})
        self.assertEqual(512, clean["peer-limit-global"])

    def test_validate_rejects_relative_download_dir(self) -> None:
        module = self.module
        with self.assertRaises(module.SettingsError) as caught:
            module.validate_settings({"download-dir": "downloads"})
        self.assertIn("download-dir", caught.exception.fields)

    def test_validate_rejects_non_object(self) -> None:
        module = self.module
        with self.assertRaises(module.SettingsError):
            module.validate_settings([1, 2, 3])

    def test_validate_accepts_clock_and_weekday_bitmaps(self) -> None:
        module = self.module
        clean = module.validate_settings(
            {"alt-speed-time-begin": 540, "alt-speed-time-end": 1020, "alt-speed-time-day": 62}
        )
        self.assertEqual({"alt-speed-time-begin": 540, "alt-speed-time-end": 1020,
                          "alt-speed-time-day": 62}, clean)

    def test_validate_rejects_out_of_range_clock(self) -> None:
        module = self.module
        with self.assertRaises(module.SettingsError):
            module.validate_settings({"alt-speed-time-begin": 1440})

    def test_validate_creates_download_dir(self) -> None:
        module = self.module
        module.ensure_download_dir({"download-dir": str(self.downloads)})
        self.assertTrue(self.downloads.is_dir())

    def test_validate_reports_uncreatable_download_dir(self) -> None:
        module = self.module
        blocker = self.data.parent / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        with self.assertRaises(module.SettingsError) as caught:
            module.ensure_download_dir({"download-dir": str(blocker / "sub")})
        self.assertIn("download-dir", caught.exception.fields)

    # ------------------------------------------------------------------
    # 写入并重启的顺序
    # ------------------------------------------------------------------

    def test_apply_settings_stops_writes_then_starts(self) -> None:
        module = self.module
        events: list[str] = []
        seen = {"running": 0}

        def fake_running():
            seen["running"] += 1
            events.append(f"running{seen['running']}")
            return True

        def fake_stop():
            events.append("stop")
            return True, ""

        def fake_start():
            events.append("start")
            return True, ""

        real_write = module.write_settings

        def fake_write(settings):
            events.append("write")
            real_write(settings)

        with mock.patch.object(module, "daemon_running", side_effect=fake_running), \
                mock.patch.object(module, "stop_daemon", side_effect=fake_stop), \
                mock.patch.object(module, "start_daemon", side_effect=fake_start), \
                mock.patch.object(module, "write_settings", side_effect=fake_write):
            result = module.apply_settings({"peer-limit-global": 321})

        # 先停 → 再写 → 再启；最后的 running2 是返回体里复查状态。
        self.assertEqual(["running1", "stop", "write", "start", "running2"], events)
        self.assertTrue(result["restarted"])
        self.assertTrue(result["daemonRunning"])
        self.assertEqual(321, module.read_settings()["peer-limit-global"])
        self.assertEqual(321, result["values"]["peer-limit-global"])

    def test_apply_settings_skips_daemon_ops_when_stopped(self) -> None:
        module = self.module
        with mock.patch.object(module, "daemon_running", return_value=False), \
                mock.patch.object(module, "stop_daemon", side_effect=AssertionError("不应停止")), \
                mock.patch.object(module, "start_daemon", side_effect=AssertionError("不应启动")), \
                mock.patch.object(module, "write_settings") as write:
            result = module.apply_settings({"speed-limit-down": 2048}, restart=True)

        write.assert_called_once()
        self.assertFalse(result["restarted"])

    def test_apply_settings_restart_false_only_writes(self) -> None:
        module = self.module
        with mock.patch.object(module, "daemon_running", return_value=True), \
                mock.patch.object(module, "stop_daemon", side_effect=AssertionError("不应停止")), \
                mock.patch.object(module, "start_daemon", side_effect=AssertionError("不应启动")):
            result = module.apply_settings({"upload-slots-per-torrent": 20}, restart=False)
        self.assertFalse(result["restarted"])
        self.assertEqual(20, module.read_settings()["upload-slots-per-torrent"])

    def test_apply_settings_rejects_unknown_before_touching_disk(self) -> None:
        module = self.module
        with self.assertRaises(module.SettingsError):
            module.apply_settings({"nope": 1})
        self.assertFalse(module.SETTINGS_FILE.exists())

    def test_apply_settings_raises_when_restart_fails(self) -> None:
        module = self.module
        with mock.patch.object(module, "daemon_running", return_value=True), \
                mock.patch.object(module, "stop_daemon", return_value=(True, "")), \
                mock.patch.object(module, "start_daemon", return_value=(False, "起不来")):
            with self.assertRaises(module.DaemonError):
                module.apply_settings({"peer-limit-global": 100})
        self.assertEqual(100, module.read_settings()["peer-limit-global"])

    # ------------------------------------------------------------------
    # daemon 生命周期
    # ------------------------------------------------------------------

    def test_start_daemon_invokes_bundled_loader(self) -> None:
        """Entware 二进制的 ELF 解释器写死为 /opt/lib/ld-linux-aarch64.so.1。

        设备上没有 /opt，直接执行会报「无法执行：找不到需要的文件」，
        所以必须用随包加载器 + --library-path 显式调用。
        """
        module = self.module
        loader = self.lib / "ld-linux-aarch64.so.1"
        loader.write_bytes(b"\x7fELF")
        with mock.patch.object(module, "daemon_running", return_value=False), \
                mock.patch.object(module, "rpc_alive", return_value=True), \
                mock.patch.object(module.subprocess, "Popen") as popen:
            ok, error = module.start_daemon(timeout=1)

        self.assertTrue(ok, error)
        command = popen.call_args.args[0]
        self.assertEqual(str(loader), command[0])
        self.assertEqual(["--library-path", str(self.lib)], command[1:3])
        self.assertEqual(str(self.daemon), command[3])
        self.assertIn("-f", command)
        self.assertEqual(str(self.data), command[command.index("-g") + 1])
        self.assertEqual(str(module.LOG_FILE), command[command.index("-e") + 1])

    def test_start_daemon_without_loader_falls_back_and_warns(self) -> None:
        module = self.module
        self.assertFalse(module.LOADER.exists())
        module._LOADER_WARNED["value"] = False
        with mock.patch.object(module, "daemon_running", return_value=False), \
                mock.patch.object(module, "rpc_alive", return_value=True), \
                mock.patch.object(module.subprocess, "Popen") as popen, \
                mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
            ok, error = module.start_daemon(timeout=1)

        self.assertTrue(ok, error)
        command = popen.call_args.args[0]
        self.assertEqual(str(self.daemon), command[0])
        self.assertNotIn("--library-path", command)
        # 现场只会看到一个含糊的 exec 错误，所以必须留下明确警告。
        self.assertIn("ld-linux-aarch64.so.1", stderr.getvalue())

    def test_runtime_argv_prefers_the_bundled_loader(self) -> None:
        module = self.module
        loader = self.lib / "ld-linux-aarch64.so.1"
        loader.write_bytes(b"\x7fELF")
        self.assertEqual(
            [str(loader), "--library-path", str(self.lib), str(module.DAEMON), "--version"],
            module.runtime_argv(module.DAEMON, "--version"),
        )

    def test_runtime_argv_warns_only_once(self) -> None:
        module = self.module
        module._LOADER_WARNED["value"] = False
        with mock.patch("sys.stderr", new_callable=io.StringIO) as stderr:
            module.runtime_argv(module.DAEMON, "--version")
            module.runtime_argv(module.DAEMON, "--version")
        self.assertEqual(1, stderr.getvalue().count("ld-linux-aarch64.so.1"))

    def test_daemon_version_uses_the_bundled_loader(self) -> None:
        module = self.module
        loader = self.lib / "ld-linux-aarch64.so.1"
        loader.write_bytes(b"\x7fELF")
        module.VERSION_CACHE.update({"mtime": None, "value": None})
        with mock.patch.object(
            module, "_run",
            return_value=mock.Mock(stdout="transmission-daemon 4.0.6 (38c164933e)\n", stderr=""),
        ) as run:
            self.assertEqual("4.0.6", module.daemon_version())
        self.assertEqual(
            [str(loader), "--library-path", str(self.lib), str(module.DAEMON), "--version"],
            run.call_args.args[0],
        )

    def test_daemon_env_exports_library_path_as_fallback(self) -> None:
        """真正决定加载哪套 .so 的是 --library-path（它会覆盖 LD_LIBRARY_PATH）。

        这个环境变量只为「加载器缺失、退回直接执行」时兜底，保留但不必依赖。
        """
        module = self.module
        self.assertTrue(module.daemon_env()["LD_LIBRARY_PATH"].startswith(str(self.lib)))

    def test_daemon_env_points_transmission_at_bundled_web_control(self) -> None:
        """不设 TRANSMISSION_WEB_HOME 时，直接打开 RPC 端口会报找不到 web 界面。"""
        module = self.module
        twc = module.WEB_DIR / module.WEB_CONTROL_DIRNAME
        twc.mkdir(parents=True, exist_ok=True)
        (twc / "index.html").write_text("<html></html>", encoding="utf-8")
        env = module.daemon_env()
        self.assertEqual(str(twc), env.get("TRANSMISSION_WEB_HOME"))

    def test_daemon_env_points_libcurl_at_an_existing_ca_bundle(self) -> None:
        """随包 libcurl 的默认 CA 路径是 /opt/...，设备上没有 /opt。

        不指一个真实存在的 CA 包，https tracker 会全部失败，报错文案是
        「Could not connect to tracker」，很容易被误判成网络或防火墙问题。
        """
        module = self.module
        bundle = self.lib.parent / "ca-certificates.crt"
        bundle.write_text("dummy\n", encoding="utf-8")
        original = module.CA_BUNDLE_CANDIDATES
        module.CA_BUNDLE_CANDIDATES = (str(bundle),)
        try:
            with mock.patch.dict(module.os.environ):
                module.os.environ.pop("CURL_CA_BUNDLE", None)
                self.assertEqual(str(bundle), module.daemon_env().get("CURL_CA_BUNDLE"))
        finally:
            module.CA_BUNDLE_CANDIDATES = original
            bundle.unlink(missing_ok=True)

    def test_daemon_env_skips_ca_bundle_when_none_exists(self) -> None:
        """候选都不存在时不能瞎设一个路径，否则等于把证书校验指向空气。"""
        module = self.module
        original = module.CA_BUNDLE_CANDIDATES
        module.CA_BUNDLE_CANDIDATES = (str(self.lib.parent / "nope.crt"),)
        try:
            with mock.patch.dict(module.os.environ):
                module.os.environ.pop("CURL_CA_BUNDLE", None)
                self.assertNotIn("CURL_CA_BUNDLE", module.daemon_env())
        finally:
            module.CA_BUNDLE_CANDIDATES = original

    def test_ipv6_switch_reads_transmission_string_values(self) -> None:
        """transmission 用 "::" / "" 表示 IPv6 开关，界面按 bool 呈现。"""
        module = self.module
        on = module.effective_values({"bind-address-ipv6": "::"}, "kebab")
        off = module.effective_values({"bind-address-ipv6": ""}, "kebab")
        self.assertIs(True, on["bind-address-ipv6"])
        self.assertIs(False, off["bind-address-ipv6"])

    def test_ipv6_switch_writes_transmission_string_values(self) -> None:
        module = self.module
        self.assertEqual("", module.merge_managed({}, "kebab", {"bind-address-ipv6": False})["bind-address-ipv6"])
        self.assertEqual("::", module.merge_managed({}, "kebab", {"bind-address-ipv6": True})["bind-address-ipv6"])

    def test_lpd_is_disabled_on_first_run(self) -> None:
        """多数 PT 站不允许 LPD，首次运行要把 transmission 自带的默认（开启）改掉。"""
        module = self.module
        self.write_raw_settings({"lpd-enabled": True})
        with mock.patch.object(module, "daemon_running", return_value=False):
            module.apply_initial_defaults()
        self.assertFalse(module.read_settings()["lpd-enabled"])
        self.assertTrue(module.read_plugin_state()["network-defaults-applied"])

    def test_lpd_initial_default_leaves_user_choice_alone(self) -> None:
        """只在首次纠正一次：用户之后手动打开，不应该被再关掉。"""
        module = self.module
        self.write_raw_settings({"lpd-enabled": True})
        module.write_plugin_state({"network-defaults-applied": True})
        with mock.patch.object(module, "daemon_running", return_value=False):
            module.apply_initial_defaults()
        self.assertTrue(module.read_settings()["lpd-enabled"])

    def test_watch_dir_fields_sit_right_after_download_dir(self) -> None:
        module = self.module
        ids = [field["id"] for field in module.FIELDS]
        self.assertEqual("watch-dir-enabled", ids[ids.index("download-dir") + 1])
        self.assertEqual("watch-dir", ids[ids.index("download-dir") + 2])
        for name in ("watch-dir-enabled", "watch-dir"):
            self.assertEqual("basic", module.FIELDS_BY_ID[name]["group"])

    def test_watch_dir_accepts_empty_value(self) -> None:
        """监视目录是可选路径，留空表示不使用，不能被路径校验拦下。"""
        module = self.module
        clean = module.validate_settings({"watch-dir": ""})
        self.assertEqual("", clean["watch-dir"])

    def test_watch_dir_must_be_set_when_enabled(self) -> None:
        """开了监视目录却没填路径，要直接报错，不能写进 settings.json。"""
        module = self.module
        with mock.patch.object(module, "daemon_running", return_value=False):
            with self.assertRaises(module.SettingsError) as caught:
                module.apply_settings({"watch-dir-enabled": True, "watch-dir": ""})
        self.assertIn("watch-dir", caught.exception.fields)

    def test_watch_dir_is_created_when_enabled(self) -> None:
        module = self.module
        seen = []
        with mock.patch.object(
            module.Path, "mkdir", autospec=True,
            side_effect=lambda self, **_kwargs: seen.append(self),
        ):
            module.ensure_watch_dir({"watch-dir-enabled": True, "watch-dir": "/nas/pool0/watch"})
        self.assertEqual([module.Path("/nas/pool0/watch")], seen)

    def test_watch_dir_is_not_created_when_disabled(self) -> None:
        """没启用就不该顺手把目录建出来。"""
        module = self.module
        with mock.patch.object(module.Path, "mkdir", autospec=True) as mkdir:
            module.ensure_watch_dir({"watch-dir-enabled": False, "watch-dir": "/nas/pool0/watch"})
        mkdir.assert_not_called()

    def test_watch_dir_error_happens_before_the_daemon_is_stopped(self) -> None:
        """校验失败必须发生在停 daemon 之前，否则 daemon 会停在停止状态起不来。"""
        module = self.module
        with mock.patch.object(module, "daemon_running", return_value=True), \
                mock.patch.object(module, "stop_daemon") as stop:
            with self.assertRaises(module.SettingsError):
                module.apply_settings({"watch-dir-enabled": True, "watch-dir": ""})
        stop.assert_not_called()

    def test_start_daemon_reports_missing_binary(self) -> None:
        module = self.module
        self.daemon.unlink()
        with mock.patch.object(module, "daemon_running", return_value=False):
            ok, error = module.start_daemon(timeout=1)
        self.assertFalse(ok)
        self.assertIn("fetch_runtime.py", error)

    def test_stop_daemon_terminates_pid_from_pidfile(self) -> None:
        module = self.module
        self.data.mkdir(parents=True, exist_ok=True)
        module.PID_FILE.write_text("4242\n", encoding="utf-8")
        calls: list[tuple[int, int]] = []
        alive = {"value": True}

        def fake_kill(pid, sig):
            calls.append((pid, sig))
            if sig == module.signal.SIGTERM:
                alive["value"] = False
                module.PID_FILE.unlink()

        with mock.patch.object(module, "process_exists", side_effect=lambda _pid: alive["value"]), \
                mock.patch.object(module.os, "kill", side_effect=fake_kill):
            ok, error = module.stop_daemon(timeout=1)

        self.assertTrue(ok, error)
        self.assertEqual((4242, module.signal.SIGTERM), calls[0])

    def test_stop_daemon_is_noop_when_nothing_runs(self) -> None:
        module = self.module
        with mock.patch.object(module, "rpc_alive", return_value=False), \
                mock.patch.object(module, "find_daemon_pids", return_value=[]):
            ok, error = module.stop_daemon(timeout=1)
        self.assertTrue(ok, error)

    # ------------------------------------------------------------------
    # pid 文件缺失时的兜底停止
    # ------------------------------------------------------------------

    def test_stop_daemon_falls_back_to_scanning_processes(self) -> None:
        """pid 文件缺失但进程在跑时必须能停掉，不能卡死。"""
        module = self.module
        killed: list[tuple[int, int]] = []

        def fake_kill(pid, sig):
            killed.append((pid, sig))

        with mock.patch.object(module, "read_pid", return_value=None), \
                mock.patch.object(module, "find_daemon_pids", return_value=[354294]), \
                mock.patch.object(module, "process_exists", return_value=False), \
                mock.patch.object(module.os, "kill", side_effect=fake_kill):
            ok, error = module.stop_daemon(timeout=1)

        self.assertTrue(ok, error)
        self.assertEqual((354294, module.signal.SIGTERM), killed[0])

    def test_stop_daemon_refuses_when_port_owned_by_something_else(self) -> None:
        """端口有响应但不是我们的进程时不能乱杀。"""
        module = self.module
        with mock.patch.object(module, "read_pid", return_value=None), \
                mock.patch.object(module, "find_daemon_pids", return_value=[]), \
                mock.patch.object(module, "rpc_alive", return_value=True):
            ok, error = module.stop_daemon(timeout=1)
        self.assertFalse(ok)
        self.assertIn("不是本插件启动的", error)

    def test_find_daemon_pids_requires_binary_path_and_data_dir(self) -> None:
        """必须同时匹配本插件二进制的完整路径与数据目录。

        只用 daemon 名不够：任何命令行里恰好含该字符串的进程都会被误判
        （例如一段带 grep 的 shell 命令），那样停止时会杀错进程。
        """
        module = self.module
        binary = str(module.BIN_DIR / module.DAEMON_NAME)
        data = str(module.DATA_DIR)
        entries = {
            "100": f"{binary} -f -g {data} -e {data}/transmission.log",
            "200": f"{binary} -f -g /somewhere/else",
            "300": f"python3 /opt/other -g {data}",
            "400": f"sh -c grep 'transmission-daemon -g {data}'",  # 假阳性样例
        }

        class FakeProc:
            def is_dir(self):
                return True

            def iterdir(self):
                paths = []
                for name, cmdline in entries.items():
                    entry = mock.Mock()
                    entry.name = name

                    def reader(text=cmdline):
                        return text.replace(" ", "\x00").encode()

                    entry.__truediv__ = lambda self, other, _r=reader: mock.Mock(read_bytes=_r)
                    paths.append(entry)
                return paths

        with mock.patch.object(
            module, "Path", side_effect=lambda p: FakeProc() if p == "/proc" else Path(p)
        ):
            found = module.find_daemon_pids()
        self.assertEqual([100], found)

    def test_start_daemon_writes_pidfile(self) -> None:
        """transmission 4.x 的 pid 文件开关是 -x（不是 -P），漏了就会停不掉。"""
        module = self.module
        captured: dict = {}

        class FakePopen:
            def __init__(self, argv, **kwargs):
                captured["argv"] = argv

        with mock.patch.object(module, "daemon_running", return_value=False), \
                mock.patch.object(module, "ensure_runtime_executables"), \
                mock.patch.object(module, "write_settings"), \
                mock.patch.object(module, "read_settings", return_value={}), \
                mock.patch.object(module, "rpc_alive", return_value=True), \
                mock.patch.object(module.subprocess, "Popen", FakePopen):
            ok, error = module.start_daemon(timeout=1)

        self.assertTrue(ok, error)
        argv = captured["argv"]
        self.assertIn("-x", argv)
        self.assertEqual(str(module.PID_FILE), argv[argv.index("-x") + 1])

    def test_daemon_version_parses_daemon_output(self) -> None:
        module = self.module
        module.VERSION_CACHE.update({"mtime": None, "value": None})
        with mock.patch.object(
            module, "_run", return_value=mock.Mock(stdout="transmission-daemon 4.0.6\n", stderr="")
        ):
            self.assertEqual("4.0.6", module.daemon_version())
        # 同一次 stat 内命中缓存，不再执行子进程。
        module.VERSION_CACHE.update({"mtime": self.daemon.stat().st_mtime, "value": "4.0.6"})
        with mock.patch.object(module, "_run", side_effect=AssertionError("不应再执行")) as run:
            self.assertEqual("4.0.6", module.daemon_version())
        run.assert_not_called()

    def test_daemon_version_ignores_garbage_output(self) -> None:
        module = self.module
        module.VERSION_CACHE.update({"mtime": None, "value": None})
        with mock.patch.object(
            module,
            "_run",
            return_value=mock.Mock(
                stdout="", stderr="Exec format error: not executable"
            ),
        ):
            self.assertIsNone(module.daemon_version())

    def test_daemon_version_is_none_without_binary(self) -> None:
        module = self.module
        self.daemon.unlink()
        self.assertIsNone(module.daemon_version())

    def test_rpc_alive_handles_409_and_records_session_id(self) -> None:
        module = self.module
        module.SESSION_ID["value"] = ""
        with mock.patch.object(
            module, "rpc_request", return_value=(409, {"X-Transmission-Session-Id": "abc"}, b"")
        ):
            self.assertTrue(module.rpc_alive())
        self.assertEqual("abc", module.SESSION_ID["value"])

    def test_rpc_alive_false_when_connection_fails(self) -> None:
        module = self.module
        with mock.patch.object(module, "rpc_request", side_effect=ConnectionRefusedError()):
            self.assertFalse(module.rpc_alive())

    def test_session_stats_returns_arguments(self) -> None:
        module = self.module
        body = json.dumps({"result": "success", "arguments": {"torrentCount": 3}}).encode()
        with mock.patch.object(module, "rpc_request", return_value=(200, {}, body)):
            self.assertEqual({"torrentCount": 3}, module.session_stats())

    def test_session_stats_retries_after_409(self) -> None:
        module = self.module
        body = json.dumps({"result": "success", "arguments": {"torrentCount": 1}}).encode()
        responses = [
            (409, {"X-Transmission-Session-Id": "s1"}, b""),
            (200, {}, body),
        ]
        with mock.patch.object(module, "rpc_request", side_effect=responses):
            self.assertEqual({"torrentCount": 1}, module.session_stats())
        self.assertEqual("s1", module.SESSION_ID["value"])

    def test_process_exists_rejects_other_processes(self) -> None:
        module = self.module
        self.assertFalse(module.process_exists(0))
        self.assertFalse(module.process_exists(-1))

    def test_enable_flag_drives_autostart(self) -> None:
        """插件服务/设备重启后按这个标志把 daemon 拉回来。"""
        module = self.module
        self.assertFalse(module.should_autostart())
        module.set_enabled(True)
        self.assertTrue(module.should_autostart())
        self.assertTrue(module.read_plugin_state()["enabled"])
        module.set_enabled(False)
        self.assertFalse(module.should_autostart())

    def test_should_autostart_requires_daemon_binary(self) -> None:
        module = self.module
        module.set_enabled(True)
        self.daemon.unlink()
        self.assertFalse(module.should_autostart())

    def test_corrupt_plugin_state_is_treated_as_empty(self) -> None:
        module = self.module
        self.data.mkdir(parents=True, exist_ok=True)
        module.PLUGIN_STATE_FILE.write_text("[1, 2]", encoding="utf-8")
        self.assertEqual({}, module.read_plugin_state())
        self.assertFalse(module.should_autostart())

    # ------------------------------------------------------------------
    # HTTP 契约
    # ------------------------------------------------------------------

    def _serve(self) -> int:
        module = self.module
        server = module.ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def _request(self, port: int, method: str, path: str, body=None, headers=None):
        connection = HTTPConnection("127.0.0.1", port, timeout=10)
        payload = json.dumps(body).encode() if body is not None else None
        merged = dict(headers or {})
        if payload is not None:
            merged.setdefault("Content-Type", "application/json")
        connection.request(method, path, body=payload, headers=merged)
        response = connection.getresponse()
        data = response.read()
        result = (response.status, data, dict(response.getheaders()))
        connection.close()
        return result

    # ---- 请求体读取（一个请求只能读一次正文）----

    def _bare_handler(self):
        """不带 socket 的 Handler，用来单测请求体读取逻辑。"""
        module = self.module
        handler = object.__new__(module.Handler)
        handler.close_connection = False
        return handler

    def test_read_raw_body_reads_exactly_the_declared_length(self) -> None:
        handler = self._bare_handler()
        handler.headers = {"Content-Length": "5"}
        handler.rfile = io.BytesIO(b"abcde")
        self.assertEqual(b"abcde", handler._read_raw_body())
        # 正文已经被消费掉了：再读一次只会拿到空，然后报「不完整」。
        # 这就是同一个请求绝不能读第二遍正文的原因。
        with self.assertRaises(ValueError):
            handler._read_raw_body()

    def test_read_raw_body_handles_partial_reads(self) -> None:
        """底层 socket 可能分片返回，不能读一次有多少就算多少。"""
        handler = self._bare_handler()

        class Dribble:
            def __init__(self, payload: bytes):
                self.payload = payload

            def read(self, size: int) -> bytes:
                chunk, self.payload = self.payload[:2], self.payload[2:]
                return chunk

        handler.headers = {"Content-Length": "5"}
        handler.rfile = Dribble(b"abcde")
        self.assertEqual(b"abcde", handler._read_raw_body())

    def test_read_raw_body_treats_missing_body_as_empty(self) -> None:
        handler = self._bare_handler()
        handler.headers = {}
        handler.rfile = io.BytesIO(b"")
        self.assertEqual(b"", handler._read_raw_body())

    def test_read_raw_body_rejects_truncated_body(self) -> None:
        handler = self._bare_handler()
        handler.headers = {"Content-Length": "9"}
        handler.rfile = io.BytesIO(b"abc")
        with self.assertRaises(ValueError) as caught:
            handler._read_raw_body()
        self.assertIn("不完整", str(caught.exception))
        self.assertTrue(handler.close_connection)

    def test_read_raw_body_rejects_oversized_body(self) -> None:
        module = self.module
        handler = self._bare_handler()
        handler.headers = {"Content-Length": str(module.MAX_BODY_BYTES + 1)}
        with self.assertRaises(ValueError) as caught:
            handler._read_raw_body()
        self.assertIn("过大", str(caught.exception))
        self.assertTrue(handler.close_connection)

    def test_read_raw_body_rejects_invalid_length(self) -> None:
        handler = self._bare_handler()
        handler.headers = {"Content-Length": "abc"}
        with self.assertRaises(ValueError) as caught:
            handler._read_raw_body()
        self.assertIn("无效请求长度", str(caught.exception))
        self.assertTrue(handler.close_connection)

    def test_send_ignores_a_closed_socket(self) -> None:
        """对端已断开时不要抛 BrokenPipe 让 handler 崩掉。"""
        module = self.module
        handler = self._bare_handler()

        class DeadWriter:
            def write(self, data):
                raise BrokenPipeError("gone")

        handler.wfile = DeadWriter()
        handler.send_response = lambda *a, **k: None
        handler.send_header = lambda *a, **k: None
        handler.end_headers = lambda *a, **k: None
        handler._send(200, b"payload", "text/plain")
        self.assertTrue(handler.close_connection)

    def test_healthz(self) -> None:
        port = self._serve()
        status, data, _ = self._request(port, "GET", "/healthz")
        self.assertEqual(200, status)
        self.assertTrue(json.loads(data)["ok"])

    def test_status_endpoint_contract(self) -> None:
        port = self._serve()
        module = self.module
        with mock.patch.object(module, "daemon_running", return_value=False), \
                mock.patch.object(module, "daemon_version", return_value="4.0.6"), \
                mock.patch.object(module, "session_stats", return_value=None):
            status, data, _ = self._request(port, "GET", "/api/status")
        self.assertEqual(200, status)
        payload = json.loads(data)
        for key in (
            "ok", "daemonRunning", "autostart", "pid", "version", "runtimeInstalled",
            "webControlInstalled", "rpcPort", "settingsKeyStyle", "downloadDir",
        ):
            self.assertIn(key, payload)
        self.assertTrue(payload["runtimeInstalled"])
        self.assertTrue(payload["webControlInstalled"])
        self.assertEqual(module.RPC_PORT, payload["rpcPort"])
        self.assertEqual("4.0.6", payload["version"])

    def test_settings_endpoint_lists_required_fields(self) -> None:
        port = self._serve()
        status, data, _ = self._request(port, "GET", "/api/settings")
        self.assertEqual(200, status)
        payload = json.loads(data)
        self.assertTrue(payload["ok"])
        self.assertIn("groups", payload)
        ids = {field["id"] for field in payload["fields"]}
        for key in REQUIRED_KEYS.values():
            self.assertIn(key, ids)
        for field in payload["fields"]:
            self.assertTrue(field["label"])
            self.assertIn("unit", field)
            self.assertIn("storageKey", field)
        self.assertEqual("kebab", payload["settingsKeyStyle"])

    def test_settings_storage_key_follows_detected_style(self) -> None:
        port = self._serve()
        self.write_raw_settings({"download_dir": str(self.downloads), "peer_limit_global": 111})
        status, data, _ = self._request(port, "GET", "/api/settings")
        self.assertEqual(200, status)
        payload = json.loads(data)
        self.assertEqual("snake", payload["settingsKeyStyle"])
        self.assertEqual(111, payload["values"]["peer-limit-global"])
        mapping = {field["id"]: field["storageKey"] for field in payload["fields"]}
        self.assertEqual("peer_limit_global", mapping["peer-limit-global"])

    def test_post_settings_updates_file(self) -> None:
        port = self._serve()
        module = self.module
        with mock.patch.object(module, "daemon_running", return_value=False):
            status, data, _ = self._request(
                port, "POST", "/api/settings",
                {"values": {"peer-limit-global": 512, "speed-limit-up": 4096,
                            "alt-speed-time-day": 62}},
            )
        self.assertEqual(200, status, data)
        payload = json.loads(data)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["restarted"])
        stored = module.read_settings()
        self.assertEqual(512, stored["peer-limit-global"])
        self.assertEqual(4096, stored["speed-limit-up"])
        self.assertEqual(62, stored["alt-speed-time-day"])

    def test_post_settings_rejects_missing_values(self) -> None:
        port = self._serve()
        status, data, _ = self._request(port, "POST", "/api/settings", {"nope": 1})
        self.assertEqual(400, status)
        self.assertIn("values", json.loads(data)["error"])

    def test_post_settings_rejects_unknown_field(self) -> None:
        port = self._serve()
        status, data, _ = self._request(
            port, "POST", "/api/settings", {"values": {"made-up": 1}}
        )
        self.assertEqual(400, status)
        payload = json.loads(data)
        self.assertFalse(payload["ok"])
        self.assertIn("made-up", payload["fields"])

    def test_post_settings_rejects_non_object_values(self) -> None:
        port = self._serve()
        status, data, _ = self._request(port, "POST", "/api/settings", {"values": []})
        self.assertEqual(400, status)
        self.assertFalse(json.loads(data)["ok"])

    def test_post_settings_rejects_non_boolean_restart_flag(self) -> None:
        port = self._serve()
        status, data, _ = self._request(
            port, "POST", "/api/settings", {"values": {}, "restart": "yes"}
        )
        self.assertEqual(400, status)
        self.assertFalse(json.loads(data)["ok"])

    def test_post_settings_rejects_invalid_json(self) -> None:
        port = self._serve()
        connection = HTTPConnection("127.0.0.1", port, timeout=10)
        connection.request(
            "POST", "/api/settings", body=b"{oops",
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        self.assertEqual(400, response.status)
        self.assertFalse(json.loads(response.read())["ok"])
        connection.close()

    def test_action_rejects_unknown(self) -> None:
        port = self._serve()
        status, data, _ = self._request(port, "POST", "/api/action", {"action": "nuke"})
        self.assertEqual(400, status)
        self.assertFalse(json.loads(data)["ok"])

    def test_action_start_invokes_start_daemon(self) -> None:
        port = self._serve()
        module = self.module
        with mock.patch.object(module, "start_daemon", return_value=(True, "")) as start, \
                mock.patch.object(module, "daemon_running", return_value=True), \
                mock.patch.object(module, "daemon_version", return_value="4.0.6"), \
                mock.patch.object(module, "session_stats", return_value=None):
            status, data, _ = self._request(port, "POST", "/api/action", {"action": "start"})
        self.assertEqual(200, status, data)
        start.assert_called_once()
        self.assertTrue(module.should_autostart())

    def test_action_stop_clears_autostart_flag(self) -> None:
        port = self._serve()
        module = self.module
        module.set_enabled(True)
        with mock.patch.object(module, "stop_daemon", return_value=(True, "")), \
                mock.patch.object(module, "daemon_running", return_value=False), \
                mock.patch.object(module, "daemon_version", return_value="4.0.6"), \
                mock.patch.object(module, "session_stats", return_value=None):
            status, data, _ = self._request(port, "POST", "/api/action", {"action": "stop"})
        self.assertEqual(200, status, data)
        self.assertFalse(module.should_autostart())

    def test_action_start_surfaces_failure(self) -> None:
        port = self._serve()
        module = self.module
        with mock.patch.object(module, "start_daemon", return_value=(False, "缺少运行文件")):
            status, data, _ = self._request(port, "POST", "/api/action", {"action": "start"})
        self.assertEqual(400, status)
        self.assertIn("缺少运行文件", json.loads(data)["error"])

    def test_index_and_web_control_are_served(self) -> None:
        port = self._serve()
        status, data, _ = self._request(port, "GET", "/index.html")
        self.assertEqual(200, status)
        self.assertIn(b"transmission", data)

        status, data, _ = self._request(port, "GET", "/")
        self.assertEqual(200, status)
        self.assertIn(b"transmission", data)

        status, data, _ = self._request(port, "GET", "/twc/index.html")
        self.assertEqual(200, status, data)
        self.assertIn(b"web-control", data)

    def test_layout_is_one_level_below_rpc(self) -> None:
        """transmission-web-control 用相对 `../rpc` 访问 RPC，目录层级不能变。"""
        port = self._serve()
        module = self.module
        status, _, _ = self._request(port, "GET", "/twc/index.html")
        self.assertEqual(200, status)
        self.assertTrue((module.WEB_DIR / "twc" / "index.html").is_file())
        self.assertEqual("twc", module.WEB_CONTROL_DIRNAME)

    def test_static_traversal_is_forbidden(self) -> None:
        port = self._serve()
        status, data, _ = self._request(port, "GET", "/../server.py")
        self.assertIn(status, (403, 404))
        self.assertFalse(json.loads(data)["ok"])

    def test_unknown_path_returns_404(self) -> None:
        port = self._serve()
        status, data, _ = self._request(port, "GET", "/api/nope")
        self.assertEqual(404, status)
        self.assertFalse(json.loads(data)["ok"])

    def test_rpc_proxy_passes_through_session_challenge(self) -> None:
        port = self._serve()
        module = self.module
        captured: dict = {}

        class FakeResponse:
            status = 409

            def read(self) -> bytes:
                return b"<h1>409 Conflict</h1>"

            def getheader(self, name):
                if name.lower() == "x-transmission-session-id":
                    return "session-42"
                if name.lower() == "content-type":
                    return "text/html"
                return None

        class FakeConnection:
            def __init__(self, host, conn_port, timeout=None):
                captured["host"] = host
                captured["port"] = conn_port

            def request(self, method, path, body=None, headers=None):
                captured["method"] = method
                captured["path"] = path
                captured["headers"] = headers or {}
                captured["body"] = body

            def getresponse(self):
                return FakeResponse()

            def close(self):
                captured["closed"] = True

        with mock.patch.object(module, "HTTPConnection", FakeConnection):
            status, data, headers = self._request(
                port, "POST", "/rpc",
                {"method": "session-stats"},
                {"X-Transmission-Session-Id": "incoming"},
            )

        self.assertEqual(409, status)
        self.assertIn("session-42", headers.get("X-Transmission-Session-Id", ""))
        self.assertEqual("POST", captured["method"])
        self.assertEqual("/transmission/rpc", captured["path"])
        self.assertEqual("incoming", captured["headers"]["X-Transmission-Session-Id"])
        self.assertEqual(module.RPC_PORT, captured["port"])
        self.assertEqual("127.0.0.1", captured["host"])
        self.assertTrue(captured["closed"])
        self.assertIn(b"409", data)
        # 所有响应路径都必须带 Content-Length，否则客户端会一直等。
        self.assertEqual(str(len(data)), headers.get("Content-Length"))

    def test_rpc_proxy_forwards_raw_body_verbatim(self) -> None:
        """RPC 是透传：正文原样转发，而且只能从连接里读一次。

        回归用例——do_POST 曾经先把正文按 JSON 解析、再让 _proxy_rpc 去读同一段
        rfile；第二次读会把处理器阻塞住，客户端最后在 getresponse() 上报错。
        """
        port = self._serve()
        module = self.module
        captured: dict = {}

        class FakeResponse:
            status = 200

            def read(self) -> bytes:
                return b'{"result":"success"}'

            def getheader(self, name):
                if name.lower() == "content-type":
                    return "application/json"
                return None

        class FakeConnection:
            def __init__(self, *args, **kwargs):
                pass

            def request(self, method, path, body=None, headers=None):
                captured["method"] = method
                captured["path"] = path
                captured["body"] = body
                captured["headers"] = headers or {}

            def getresponse(self):
                return FakeResponse()

            def close(self):
                captured["closed"] = True

        raw = b"GET /transmission/rpc"     # 故意不是 JSON：透传路径不该解析它
        with mock.patch.object(module, "HTTPConnection", FakeConnection):
            connection = HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request(
                "POST", "/rpc", body=raw,
                headers={"Content-Type": "application/octet-stream"},
            )
            response = connection.getresponse()
            data = response.read()
            status = response.status
            headers = dict(response.getheaders())
            connection.close()

        self.assertEqual(200, status)
        self.assertEqual(b'{"result":"success"}', data)
        self.assertEqual(raw, captured["body"])
        self.assertEqual("/transmission/rpc", captured["path"])
        self.assertEqual(str(len(data)), headers.get("Content-Length"))

    def test_rpc_proxy_reports_daemon_down(self) -> None:
        port = self._serve()
        module = self.module

        class BoomConnection:
            def __init__(self, *args, **kwargs):
                raise ConnectionRefusedError("nope")

        with mock.patch.object(module, "HTTPConnection", BoomConnection):
            status, data, headers = self._request(
                port, "POST", "/rpc", {"method": "session-stats"}
            )
        self.assertEqual(502, status)
        self.assertFalse(json.loads(data)["ok"])
        # 502 分支同样要带 Content-Length（之前的报错就在这条路径上）。
        self.assertEqual(str(len(data)), headers.get("Content-Length"))

    def test_rpc_proxy_rejects_oversized_body(self) -> None:
        port = self._serve()
        module = self.module
        status, data, headers = self._request(
            port, "POST", "/rpc", {"method": "session-stats"},
            headers={"Content-Length": str(module.MAX_BODY_BYTES + 1)},
        )
        self.assertEqual(400, status)
        self.assertFalse(json.loads(data)["ok"])
        self.assertEqual(str(len(data)), headers.get("Content-Length"))

    def test_get_is_not_allowed_on_rpc(self) -> None:
        port = self._serve()
        status, data, _ = self._request(port, "GET", "/rpc")
        self.assertEqual(404, status)
        self.assertFalse(json.loads(data)["ok"])


if __name__ == "__main__":
    unittest.main()
