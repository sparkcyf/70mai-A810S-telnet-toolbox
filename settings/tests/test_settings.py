import copy
from dataclasses import replace
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mai_settings as m


def baseline():
    data = bytearray(m.BLOCK_SIZE)
    for i, value in {20: 6, 21: 0, 22: 1, 23: 26, 41: 0}.items():
        struct.pack_into("<I", data, m.PARAM_OFFSET + i * 4, value)
    return m.Snapshot(bytes(data) + bytes(m.FLASH_SIZE - m.BLOCK_SIZE), bytes(data),
                      None, None, "zh-CN", "camera-one")


class FakeDevice:
    """Models native saves, including a GPS change before a lost reply."""
    def __init__(self, snapshot, fail_file=False, fail_save=False):
        self.current = snapshot
        self.calls = []
        self.fail_file = fail_file
        self.fail_save = fail_save

    def snapshot(self):
        if self.current.flash[:m.BLOCK_SIZE] != self.current.memory:
            raise m.SettingsError("Unsaved parameters")
        return copy.deepcopy(self.current)

    def memory(self, address, size):
        return self.current.memory

    def read(self, path):
        return self.current.flash

    def set_param(self, index, value):
        self.calls.append(("param", index, value))
        self.current = replace(self.current, memory=m.with_parameters(self.current.memory, {index: value}))

    def set_gps(self, value):
        self.calls.append(("gps", value))
        data = bytearray(self.current.memory)
        struct.pack_into("<I", data, m.PARAM_OFFSET + 22 * 4, value)
        self.current = replace(self.current, memory=bytes(data),
                               flash=bytes(data) + self.current.flash[m.BLOCK_SIZE:])

    def flush(self, original):
        try:
            self.set_gps(1 - original)
            if self.fail_save:
                self.fail_save = False
                raise m.SettingsError("Lost reply after the native save")
        finally:
            self.set_gps(original)

    def file(self, path, data):
        self.calls.append(("file", path))
        if self.fail_file and path == m.STATE_PATH:
            self.fail_file = False
            raise m.SettingsError("File upload interrupted")
        key = {m.HELPER_PATH: "helper", m.STATE_PATH: "state_bytes", m.BOOT_PATH: "swapon"}[path]
        self.current = replace(self.current, **{key: data})

    def optional_file(self, path):
        return self.current.swapon

    def ui_language(self, language):
        self.calls.append(("ui", language))
        self.current = replace(self.current, ui_language=language)


