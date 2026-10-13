import contextlib
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import pty
import select
import shlex
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

# Hardware captures live in the research tree, not in this repo. Point
# MAI_CAPTURES at that captures/ directory to run the tests that use them.
CAPTURES = Path(os.environ.get("MAI_CAPTURES", Path(__file__).resolve().parents[4] / "captures"))
SERIAL_CAPTURE = CAPTURES / "2026-10-12-serial/persistence-backup"
needs_capture = unittest.skipUnless(SERIAL_CAPTURE.is_dir(), "hardware captures not available (set MAI_CAPTURES)")
SPEC = importlib.util.spec_from_file_location("telnet_macos", Path(__file__).resolve().parents[1] / "macos.py")
m = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = m
SPEC.loader.exec_module(m)


class FakeCamera(m.Camera):
    def __init__(self, files=None, fail_write=None, fail_remove=None, concurrent=False):
        super().__init__(None)
        self.files = files or {p: None for p in (*m.PAYLOADS, m.DISABLED)}
        self.calls = []
        self.fail_write, self.fail_remove, self.concurrent = fail_write, fail_remove, concurrent

    def guard(self):
        self.calls.append(("guard",))

    @contextlib.contextmanager
    def lock(self):
        yield

    def info(self, path):
        return self.files[path]

    def backup(self, before, directory):
        self.calls.append(("backup",))
        if self.concurrent:
            self.files[m.START] = m.FileInfo(m.PAYLOADS[m.START][1], len(m.payload(m.START)), 0o600)
        return "/mnt/other/test-backup"

    def write(self, path, data, mode, script=False):
        self.calls.append(("write", path))
        self.files[path] = m.FileInfo(hashlib.md5(data).hexdigest(), len(data), mode)
        if self.fail_write == path:
            self.fail_write = None
            raise m.SetupError("Reply lost after rename")

    def remove(self, path):
        self.calls.append(("remove", path))
        if self.fail_remove == path:
            raise m.SetupError("Remove failed")
        self.files[path] = None


def installed(disabled=False):
    files = {p: m.FileInfo(spec[1], len(m.payload(p)), spec[2]) for p, spec in m.PAYLOADS.items()}
    files[m.DISABLED] = m.FileInfo(hashlib.md5(b"").hexdigest(), 0, 0o600) if disabled else None
    return files


class ForeignFirmware:
    """Another firmware with compatible paths, but different original bytes."""
    def __init__(self):
        self.commands = []
        self.hashes = {p: hashlib.md5((p + "foreign").encode()).hexdigest() for p in m.ORIGINALS}
        self.mount = "/dev/ubi0_0 /usr/bin ubifs rw,relatime"
        self.path = 'export PATH="/usr/bin:/bin:/sbin:/mnt/app/bin"'
        self.root, self.bare_hostapd = True, True

    def shell(self, command):
        self.commands.append(command)
        if command == "cat /etc/profile_prjcfg": return "OTHER_MODEL 2.0 ENGLISH"
        if command == "id -u": return "0" if self.root else "1000"
        if command.startswith("awk "): return self.mount
        if command.startswith("sed "): return self.path
        if command.startswith("grep ") and not self.bare_hostapd:
            raise m.SetupError("No compatible hostapd invocation")
        if command.startswith("if [ -f "):
            path = shlex.split(command)[3]
            digest = self.hashes.get(path)
            return digest + " file\n" if digest else "ABSENT\n"
        if command.startswith("if [ -L "): return "ABSENT\n"
        return ""

    def close(self):
        pass


