"""旧版本清理（prune_releases）与一键回滚（rollback）的回归测试。

本机（Windows）通常没有创建软链的权限，因此「current 软链」用项目里已有的
mock 方式模拟：`Path.symlink_to` 被替换成「真目录 + .symlink-target 标记」，
再配合一个假的 current 解析函数。逻辑（挑 current、挑上一版、原子替换后的
收尾步骤）都是真实执行的；真软链的解析另有一条 skipUnless 用例，在 Linux/NAS
上会真正跑起来。
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import shutil
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import server as server_module
import storelib
from server import ACTION_LOCK, StoreServer
from storelib import InstallManager, StoreError, current_release_target, release_name_parts, release_version


PROJECT = Path(__file__).resolve().parents[1]
CATALOG = PROJECT / "catalog"

# 版本目录名里 `<版本>-<时间戳>-<pid>`
OLD = "0.9.0-1700000000-100"
MID = "1.0.0-1700000100-100"
NEW = "1.1.0-1700000200-100"
NEWEST = "1.2.0-1700000300-100"


def _manifest(version: str = "1.0.0", package_id: str = "demo", ui_key: str = "demo") -> dict:
    return {
        "schemaVersion": 1,
        "id": package_id,
        "name": "演示应用",
        "version": version,
        "pluginId": 99999,
        "port": 19999,
        "paths": {"releaseRoot": f"/data/plugin/{ui_key}", "uiKey": ui_key, "iconName": f"{ui_key}.icon"},
        "service": f"{ui_key}.service",
        "nginx": f"{ui_key}.conf",
        "healthPath": "/healthz",
        "registry": {"frontend": {}, "info": {"desc": "测试用"}},
    }


def _remove(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _fake_symlink(self: Path, target, target_is_directory: bool = False) -> None:
    """模拟软链：建一个真目录，用标记文件记住指向（Windows 无软链权限时用）。"""
    self.parent.mkdir(parents=True, exist_ok=True)
    _remove(self)
    self.mkdir()
    (self / ".symlink-target").write_text(str(target), encoding="utf-8")


def _fake_current_target(release_root: Path) -> Path | None:
    marker = Path(release_root) / "current" / ".symlink-target"
    if not marker.is_file():
        return None
    return Path(marker.read_text(encoding="utf-8")).resolve()


@contextlib.contextmanager
def fake_links():
    """把软链创建与 current 解析都换成标记文件实现。"""
    with mock.patch.object(Path, "symlink_to", _fake_symlink), mock.patch(
        "storelib.current_release_target", _fake_current_target
    ):
        yield


def _set_current(release_root: Path, target: Path) -> None:
    """按 fake_links() 的约定设置 current 指向。"""
    current = Path(release_root) / "current"
    _remove(current)
    _fake_symlink(current, target)


def _current_target(release_root: Path) -> Path | None:
    """走 storelib 模块里的名字，才能吃到 fake_links() 的补丁。"""
    return storelib.current_release_target(release_root)


def symlinks_supported() -> bool:
    try:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "target").mkdir()
            (root / "link").symlink_to(root / "target")
        return True
    except (OSError, NotImplementedError):
        return False


SYMLINKS = symlinks_supported()


class PruneReleasesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.manager = InstallManager(
            CATALOG, None, "u_test", root=self.root, execute_system=False, remote_apps=False
        )
        self.release_root = self.root / "data/plugin/demo"
        self.releases_root = self.release_root / "releases"
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(fake_links())

    def tearDown(self) -> None:
        self.stack.close()
        self.temporary.cleanup()

    def _make_releases(self, names: list[str], *, current: str | None = None, package_id: str = "demo") -> None:
        self.releases_root.mkdir(parents=True, exist_ok=True)
        for name in names:
            directory = self.releases_root / name
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "payload.bin").write_bytes(b"x" * 512)
        state_dir = self.root / "data/plugin/community-store/state"
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / f"{package_id}.json").write_text(
            json.dumps(
                {
                    "managed": True,
                    "installedAt": 1700000000,
                    "manifest": _manifest(
                        (release_version(current) if current else "1.0.0"),
                        package_id=package_id,
                        ui_key=package_id,
                    ),
                    "release": str(self.releases_root / current) if current else "",
                }
            ),
            encoding="utf-8",
        )
        if current:
            _set_current(self.release_root, (self.releases_root / current).resolve())

    def test_prune_keeps_current_and_previous_only(self) -> None:
        self._make_releases([OLD, MID, NEW, NEWEST], current=NEWEST)
        result = self.manager.prune_releases("demo", keep=2)

        self.assertEqual([NEWEST, NEW], result["kept"])
        self.assertEqual(sorted([OLD, MID]), sorted(result["removed"]))
        self.assertEqual([], result["skipped"])
        self.assertEqual([], result["errors"])
        self.assertGreater(result["freedBytes"], 0)
        # releases/ 里只剩下 current 与上一版（current 软链在 release 根目录，不在里面）
        self.assertEqual(sorted([NEW, NEWEST]), sorted(item.name for item in self.releases_root.iterdir()))
        self.assertTrue((self.releases_root / NEWEST).is_dir())
        self.assertTrue((self.releases_root / NEW).is_dir())
        self.assertFalse((self.releases_root / OLD).exists())
        self.assertFalse((self.releases_root / MID).exists())

    def test_prune_never_removes_current_version(self) -> None:
        """current 指向最旧版本时也必须保留它，其余按最新优先补足。"""
        self._make_releases([OLD, MID, NEW, NEWEST], current=OLD)
        result = self.manager.prune_releases("demo", keep=2)

        self.assertIn(OLD, result["kept"])
        self.assertNotIn(OLD, result["removed"])
        self.assertTrue((self.releases_root / OLD).is_dir())
        # kept 按最新优先：current(最旧) 之外再留最新的一个
        self.assertEqual([NEWEST, OLD], result["kept"])
        self.assertEqual(sorted([MID, NEW]), sorted(result["removed"]))

    def test_prune_skips_directories_not_created_by_this_store(self) -> None:
        self._make_releases([OLD, MID, NEWEST], current=NEWEST)
        legacy = self.releases_root / "0.1.0-beta"
        legacy.mkdir()
        (legacy / "keep.txt").write_text("历史遗留", encoding="utf-8")
        (self.releases_root / "notes.txt").write_text("不是目录", encoding="utf-8")

        result = self.manager.prune_releases("demo", keep=2)

        self.assertEqual(sorted(["0.1.0-beta", "notes.txt"]), sorted(result["skipped"]))
        self.assertTrue(legacy.is_dir())
        self.assertTrue((legacy / "keep.txt").is_file())
        self.assertEqual([OLD], result["removed"])
        self.assertEqual([NEWEST, MID], result["kept"])

    def test_prune_single_failure_does_not_stop_the_rest(self) -> None:
        self._make_releases([OLD, MID, NEW, NEWEST], current=NEWEST)
        real_rmtree = shutil.rmtree

        def fake_rmtree(path, *args, **kwargs):
            if Path(path).name == MID:
                raise OSError("拒绝访问")
            return real_rmtree(path, *args, **kwargs)

        with mock.patch("storelib.shutil.rmtree", side_effect=fake_rmtree):
            result = self.manager.prune_releases("demo", keep=2)

        self.assertEqual([OLD], result["removed"])
        self.assertEqual(1, len(result["errors"]))
        self.assertIn(MID, result["errors"][0])
        self.assertTrue((self.releases_root / MID).is_dir())
        self.assertFalse((self.releases_root / OLD).exists())

    def test_prune_without_state_is_rejected(self) -> None:
        with self.assertRaises(StoreError) as caught:
            self.manager.prune_releases("ghost")
        self.assertIn("不是由本商店安装的", str(caught.exception))

    def test_prune_requires_positive_keep(self) -> None:
        self._make_releases([OLD, NEWEST], current=NEWEST)
        with self.assertRaises(StoreError):
            self.manager.prune_releases("demo", keep=0)

    def test_prune_keeps_single_release_when_no_current_link(self) -> None:
        """没有 current 软链时按 keep-1 保留（不会把仅剩的版本也删掉）。"""
        self._make_releases([OLD, MID, NEWEST], current=None)
        result = self.manager.prune_releases("demo", keep=2)
        self.assertEqual([NEWEST], result["kept"])
        self.assertEqual(sorted([OLD, MID]), sorted(result["removed"]))

    def test_prune_all_summarises_every_managed_plugin(self) -> None:
        self._make_releases([OLD, MID, NEWEST], current=NEWEST, package_id="demo")
        other_root = self.root / "data/plugin/other"
        other_releases = other_root / "releases"
        other_releases.mkdir(parents=True)
        for name in (OLD, NEWEST):
            (other_releases / name).mkdir()
            (other_releases / name / "payload.bin").write_bytes(b"y" * 256)
        state_dir = self.root / "data/plugin/community-store/state"
        (state_dir / "other.json").write_text(
            json.dumps(
                {
                    "managed": True,
                    "installedAt": 1700000000,
                    "manifest": _manifest(release_version(NEWEST), package_id="other", ui_key="other"),
                    "release": str(other_releases / NEWEST),
                }
            ),
            encoding="utf-8",
        )
        _set_current(other_root, (other_releases / NEWEST).resolve())

        summary = self.manager.prune_all(keep=2)

        self.assertTrue(summary["ok"])
        self.assertEqual(1, summary["removedCount"])
        self.assertGreater(summary["freedBytes"], 0)
        self.assertEqual([], summary["errors"])
        self.assertEqual(sorted(["demo", "other"]), sorted(summary["results"]))
        self.assertEqual([OLD], summary["results"]["demo"]["removed"])

    def test_prune_all_reports_failures_per_plugin(self) -> None:
        self._make_releases([OLD, MID, NEWEST], current=NEWEST, package_id="demo")

        def boom(package_id, keep=2):
            raise StoreError("state 文件损坏")

        with mock.patch.object(self.manager, "prune_releases", side_effect=boom):
            summary = self.manager.prune_all(keep=2)

        self.assertEqual(1, len(summary["errors"]))
        self.assertIn("demo: state 文件损坏", summary["errors"][0])
        self.assertEqual(0, summary["removedCount"])
        self.assertEqual({}, summary["results"])


class ReleaseNameParsingTests(unittest.TestCase):
    def test_release_name_parts(self) -> None:
        self.assertEqual(("1.2.0", 1700000300, 100), release_name_parts(NEWEST))
        self.assertEqual("0.1.0-rc1", release_version("0.1.0-rc1-1700000300-7"))
        # 历史遗留 / 商店自身的 self-update 目录都不算本商店创建的版本目录
        for name in ("0.1.0-beta", "0.2.26-self-1791213759", "current", "1.0.0", "bundles"):
            self.assertIsNone(release_name_parts(name), name)
            self.assertIsNone(release_version(name), name)


@unittest.skipUnless(SYMLINKS, "本机没有创建软链的权限")
class RealSymlinkTests(unittest.TestCase):
    """真软链下的 current 解析（Windows 无权限时跳过，NAS/Linux 上会跑）。"""

    def test_current_release_target_reads_real_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            release_root = Path(temporary) / "plugin"
            releases = release_root / "releases"
            target = releases / NEWEST
            target.mkdir(parents=True)
            (release_root / "current").symlink_to(target)
            self.assertEqual(target.resolve(), current_release_target(release_root))

    def test_current_release_target_returns_none_without_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            release_root = Path(temporary) / "plugin"
            release_root.mkdir(parents=True)
            self.assertIsNone(current_release_target(release_root))


class InstallPruneIntegrationTests(unittest.TestCase):
    """走真实 install()（软链用 mock）验证「更新成功后自动清理」。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.apps_json = self.root / "apps.json"
        self.bundles = self.root / "apps"
        self.bundles.mkdir(parents=True)
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(fake_links())
        self.manager = InstallManager(
            CATALOG,
            None,
            "u_test",
            root=self.root,
            execute_system=False,
            apps_json=self.apps_json,
            apps_root=self.root,
            remote_apps=False,
        )
        self.release_root = self.root / "data/plugin/demo"
        self.releases_root = self.release_root / "releases"
        registry = self.root / "data/plugin/u_test.list"
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text("{}\n", encoding="utf-8")
        # install() 的 release 目录名精确到秒（<版本>-<时间戳>-<pid>），同一进程同一秒
        # 内两次安装会撞名。测试里用自增时钟代替真实时间，稳定且不用 sleep。
        self.clock = 1700000000
        self.stack.enter_context(mock.patch("storelib.time.time", side_effect=self._tick))

    def _tick(self) -> int:
        self.clock += 1
        return self.clock

    def tearDown(self) -> None:
        self.stack.close()
        self.temporary.cleanup()

    def _publish(self, version: str) -> None:
        """把某个版本做成 bundle 并写进（本地优先的）apps.json。"""
        stage = self.root / f"stage-{version}"
        (stage / "runtime").mkdir(parents=True)
        (stage / "runtime" / "server.py").write_text(f"# demo {version}\n", encoding="utf-8")
        (stage / "ui").mkdir()
        (stage / "ui" / "index.html").write_text(f"<html>{version}</html>", encoding="utf-8")
        (stage / "icon").write_bytes(b"\x89PNG\r\n\x1a\n")
        (stage / "config").mkdir()
        (stage / "config" / "demo.service").write_text("[Unit]\nDescription=demo\n", encoding="utf-8")
        (stage / "config" / "demo.conf").write_text("location / {}\n", encoding="utf-8")
        (stage / "manifest.json").write_text(
            json.dumps(_manifest(version), ensure_ascii=False), encoding="utf-8"
        )
        bundle = self.bundles / f"demo-{version}.zip"
        with zipfile.ZipFile(bundle, "w") as archive:
            for path in sorted(stage.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(stage).as_posix())
        self.apps_json.write_text(
            json.dumps(
                {
                    "schemaVersion": 2,
                    "name": "小米智能存储应用商店",
                    "generatedAt": 1700000000,
                    "apps": [
                        {
                            "id": "demo",
                            "name": "演示应用",
                            "version": version,
                            "summary": "测试用",
                            "bundle": f"apps/demo-{version}.zip",
                            "sha256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
                            "size": bundle.stat().st_size,
                            "icon": "apps/icons/demo.png",
                            "channel": "stable",
                            "tags": ["tool"],
                            "author": "test",
                            "pluginId": 99999,
                            "port": 19999,
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def test_install_prunes_old_releases_and_reports_it(self) -> None:
        for version in ("1.0.0", "1.1.0", "1.2.0"):
            self._publish(version)
            result = self.manager.install("demo")
            self.assertTrue(result["ok"])
            self.assertEqual(version, result["version"])
            self.assertIn("prune", result)
            self.assertEqual("demo", result["prune"]["packageId"])

        # 第三次安装后：current(1.2.0) + 1.1.0 保留，1.0.0 被清理
        last = result["prune"]
        self.assertEqual(1, len(last["removed"]))
        self.assertTrue(last["removed"][0].startswith("1.0.0-"))
        self.assertEqual(2, len(list(self.releases_root.iterdir())))
        self.assertEqual(
            {"version": "1.2.0", "previousVersion": "1.1.0", "canRollback": True},
            self.manager.installed()["demo"],
        )

    def test_install_calls_prune_with_keep_two(self) -> None:
        self._publish("1.0.0")
        with mock.patch.object(
            self.manager, "prune_releases", return_value={"packageId": "demo", "removed": []}
        ) as pruner:
            result = self.manager.install("demo")
        self.assertTrue(result["ok"])
        pruner.assert_called_once_with("demo", keep=2)
        self.assertEqual({"packageId": "demo", "removed": []}, result["prune"])

    def test_install_survives_prune_failure(self) -> None:
        self._publish("1.0.0")
        with mock.patch.object(
            self.manager, "prune_releases", side_effect=StoreError("磁盘只读")
        ):
            result = self.manager.install("demo")

        self.assertTrue(result["ok"])
        self.assertEqual("1.0.0", result["version"])
        self.assertEqual(["磁盘只读"], result["prune"]["errors"])
        self.assertTrue(self.manager.installed()["demo"]["version"] == "1.0.0")
        self.assertTrue((self.release_root / "current").is_dir())


class RollbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.apps_json = self.root / "apps.json"
        self.bundles = self.root / "apps"
        self.bundles.mkdir(parents=True)
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(fake_links())
        self.manager = InstallManager(
            CATALOG,
            None,
            "u_test",
            root=self.root,
            execute_system=False,
            apps_json=self.apps_json,
            apps_root=self.root,
            remote_apps=False,
        )
        self.release_root = self.root / "data/plugin/demo"
        self.releases_root = self.release_root / "releases"
        self.ui_target = self.root / "home/u_test/plugin/demo/src/ui"
        registry = self.root / "data/plugin/u_test.list"
        registry.parent.mkdir(parents=True, exist_ok=True)
        registry.write_text("{}\n", encoding="utf-8")
        # 同上：用自增时钟保证两次安装的 operation 目录名不撞车
        self.clock = 1700000000
        self.stack.enter_context(mock.patch("storelib.time.time", side_effect=self._tick))

    def _tick(self) -> int:
        self.clock += 1
        return self.clock

    def tearDown(self) -> None:
        self.stack.close()
        self.temporary.cleanup()

    def _publish(self, version: str, content: str) -> None:
        stage = self.root / f"stage-{version}"
        (stage / "runtime").mkdir(parents=True)
        (stage / "runtime" / "server.py").write_text(f"# demo {content}\n", encoding="utf-8")
        (stage / "ui").mkdir()
        (stage / "ui" / "index.html").write_text(f"<html>{content}</html>", encoding="utf-8")
        (stage / "icon").write_bytes(b"\x89PNG\r\n\x1a\n")
        (stage / "config").mkdir()
        (stage / "config" / "demo.service").write_text("[Unit]\nDescription=demo\n", encoding="utf-8")
        (stage / "config" / "demo.conf").write_text("location / {}\n", encoding="utf-8")
        (stage / "manifest.json").write_text(
            json.dumps(_manifest(version), ensure_ascii=False), encoding="utf-8"
        )
        bundle = self.bundles / f"demo-{version}.zip"
        with zipfile.ZipFile(bundle, "w") as archive:
            for path in sorted(stage.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(stage).as_posix())
        self.apps_json.write_text(
            json.dumps(
                {
                    "schemaVersion": 2,
                    "name": "小米智能存储应用商店",
                    "generatedAt": 1700000000,
                    "apps": [
                        {
                            "id": "demo",
                            "name": "演示应用",
                            "version": version,
                            "summary": "测试用",
                            "bundle": f"apps/demo-{version}.zip",
                            "sha256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
                            "size": bundle.stat().st_size,
                            "icon": "apps/icons/demo.png",
                            "channel": "stable",
                            "tags": ["tool"],
                            "author": "test",
                            "pluginId": 99999,
                            "port": 19999,
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def _install_two_versions(self) -> tuple[Path, Path]:
        self._publish("1.0.0", "v1.0.0")
        self.manager.install("demo")
        old_release = _current_target(self.release_root)
        self._publish("1.1.0", "v1.1.0")
        self.manager.install("demo")
        new_release = _current_target(self.release_root)
        assert old_release is not None and new_release is not None
        return old_release, new_release

    def test_rollback_switches_current_and_runs_install_post_steps(self) -> None:
        old_release, new_release = self._install_two_versions()
        self.assertNotEqual(old_release, new_release)
        self.assertIn("v1.1.0", (self.ui_target / "index.html").read_text(encoding="utf-8"))

        layout = mock.patch.object(
            self.manager, "_apply_native_layout", wraps=self.manager._apply_native_layout
        )
        runner = mock.patch.object(self.manager, "_run", wraps=self.manager._run)
        with layout as layout_mock, runner as run_mock:
            result = self.manager.rollback("demo")

        self.assertEqual(
            {"ok": True, "packageId": "demo", "version": "1.0.0", "previous": "1.1.0"}, result
        )
        # current 切到上一版（同一套原子替换：临时软链 + replace）
        self.assertEqual(old_release, _current_target(self.release_root))
        self.assertFalse((self.release_root / "current.community-store.tmp").exists())
        # 与安装成功后相同的后置步骤：补原生结构 + 重启服务 + reload nginx
        layout_mock.assert_called_once()
        commands = [call.args[0] for call in run_mock.call_args_list]
        self.assertIn(["systemctl", "restart", "demo.service"], commands)
        self.assertIn(["systemctl", "reload", "nginx"], commands)
        # UI 也回到旧版本，注册表与状态文件都写成回滚后的版本
        self.assertIn("v1.0.0", (self.ui_target / "index.html").read_text(encoding="utf-8"))
        registry = json.loads((self.root / "data/plugin/u_test.list").read_text(encoding="utf-8"))
        self.assertEqual("1.0.0", registry["demo"]["info"]["version"])
        state = json.loads(
            (self.root / "data/plugin/community-store/state/demo.json").read_text(encoding="utf-8")
        )
        self.assertEqual("1.0.0", state["manifest"]["version"])
        self.assertEqual(str(old_release), str(Path(state["release"]).resolve()))

    def test_prune_and_rollback_leave_user_data_alone(self) -> None:
        old_release, _new_release = self._install_two_versions()
        data_dir = self.release_root / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        marker = data_dir / "keep.txt"
        marker.write_text("用户数据", encoding="utf-8")
        lib_dir = self.release_root / "lib"
        lib_dir.mkdir(exist_ok=True)
        (lib_dir / "extra.py").write_text("# 插件额外文件\n", encoding="utf-8")

        self.manager.prune_releases("demo", keep=2)
        self.manager.rollback("demo")

        self.assertEqual("用户数据", marker.read_text(encoding="utf-8"))
        self.assertTrue((lib_dir / "extra.py").is_file())
        self.assertEqual(old_release, _current_target(self.release_root))

    def test_installed_can_rollback_follows_current_version(self) -> None:
        old_release, new_release = self._install_two_versions()
        self.assertEqual(
            {"version": "1.1.0", "previousVersion": "1.0.0", "canRollback": True},
            self.manager.installed()["demo"],
        )

        self.manager.rollback("demo")
        self.assertEqual(
            {"version": "1.0.0", "previousVersion": "1.1.0", "canRollback": True},
            self.manager.installed()["demo"],
        )
        # 再回滚一次回到最新版（releases 里除 current 外最新的那个）
        again = self.manager.rollback("demo")
        self.assertEqual("1.1.0", again["version"])
        self.assertEqual("1.0.0", again["previous"])
        self.assertEqual(new_release, _current_target(self.release_root))

    def test_rollback_without_previous_version_is_rejected(self) -> None:
        self._publish("1.0.0", "v1.0.0")
        self.manager.install("demo")
        with self.assertRaises(StoreError) as caught:
            self.manager.rollback("demo")
        self.assertIn("没有可回滚的上一版本", str(caught.exception))

    def test_rollback_uninstalled_package_is_rejected(self) -> None:
        with self.assertRaises(StoreError) as caught:
            self.manager.rollback("ghost")
        self.assertIn("不是由本商店安装的", str(caught.exception))

    def test_rollback_refuses_the_store_itself(self) -> None:
        state_dir = self.root / "data/plugin/community-store/state"
        state_dir.mkdir(parents=True, exist_ok=True)
        store_root = self.root / "data/plugin/community-store"
        release = store_root / "releases" / NEW
        release.mkdir(parents=True)
        (state_dir / "communitystore.json").write_text(
            json.dumps(
                {
                    "managed": True,
                    "installedAt": 1700000000,
                    "manifest": _manifest("1.1.0", package_id="communitystore", ui_key="communitystore"),
                    "release": str(release),
                }
            ),
            encoding="utf-8",
        )
        with self.assertRaises(StoreError) as caught:
            self.manager.rollback("communitystore")
        self.assertIn("自身", str(caught.exception))
        # 清理也不碰商店自身（这里直接确认状态没变）
        self.assertTrue(release.is_dir())

    def test_rollback_ignores_legacy_directories(self) -> None:
        old_release, new_release = self._install_two_versions()
        legacy = self.releases_root / "9.9.9-legacy"
        legacy.mkdir()
        (legacy / "keep.txt").write_text("历史遗留", encoding="utf-8")

        result = self.manager.rollback("demo")

        self.assertEqual("1.0.0", result["version"])
        self.assertEqual(old_release, _current_target(self.release_root))
        self.assertTrue(legacy.is_dir())
        self.assertTrue((legacy / "keep.txt").is_file())


class _StubManager:
    """HTTP 层用的替身：只记录调用并返回结构化的假结果。"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.rollback_error: str | None = None

    def install(self, package_id: str):
        self.calls.append(("install", package_id))
        return {"ok": True, "id": package_id, "version": "1.0.0"}

    def uninstall(self, package_id: str):
        self.calls.append(("uninstall", package_id))
        return {"ok": True, "id": package_id, "dataPreserved": True}

    def rollback(self, package_id: str):
        self.calls.append(("rollback", package_id))
        if self.rollback_error:
            raise StoreError(self.rollback_error)
        return {"ok": True, "packageId": package_id, "version": "1.0.0", "previous": "1.1.0"}

    def prune_releases(self, package_id: str, keep: int = 2):
        self.calls.append(("prune_releases", package_id, keep))
        return {
            "packageId": package_id,
            "removed": ["0.9.0-1700000000-100"],
            "kept": [],
            "skipped": [],
            "errors": [],
            "freedBytes": 1024,
        }

    def prune_all(self, keep: int = 2):
        self.calls.append(("prune_all", keep))
        return {
            "ok": True,
            "removedCount": 2,
            "freedBytes": 5 * 1024 * 1024,
            "errors": ["other: 拒绝访问"],
            "results": {},
        }

    def inventory(self):
        return {"demo": {"version": "1.1.0", "managed": True}}

    def installed(self):
        return {"demo": {"version": "1.1.0", "previousVersion": "1.0.0", "canRollback": True}}


class PruneRollbackHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = _StubManager()
        self.server = StoreServer(("127.0.0.1", 0), False, self.manager, "a" * 32)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method: str, path: str, body: dict | None = None, headers: dict | None = None):
        import http.client

        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        encoded = json.dumps(body).encode("utf-8") if body is not None else None
        request_headers = dict(headers or {})
        if encoded is not None:
            request_headers["Content-Type"] = "application/json"
            request_headers["Content-Length"] = str(len(encoded))
        connection.request(method, path, body=encoded, headers=request_headers)
        response = connection.getresponse()
        payload = response.read()
        connection.close()
        return response.status, json.loads(payload.decode("utf-8"))

    def session(self) -> tuple[str, str]:
        import http.client

        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request("GET", "/index.html", headers={"X-Xiaomi-Client-Verify": "SUCCESS"})
        response = connection.getresponse()
        html = response.read().decode("utf-8")
        connection.close()
        session_id = html.split('<meta name="session-token" content="', 1)[1].split('"', 1)[0]
        key = hashlib.sha256(self.server.admin_token.encode("utf-8")).digest()
        csrf = hmac.new(key, f"csrf:{session_id}".encode("ascii"), hashlib.sha256).hexdigest()
        return session_id, csrf

    def post(self, path: str, body: dict) -> tuple[int, dict]:
        session_id, csrf = self.session()
        return self.request(
            "POST",
            path,
            body,
            {"X-Community-Session": session_id, "X-CSRF-Token": csrf},
        )

    def test_rollback_endpoint_calls_manager(self) -> None:
        status, payload = self.post("/api/rollback", {"id": "demo"})
        self.assertEqual(200, status)
        self.assertEqual(
            {"ok": True, "packageId": "demo", "version": "1.0.0", "previous": "1.1.0"}, payload
        )
        self.assertEqual([("rollback", "demo")], self.manager.calls)

    def test_rollback_endpoint_returns_backend_error_verbatim(self) -> None:
        self.manager.rollback_error = "没有可回滚的上一版本"
        status, payload = self.post("/api/rollback", {"id": "demo"})
        self.assertEqual(400, status)
        self.assertEqual("没有可回滚的上一版本", payload["error"])

    def test_prune_endpoint_without_id_prunes_every_plugin(self) -> None:
        status, payload = self.post("/api/prune", {})
        self.assertEqual(200, status)
        self.assertEqual(2, payload["removedCount"])
        self.assertEqual(["other: 拒绝访问"], payload["errors"])
        self.assertEqual([("prune_all", 2)], self.manager.calls)

    def test_prune_endpoint_with_id_prunes_one_plugin(self) -> None:
        status, payload = self.post("/api/prune", {"id": "demo"})
        self.assertEqual(200, status)
        self.assertTrue(payload["ok"])
        self.assertEqual([("prune_releases", "demo", 2)], self.manager.calls)

    def test_rollback_and_prune_are_rejected_while_busy(self) -> None:
        self.assertTrue(ACTION_LOCK.acquire(blocking=False))
        try:
            status, payload = self.post("/api/rollback", {"id": "demo"})
        finally:
            ACTION_LOCK.release()
        self.assertEqual(400, status)
        self.assertEqual("另一个安装任务正在执行", payload["error"])

        self.assertTrue(ACTION_LOCK.acquire(blocking=False))
        try:
            status, payload = self.post("/api/prune", {})
        finally:
            ACTION_LOCK.release()
        self.assertEqual(400, status)
        self.assertEqual("另一个安装任务正在执行", payload["error"])
        self.assertEqual([], self.manager.calls)

    def test_post_actions_require_csrf(self) -> None:
        status, payload = self.request("POST", "/api/rollback", {"id": "demo"})
        self.assertEqual(401, status)
        self.assertFalse(payload["ok"])

    def test_catalog_exposes_rollback_fields(self) -> None:
        catalog = {
            "schemaVersion": 2,
            "apps": [
                {
                    "id": "demo",
                    "name": "演示应用",
                    "version": "1.1.0",
                    "summary": "测试用",
                    "bundle": "apps/demo-1.1.0.zip",
                    "sha256": "a" * 64,
                    "icon": "apps/icons/demo.png",
                }
            ],
        }
        session_id, _ = self.session()
        with mock.patch("server.load_apps_catalog", return_value=catalog):
            status, payload = self.request(
                "GET", "/api/catalog", headers={"X-Community-Session": session_id}
            )
        self.assertEqual(200, status)
        package = payload["catalog"]["packages"][0]
        self.assertEqual("1.1.0", package["installedVersion"])
        self.assertEqual("1.0.0", package["previousVersion"])
        self.assertTrue(package["canRollback"])
        self.assertTrue(package["managed"])

    def test_catalog_marks_unrollbackable_plugin(self) -> None:
        class _NoRollback(_StubManager):
            def installed(self):
                return {"demo": {"version": "1.0.0", "previousVersion": None, "canRollback": False}}

        self.manager = _NoRollback()
        self.server.manager = self.manager
        catalog = {
            "schemaVersion": 2,
            "apps": [
                {
                    "id": "demo",
                    "name": "演示应用",
                    "version": "1.0.0",
                    "summary": "测试用",
                    "bundle": "apps/demo-1.0.0.zip",
                    "sha256": "a" * 64,
                    "icon": "apps/icons/demo.png",
                }
            ],
        }
        session_id, _ = self.session()
        with mock.patch("server.load_apps_catalog", return_value=catalog):
            _, payload = self.request(
                "GET", "/api/catalog", headers={"X-Community-Session": session_id}
            )
        package = payload["catalog"]["packages"][0]
        self.assertIsNone(package["previousVersion"])
        self.assertFalse(package["canRollback"])

    def test_dev_mode_does_not_touch_the_nas(self) -> None:
        dev = StoreServer(("127.0.0.1", 0), True, self.manager, "a" * 32)
        thread = threading.Thread(target=dev.serve_forever, daemon=True)
        thread.start()
        try:
            import http.client

            port = dev.server_address[1]
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            connection.request("GET", "/index.html")
            html = connection.getresponse().read().decode("utf-8")
            connection.close()
            session_id = html.split('<meta name="session-token" content="', 1)[1].split('"', 1)[0]
            key = hashlib.sha256(dev.admin_token.encode("utf-8")).digest()
            csrf = hmac.new(key, f"csrf:{session_id}".encode("ascii"), hashlib.sha256).hexdigest()
            for path in ("/api/rollback", "/api/prune"):
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                encoded = json.dumps({"id": "demo"}).encode("utf-8")
                connection.request(
                    "POST",
                    path,
                    body=encoded,
                    headers={
                        "Content-Type": "application/json",
                        "Content-Length": str(len(encoded)),
                        "X-Community-Session": session_id,
                        "X-CSRF-Token": csrf,
                    },
                )
                response = connection.getresponse()
                payload = json.loads(response.read().decode("utf-8"))
                connection.close()
                self.assertEqual(409, response.status, path)
                self.assertEqual("本地预览模式不会修改 NAS", payload["error"])
            self.assertEqual([], self.manager.calls)
        finally:
            dev.shutdown()
            dev.server_close()
            thread.join(timeout=2)


class LocalCatalogPreferenceTests(unittest.TestCase):
    """显式传入 apps.json 时优先本地；不传时仍然走远程。"""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _document(self) -> dict:
        return {
            "schemaVersion": 2,
            "apps": [
                {
                    "id": "demo",
                    "name": "演示应用",
                    "version": "1.0.0",
                    "summary": "测试用",
                    "bundle": "apps/demo-1.0.0.zip",
                    "sha256": "b" * 64,
                    "icon": "apps/icons/demo.png",
                }
            ],
        }

    def test_local_apps_json_wins_when_configured(self) -> None:
        local = self.root / "apps.json"
        local.write_text(json.dumps(self._document()), encoding="utf-8")
        manager = InstallManager(
            CATALOG, None, "u_test", root=self.root, execute_system=False, apps_json=local
        )
        with mock.patch("storelib.load_apps_catalog", side_effect=StoreError("网络不可用")) as remote:
            entry = manager._package_entry("demo")
        self.assertEqual("demo", entry["id"])
        remote.assert_not_called()

    def test_remote_is_used_without_apps_json(self) -> None:
        manager = InstallManager(CATALOG, None, "u_test", root=self.root, execute_system=False)
        with mock.patch("storelib.load_apps_catalog", return_value=self._document()) as remote:
            entry = manager._package_entry("demo")
        self.assertEqual("demo", entry["id"])
        remote.assert_called_once()

    def test_broken_local_apps_json_falls_back_to_remote(self) -> None:
        local = self.root / "apps.json"
        local.write_text("{ not json", encoding="utf-8")
        manager = InstallManager(
            CATALOG, None, "u_test", root=self.root, execute_system=False, apps_json=local
        )
        with mock.patch("storelib.load_apps_catalog", return_value=self._document()) as remote:
            entry = manager._package_entry("demo")
        self.assertEqual("demo", entry["id"])
        remote.assert_called_once()


if __name__ == "__main__":
    unittest.main()