class SettingsTests(unittest.TestCase):
    def test_dst_is_idempotent_and_reversible(self):
        original = baseline()
        on = m.plan(original, utc_offset=-420, dst=True)
        self.assertEqual(on.params[20], 7)
        repeated = m.plan(on, dst=True)
        self.assertEqual(repeated.memory, on.memory)
        self.assertEqual(repeated.state_bytes, on.state_bytes)
        off = m.plan(on, dst=False)
        self.assertEqual(off.params[20], 6)

    def test_unknown_or_stale_dst_requires_base(self):
        original = baseline()
        with self.assertRaises(m.SettingsError):
            m.plan(original, dst=True)
        tracked = m.plan(original, utc_offset=-420, dst=False)
        changed_elsewhere = replace(tracked, memory=m.with_parameters(tracked.memory, {20: 8}))
        with self.assertRaises(m.SettingsError):
            m.plan(changed_elsewhere, dst=True)
        repaired = m.plan(changed_elsewhere, utc_offset=-420, dst=True)
        self.assertEqual(repaired.params[20], 7)

    def test_fractional_offsets_and_cn_fallback(self):
        self.assertEqual(m.parse_offset("-03:30"), -210)
        self.assertEqual(m.plan(baseline(), utc_offset=-210).params[20], 10)
        self.assertEqual(m.plan(baseline(), utc_offset=345).params[20], 23)
        for base, dst in [(360, False), (300, True), (840, True)]:
            with self.assertRaises(m.SettingsError):
                m.plan(baseline(), utc_offset=base, dst=dst)
        self.assertEqual(m.effective_offset(24), 480)

    def test_ui_language_preserves_native_voice_parameter(self):
        desired = m.plan(baseline(), language="en")
        self.assertEqual(desired.memory, baseline().memory)
        self.assertEqual(desired.params[23], 26)
        self.assertEqual(desired.helper, m.helper_bytes())
        device = FakeDevice(baseline())
        m.apply(device, baseline(), desired)
        self.assertFalse(any(c[0] in ("gps", "param") for c in device.calls))

    def test_parameter_changes_are_limited_and_no_unneeded_state_file(self):
        before = baseline()
        desired = m.plan(before, units="mph", date_order="mdy")
        changed = {i for i, (a, b) in enumerate(zip(before.memory, desired.memory)) if a != b}
        self.assertEqual(changed, {m.PARAM_OFFSET + 41 * 4, m.PARAM_OFFSET + 21 * 4})
        self.assertIsNone(desired.state_bytes)

    def test_failure_after_native_save_restores_settings_and_gps(self):
        before = baseline()
        device = FakeDevice(before, fail_save=True)
        desired = m.plan(before, units="mph", utc_offset=-420, dst=True)
        with self.assertRaisesRegex(m.SettingsError, "owned settings restored"):
            m.apply(device, before, desired)
        self.assertEqual(device.current, before)
        self.assertEqual(device.current.params[22], 1)

    def test_file_failure_rolls_back_params_and_installed_helper(self):
        before = baseline()
        device = FakeDevice(before, fail_file=True)
        desired = m.plan(before, language="en", units="mph")
        with self.assertRaisesRegex(m.SettingsError, "owned settings restored"):
            m.apply(device, before, desired)
        self.assertEqual(device.current, before)

    def test_concurrent_change_before_apply_performs_no_writes(self):
        before = baseline()
        changed = m.plan(before, date_order="dmy")
        changed = replace(changed, flash=changed.memory + before.flash[m.BLOCK_SIZE:])
        device = FakeDevice(changed)
        with self.assertRaisesRegex(m.SettingsError, "changed after backup"):
            m.apply(device, before, m.plan(before, units="mph"))
        self.assertEqual(device.calls, [])

    def test_restore_preserves_unrelated_new_settings_and_checks_identity(self):
        before = baseline()
        with tempfile.TemporaryDirectory() as temp:
            directory = m.save_backup(before, Path(temp) / "backup")
            data = bytearray(m.plan(before, units="mph").memory)
            data[200] = 42  # A later setting outside the CLI's three slots.
            current = replace(before, memory=bytes(data), flash=bytes(data) + before.flash[m.BLOCK_SIZE:])
            restored = m.load_backup(directory, current)
            self.assertEqual(restored.params[41], 0)
            self.assertEqual(restored.memory[200], 42)
            with self.assertRaises(m.SettingsError):
                m.load_backup(directory, replace(current, identity="another-camera"))
            (directory / "params.bin").write_bytes(b"tampered")
            with self.assertRaisesRegex(m.SettingsError, "checksum mismatch"):
                m.load_backup(directory, current)

    def test_unknown_firmware_guard_issues_no_mutation(self):
        device = m.Device.__new__(m.Device)
        device.pid, device.start_time = 100, "123"
        commands = []
        def run(command):
            commands.append(command)
            if command == "pidof main_app": return "100"
            if command.startswith("cat /proc/"): return "100 (main_app) " + " ".join(["S"] * 19 + ["123"])
            if command.startswith("md5sum"): return "different /proc/100/exe"
            return "DR2830 1.2.42 CHINESE"
        device.run = run
        with self.assertRaisesRegex(m.SettingsError, "Unsupported firmware"):
            device.guard()
        self.assertTrue(all(c.startswith(("cat ", "md5sum ", "pidof ")) for c in commands))

    def test_main_pid_waits_for_transient_fork_without_changing_parent(self):
        device = m.Device.__new__(m.Device)
        replies = iter(["2178 501\n", "501\n"])
        device.run = lambda command: next(replies)
        with patch.object(m.time, "sleep") as sleep:
            self.assertEqual(device.main_pid(501), 501)
            sleep.assert_called_once_with(0.1)
        device.run = lambda command: "502"
        with self.assertRaisesRegex(m.SettingsError, "restarted"):
            device.main_pid(501)

    def test_main_pid_refuses_persistent_duplicate_processes(self):
        device = m.Device.__new__(m.Device)
        device.run = lambda command: "2178 501"
        with patch.object(m.time, "sleep"), self.assertRaisesRegex(m.SettingsError, "Multiple"):
            device.main_pid(501)

    def test_helper_uses_reviewed_addresses_and_firmware_guard(self):
        helper = m.helper_bytes().decode()
        self.assertIn(f"seek={m.UI_ADDRESS // 4}", helper)
        self.assertIn(str(m.UI_POINTERS["zh-CN"]), helper)
        self.assertIn(str(m.UI_POINTERS["en"]), helper)
        self.assertIn(m.FIRMWARE_MD5, helper)

    def test_actual_flush_restores_original_gps_when_first_reply_fails(self):
        device = m.Device.__new__(m.Device)
        seen = []
        def setter(value):
            seen.append(value)
            if len(seen) == 1:
                raise m.SettingsError("Reply lost")
        device.set_gps = setter
        with self.assertRaises(m.SettingsError):
            device.flush(0)
        self.assertEqual(seen, [1, 0])

    def test_telnet_handles_iac_split_across_packets(self):
        class Socket:
            def __init__(self):
                self.packets = iter([b"hello\xff", b"\xfb", b"\x03\r\nroot@NVTEVM:/root$ "])
                self.sent = []
            def recv(self, size): return next(self.packets)
            def sendall(self, data): self.sent.append(data)
        transport = m.Telnet.__new__(m.Telnet)
        transport.sock, transport.pending = Socket(), b""
        text = transport.receive(m.PROMPT)
        self.assertIn("hello", text)
        self.assertEqual(transport.sock.sent, [b"\xff\xfd\x03"])

    def test_command_frames_ignore_leftover_prompts(self):
        transport = m.Telnet.__new__(m.Telnet)
        transport.send = lambda text: None
        def receive(pattern, timeout):
            raw = "root@NVTEVM:/root$ \n\nMAI_test_BEGIN\n496\n\nMAI_test_END:0\nroot@NVTEVM:/root$ "
            self.assertRegex(raw.encode(), pattern)
            return raw
        transport.receive = receive
        with patch.object(m.secrets, "token_hex", return_value="test"):
            self.assertEqual(transport.run("pidof main_app"), "496\n")

    def test_shared_boot_wrapper_is_preserved_on_install_and_restore(self):
        other = b'#!/bin/sh\n# existing ssoverlay hook\nexec /sbin/swapon "$@"\n'
        installed = m.with_boot_hook(other, True)
        self.assertEqual(m.without_boot_hook(installed), other)
        self.assertEqual(m.with_boot_hook(installed, True), installed)
        newer = installed + b"# another owner's later edit\n"
        self.assertEqual(m.with_boot_hook(newer, False), other + b"# another owner's later edit\n")
        created = m.with_boot_hook(None, True)
        self.assertIsNone(m.with_boot_hook(created, False))


if __name__ == "__main__":
    unittest.main()