class FakeFactoryUSB:
    """Real macOS PTY I/O; AT echo/CR layout follows the recorded device log."""
    def __init__(self):
        self.master, self.slave = pty.openpty()
        self.port = os.ttyname(self.slave)
        self.stop = threading.Event()
        self.commands = []
        self.thread = threading.Thread(target=self.serve, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def serve(self):
        pending = b""
        while not self.stop.is_set():
            if not select.select([self.master], [], [], 0.1)[0]:
                continue
            try:
                data = os.read(self.master, 8192)
            except OSError:
                return
            pending += data
            while b"\r" in pending:
                command, pending = pending.split(b"\r", 1)
                text = command.decode()
                self.commands.append(text)
                if text == "AT":
                    reply = b"AT\r\nOK\r\r\n"
                elif text.startswith("AT^cmd^"):
                    result = subprocess.run(["/bin/sh", "-c", text[len("AT^cmd^"):]],
                                            capture_output=True, timeout=3)
                    body = result.stdout.replace(b"\n", b"\r\r\n")
                    reply = command + b"\r\n" + body + b"OK\r\r\n"
                else:
                    reply = command + b"\r\nERROR\r\r\n"
                os.write(self.master, reply)

    def __exit__(self, *args):
        self.stop.set()
        self.thread.join(timeout=2)
        os.close(self.master)
        os.close(self.slave)


class SetupTests(unittest.TestCase):
    def test_payloads_match_tested_hashes(self):
        for path in m.PAYLOADS:
            self.assertEqual(hashlib.md5(m.payload(path)).hexdigest(), m.PAYLOADS[path][1])

    @needs_capture
    def test_payloads_match_hardware_verified_files(self):
        captured = SERIAL_CAPTURE / "usr__bin__hostapd"
        self.assertEqual(m.payload(m.HOOK), captured.read_bytes())

    def test_auto_detection_refuses_ambiguous_ports(self):
        with patch.object(m, "ports", return_value=["/dev/cu.usbmodem1", "/dev/cu.usbmodem2"]):
            with self.assertRaisesRegex(m.SetupError, "Several serial ports"):
                m.choose_port()
        with patch.object(m, "ports", return_value=["/dev/cu.usbmodem1"]):
            self.assertEqual(m.choose_port(), "/dev/cu.usbmodem1")
        with self.assertRaises(m.SetupError):
            m.choose_port("/tmp/not-a-serial-port")

    def test_permission_parser_preserves_modes_and_refuses_special_files(self):
        self.assertEqual(m.permission_mode("-rwx------"), 0o700)
        self.assertEqual(m.permission_mode("-rwxr-xr-x"), 0o755)
        for bad in ("lrwxrwxrwx", "-rwsr-xr-x", "drwxr-xr-x"):
            with self.assertRaises(m.SetupError):
                m.permission_mode(bad)

    def test_install_is_ordered_and_idempotent(self):
        camera = FakeCamera()
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()):
            directory = Path(temp) / "first"
            m.change(camera, "install", directory)
            self.assertEqual(camera.files, installed())
            self.assertEqual([c for c in camera.calls if c[0] == "write"],
                             [("write", m.START), ("write", m.HOOK)])
            self.assertEqual(json.loads((directory / "manifest.json").read_text())["result"], "verified")
            before = copy.deepcopy(camera.calls)
            m.change(camera, "install", Path(temp) / "second")
            self.assertFalse((Path(temp) / "second").exists())
            self.assertFalse(any(c[0] in ("write", "remove", "backup") for c in camera.calls[len(before):]))

    def test_install_reenables_owned_disabled_marker(self):
        camera = FakeCamera(installed(disabled=True))
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()):
            m.change(camera, "install", Path(temp) / "backup")
        self.assertEqual(camera.files, installed())
        self.assertEqual([c for c in camera.calls if c[0] == "remove"], [("remove", m.DISABLED)])

    def test_uninstall_removes_hook_before_helper_and_saves_previous_bytes(self):
        camera = FakeCamera(installed())
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()):
            directory = Path(temp) / "backup"
            m.change(camera, "uninstall", directory)
            self.assertEqual([c for c in camera.calls if c[0] == "remove"],
                             [("remove", m.HOOK), ("remove", m.START)])
            self.assertEqual((directory / "hostapd").read_bytes(), m.payload(m.HOOK))
            self.assertEqual((directory / "hostapd").stat().st_mode & 0o777, 0o600)
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.assertTrue(all(v is None for v in camera.files.values()))

    def test_lost_reply_after_rename_rolls_back_both_files(self):
        camera = FakeCamera(fail_write=m.HOOK)
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()):
            directory = Path(temp) / "backup"
            with self.assertRaisesRegex(m.SetupError, "was rolled back"):
                m.change(camera, "install", directory)
            self.assertEqual(json.loads((directory / "manifest.json").read_text())["result"], "rolled_back")
        self.assertTrue(all(v is None for v in camera.files.values()))

    def test_incomplete_recovery_is_reported_and_recorded(self):
        camera = FakeCamera(fail_write=m.HOOK, fail_remove=m.START)
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()):
            directory = Path(temp) / "backup"
            with self.assertRaisesRegex(m.SetupError, "rollback is incomplete"):
                m.change(camera, "install", directory)
            self.assertEqual(json.loads((directory / "manifest.json").read_text())["result"], "recovery_incomplete")
        self.assertIsNotNone(camera.files[m.START])

    def test_unknown_existing_file_is_never_overwritten(self):
        camera = FakeCamera()
        camera.files[m.HOOK] = m.FileInfo("0" * 32, 123, 0o755)
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(m.SetupError, "not installed by this version"):
                m.change(camera, "install", Path(temp) / "backup")
            self.assertFalse((Path(temp) / "backup").exists())
        self.assertFalse(any(c[0] in ("write", "remove", "backup") for c in camera.calls))

    def test_concurrent_change_during_backup_stops_before_writes(self):
        camera = FakeCamera(concurrent=True)
        with tempfile.TemporaryDirectory() as temp, contextlib.redirect_stdout(io.StringIO()):
            directory = Path(temp) / "backup"
            with self.assertRaisesRegex(m.SetupError, "changed during the backup"):
                m.change(camera, "install", directory)
            self.assertEqual(json.loads((directory / "manifest.json").read_text())["result"], "stopped_before_write")
        self.assertFalse(any(c[0] in ("write", "remove") for c in camera.calls))

    def test_unsupported_firmware_guard_is_readonly(self):
        class Remote:
            def __init__(self): self.commands = []
            def shell(self, command):
                self.commands.append(command)
                if command.startswith("cat "): return "DR2830 1.2.42 CHINESE"
                if command == "id -u": return "0\n"
                return "0" * 32 + " filename\n"
        remote = Remote()
        with self.assertRaisesRegex(m.SetupError, "differs from the tested firmware"):
            m.Camera(remote).guard()
        self.assertTrue(all(c.startswith(("cat ", "id ", "if [ -f ")) for c in remote.commands))

    def test_skip_version_accepts_foreign_hashes_and_records_actual_files(self):
        remote = ForeignFirmware()
        with self.assertRaisesRegex(m.SetupError, "Untested model"):
            m.Camera(remote).guard()
        remote.commands.clear()
        camera = m.Camera(remote, skip_version_check=True)
        camera.guard()
        self.assertEqual(camera.original_hashes, {p: v for p, v in remote.hashes.items() if p != "/usr/bin/main_app"})
        self.assertFalse(any("profile_prjcfg" in c or "/usr/bin/main_app" in c for c in remote.commands))
        self.assertTrue(all(c.startswith(("id ", "test ", "awk ", "sed ", "grep ", "if [ -f ")) for c in remote.commands))
        self.assertIsNone(m.status(installed(), skip_version_check=True)["firmware"])

    def test_skip_version_still_rejects_ramfs_readonly_and_incompatible_path(self):
        for mount, path in [
            ("rootfs /usr/bin ramfs rw", None),
            ("/dev/ubi0_0 /usr/bin ubifs ro", None),
            (None, 'export PATH="/mnt/app/bin:/usr/bin:/bin"'),
        ]:
            with self.subTest(mount=mount, path=path):
                remote = ForeignFirmware()
                if mount: remote.mount = mount
                if path: remote.path = path
                with self.assertRaises(m.SetupError):
                    m.Camera(remote, skip_version_check=True).guard()

    def test_skip_version_still_requires_root_and_bare_hostapd_call(self):
        for key in ("root", "bare_hostapd"):
            remote = ForeignFirmware()
            setattr(remote, key, False)
            with self.subTest(key=key), self.assertRaises(m.SetupError):
                m.Camera(remote, skip_version_check=True).guard()

    def test_skip_version_detects_original_file_changes_within_operation(self):
        remote = ForeignFirmware()
        camera = m.Camera(remote, skip_version_check=True)
        camera.guard()
        initial = dict(camera.original_hashes)
        remote.hashes["/mnt/app/bin/hostapd"] = "a" * 32
        with self.assertRaisesRegex(m.SetupError, "while this run was in progress"):
            camera.guard()
        self.assertEqual(camera.original_hashes, initial)

    def test_skip_version_allows_another_mount_script(self):
        remote = ForeignFirmware()
        del remote.hashes["/etc/init.d/S05_FS_Overlay"]
        camera = m.Camera(remote, skip_version_check=True)
        camera.guard()
        self.assertNotIn("/etc/init.d/S05_FS_Overlay", camera.original_hashes)

    def test_cli_skip_version_reaches_readonly_check(self):
        with patch.object(m, "SerialPort", return_value=ForeignFirmware()), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(m.main(["--port", "/dev/cu.usbmodem1", "--skip-version-check", "check"]), 0)
        self.assertIn('"version_check_skipped": true', out.getvalue())
        self.assertIn('"firmware": null', out.getvalue())

    @needs_capture
    def test_device_backup_keeps_vendor_and_wrapper_hostapd_separate(self):
        capture = SERIAL_CAPTURE
        data = {p: (capture / ("mnt__other__70mai-telnet-backup-2026-10-12__" + Path(p).name)).read_bytes()
                for p in list(m.ORIGINALS)[1:]}
        data.update({p: m.payload(p) for p in m.PAYLOADS})
        class Remote:
            def shell(self, command):
                fields = shlex.split(command)
                if fields[0] == "cp":
                    self.source, target = fields[2:4]
                    if target in data: raise AssertionError("Backup overwritten")
                    data[target] = data[self.source]
                    return ""
                if fields[0] == "md5sum":
                    return hashlib.md5(data[fields[1]]).hexdigest() + " file\n"
                return ""
        camera = m.Camera(Remote())
        camera.original_hashes = dict(m.ORIGINALS)
        device_dir = camera.backup(installed(), Path("test-unique"))
        self.assertEqual(data[device_dir + "/vendor-hostapd"], data["/mnt/app/bin/hostapd"])
        self.assertEqual(data[device_dir + "/previous-hostapd"], m.payload(m.HOOK))

    def test_cross_version_backup_verifies_actual_instead_of_known_hash(self):
        data = {p: (p + "foreign bytes").encode() for p in list(m.ORIGINALS)[1:]}
        class Remote:
            def shell(self, command):
                fields = shlex.split(command)
                if fields[0] == "cp":
                    source, target = fields[2:4]
                    data[target] = data[source]
                if fields[0] == "md5sum":
                    return hashlib.md5(data[fields[1]]).hexdigest() + " file\n"
                return ""
        camera = m.Camera(Remote(), skip_version_check=True)
        camera.original_hashes = {p: hashlib.md5(v).hexdigest() for p, v in data.items()}
        before = {p: None for p in (*m.PAYLOADS, m.DISABLED)}
        device_dir = camera.backup(before, Path("foreign-backup"))
        self.assertEqual(data[device_dir + "/vendor-hostapd"], data["/mnt/app/bin/hostapd"])

    def test_real_pty_echo_framing_and_shell_exit_status(self):
        with FakeFactoryUSB() as usb:
            remote = m.SerialPort(usb.port, timeout=2)
            try:
                self.assertEqual(remote.shell("printf 'hello\\n'"), "hello\n")
                with self.assertRaisesRegex(m.SetupError, "exit code 7"):
                    remote.shell("exit 7")
                self.assertFalse(remote.broken)
                self.assertEqual(remote.shell("printf 'after\\n'"), "after\n")
            finally:
                remote.close()
        self.assertEqual(usb.commands[0], "AT")

    def test_real_pty_truncated_results_stop_further_commands(self):
        with FakeFactoryUSB() as usb:
            remote = m.SerialPort(usb.port, timeout=2)
            try:
                with self.assertRaisesRegex(m.SetupError, "missing its end marker"):
                    remote.shell("printf '%03000d' 0")
                count = len(usb.commands)
                with self.assertRaisesRegex(m.SetupError, "stopped sending"):
                    remote.shell("echo later")
                self.assertEqual(len(usb.commands), count)
            finally:
                remote.close()

    def test_atomic_upload_over_real_pty_preserves_verified_payload_bytes(self):
        with tempfile.TemporaryDirectory() as temp, FakeFactoryUSB() as usb:
            serial = m.SerialPort(usb.port, timeout=2)
            class SandboxedRemote:
                def shell(self, command):
                    # Translate device paths before executing the test on this Mac.
                    command = command.replace("/usr/bin/", temp + "/")
                    checksum = (shlex.quote(sys.executable) + " -c " + shlex.quote(
                        "import hashlib,sys; print(hashlib.md5(open(sys.argv[1],'rb').read()).hexdigest()+'  '+sys.argv[1])"))
                    command = command.replace("md5sum", checksum).replace("&& sync", "&& :")
                    return serial.shell(command)
            try:
                camera = m.Camera(SandboxedRemote())
                for path in m.PAYLOADS:
                    camera.write(path, m.payload(path), m.PAYLOADS[path][2], script=True)
                    target = Path(temp) / Path(path).name
                    self.assertEqual(target.read_bytes(), m.payload(path))
                    self.assertEqual(target.stat().st_mode & 0o777, m.PAYLOADS[path][2])
                self.assertFalse(list(Path(temp).glob(".70mai-telnet-new-*")))
                self.assertTrue(all(len(c.encode()) <= 1800 for c in usb.commands))
            finally:
                serial.close()

    def test_octal_upload_encoding_roundtrips_every_byte(self):
        data = bytes(range(256))
        encoded = "".join(f"\\0{b:03o}" for b in data)
        result = subprocess.run(["/bin/sh", "-c", f"printf '%b' '{encoded}'"], capture_output=True, check=True)
        self.assertEqual(result.stdout, data)


if __name__ == "__main__":
    unittest.main()
