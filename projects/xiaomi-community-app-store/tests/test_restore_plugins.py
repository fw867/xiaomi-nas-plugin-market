#!/usr/bin/env python3
"""`restore-plugins.py` 的厂商条目合并单测。

覆盖用户实际遇到的回归：`plugincenter` 清空注册表后重建，厂商条目
（影视 / 中枢功能 / 监控存储 / 百度网盘 / 迅雷）必须仍在，且已有条目一字不改。
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "restore-plugins.py"

VENDOR_KEYS = ("mediacenter", "central", "ipc", "baidupan", "xunlei")


def load_module(baseline: Path):
    os.environ["VENDOR_BASELINE"] = str(baseline)
    os.environ["STORE_ROOT"] = str(baseline.parent / "store")
    spec = importlib.util.spec_from_file_location("restore_plugins_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def vendor_baseline_data() -> dict:
    out = {}
    for index, key in enumerate(VENDOR_KEYS):
        free = key in ("baidupan", "xunlei")
        out[key] = {
            "status": "free" if free else "running",
            "install": not free,
            "enable": not free,
            "info": {
                "plugin": key,
                "name": f"vendor-{key}",
                "id": 100 + index,
                "version": f"9.9.{index}",
                "system": not free,
                "type": "standard",
            },
            "resource": {"mpk": f"https://example.invalid/{key}.mpk"},
        }
    return out


class MergeVendorTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="restore-plugins-test-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.baseline = self.tmp / "vendor-entries.json"
        self.baseline.write_text(
            json.dumps(vendor_baseline_data(), ensure_ascii=False), encoding="utf-8"
        )
        self.module = load_module(self.baseline)

    def test_missing_vendor_entries_are_added(self) -> None:
        registry: dict = {}
        added = self.module.merge_vendor_entries(registry)
        self.assertEqual(sorted(added), sorted(VENDOR_KEYS))
        for key in VENDOR_KEYS:
            self.assertIn(key, registry)
            self.assertEqual(registry[key]["info"]["plugin"], key)

    def test_vendor_fields_are_verbatim(self) -> None:
        registry: dict = {}
        self.module.merge_vendor_entries(registry)
        expected = vendor_baseline_data()
        self.assertEqual(registry, expected, "厂商条目必须逐字照抄基线")

    def test_existing_entry_is_untouched(self) -> None:
        original = {
            "status": "running",
            "install": True,
            "info": {"plugin": "ipc", "name": "监控存储", "id": 6, "version": "1.0.0.106"},
        }
        registry = {"ipc": original, "communitystore": {"info": {"plugin": "communitystore"}}}
        added = self.module.merge_vendor_entries(registry)
        self.assertNotIn("ipc", added)
        self.assertIs(registry["ipc"], original)
        self.assertIn("communitystore", registry, "我们的条目不能被删")

    def test_our_entries_survive(self) -> None:
        registry = {f"ours{i}": {"info": {"plugin": f"ours{i}"}} for i in range(10)}
        self.module.merge_vendor_entries(registry)
        for i in range(10):
            self.assertIn(f"ours{i}", registry)

    def test_idempotent(self) -> None:
        registry: dict = {"communitystore": {"info": {"plugin": "communitystore"}}}
        self.module.merge_vendor_entries(registry)
        snapshot = json.dumps(registry, sort_keys=True)
        self.assertEqual(self.module.merge_vendor_entries(registry), [])
        self.assertEqual(json.dumps(registry, sort_keys=True), snapshot)

    def test_no_baseline_file(self) -> None:
        self.baseline.unlink()
        module = load_module(self.baseline)
        registry: dict = {}
        self.assertEqual(module.merge_vendor_entries(registry), [])
        self.assertEqual(registry, {})

    def test_merges_to_fifteen_for_real_world_case(self) -> None:
        """真实场景：注册表被清空 → 重建后应是 5 厂商 + 10 我方 = 15。"""
        registry = {f"ours{i}": {"info": {"plugin": f"ours{i}"}} for i in range(10)}
        self.module.merge_vendor_entries(registry)
        self.assertEqual(len(registry), 15)


if __name__ == "__main__":
    unittest.main()
