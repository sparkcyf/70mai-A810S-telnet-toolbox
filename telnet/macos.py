#!/usr/bin/env python3
"""macOS USB installer for a persistent Telnet hook on the 70mai A810Pro (DR2830 CN 1.2.42)."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import datetime as dt
import fcntl
import glob
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import select
import shlex
import sys
import termios
import time
import tty

VERSION = "0.2.0"
BASE = Path(__file__).resolve().parent
HOOK = "/usr/bin/hostapd"
START = "/usr/bin/70mai-telnet-start"
DISABLED = "/usr/bin/70mai-telnet.disabled"
LOCK = "/tmp/70mai-telnet-setup.lock"
# The payloads are the exact files previously installed and tested on hardware.
PAYLOADS = {
    START: ("70mai-telnet-start", "d31f9adc33086436184fe58895e5f7c7", 0o700),
    HOOK: ("hostapd", "551795c98df05487a9e6349d78f64e6c", 0o755),
}
ORIGINALS = {
    "/usr/bin/main_app": "40c91047536e78e80572363fe2ee8a4b",
    "/mnt/app/bin/hostapd": "ea62ac7ab920dc2d2ca0c9b78c4315ae",
    "/etc/init.d/S99_Sysctl": "f175bef246b0924be2bd650ceb547cec",
    "/etc/init.d/S05_FS_Overlay": "1ca3c3f32b0d7df96554a83dd4bd19e7",
    "/usr/share/wifiscripts/up.sh": "72f5076612df07719f4b7656c9ff2edf",
}


class SetupError(Exception):
    pass


def payload(path):
    name, digest, _ = PAYLOADS[path]
    data = (BASE / name).read_bytes()
    if hashlib.md5(data).hexdigest() != digest:
        raise SetupError(f"Local script {name} does not match the tested version; refusing to install")
    return data


def ports():
    return sorted(glob.glob("/dev/cu.usbmodem*"))


def choose_port(explicit=None):
    if explicit:
        if not re.fullmatch(r"/dev/cu\.usbmodem[A-Za-z0-9_.-]+", explicit):
            raise SetupError("--port must be a /dev/cu.usbmodem… serial device")
        return explicit
    found = ports()
    if not found:
        raise SetupError("No USB serial port found. Make sure the camera is in factory mode and the cable carries data; run `ports` to list ports.")
    if len(found) != 1:
        raise SetupError("Several serial ports found; pick one with --port: " + ", ".join(found))
    return found[0]


class SerialPort:
    def __init__(self, port, timeout=8):
        self.timeout, self.broken = timeout, False
        self.fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        self.previous = None
        try:
            if hasattr(termios, "TIOCEXCL"):
                fcntl.ioctl(self.fd, termios.TIOCEXCL)
            self.previous = termios.tcgetattr(self.fd)
            tty.setraw(self.fd, when=termios.TCSANOW)
            attr = termios.tcgetattr(self.fd)
            attr[2] |= termios.CLOCAL | termios.CREAD
            attr[4] = attr[5] = termios.B115200
            termios.tcsetattr(self.fd, termios.TCSANOW, attr)
            self.receive(0.3, require_status=False)  # Drain old unsolicited bytes.
            self.request("AT")
        except Exception:
            self.close()
            raise

    def receive(self, timeout, require_status=True):
        deadline, last, data = time.monotonic() + timeout, None, bytearray()
        while len(data) < 65536:
            now = time.monotonic()
            complete = re.search(rb"(?:OK|ERROR)\r+\n$", data) is not None
            remaining = deadline - now
            if last is not None and (complete or not require_status):
                remaining = min(remaining, 0.15 - (now - last))
            if remaining <= 0 or not select.select([self.fd], [], [], remaining)[0]:
                break
            try:
                chunk = os.read(self.fd, 8192)
            except BlockingIOError:
                continue
            if not chunk:
                break
            data.extend(chunk)
            last = time.monotonic()
        return bytes(data)

    def request(self, command):
        if self.broken:
            raise SetupError("An earlier serial reply was incomplete, so this connection stopped sending. Reconnect and run `check`.")
        wire = command.encode()
        if any(c in wire for c in (b"\r", b"\n", b"\0")) or len(wire) > 1800:
            raise SetupError("Serial command is too long or contains line breaks")
        deadline = time.monotonic() + self.timeout
        wire += b"\r"
        try:
            while wire:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([], [self.fd], [], remaining)[1]:
                    raise SetupError("Timed out writing to the serial port")
                try:
                    count = os.write(self.fd, wire)
                except BlockingIOError:
                    continue
                wire = wire[count:]
            reply = self.receive(self.timeout)
            if not re.search(rb"(?:OK|ERROR)\r+\n$", reply):
                raise SetupError("Incomplete AT reply; the result of this step is unknown. Reconnect and run `check`.")
        except Exception:
            self.broken = True
            raise
        if re.search(rb"ERROR\r+\n$", reply):
            raise SetupError("The camera rejected the AT command")
        return reply.decode(errors="replace").replace("\r", "")

    def shell(self, command):
        # Native AT OK is not the shell exit status. Frame and check it ourselves.
        token = "MAITN_" + secrets.token_hex(6)
        begin, end = token + "_BEGIN", token + "_END"
        wire = (f"AT^cmd^(printf '\\n{begin}\\n'; ( {command} ); "
                f"_mai_status=$?; printf '\\n{end}:%s\\n' \"$_mai_status\") "
                "2>&1 | head -c 2800")
        reply = self.request(wire)
        result = re.search(rf"\n{begin}\n(.*?)\n{end}:(\d+)\n", reply, re.S)
        if not result:
            self.broken = True
            raise SetupError("Command output is missing its end marker (probably truncated); the result is unknown.")
        if int(result[2]):
            raise SetupError(f"Camera command failed with exit code {result[2]}: {result[1].strip()[:160]}")
        return result[1]

    def close(self):
        if self.previous is not None:
            try:
                termios.tcsetattr(self.fd, termios.TCSANOW, self.previous)
            except OSError:
                pass
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


@dataclass(frozen=True)
class FileInfo:
    md5: str
    size: int
    mode: int


def permission_mode(text):
    if not re.fullmatch(r"-(?:r|-)(?:w|-)(?:x|-)(?:r|-)(?:w|-)(?:x|-)(?:r|-)(?:w|-)(?:x|-)", text):
        raise SetupError("Unexpected file type or permissions")
    return sum(bit for char, bit in zip(text[1:], (256, 128, 64, 32, 16, 8, 4, 2, 1)) if char != "-")


class Camera:
    def __init__(self, remote, skip_version_check=False):
        self.remote = remote
        self.skip_version_check = skip_version_check
        self.original_hashes = None

    def check_layout(self):
        self.remote.shell("test -x /mnt/app/bin/hostapd && test -x /usr/sbin/telnetd && "
                          "test -x /sbin/ifconfig && test -x /bin/sh")
        mount = self.remote.shell(
            "awk '$2 == \"/usr/bin\" {print $1, $2, $3, $4}' /proc/mounts").strip().split()
        if (len(mount) != 4 or mount[1:3] != ["/usr/bin", "ubifs"] or
                not mount[0].startswith("/dev/") or "rw" not in mount[3].split(",")):
            raise SetupError("This method needs /usr/bin to be a writable, persistent UBIFS mount; this camera is laid out differently.")
        exported = self.remote.shell("sed -n '/^export PATH=/p' /etc/init.d/S99_Sysctl").strip()
        if not exported.startswith("export PATH=") or "\n" in exported:
            raise SetupError("Could not read the PATH that main_app is started with")
        search = exported.split("=", 1)[1].strip().strip("\"'").split(":")
        if ("/usr/bin" not in search or "/mnt/app/bin" not in search or
                search.index("/usr/bin") > search.index("/mnt/app/bin")):
            raise SetupError("main_app's PATH does not search /usr/bin before /mnt/app/bin")
        self.remote.shell("grep -q -E '^[[:space:]]*hostapd[[:space:]]+/var/run/hostapd[.]conf([[:space:]]|$)' "
                          "/usr/share/wifiscripts/up.sh")

    def guard(self):
        if not self.skip_version_check:
            profile = self.remote.shell("cat /etc/profile_prjcfg")
            if not all(word in profile for word in ("DR2830", "1.2.42", "CHINESE")):
                raise SetupError("Untested model or firmware; only DR2830 CN 1.2.42 is supported by default. "
                                 "If the layout is the same, try --skip-version-check.")
        if self.remote.shell("id -u").strip() != "0":
            raise SetupError("The serial shell is not running as root")
        if self.skip_version_check:
            self.check_layout()
        observed = {}
        for path, expected in ORIGINALS.items():
            if self.skip_version_check and path == "/usr/bin/main_app":
                continue  # No main_app addresses or instructions are used by this installer.
            q = shlex.quote(path)
            actual = self.remote.shell(f"if [ -f {q} ]; then md5sum {q}; else echo ABSENT; fi").split()[0]
            if actual == "ABSENT" and self.skip_version_check and path == "/etc/init.d/S05_FS_Overlay":
                continue  # A different script can establish the persistent mount.
            if not re.fullmatch(r"[0-9a-f]{32}", actual):
                raise SetupError(f"Could not read the checksum of stock file {path}")
            observed[path] = actual
            if not self.skip_version_check and actual != expected:
                raise SetupError(f"{path} differs from the tested firmware; stopping")
        if self.original_hashes is not None and self.original_hashes != observed:
            raise SetupError("A stock file changed while this run was in progress; stopping.")
        self.original_hashes = observed
        self.remote.shell("test -x /usr/sbin/telnetd && test -x /sbin/ifconfig")

    def info(self, path):
        q = shlex.quote(path)
        text = self.remote.shell(
            f"if [ -L {q} ]; then echo LINK; elif [ -f {q} ]; then "
            f"ls -ld {q}; wc -c < {q}; md5sum {q}; "
            f"elif [ -e {q} ]; then echo OTHER; else echo ABSENT; fi").strip()
        if text == "ABSENT":
            return None
        lines = text.splitlines()
        if len(lines) != 3 or not lines[1].strip().isdigit():
            raise SetupError(f"Unexpected file type: {path}")
        digest = lines[2].split()[0]
        if not re.fullmatch(r"[0-9a-f]{32}", digest):
            raise SetupError("Malformed checksum from the camera")
        return FileInfo(digest, int(lines[1]), permission_mode(lines[0].split()[0]))

    def snapshot(self):
        result = {p: self.info(p) for p in (*PAYLOADS, DISABLED)}
        for path, info in result.items():
            if info is None:
                continue
            expected = (hashlib.md5(b"").hexdigest(), 0) if path == DISABLED else (
                PAYLOADS[path][1], len(payload(path)))
            if (info.md5, info.size) != expected:
                raise SetupError(f"{path} exists but was not installed by this version; refusing to overwrite or delete it")
        return result

    @contextmanager
    def lock(self):
        self.remote.shell(f"mkdir {LOCK}")
        try:
            yield
        finally:
            already_failing = sys.exc_info()[0] is not None
            try:
                self.remote.shell(f"rmdir {LOCK}")
            except Exception:
                if not already_failing:
                    raise
                print(f"The lock {LOCK} may still be on the camera; reconnect and check.", file=sys.stderr)

    def write(self, path, data, mode, script=False):
        if path not in (*PAYLOADS, DISABLED):
            raise SetupError("Refusing to write an unreviewed path")
        if data != (b"" if path == DISABLED else payload(path)):
            raise SetupError("Refusing to write unreviewed content")
        temporary = str(Path(path).parent / (".70mai-telnet-new-" + secrets.token_hex(6)))
        q = shlex.quote(temporary)
        self.remote.shell(f"test ! -e {q} && test ! -L {q} && (umask 077; : > {q})")
        try:
            for offset in range(0, len(data), 192):
                encoded = "".join(f"\\0{b:03o}" for b in data[offset:offset + 192])
                self.remote.shell(f"printf '%b' '{encoded}' >> {q}")
            actual = self.remote.shell(f"md5sum {q}").split()[0]
            if actual != hashlib.md5(data).hexdigest():
                raise SetupError("Uploaded file failed its checksum")
            if script:
                self.remote.shell(f"/bin/sh -n {q}")
            self.remote.shell(f"chmod {mode:o} {q} && mv {q} {shlex.quote(path)} && sync")
        except Exception:
            try:
                self.remote.shell(f"rm -f {q}")
            except Exception:
                pass
            raise

    def remove(self, path):
        if path not in (*PAYLOADS, DISABLED):
            raise SetupError("Refusing to delete an unreviewed path")
        self.remote.shell(f"rm -f {shlex.quote(path)} && sync")

    def backup(self, before, directory):
        if self.original_hashes is None:
            raise SetupError("The camera must be checked before backing up")
        device_dir = "/mnt/other/70mai-telnet-backup-" + directory.name
        q = shlex.quote(device_dir)
        self.remote.shell(f"(umask 077; mkdir {q})")
        # Large vendor files stay on-device; do not send them through the AT buffer.
        sources = [p for p in self.original_hashes if p != "/usr/bin/main_app"] + list(before)
        for source in sources:
            if source in before and before[source] is None:
                continue
            # Vendor hostapd and our wrapper share a basename: keep both copies.
            name = ("previous-" if source in before else "vendor-") + Path(source).name
            target = device_dir + "/" + name
            self.remote.shell(f"cp -p {shlex.quote(source)} {shlex.quote(target)} && sync")
            actual = self.remote.shell(f"md5sum {shlex.quote(target)}").split()[0]
            expected = self.original_hashes[source] if source in self.original_hashes else before[source].md5
            if actual != expected:
                raise SetupError("On-camera backup failed its checksum")
        return device_dir


def status(snapshot, skip_version_check=False):
    installed = all(snapshot[p] == FileInfo(PAYLOADS[p][1], len(payload(p)), PAYLOADS[p][2]) for p in PAYLOADS)
    return {"firmware": None if skip_version_check else "DR2830 CN 1.2.42",
            "version_check_skipped": skip_version_check, "installed": installed,
            "disabled": snapshot[DISABLED] is not None,
            "files": {p: asdict(info) if info else None for p, info in snapshot.items()}}


def manifest(directory, record):
    path = directory / "manifest.json"
    with path.open("w") as stream:
        os.chmod(path, 0o600)
        json.dump(record, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def change(camera, action, directory=None):
    camera.guard()
    for path in PAYLOADS:
        payload(path)
    with camera.lock():
        before = camera.snapshot()
        desired = {p: (FileInfo(spec[1], len(payload(p)), spec[2]) if action == "install" else None)
                   for p, spec in PAYLOADS.items()}
        desired[DISABLED] = None
        if before == desired:
            print("Already installed and enabled; nothing to do." if action == "install" else "Already uninstalled; nothing to do.")
            return
        if directory is None:
            stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            directory = Path.home() / ".local/state/70mai-telnet/backups" / (stamp + "-" + secrets.token_hex(3))
        directory = Path(directory).expanduser().resolve()
        directory.mkdir(parents=True, mode=0o700, exist_ok=False)
        for path, info in before.items():
            if info is not None:
                backup = directory / Path(path).name
                backup.write_bytes(b"" if path == DISABLED else payload(path))
                backup.chmod(0o600)
        record = {"schema": 1, "action": action,
                  "before": status(before, camera.skip_version_check),
                  "original_file_md5": camera.original_hashes, "result": "backing_up"}
        manifest(directory, record)
        print(f"Mac backup: {directory}", flush=True)
        record["device_backup"] = camera.backup(before, directory)
        record["result"] = "applying"
        manifest(directory, record)
        print(f"Camera backup: {record['device_backup']}", flush=True)
        if camera.snapshot() != before:
            record["result"] = "stopped_before_write"
            manifest(directory, record)
            raise SetupError("Files on the camera changed during the backup; nothing was installed or removed.")
        touched = []
        try:
            # Install helper before hook; uninstall hook before helper.
            order = (START, HOOK, DISABLED) if action == "install" else (HOOK, START, DISABLED)
            for path in order:
                if before[path] == desired[path]:
                    continue
                if camera.info(path) != before[path]:
                    raise SetupError(f"{path} was modified by something else during this run")
                touched.append(path)  # An interrupted reply may follow a successful rename.
                if desired[path] is None:
                    camera.remove(path)
                else:
                    camera.write(path, payload(path), desired[path].mode, script=True)
            if camera.snapshot() != desired:
                raise SetupError("Final read-back does not match")
            camera.guard()  # Verify vendor files match this session's initial hashes.
        except Exception as exc:
            errors = []
            for path in (HOOK, START, DISABLED):
                if path not in touched:
                    continue
                try:
                    # Reject a file changed by someone else, even during recovery.
                    current = camera.snapshot()[path]
                    if current not in (before[path], desired[path]):
                        raise SetupError("File changed during rollback")
                    if before[path] is None:
                        camera.remove(path)
                    else:
                        camera.write(path, b"" if path == DISABLED else payload(path),
                                     before[path].mode, script=path != DISABLED)
                except Exception:
                    errors.append(path)
            try:
                if camera.snapshot() != before:
                    errors.append("final rollback check")
            except Exception:
                errors.append("rollback read-back")
            record.update(result="recovery_incomplete" if errors else "rolled_back", error=str(exc), recovery_errors=errors)
            manifest(directory, record)
            if errors:
                raise SetupError(f"Operation failed and rollback is incomplete for: {', '.join(errors)}. Keep the backup at {directory}, reconnect and run `check`.") from None
            raise SetupError(f"Operation failed and was rolled back: {exc}. Backup: {directory}") from None
        record["result"] = "verified"
        manifest(directory, record)
        print("Installed and verified. Leave factory mode, boot normally, turn on the camera's Wi-Fi, then try Telnet." if action == "install" else
              "Uninstalled. A Telnet server that is already running stays up until the camera reboots.")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Install or remove a persistent Telnet server over the USB factory-mode serial port (macOS). Checks for DR2830 CN 1.2.42 by default.")
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("--port", help="serial device; defaults to the only /dev/cu.usbmodem… present")
    parser.add_argument("--timeout", type=float, default=8, help="seconds to wait for each serial reply (default 8)")
    parser.add_argument("--skip-version-check", action="store_true",
                        help="for other firmware: skip the model/version and stock-file MD5 checks; layout, backup and owned-file checks still run")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("ports", help="list candidate serial ports without opening them")
    sub.add_parser("check", aliases=["status"], help="read-only: check the firmware and what is installed")
    for action in ("install", "uninstall"):
        p = sub.add_parser(action, help="install the Telnet hook" if action == "install" else "remove the Telnet hook")
        p.add_argument("--backup-dir", type=Path, help="Mac backup directory (must not exist yet)")
    args = parser.parse_args(argv)
    remote = None
    try:
        if sys.platform != "darwin":
            raise SetupError("Only macOS is supported for now")
        if not 1 <= args.timeout <= 60:
            raise SetupError("--timeout must be between 1 and 60 seconds")
        if args.action == "ports":
            found = ports()
            print("\n".join(found) if found else "No /dev/cu.usbmodem… serial ports found.")
            return 0
        port = choose_port(args.port)
        print(f"Serial port: {port}", flush=True)
        if args.skip_version_check:
            print("--skip-version-check: not checking model, version or stock-file MD5s; still checking the layout.", flush=True)
        remote = SerialPort(port, args.timeout)
        camera = Camera(remote, args.skip_version_check)
        if args.action in ("check", "status"):
            camera.guard()
            print(json.dumps(status(camera.snapshot(), args.skip_version_check), ensure_ascii=False, indent=2))
        else:
            change(camera, args.action, args.backup_dir)
        return 0
    except (SetupError, OSError, ValueError, IndexError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        if remote is not None:
            remote.close()


if __name__ == "__main__":
    raise SystemExit(main())
