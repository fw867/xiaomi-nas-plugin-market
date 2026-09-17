from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from storelib import (
    InstallManager,
    StoreError,
    _is_remote_reference,
    _validate_download_url,
    is_newer_version,
    load_apps_catalog,
    load_verified_catalog,
    safe_extract_bundle,
    validate_apps_catalog,
    validate_manifest,
    verify_detached_signature,
    version_tuple,
)


PROJECT = Path(__file__).resolve().parents[1]
CATALOG = PROJECT / "catalog"
PUBLIC_KEY = CATALOG / "repository-public.pem"


class StoreLibraryTests(unittest.TestCase):
    def test_catalog_and_all_bundles_verify(self) -> None:
        catalog = load_verified_catalog(CATALOG, PUBLIC_KEY)
        self.assertEqual({'devicemanager', '115sync', 'aliyundrivesync', 'webdav', 'qbittorrent'}, {p['id'] for p in catalog['packages']})
        for package in catalog["packages"]:
            verify_detached_signature(
                CATALOG / package["bundle"],
                CATALOG / package["signature"],
                PUBLIC_KEY,
            )

    def test_all_bundle_manifests_are_valid(self) -> None:
        catalog = load_verified_catalog(CATALOG, PUBLIC_KEY)
        with tempfile.TemporaryDirectory() as temporary:
            for package in catalog["packages"]:
                target = Path(temporary) / package["id"]
                target.mkdir()
                safe_extract_bundle(CATALOG / package["bundle"], target)
                manifest = validate_manifest(json.loads((target / "manifest.json").read_text(encoding="utf-8")))
                self.assertEqual(package["id"], manifest["id"])
                self.assertEqual(package["version"], manifest["version"])

    def test_path_traversal_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            bundle = Path(temporary) / "bad.zip"
            with zipfile.ZipFile(bundle, "w") as archive:
                archive.writestr("../escape", "bad")
            with self.assertRaises(StoreError):
                safe_extract_bundle(bundle, Path(temporary) / "out")

    def test_tampered_catalog_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = root / "catalog.json"
            signature = root / "catalog.json.sig"
            payload.write_text("{}\n", encoding="utf-8")
            signature.write_bytes((CATALOG / "catalog.json.sig").read_bytes())
            with self.assertRaises(StoreError):
                verify_detached_signature(payload, signature, PUBLIC_KEY)

    def test_dry_run_install_and_uninstall_preserve_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = root / "data/plugin/u_test.list"
            registry.parent.mkdir(parents=True)
            registry.write_text("{}\n", encoding="utf-8")
            data_dir = root / "data/plugin/aliyundrive-sync/data"
            data_dir.mkdir(parents=True)
            marker = data_dir / "keep.txt"
            marker.write_text("user data", encoding="utf-8")
            manager = InstallManager(CATALOG, PUBLIC_KEY, "u_test", root=root, execute_system=False, remote_apps=False)
            result = manager.install("aliyundrivesync")
            self.assertTrue(result["ok"])
            self.assertIn("aliyundrivesync", json.loads(registry.read_text(encoding="utf-8")))
            self.assertEqual(
                {"version": "0.1.0", "managed": True},
                manager.inventory()["aliyundrivesync"],
            )
            current = root / "data/plugin/aliyundrive-sync/current"
            release = current.resolve()
            self.assertTrue(release.is_dir())
            result = manager.uninstall("aliyundrivesync")
            self.assertTrue(result["dataPreserved"])
            self.assertEqual("user data", marker.read_text(encoding="utf-8"))
            self.assertFalse(current.exists())
            self.assertFalse(release.exists())
            self.assertNotIn("aliyundrivesync", json.loads(registry.read_text(encoding="utf-8")))

    def test_inventory_marks_registry_only_plugin_as_external(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = root / "data/plugin/u_test.list"
            registry.parent.mkdir(parents=True)
            registry.write_text(
                json.dumps({"devicemanager": {"info": {"version": "0.4.1"}}}),
                encoding="utf-8",
            )
            manager = InstallManager(CATALOG, PUBLIC_KEY, "u_test", root=root, execute_system=False, remote_apps=False)
            self.assertEqual(
                {"version": "0.4.1", "managed": False},
                manager.inventory()["devicemanager"],
            )

    def test_remote_reference_detection(self) -> None:
        self.assertTrue(_is_remote_reference("https://github.com/a/b/releases/download/v1/x.zip"))
        self.assertTrue(_is_remote_reference("http://github.com/a/b"))
        self.assertFalse(_is_remote_reference("bundles/x.zip"))
        self.assertFalse(_is_remote_reference(""))
        self.assertFalse(_is_remote_reference("icons/x.png"))

    def test_download_url_host_whitelist(self) -> None:
        ok = _validate_download_url(
            "https://github.com/fw867/xiaomi-nas-plugin-market/releases/download/v0.1.2/devicemanager-0.4.1.bundle.zip",
            field="bundle",
            package_id="devicemanager",
        )
        self.assertEqual("github.com", ok.hostname)

        for bad in (
            "https://evil.example.com/bundle.zip",
            "https://github.com.evil.com/bundle.zip",
            "ftp://github.com/bundle.zip",
            "https://user:pass@github.com/bundle.zip",
            "https://192.168.1.1/bundle.zip",
        ):
            with self.assertRaises(StoreError, msg=bad):
                _validate_download_url(bad, field="bundle", package_id="x")

    def test_resolve_local_artifact_still_works(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager = InstallManager(CATALOG, PUBLIC_KEY, "u_test", root=root, execute_system=False, remote_apps=False)
            package = next(p for p in load_verified_catalog(CATALOG, PUBLIC_KEY)["packages"] if p["id"] == "aliyundrivesync")
            path = manager._resolve_artifact(package["bundle"], expected_sha256=package["sha256"])
            self.assertTrue(path.is_file())
            self.assertEqual(package["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())

    def test_resolve_remote_artifact_downloads_and_caches(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            payload = b"fake-bundle-bytes"
            digest = hashlib.sha256(payload).hexdigest()
            url = "https://github.com/fw867/xiaomi-nas-plugin-market/releases/download/v0/test.bundle.zip"

            manager = InstallManager(CATALOG, PUBLIC_KEY, "u_test", root=root, execute_system=False, remote_apps=True)

            class _FakeResponse:
                status = 200
                def read(self, n: int = -1) -> bytes:
                    data, self._buf = self._buf[:n], self._buf[n:]
                    return data
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    return False
                _buf = payload

            with mock.patch("storelib._assert_public_ip"), mock.patch(
                "urllib.request.build_opener"
            ) as build_opener:
                opener = mock.Mock()
                build_opener.return_value = opener
                opener.open.return_value = _FakeResponse()
                path = manager._resolve_artifact(url, expected_sha256=digest)

            self.assertTrue(path.is_file())
            self.assertEqual(digest, hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(1, opener.open.call_count)

            # Second call hits the cache, no network.
            with mock.patch("urllib.request.build_opener") as build_opener:
                path2 = manager._resolve_artifact(url, expected_sha256=digest)
                build_opener.assert_not_called()
            self.assertEqual(path, path2)

    def test_resolve_remote_rejects_checksum_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            url = "https://github.com/fw867/xiaomi-nas-plugin-market/releases/download/v0/bad.zip"
            manager = InstallManager(CATALOG, PUBLIC_KEY, "u_test", root=root, execute_system=False, remote_apps=True)

            class _FakeResponse:
                status = 200
                def read(self, n: int = -1) -> bytes:
                    data, self._buf = self._buf[:n], self._buf[n:]
                    return data
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    return False
                _buf = b"tampered"

            with mock.patch("storelib._assert_public_ip"), mock.patch(
                "urllib.request.build_opener"
            ) as build_opener:
                build_opener.return_value.open.return_value = _FakeResponse()
                with self.assertRaises(StoreError):
                    manager._resolve_artifact(url, expected_sha256="0" * 64)

    # ---- apps.json (schemaVersion 2) ----

    def _sample_apps_doc(self) -> dict:
        return {
            "schemaVersion": 2,
            "name": "小米智能存储应用商店",
            "generatedAt": 1700000000,
            "repository": "fw867/xiaomi-nas-plugin-market",
            "branch": "main",
            "store": {"version": "0.2.0", "releasesUrl": "https://github.com/fw867/xiaomi-nas-plugin-market/releases"},
            "apps": [
                {
                    "id": "demo",
                    "name": "演示应用",
                    "version": "1.0.0",
                    "summary": "测试用",
                    "bundle": "apps/demo-1.0.0.zip",
                    "sha256": "a" * 64,
                    "size": 1024,
                    "icon": "apps/icons/demo.png",
                    "channel": "stable",
                    "tags": ["tool"],
                    "author": "test",
                    "pluginId": 99999,
                    "port": 19999,
                }
            ],
        }

    def test_validate_apps_catalog_accepts_v2(self) -> None:
        document = validate_apps_catalog(self._sample_apps_doc())
        self.assertEqual(2, document["schemaVersion"])
        self.assertEqual("demo", document["apps"][0]["id"])

    def test_validate_apps_catalog_rejects_bad_entries(self) -> None:
        doc = self._sample_apps_doc()
        doc["apps"][0]["sha256"] = "not-a-hash"
        with self.assertRaises(StoreError):
            validate_apps_catalog(doc)

        doc = self._sample_apps_doc()
        doc["schemaVersion"] = 1
        with self.assertRaises(StoreError):
            validate_apps_catalog(doc)

        doc = self._sample_apps_doc()
        doc["apps"] = []
        with self.assertRaises(StoreError):
            validate_apps_catalog(doc)

    def test_load_apps_catalog_prefers_remote(self) -> None:
        """远程可达时必须用远程内容，不能被本地副本挡住。"""
        remote = self._sample_apps_doc()
        remote["apps"][0]["id"] = "remoteapp"
        with tempfile.TemporaryDirectory() as temporary:
            local = Path(temporary) / "apps.json"
            local.write_text(json.dumps(self._sample_apps_doc()), encoding="utf-8")
            with mock.patch("storelib.fetch_url_json", return_value=remote):
                document = load_apps_catalog(
                    local_apps_json=local,
                    remote_url="https://raw.githubusercontent.com/fw867/xiaomi-nas-plugin-market/main/apps.json",
                )
            self.assertEqual("remoteapp", document["apps"][0]["id"])
            self.assertNotIn("_stale", document)

    def test_load_apps_catalog_falls_back_when_offline(self) -> None:
        """远程不可达时回退到本地副本，并标记为陈旧。"""
        with tempfile.TemporaryDirectory() as temporary:
            local = Path(temporary) / "apps.json"
            local.write_text(json.dumps(self._sample_apps_doc()), encoding="utf-8")
            with mock.patch("storelib.fetch_url_json", side_effect=StoreError("offline")):
                document = load_apps_catalog(
                    local_apps_json=local,
                    remote_url="https://raw.githubusercontent.com/fw867/xiaomi-nas-plugin-market/main/apps.json",
                )
            self.assertEqual("demo", document["apps"][0]["id"])
            self.assertTrue(document.get("_stale"))

    def test_load_apps_catalog_uses_sha_pinned_raw(self) -> None:
        """默认路径必须先解析 HEAD SHA，再按 SHA 拉 apps.json。

        raw 的分支路径（<repo>/main/...）会被 CDN 缓存住，内容更新后仍可能
        返回旧版本，因此必须用 commit SHA 精确定位。
        """
        sha = "b" * 40
        seen: list[str] = []

        def fake_fetch(url, timeout=20):
            seen.append(url)
            return self._sample_apps_doc()

        with mock.patch("storelib.fetch_branch_sha", return_value=sha) as resolver, \
                mock.patch("storelib.fetch_url_json", side_effect=fake_fetch):
            document = load_apps_catalog()

        resolver.assert_called_once()
        self.assertEqual("demo", document["apps"][0]["id"])
        self.assertTrue(any(sha in url for url in seen), seen)

    def test_load_apps_catalog_uses_fresh_cache_within_ttl(self) -> None:
        """TTL 内的缓存可以直接用，避免频繁请求。"""
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "apps-cache.json"
            cache.write_text(json.dumps(self._sample_apps_doc()), encoding="utf-8")
            with mock.patch("storelib.fetch_url_json") as fetcher:
                document = load_apps_catalog(cache_path=cache, ttl=300)
                fetcher.assert_not_called()
            self.assertEqual("demo", document["apps"][0]["id"])

    def test_install_manager_uses_apps_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            # Fake apps.json + a local bundle
            apps_dir = root / "apps"
            apps_dir.mkdir()
            bundle = apps_dir / "demo-1.0.0.zip"
            # Create a minimal valid bundle
            with tempfile.TemporaryDirectory() as stage:
                stage_path = Path(stage)
                (stage_path / "manifest.json").write_text(json.dumps({
                    "schemaVersion": 1,
                    "id": "demo",
                    "name": "演示应用",
                    "version": "1.0.0",
                    "pluginId": 99999,
                    "port": 19999,
                    "paths": {"releaseRoot": "/data/plugin/demo", "uiKey": "demo", "iconName": "demo.icon"},
                    "service": "demo.service",
                    "nginx": "demo.conf",
                    "healthPath": "/healthz",
                    "registry": {"frontend": {}, "info": {}},
                }), encoding="utf-8")
                (stage_path / "runtime").mkdir()
                (stage_path / "runtime" / "server.py").write_text("# demo\n", encoding="utf-8")
                (stage_path / "ui").mkdir()
                (stage_path / "ui" / "index.html").write_text("<html></html>", encoding="utf-8")
                (stage_path / "icon").write_bytes(b"\x89PNG\r\n\x1a\n")
                (stage_path / "config").mkdir()
                (stage_path / "config" / "demo.service").write_text("[Unit]\n", encoding="utf-8")
                (stage_path / "config" / "demo.conf").write_text("location / {}\n", encoding="utf-8")
                with zipfile.ZipFile(bundle, "w") as zf:
                    for f in stage_path.rglob("*"):
                        if f.is_file():
                            zf.write(f, f.relative_to(stage_path).as_posix())

            sha = hashlib.sha256(bundle.read_bytes()).hexdigest()
            doc = self._sample_apps_doc()
            doc["apps"][0]["sha256"] = sha
            apps_json = root / "apps.json"
            apps_json.write_text(json.dumps(doc), encoding="utf-8")

            registry = root / "data/plugin/u_test.list"
            registry.parent.mkdir(parents=True)
            registry.write_text("{}\n", encoding="utf-8")

            manager = InstallManager(
                CATALOG,
                None,
                "u_test",
                root=root,
                execute_system=False,
                apps_json=apps_json,
                apps_root=root,
                remote_apps=False,
            )
            # Windows may lack symlink privilege; emulate with a directory + marker
            def _fake_symlink(self, target, target_is_directory=False):
                self.parent.mkdir(parents=True, exist_ok=True)
                if self.exists() or self.is_symlink():
                    self.unlink()
                self.mkdir()
                (self / ".symlink-target").write_text(str(target), encoding="utf-8")
            with mock.patch.object(Path, "symlink_to", _fake_symlink):
                result = manager.install("demo")
            self.assertTrue(result["ok"])
            self.assertIn("demo", json.loads(registry.read_text(encoding="utf-8")))

    def test_version_comparison(self) -> None:
        self.assertTrue(is_newer_version("0.2.0", "0.1.2"))
        self.assertFalse(is_newer_version("0.1.2", "0.2.0"))
        self.assertFalse(is_newer_version("0.2.0", "0.2.0"))
        self.assertTrue(is_newer_version("1.0.0", "0.9.9"))
        self.assertEqual((0, 2, 0), version_tuple("0.2.0"))
        self.assertEqual((0, 1, 2), version_tuple("0.1.2-rc1"))


if __name__ == "__main__":
    unittest.main()
