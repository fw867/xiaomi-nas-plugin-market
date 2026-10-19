#!/usr/bin/env python3
"""keepalive 模块单测：条目生成 / 注入幂等 / 不碰厂商条目 / 备份还原。"""

from __future__ import annotations

import json
import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import keepalive  # noqa: E402


VENDOR_KEYS = ("central", "mediacenter", "ipc", "baidupan", "xunlei")


class HostRootCase(unittest.TestCase):
    """把绝对路径重定向到临时目录，避免测试碰真实 /data。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="keepalive-test-")
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self._saved = {
            name: os.environ.get(name)
            for name in ("XCM_HOST_ROOT", "XCM_SERVER_LIST", "XCM_BACKUP_ROOT", "XCM_KEEPALIVE_LOG")
        }
        os.environ["XCM_HOST_ROOT"] = str(self.root)
        for name in ("XCM_SERVER_LIST", "XCM_BACKUP_ROOT", "XCM_KEEPALIVE_LOG"):
            os.environ.pop(name, None)

    def tearDown(self) -> None:
        for name, value in self._saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    # -- 辅助 ---------------------------------------------------------------

    def make_plugin(self, item: keepalive.Plugin, version: str = "0.1.2",
                    with_info: bool = True) -> Path:
        directory = keepalive.plugin_root() / item.directory
        (directory / "current").mkdir(parents=True, exist_ok=True)
        if with_info:
            (directory / "current" / "INFO").write_text(
                json.dumps({"plugin": item.key, "name": item.title,
                            "version": version, "desc": "test"}, ensure_ascii=False),
                encoding="utf-8",
            )
        (directory / "current" / "server.py").write_text("# stub\n", encoding="utf-8")
        # 用户数据目录：备份必须排除
        (directory / "data").mkdir(parents=True, exist_ok=True)
        (directory / "data" / "secret.bin").write_bytes(b"\x00user-data\x00")
        return directory

    def write_registry(self, user_id: str, keys: dict[str, dict]) -> Path:
        path = keepalive.registry_path(user_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(keys, ensure_ascii=False), encoding="utf-8")
        return path


class ServerEntryTests(HostRootCase):
    def test_entry_from_info(self) -> None:
        item = keepalive.PLUGINS[0]
        self.make_plugin(item, version="9.9.9")
        entry = keepalive.build_server_entry("", item)
        self.assertIn("info", entry)
        self.assertIn("resource", entry)
        self.assertEqual(entry["info"]["plugin"], item.key)
        self.assertEqual(entry["info"]["version"], "9.9.9")
        self.assertEqual(entry["info"]["port"], str(item.port))
        self.assertFalse(entry["info"]["system"])

    def test_entry_prefers_registry(self) -> None:
        item = keepalive.PLUGINS[0]
        self.make_plugin(item, version="0.0.1")
        self.write_registry("u1", {
            item.key: {"info": {"id": 11002, "version": "0.1.2", "name": "插件市场"},
                       "frontend": {"desc": "商店"}},
        })
        entry = keepalive.build_server_entry("u1", item)
        self.assertEqual(entry["info"]["version"], "0.1.2")
        self.assertEqual(entry["info"]["id"], 11002)
        self.assertEqual(entry["info"]["desc"], "商店")

    def test_entry_without_any_source_still_valid(self) -> None:
        item = keepalive.PLUGINS[-1]
        entry = keepalive.build_server_entry("", item)
        self.assertEqual(entry["info"]["version"], "0.0.0")
        self.assertEqual(entry["info"]["name"], item.title)


class InjectionTests(HostRootCase):
    def test_inject_adds_missing_and_is_idempotent(self) -> None:
        for item in keepalive.PLUGINS:
            self.make_plugin(item)
        document = {"code": 0, "message": "", "data": {}}
        entries = {item.key: keepalive.build_server_entry("", item) for item in keepalive.PLUGINS}

        added = keepalive.inject_server_entries(document, entries)
        self.assertEqual(set(added), set(entries))
        self.assertEqual(len(document["data"]), len(entries))

        again = keepalive.inject_server_entries(document, entries)
        self.assertEqual(again, [], "第二次注入应为空（幂等）")

    def test_vendor_entries_untouched(self) -> None:
        document = {"code": 0, "message": "", "data": {
            key: {"info": {"plugin": key, "vendor": True}, "resource": {}}
            for key in VENDOR_KEYS
        }}
        before = json.dumps(document["data"][VENDOR_KEYS[0]], sort_keys=True)
        entries = {item.key: keepalive.build_server_entry("", item) for item in keepalive.PLUGINS}
        keepalive.inject_server_entries(document, entries)

        self.assertEqual(json.dumps(document["data"][VENDOR_KEYS[0]], sort_keys=True), before)
        for key in VENDOR_KEYS:
            self.assertIn(key, document["data"])
        self.assertEqual(len(document["data"]), len(VENDOR_KEYS) + len(entries))

    def test_heal_server_list_creates_file(self) -> None:
        for item in keepalive.PLUGINS:
            self.make_plugin(item)
        result = keepalive.heal_server_list("u1")
        self.assertEqual(len(result["added"]), len(keepalive.PLUGINS))
        saved = json.loads(keepalive.server_list_path().read_text(encoding="utf-8"))
        self.assertEqual(saved["code"], 0)
        for item in keepalive.PLUGINS:
            self.assertIn(item.key, saved["data"])

    def test_heal_server_list_keeps_existing_entry(self) -> None:
        item = keepalive.PLUGINS[0]
        self.make_plugin(item)
        path = keepalive.server_list_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "code": 0, "message": "", "data": {item.key: {"info": {"vendor": "cloud"}}},
        }), encoding="utf-8")

        result = keepalive.heal_server_list("u1")
        self.assertNotIn(item.key, result["added"])
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["data"][item.key]["info"]["vendor"], "cloud")

    def test_placeholder_version_is_repaired(self) -> None:
        """早期注入留下 version=0.0.0 的占位条目，应被修成真实版本。"""
        item = keepalive.PLUGINS[0]
        self.make_plugin(item)
        self.write_registry("3943892", {item.key: {"info": {"version": "0.1.2"}}})
        path = keepalive.server_list_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "code": 0, "message": "",
            "data": {item.key: {"info": {"version": "0.0.0", "plugin": item.key}, "resource": {}}},
        }), encoding="utf-8")

        result = keepalive.heal_server_list("3943892")
        self.assertIn(item.key, result["repaired"])
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["data"][item.key]["info"]["version"], "0.1.2")

    def test_vendor_placeholder_is_never_repaired(self) -> None:
        """厂商条目即使 version 是 0.0.0 也不能动。"""
        document = {"code": 0, "data": {"central": {"info": {"version": "0.0.0"}}}}
        entries = {item.key: keepalive.build_server_entry("", item) for item in keepalive.PLUGINS}
        repaired = keepalive.repair_placeholder_versions(document, entries)
        self.assertEqual(repaired, [])
        self.assertEqual(document["data"]["central"]["info"]["version"], "0.0.0")


class BackupTests(HostRootCase):
    def test_backup_excludes_user_data(self) -> None:
        item = keepalive.PLUGINS[0]
        self.make_plugin(item)
        outcome = keepalive.backup_plugin(item)
        self.assertTrue(outcome["ok"], outcome)
        archive = keepalive.backup_root() / f"{item.directory}.tar.gz"
        self.assertTrue(archive.is_file())
        with tarfile.open(archive, "r:gz") as bundle:
            names = bundle.getnames()
        self.assertTrue(any("server.py" in name for name in names))
        self.assertFalse(any("secret.bin" in name for name in names), "用户数据不能进备份")

    def test_backup_then_restore(self) -> None:
        item = keepalive.PLUGINS[0]
        directory = self.make_plugin(item)
        self.assertTrue(keepalive.backup_plugin(item)["ok"])

        # 模拟 plugincenter 把目录整个删掉
        import shutil as _shutil
        _shutil.rmtree(directory)
        self.assertFalse(directory.is_dir())

        outcome = keepalive.restore_plugin(item)
        self.assertTrue(outcome["ok"], outcome)
        self.assertTrue((directory / "current" / "server.py").is_file())

    def test_restore_without_backup_reports_reason(self) -> None:
        item = keepalive.PLUGINS[0]
        outcome = keepalive.restore_plugin(item)
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["reason"], "no backup")

    def test_backup_restore_preserves_absolute_symlink(self) -> None:
        """真实插件目录里 `current` 是**绝对路径软链**，还原必须保留它。

        这是真机踩到的坑：tarfile 的 filter="data" 会以
        「link to an absolute path」拒绝整份归档，导致还原永远失败。
        """
        item = keepalive.PLUGINS[0]
        directory = keepalive.plugin_root() / item.directory
        release = directory / "releases" / "0.1.2"
        release.mkdir(parents=True)
        (release / "server.py").write_text("# stub\n", encoding="utf-8")
        try:
            (directory / "current").symlink_to(release)  # 绝对路径
        except OSError as error:  # Windows 未开开发者模式时无 symlink 特权（WinError 1314）
            self.skipTest(f"当前平台无法创建符号链接：{error}")

        self.assertTrue(keepalive.backup_plugin(item)["ok"])

        import shutil as _shutil
        _shutil.rmtree(directory)
        outcome = keepalive.restore_plugin(item)
        self.assertTrue(outcome["ok"], outcome)
        self.assertTrue((directory / "current").is_symlink())
        self.assertTrue((directory / "current" / "server.py").is_file())


class RegistryPathTests(HostRootCase):
    """注册表文件名带 `u` 前缀（`u3943892.list`），CLI 常传纯数字。"""

    def test_numeric_user_id_maps_to_u_prefixed_file(self) -> None:
        path = keepalive.registry_path("3943892")
        self.assertEqual(path.name, "u3943892.list")

    def test_already_prefixed_is_kept(self) -> None:
        self.assertEqual(keepalive.registry_path("u3943892").name, "u3943892.list")

    def test_entry_version_comes_from_prefixed_registry(self) -> None:
        item = keepalive.PLUGINS[0]
        self.make_plugin(item, with_info=False)
        self.write_registry("3943892", {item.key: {"info": {"version": "0.1.2", "port": "18119"}}})
        entry = keepalive.build_server_entry("3943892", item)
        self.assertEqual(entry["info"]["version"], "0.1.2", "必须从 u<uid>.list 读到版本")


class CurrentLinkTests(HostRootCase):
    """`current` 是服务 ExecStart 的落点；对账可能只删它、留目录。"""

    def _make_with_release(self, item: keepalive.Plugin, release: str = "0.1.2"):
        directory = keepalive.plugin_root() / item.directory
        target = directory / "releases" / release
        target.mkdir(parents=True)
        (target / "server.py").write_text("# stub\n", encoding="utf-8")
        return directory, target

    def test_relinks_when_current_missing(self) -> None:
        item = keepalive.PLUGINS[0]
        directory, target = self._make_with_release(item)
        self.assertFalse((directory / "current").exists())
        outcome = keepalive.ensure_current(item)
        if outcome["state"] == "relink-failed":
            self.skipTest(f"当前平台无法创建符号链接：{outcome.get('detail')}")
        self.assertEqual(outcome["state"], "relinked", outcome)
        self.assertTrue((directory / "current").exists())
        self.assertTrue((directory / "current" / "server.py").is_file())

    def test_keeps_existing_current(self) -> None:
        item = keepalive.PLUGINS[0]
        directory, target = self._make_with_release(item)
        try:
            (directory / "current").symlink_to(target)
        except OSError as error:
            self.skipTest(f"当前平台无法创建符号链接：{error}")
        outcome = keepalive.ensure_current(item)
        self.assertEqual(outcome["state"], "ok")

    def test_reports_missing_releases(self) -> None:
        item = keepalive.PLUGINS[0]
        (keepalive.plugin_root() / item.directory).mkdir(parents=True)
        outcome = keepalive.ensure_current(item)
        self.assertEqual(outcome["state"], "no-releases")


class HealOnceTests(HostRootCase):
    def test_heal_once_reports_missing_dirs(self) -> None:
        # 故意一个插件目录都不建 → 应该报 problems 而不是崩
        report = keepalive.heal_once("u1", do_restore=True)
        self.assertTrue(report.problems)
        self.assertTrue(all("no backup" in problem for problem in report.problems))

    def test_heal_once_injects_and_restores(self) -> None:
        item = keepalive.PLUGINS[0]
        self.make_plugin(item)
        self.assertTrue(keepalive.backup_plugin(item)["ok"])
        import shutil as _shutil
        _shutil.rmtree(keepalive.plugin_root() / item.directory)

        report = keepalive.heal_once("u1", do_restore=True)
        self.assertIn(item.key, report.restored)
        self.assertIn(item.key, report.server_list_added)
        self.assertTrue((keepalive.plugin_root() / item.directory).is_dir())


if __name__ == "__main__":
    unittest.main(verbosity=2)
