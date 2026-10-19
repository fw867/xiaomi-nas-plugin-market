#!/usr/bin/env python3
"""执行通道修复（mi-shell 垫片）单测。

背景：这台固件的 dropbear 被厂商写成固定 exec `/usr/sbin/mi-shell`（JSON 协议受限壳，
忽略 `-c 命令`），所以 `ssh host 'id'` 认证成功却不执行。插件用 bind-mount 把
mi-shell 顶替成"转发 bash"的垫片来修；`umount` 立刻还原。

这些用例只跑在测试用的临时目录里，**不会**真的去 mount（mount/umount 走 mock）。
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


class ExecChannelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="sshcontrol-exec-")
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)

        self.state_dir = base / "state"
        self.state_file = base / "state.json"
        self.hotplug_dir = base / "syshotplug" / "pool"
        self.mi_shell = base / "usr" / "sbin" / "mi-shell"
        self.mi_shell.parent.mkdir(parents=True)
        self.mi_shell.write_bytes(b"\x7fELF fake mi-shell")
        self.proc_mounts = base / "proc_mounts"
        self.proc_mounts.write_text("", encoding="utf-8")
        self.passwd = base / "passwd"
        self.passwd.write_text(
            "root:x:0:0:root:/home/tester:/bin/sh\n", encoding="utf-8"
        )
        self.shadow = base / "shadow"
        self.shadow.write_text("root:$6$abc$def:19000:0:99999:7:::\n", encoding="utf-8")
        self.dropbear_keys = base / "dropbear" / "authorized_keys"
        self.dropbear_keys.parent.mkdir(parents=True)
        self.dropbear_keys.write_text("ssh-ed25519 AAAATESTKEY comment\n", encoding="utf-8")
        self.root_home = base / "home" / "tester"
        (self.root_home / ".ssh").mkdir(parents=True)
        (self.root_home / ".ssh" / "authorized_keys").write_text(
            "ssh-ed25519 AAAATESTKEY comment\n", encoding="utf-8"
        )
        self.plugin_dir = base / "plugin"
        self.plugin_dir.mkdir()
        (self.plugin_dir / "u777.list").write_text("{}", encoding="utf-8")

        env = {
            "SSH_STATE_DIR": str(self.state_dir),
            "STATE_FILE": str(self.state_file),
            "HOTPLUG_DIR": str(self.hotplug_dir),
            "MI_SHELL_PATH": str(self.mi_shell),
            "PROC_MOUNTS": str(self.proc_mounts),
            "PASSWD_FILE": str(self.passwd),
            "SHADOW_FILE": str(self.shadow),
            "DROPBEAR_AUTHORIZED_KEYS": str(self.dropbear_keys),
            "PLUGIN_DIR": str(self.plugin_dir),
            "STORE_REGISTRATION": str(base / "missing-store.json"),
            "SSH_CONTROL_USER_ID": "777",
            "SSH_CONTROL_ROOT_HOME": str(self.root_home),
            "RUNTIME_DIR": str(ROOT / "deploy"),
            "PORT": "0",
        }
        self._saved = {key: os.environ.get(key) for key in env}
        os.environ.update(env)
        self.addCleanup(self._restore_env)
        for name in ("SSH_SHIM_PATH", "SSH_PUBKEY_MASTER", "SSH_SHADOW_MASTER",
                     "SSH_RESTORE_LOG", "SSH_CONTROL_POOL_DATA"):
            os.environ.pop(name, None)

        sys.path.insert(0, str(ROOT))
        import server  # noqa: PLC0415
        importlib.reload(server)
        self.module = server

        self.calls: list[list[str]] = []

        def fake_run(command, **kwargs):
            self.calls.append(list(command))
            if command[0] == "mount" and command[1] == "--bind":
                self.proc_mounts.write_text(
                    f"/dev/fake {command[3]} ext4 rw 0 0\n", encoding="utf-8"
                )
                return mock.Mock(returncode=0, stdout="", stderr="")
            if command[0] == "umount":
                self.proc_mounts.write_text("", encoding="utf-8")
                return mock.Mock(returncode=0, stdout="", stderr="")
            if command[:2] == ["systemctl", "is-active"]:
                return mock.Mock(returncode=1, stdout="", stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")

        patcher = mock.patch.object(server, "_run", side_effect=fake_run)
        patcher.start()
        self.addCleanup(patcher.stop)

        hook_patcher = mock.patch.object(server, "run_restore_hook", return_value=(True, ""))
        hook_patcher.start()
        self.addCleanup(hook_patcher.stop)

    def _restore_env(self) -> None:
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    # ---- 状态判定 ------------------------------------------------------

    def test_not_applicable_without_mi_shell(self) -> None:
        self.mi_shell.unlink()
        status = self.module.exec_channel_status()
        self.assertEqual(status["state"], "not-applicable")
        self.assertFalse(status["applicable"])

    def test_broken_before_fix(self) -> None:
        status = self.module.exec_channel_status()
        self.assertEqual(status["state"], "broken")
        self.assertFalse(status["hookInstalled"])
        self.assertFalse(status["shimMounted"])

    def test_fixed_after_fix(self) -> None:
        ok, error = self.module.fix_exec_channel()
        self.assertTrue(ok, error)
        status = self.module.exec_channel_status()
        self.assertEqual(status["state"], "fixed")
        self.assertTrue(status["hookInstalled"])
        self.assertTrue(status["shimMounted"])
        self.assertTrue(status["authorizedKeys"])

    # ---- 安装 / 卸载 / 幂等 ---------------------------------------------

    def test_fix_installs_hook_and_shim(self) -> None:
        ok, error = self.module.fix_exec_channel()
        self.assertTrue(ok, error)
        hook = self.module.restore_hook_path()
        self.assertTrue(hook.is_file())
        self.assertTrue(os.access(hook, os.X_OK), "钩子必须可执行")
        shim = self.module.SHIM_PATH
        self.assertTrue(shim.is_file())
        self.assertEqual(shim.read_bytes(), (ROOT / "deploy" / "mi-shell-shim").read_bytes())

    def test_fix_is_idempotent(self) -> None:
        self.assertTrue(self.module.fix_exec_channel()[0])
        first = self.module.restore_hook_path().read_bytes()
        self.assertTrue(self.module.fix_exec_channel()[0])
        self.assertEqual(self.module.restore_hook_path().read_bytes(), first)
        mounts = [c for c in self.calls if c[0] == "mount"]
        self.assertEqual(len(mounts), 1, "已经挂上就不该重复 mount")

    def test_hook_has_no_hardcoded_user_id(self) -> None:
        """钩子里的用户/路径必须来自插件参数，不能写死某个 uid。"""
        self.module.install_restore_hook()
        content = self.module.restore_hook_path().read_text(encoding="utf-8")
        self.assertNotIn("@", content, "占位符应全部被替换")
        self.assertIn("777", content, "应带上探测到的用户 id")
        self.assertIn(str(self.root_home), content)
        self.assertIn(str(self.mi_shell), content)
        # 不能出现别的设备的 uid
        self.assertNotIn("3943892", content)

    def test_revert_removes_hook_and_unmounts(self) -> None:
        self.assertTrue(self.module.fix_exec_channel()[0])
        ok, error = self.module.revert_exec_channel()
        self.assertTrue(ok, error)
        self.assertFalse(self.module.restore_hook_path().exists())
        self.assertFalse(self.module.mi_shell_shim_mounted())
        self.assertEqual(self.module.exec_channel_status()["state"], "broken")

    def test_revert_keeps_authorized_keys(self) -> None:
        """一键还原**不动**公钥登录本身。"""
        before = self.dropbear_keys.read_bytes()
        self.assertTrue(self.module.fix_exec_channel()[0])
        self.assertTrue(self.module.revert_exec_channel()[0])
        self.assertEqual(self.dropbear_keys.read_bytes(), before)
        self.assertTrue((self.root_home / ".ssh" / "authorized_keys").is_file())

    def test_revert_then_fix_roundtrip(self) -> None:
        self.assertTrue(self.module.fix_exec_channel()[0])
        self.assertEqual(self.module.exec_channel_status()["state"], "fixed")
        self.assertTrue(self.module.revert_exec_channel()[0])
        self.assertEqual(self.module.exec_channel_status()["state"], "broken")
        self.assertTrue(self.module.fix_exec_channel()[0])
        self.assertEqual(self.module.exec_channel_status()["state"], "fixed")

    def test_revert_is_idempotent(self) -> None:
        self.assertTrue(self.module.revert_exec_channel()[0])
        self.assertTrue(self.module.revert_exec_channel()[0])
        self.assertFalse(self.module.restore_hook_path().exists())

    def test_fix_records_intent_and_revert_clears_it(self) -> None:
        self.assertTrue(self.module.fix_exec_channel()[0])
        self.assertTrue(self.module.exec_channel_wanted())
        self.assertTrue(self.module.revert_exec_channel()[0])
        self.assertFalse(self.module.exec_channel_wanted())

    def test_hook_requires_public_keys(self) -> None:
        self.dropbear_keys.unlink()
        (self.root_home / ".ssh" / "authorized_keys").unlink()
        ok, error = self.module.fix_exec_channel()
        self.assertFalse(ok)
        self.assertIn("公钥", error)

    def test_pubkey_master_has_both_sources_deduped(self) -> None:
        extra = self.root_home / ".ssh" / "authorized_keys"
        extra.write_text(
            "ssh-ed25519 AAAATESTKEY comment\nssh-rsa AAAASECOND x\n", encoding="utf-8"
        )
        ok, error = self.module.ensure_pubkey_master()
        self.assertTrue(ok, error)
        lines = [line for line in self.module.PUBKEY_MASTER.read_text(encoding="utf-8").splitlines() if line]
        self.assertEqual(len(lines), 2, "重复的公钥应去重")
        if os.name == "posix":
            self.assertEqual(oct(self.module.PUBKEY_MASTER.stat().st_mode)[-3:], "600")

    def test_shadow_master_skips_locked_hash(self) -> None:
        self.shadow.write_text("root:!:19000:0:99999:7:::\n", encoding="utf-8")
        ok, error = self.module.ensure_shadow_master()
        self.assertTrue(ok, error)
        self.assertFalse(self.module.SHADOW_MASTER.exists(), "锁定状态不应写母本")

    def test_state_dir_is_private(self) -> None:
        self.assertTrue(self.module.fix_exec_channel()[0])
        if os.name == "posix":
            self.assertEqual(oct(self.state_dir.stat().st_mode)[-3:], "700")

    def test_hook_renders_without_pool_data(self) -> None:
        """没有池目录时也要能渲染（用占位说明，而不是崩）。"""
        os.environ.pop("SSH_CONTROL_POOL_DATA", None)
        rendered = self.module.render_restore_hook()
        self.assertIn("98.ssh-restore", rendered)
        self.assertNotIn("@", rendered)


class ControlScriptTests(unittest.TestCase):
    """`control disable` 必须能幂等卸载执行通道。"""

    def test_control_disable_mentions_teardown(self) -> None:
        text = (ROOT / "deploy" / "control").read_text(encoding="utf-8")
        self.assertIn("umount /usr/sbin/mi-shell", text)
        self.assertIn("98.ssh-restore", text)
        self.assertIn("mi-shell-shim", text)
        self.assertIn('disable)', text)
        self.assertTrue(text.rstrip().endswith("exit 0"))


if __name__ == "__main__":
    unittest.main()
