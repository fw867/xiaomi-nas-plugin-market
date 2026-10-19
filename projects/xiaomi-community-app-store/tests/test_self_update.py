#!/usr/bin/env python3
"""自升级落盘自测（回归：2026-10-19 自升级漏 `keepalive.py` 导致整站空白）。

这些用例只碰临时目录，不联网、不重启。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import storelib  # noqa: E402


def make_project(root: Path, *, keepalive: bool = True,
                 server_imports_keepalive: bool = True,
                 web: bool = True) -> Path:
    project = root / "project"
    project.mkdir(exist_ok=True)
    head = "from keepalive import start_background as start_keepalive\n" \
        if server_imports_keepalive else ""
    (project / "server.py").write_text(head + "print('server')\n", encoding="utf-8")
    (project / "storelib.py").write_text("# storelib\n", encoding="utf-8")
    if keepalive:
        (project / "keepalive.py").write_text("# keepalive\n", encoding="utf-8")
    if web:
        (project / "web").mkdir(exist_ok=True)
        (project / "web" / "index.html").write_text("<html></html>", encoding="utf-8")
    return project


class MaterializeReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="store-selfupd-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.release = self.root / "releases" / "9.9.9-self-1"

    def test_copies_keepalive(self) -> None:
        project = make_project(self.root)
        storelib.materialize_release(project, self.release, "9.9.9")
        self.assertTrue((self.release / "keepalive.py").is_file())
        self.assertEqual((self.release / "VERSION").read_text(encoding="utf-8").strip(), "9.9.9")

    def test_aborts_and_cleans_up_when_keepalive_missing(self) -> None:
        """server.py 依赖 keepalive 但包里没有 ⇒ 必须中止且不留半成品。"""
        project = make_project(self.root, keepalive=False)
        with self.assertRaises(storelib.StoreError) as ctx:
            storelib.materialize_release(project, self.release, "9.9.9")
        self.assertIn("keepalive", str(ctx.exception))
        self.assertFalse(self.release.exists(), "半成品必须被删掉")

    def test_aborts_when_web_index_missing(self) -> None:
        project = make_project(self.root, web=False)
        with self.assertRaises(storelib.StoreError) as ctx:
            storelib.materialize_release(project, self.release, "9.9.9")
        self.assertIn("web/index.html", str(ctx.exception))
        self.assertFalse(self.release.exists())

    def test_keepalive_is_always_required(self) -> None:
        """即使 server.py 没显式引用，keepalive.py 也在硬性必检清单里。"""
        project = make_project(self.root, keepalive=False, server_imports_keepalive=False)
        with self.assertRaises(storelib.StoreError) as ctx:
            storelib.materialize_release(project, self.release, "9.9.9")
        self.assertIn("keepalive.py", str(ctx.exception))
        self.assertFalse(self.release.exists())

    def test_refuses_existing_release_dir(self) -> None:
        project = make_project(self.root)
        self.release.mkdir(parents=True)
        with self.assertRaises(FileExistsError):
            storelib.materialize_release(project, self.release, "9.9.9")

    def test_runtime_required_constant(self) -> None:
        for name in ("server.py", "storelib.py", "keepalive.py", "web/index.html"):
            self.assertIn(name, storelib.RUNTIME_REQUIRED)


class SelfUpdateGateTests(unittest.TestCase):
    """`/api/self-update` 入口硬闸：能力不足必须拒绝并提示改用 bundle。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="store-gate-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        # 让 fetch_* 一被调用就失败，确保"放行"用例也只走到网络之前
        self._calls: list[str] = []

    def test_new_storelib_passes_capability(self) -> None:
        capability = storelib.self_update_capability()
        self.assertTrue(capability["ok"], capability)
        self.assertTrue(capability["hasMaterializeRelease"])
        self.assertIn("keepalive.py", capability["runtimeRequired"])
        self.assertEqual(capability["problems"], [])

    def test_gate_blocks_when_materialize_release_absent(self) -> None:
        """模拟"旧 storelib"：没有 materialize_release ⇒ 自升级必须被拒绝。"""
        saved = storelib.materialize_release
        try:
            del storelib.materialize_release
            capability = storelib.self_update_capability()
            self.assertFalse(capability["ok"])
            self.assertFalse(capability["hasMaterializeRelease"])
            self.assertTrue(any("materialize_release" in p for p in capability["problems"]))

            with self.assertRaises(storelib.StoreError) as ctx:
                storelib.self_update_store("0.0.1", store_root=str(self.root))
            message = str(ctx.exception)
            self.assertIn("bundle", message)
            self.assertIn("当前商店版本较旧", message)
        finally:
            storelib.materialize_release = saved  # type: ignore[attr-defined]

    def test_gate_blocks_when_keepalive_not_in_required(self) -> None:
        saved = storelib.RUNTIME_REQUIRED
        try:
            storelib.RUNTIME_REQUIRED = ("server.py", "storelib.py", "web/index.html")
            capability = storelib.self_update_capability()
            self.assertFalse(capability["ok"])
            self.assertTrue(any("keepalive.py" in p for p in capability["problems"]))
            with self.assertRaises(storelib.StoreError) as ctx:
                storelib.self_update_store("0.0.1", store_root=str(self.root))
            self.assertIn("bundle", str(ctx.exception))
        finally:
            storelib.RUNTIME_REQUIRED = saved  # type: ignore[attr-defined]

    def test_gate_message_mentions_bundle_and_remedy(self) -> None:
        capability = storelib.self_update_capability()
        self.assertIn("bundle", capability["remedy"])

    def test_gate_does_not_touch_filesystem_when_blocked(self) -> None:
        saved = storelib.materialize_release
        try:
            del storelib.materialize_release
            with self.assertRaises(storelib.StoreError):
                storelib.self_update_store("0.0.1", store_root=str(self.root))
            self.assertFalse((self.root / "releases").exists(), "被拦下时不应创建任何 release")
        finally:
            storelib.materialize_release = saved  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()
