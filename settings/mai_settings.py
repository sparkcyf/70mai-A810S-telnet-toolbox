#!/usr/bin/env python3
"""Hidden settings for the verified 70mai DR2830 CN 1.2.42 firmware."""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from dataclasses import dataclass, replace
import datetime as dt
import gzip
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import socket
import struct
import sys
import time
import urllib.request

VERSION = "0.1.0"
FIRMWARE_MD5 = "40c91047536e78e80572363fe2ee8a4b"
BLOCK_ADDRESS = 0x1a27888
BLOCK_SIZE = 0x4938
PARAM_OFFSET = 0x4070
FLASH_SIZE = 262144
PAD_END = 0x5000  # Native NAND writer rounds to a page; padding is uninitialized.
INDICES = {"timezone": 20, "date_order": 21, "gps_auto_time": 22,
           "native_language": 23, "units": 41}
WRITABLE = {20, 21, 41}
UI_ADDRESS = 0x19fe710
UI_POINTERS = {"zh-CN": 0x1a07688, "en": 0x1a004c8}
STATE_PATH = "/mnt/other/70mai-settings-state.json"
HELPER_PATH = "/usr/bin/70mai-settings-ui"
BOOT_PATH = "/usr/bin/swapon"
BOOT_HOOK = (b"# BEGIN 70mai-settings UI hook v1\n"
             b"if [ -x /usr/bin/70mai-settings-ui ]; then\n"
             b"    /usr/bin/70mai-settings-ui start\n"
             b"fi\n# END 70mai-settings UI hook v1\n")
CREATED_BOOT_BASE = (b"#!/bin/sh\n# 70mai-settings swapon wrapper v1 (created; no previous wrapper)\n"
                     b'exec /sbin/swapon "$@"\n')
LOCK_PATH = "/tmp/70mai-settings.lock"
PROMPT = rb"@NVTEVM:[^\r\n]*\$\s*$"
DATE_ORDERS = {"ymd": 0, "mdy": 1, "dmy": 2}
UNITS = {"kmh": 0, "mph": 1}
# Total signed minutes from the actual firmware table. Enum 24 is overridden
# to UTC+8 by CN code, so effective UTC+6 is deliberately not exposed.
_OFFSETS = [-720, -660, -600, -540, -480, -420, -360, -300, -240,
            -210, -180, -120, -60, 0, 60, 120, 180, 210, 240, 270,
            300, 330, 345, 360, 390, 420, 480, 540, 570, 600, 630,
            660, 720, 780, 840]
OFFSETS = {i: value for i, value in enumerate(_OFFSETS, 1) if i != 24}
OFFSET_ENUM = {v: k for k, v in OFFSETS.items()}


class SettingsError(Exception):
    pass


def offset_text(minutes):
    sign = "+" if minutes >= 0 else "-"
    hours, mins = divmod(abs(minutes), 60)
    return f"{sign}{hours:02d}:{mins:02d}"


def parse_offset(value):
    match = re.fullmatch(r"([+-])(\d{2}):(\d{2})", value)
    if not match or int(match[3]) >= 60:
        raise argparse.ArgumentTypeError("Use a signed UTC offset, e.g. -07:00 or +05:45")
    minutes = int(match[2]) * 60 + int(match[3])
    return minutes if match[1] == "+" else -minutes


def effective_offset(enum):
    if enum in (0, 24):
        return 480  # Explicit CN fallback, not the nominal table value.
    if enum not in OFFSETS:
        raise SettingsError(f"Unknown timezone enum {enum}")
    return OFFSETS[enum]


def parameters(data):
    if len(data) != BLOCK_SIZE:
        raise SettingsError("Unexpected parameter block size")
    return {i: struct.unpack_from("<I", data, PARAM_OFFSET + i * 4)[0]
            for i in INDICES.values()}


def with_parameters(data, changes):
    result = bytearray(data)
    for index, value in changes.items():
        if index not in WRITABLE:
            raise SettingsError("Unreviewed parameter write")
        struct.pack_into("<I", result, PARAM_OFFSET + index * 4, value)
    return bytes(result)


def helper_bytes():
    return Path(__file__).with_name("camera-ui-language.sh").read_bytes()


def without_boot_hook(data):
    if data is None:
        return None
    if not data.startswith(b"#!/bin/sh\n"):
        raise SettingsError("Existing /usr/bin/swapon is not a supported shell wrapper")
    if b"70mai-settings UI hook" in data:
        if data.count(BOOT_HOOK) != 1 or data.count(b"70mai-settings UI hook") != 2:
            raise SettingsError("Unrecognized or modified 70mai-settings boot hook")
        data = data.replace(BOOT_HOOK, b"", 1)
    return data


def with_boot_hook(data, enabled):
    base = without_boot_hook(data)
    if not enabled:
        return None if base == CREATED_BOOT_BASE else base
    base = base or CREATED_BOOT_BASE
    return b"#!/bin/sh\n" + BOOT_HOOK + base[len(b"#!/bin/sh\n"):]


def check_state(state, identity):
    if not isinstance(state, dict) or state.get("owner") != "70mai-settings-v1":
        raise SettingsError("Unrecognized device state file")
    if state.get("firmware_md5") != FIRMWARE_MD5 or state.get("device") != identity:
        raise SettingsError("Device state belongs to another device or firmware")
    if state.get("ui_language") not in UI_POINTERS:
        raise SettingsError("Invalid UI language in state file")
    base, dst, enum = (state.get(k) for k in ("base_utc_offset_minutes", "dst", "effective_enum"))
    if (base, dst, enum) != (None, None, None):
        if type(base) is not int or type(dst) is not bool or type(enum) is not int:
            raise SettingsError("Invalid timezone metadata")
        if OFFSET_ENUM.get(base + (60 if dst else 0)) != enum:
            raise SettingsError("Inconsistent timezone metadata")
    return state


@dataclass
class Snapshot:
    flash: bytes
    memory: bytes
    state_bytes: bytes | None
    helper: bytes | None
    ui_language: str
    identity: str
    swapon: bytes | None = None

    @property
    def params(self):
        return parameters(self.memory)

    @property
    def state(self):
        if self.state_bytes is None:
            return None
        try:
            return check_state(json.loads(self.state_bytes), self.identity)
        except (ValueError, UnicodeDecodeError) as exc:
            raise SettingsError("Invalid device state JSON") from exc


def plan(current, *, language=None, units=None, date_order=None, utc_offset=None, dst=None):
    """Calculate an idempotent change without any I/O."""
    if all(v is None for v in (language, units, date_order, utc_offset, dst)):
        raise SettingsError("Specify at least one setting")
    values = current.params
    state = dict(current.state or {
        "owner": "70mai-settings-v1", "firmware_md5": FIRMWARE_MD5,
        "device": current.identity, "ui_language": current.ui_language,
        "base_utc_offset_minutes": None, "dst": None, "effective_enum": None,
    })
    changes = {}
    if units is not None:
        changes[41] = UNITS[units]
    if date_order is not None:
        changes[21] = DATE_ORDERS[date_order]
    if utc_offset is not None or dst is not None:
        tracked = state["effective_enum"] == values[20] and state["dst"] is not None
        if utc_offset is None and not tracked:
            raise SettingsError("DST base is unknown or stale; include --utc-offset=±HH:MM")
        base = utc_offset if utc_offset is not None else state["base_utc_offset_minutes"]
        summer = dst if dst is not None else (state["dst"] if tracked else False)
        total = base + (60 if summer else 0)
        if total not in OFFSET_ENUM:
            raise SettingsError(f"Effective UTC{offset_text(total)} is unsupported by this CN firmware")
        changes[20] = OFFSET_ENUM[total]
        state.update(base_utc_offset_minutes=base, dst=summer, effective_enum=changes[20])
    if language is not None:
        state["ui_language"] = language
    needs_state = current.state_bytes is not None or language is not None or utc_offset is not None or dst is not None
    new_state = (json.dumps(state, sort_keys=True, indent=2) + "\n").encode() if needs_state else None
    return replace(current, memory=with_parameters(current.memory, changes),
                   state_bytes=new_state,
                   helper=(helper_bytes() if language == "en" else
                           None if language == "zh-CN" else current.helper),
                   ui_language=language or current.ui_language,
                   swapon=with_boot_hook(current.swapon, language == "en") if language is not None else current.swapon)


class Telnet:
    """Small stdlib-only Telnet transport with bounded output and IAC handling."""
    def __init__(self, host):
        self.sock = socket.create_connection((host, 23), timeout=4)
        self.sock.settimeout(1)
        self.pending = b""
        try:
            self.receive(rb"login:\s*$")
            self.send("root")
            self.receive(PROMPT)
            self.send("stty -echo")
            self.receive(PROMPT)
        except Exception:
            self.sock.close()
            raise

    def send(self, command):
        if any(c in command for c in "\r\n\0") or len(command.encode()) > 3500:
            raise SettingsError("Invalid remote command size or characters")
        self.sock.sendall(command.encode() + b"\r\n")

    def receive(self, pattern, timeout=15):
        output = bytearray()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data = self.pending + self.sock.recv(8192)
            except socket.timeout:
                continue
            self.pending = b""
            if not data:
                raise SettingsError("Telnet disconnected")
            i = 0
            while i < len(data):
                if data[i] != 255:
                    output.append(data[i]); i += 1; continue
                if i + 1 >= len(data):
                    self.pending = data[i:]; break
                verb = data[i + 1]
                if verb == 255:
                    output.append(255); i += 2
                elif verb in (251, 252, 253, 254):
                    if i + 2 >= len(data):
                        self.pending = data[i:]; break
                    option = data[i + 2]
                    if verb == 253:
                        self.sock.sendall(bytes((255, 252, option)))
                    elif verb == 251:
                        self.sock.sendall(bytes((255, 253 if option in (1, 3) else 254, option)))
                    i += 3
                elif verb == 250:
                    end = data.find(b"\xff\xf0", i + 2)
                    if end < 0:
                        self.pending = data[i:]; break
                    i = end + 2
                else:
                    i += 2
            if re.search(pattern, output):
                return bytes(output).decode(errors="replace").replace("\r", "")
            if len(output) > 2 * 1024 * 1024:
                raise SettingsError("Telnet output limit exceeded")
        # Never include the remote output: it could contain a private key read.
        raise SettingsError("Telnet response timed out")

    def run(self, command, timeout=25):
        marker = "MAI_" + secrets.token_hex(8)
        begin, end = marker + "_BEGIN", marker + "_END"
        self.send(f"printf '\\n{begin}\\n'; {{ {command}; }}; "
                  f"_mai_rc=$?; printf '\\n{end}:%s\\n' \"$_mai_rc\"")
        completion = re.escape(end.encode()) + rb":\d+\r*\n[^\r\n]*@NVTEVM:[^\r\n]*\$\s*$"
        out = self.receive(completion, timeout)
        match = re.search(rf"\n{begin}\n(.*?)\n{end}:(\d+)\n", out, re.S)
        if not match or int(match[2]):
            raise SettingsError("Remote command failed or did not complete")
        return match[1]

    def close(self):
        self.sock.close()


class Device:
    def __init__(self, host):
        # Only an explicit IP is accepted, preventing shell / URL injection.
        self.host = str(ipaddress.IPv4Address(host))
        self.transport = Telnet(self.host)
        try:
            self.pid = self.main_pid()
            self.start_time = self.run(f"cat /proc/{self.pid}/stat").rsplit(")", 1)[1].split()[19]
            self.guard()
            mac = self.run("cat /sys/class/net/wlan0/address").strip().lower()
            if not re.fullmatch(r"(?:[0-9a-f]{2}:){5}[0-9a-f]{2}", mac):
                raise SettingsError("Could not identify the camera")
            self.identity = hashlib.sha256(mac.encode()).hexdigest()
        except Exception:
            self.close()
            raise

    def run(self, command, timeout=25):
        return self.transport.run(command, timeout)

    def main_pid(self, expected=None):
        # main_app briefly forks same-name children before exec. Wait for those
        # to leave, but never switch to another parent during an operation.
        for attempt in range(10):
            pids = self.run("pidof main_app").split()
            if not pids or any(not p.isdigit() for p in pids):
                raise SettingsError("Expected a main_app process")
            if expected is not None and str(expected) not in pids:
                raise SettingsError("main_app restarted; reconnect before changing settings")
            if len(pids) == 1:
                return int(pids[0])
            if attempt < 9:
                time.sleep(0.1)
        raise SettingsError("Multiple main_app processes remain; retry after the camera settles")

    def guard(self):
        self.main_pid(self.pid)
        start = self.run(f"cat /proc/{self.pid}/stat").rsplit(")", 1)[1].split()[19]
        if start != self.start_time:
            raise SettingsError("main_app process identity changed")
        md5 = self.run(f"md5sum /proc/{self.pid}/exe").split()[0]
        profile = self.run("cat /etc/profile_prjcfg")
        if md5 != FIRMWARE_MD5 or not all(s in profile for s in ("DR2830", "1.2.42", "CHINESE")):
            raise SettingsError("Unsupported firmware; only the verified DR2830 CN 1.2.42 is supported")

    def read(self, path, offset=0, count=None):
        source = (f"gzip -c {shlex.quote(path)}" if count is None else
                  f"dd if={shlex.quote(path)} bs=1 skip={offset} count={count} 2>/dev/null | gzip -c")
        out = self.run(source + " | uuencode -m data.gz", 45)
        match = re.search(r"begin-base64 \d+ data.gz\n(.*?)====", out, re.S)
        if not match:
            raise SettingsError("Device binary read failed")
        try:
            data = gzip.decompress(base64.b64decode(re.sub(r"\s", "", match[1]), validate=True))
        except (ValueError, OSError) as exc:
            raise SettingsError("Invalid encoded device data") from exc
        if len(data) > FLASH_SIZE or count is not None and len(data) != count:
            raise SettingsError("Unexpected device read size")
        return data

    def memory(self, address, count):
        return self.read(f"/proc/{self.pid}/mem", address, count)

    def optional_file(self, path):
        status = self.run(f"if [ -L {shlex.quote(path)} ]; then echo LINK; "
                          f"elif [ -f {shlex.quote(path)} ]; then wc -c < {shlex.quote(path)}; "
                          f"elif [ -e {shlex.quote(path)} ]; then echo OTHER; else echo ABSENT; fi").strip()
        if status == "ABSENT":
            return None
        if not status.isdigit() or int(status) > 4096:
            raise SettingsError("Unexpected managed file type or size")
        return self.read(path, count=int(status))

    def snapshot(self):
        self.guard()
        flash = self.read("/dev/mtd6")
        memory = self.memory(BLOCK_ADDRESS, BLOCK_SIZE)
        if len(flash) != FLASH_SIZE or flash[:BLOCK_SIZE] != memory:
            raise SettingsError("Live and saved parameters differ; retry after the camera settles")
        if self.read("/dev/mtd6", count=BLOCK_SIZE) != memory:
            raise SettingsError("Parameters changed during the read")
        pointer = struct.unpack("<I", self.memory(UI_ADDRESS, 4))[0]
        language = next((k for k, v in UI_POINTERS.items() if v == pointer), None)
        if language is None:
            raise SettingsError("Unrecognized UI resource table")
        helper = self.optional_file(HELPER_PATH)
        if helper is not None and helper != helper_bytes():
            raise SettingsError("Existing UI helper is not this version's managed file")
        swapon = self.optional_file(BOOT_PATH)
        without_boot_hook(swapon)  # Refuse unknown shared startup files.
        result = Snapshot(flash, memory, self.optional_file(STATE_PATH), helper, language, self.identity, swapon)
        result.state  # Validate before any operation, including backup.
        return result

    @contextmanager
    def lock(self):
        try:
            self.run(f"mkdir {LOCK_PATH}")
        except SettingsError:
            raise SettingsError(f"Another operation or stale lock exists at {LOCK_PATH}") from None
        try:
            yield
        finally:
            already_failing = sys.exc_info()[0] is not None
            try:
                self.run(f"rmdir {LOCK_PATH}")
            except SettingsError:
                if not already_failing:
                    raise
                print(f"Lock cleanup failed; {LOCK_PATH} may remain.", file=sys.stderr)

    def write_word(self, address, value):
        allowed = {BLOCK_ADDRESS + PARAM_OFFSET + i * 4 for i in WRITABLE} | {UI_ADDRESS}
        if address not in allowed:
            raise SettingsError("Unreviewed memory address")
        self.guard()
        data = struct.pack("<I", value)
        octal = "".join(f"\\{b:03o}" for b in data)
        self.run(f"printf '{octal}' | dd of=/proc/{self.pid}/mem bs=4 "
                 f"seek={address // 4} count=1 conv=notrunc 2>/dev/null")
        if self.memory(address, 4) != data:
            raise SettingsError("Memory write did not verify")

    def set_param(self, index, value):
        if index not in WRITABLE:
            raise SettingsError("Unreviewed parameter index")
        self.write_word(BLOCK_ADDRESS + PARAM_OFFSET + index * 4, value)

    def ui_language(self, language):
        pointer = struct.unpack("<I", self.memory(UI_ADDRESS, 4))[0]
        if pointer not in UI_POINTERS.values():
            raise SettingsError("UI pointer changed unexpectedly")
        self.write_word(UI_ADDRESS, UI_POINTERS[language])

    def set_gps(self, value):
        if value not in (0, 1):
            raise SettingsError("Unknown GPS auto-time value")
        self.guard()
        key = self.memory(0x2860988, 16).hex()
        query = f"-enable={value}&-act=set&-timestamp={int(time.time())}"
        signature = hashlib.md5(("setgpstime.cgi?" + query + key).encode()).hexdigest()
        url = f"http://{self.host}/cgi-bin/setgpstime.cgi?{query}&-signkey={signature}"
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(url, timeout=10) as response:
                result = json.loads(response.read(8192))
        except Exception:
            raise SettingsError("Native GPS save request failed") from None
        if not isinstance(result, dict) or str(result.get("ResultCode")) != "0":
            raise SettingsError("Native GPS save request was rejected")
        actual = struct.unpack("<I", self.memory(BLOCK_ADDRESS + PARAM_OFFSET + 22 * 4, 4))[0]
        if actual != value:
            raise SettingsError("Native GPS setter did not verify")

    def flush(self, original_gps):
        try:
            self.set_gps(1 - original_gps)
        finally:
            self.set_gps(original_gps)

    def file(self, path, data):
        if path not in (STATE_PATH, HELPER_PATH, BOOT_PATH):
            raise SettingsError("Unreviewed persistent path")
        self.guard()
        existing = self.optional_file(path)
        if path == HELPER_PATH and existing is not None and existing != helper_bytes():
            raise SettingsError("Refusing to replace an unrecognized UI helper")
        if existing == data:
            return
        if data is None:
            self.run(f"rm -f {shlex.quote(path)}; sync")
            return
        if len(data) > 4096 or path == HELPER_PATH and data != helper_bytes():
            raise SettingsError("Invalid managed file content")
        if path == BOOT_PATH:
            without_boot_hook(data)
        temporary = str(Path(path).parent / (".70mai-settings-new-" + secrets.token_hex(6)))
        self.run(f"umask 077; : > {shlex.quote(temporary)}")
        try:
            for start in range(0, len(data), 256):
                encoded = "".join(f"\\{b:03o}" for b in data[start:start + 256])
                self.run(f"printf '{encoded}' >> {shlex.quote(temporary)}")
            got = self.run(f"md5sum {shlex.quote(temporary)}").split()[0]
            if got != hashlib.md5(data).hexdigest():
                raise SettingsError("Uploaded file did not verify")
            if path in (HELPER_PATH, BOOT_PATH):
                self.run(f"sh -n {shlex.quote(temporary)}")
            mode = "755" if path in (HELPER_PATH, BOOT_PATH) else "600"
            self.run(f"chmod {mode} {shlex.quote(temporary)} && "
                     f"mv {shlex.quote(temporary)} {shlex.quote(path)} && sync")
        except Exception:
            self.run(f"rm -f {shlex.quote(temporary)}")
            raise

    def reboot(self):
        self.run("sync")
        # Receive completion so the PTY processes the request before closing.
        try:
            self.run("reboot", 10)
        except SettingsError:
            pass  # A closed Telnet session is expected during shutdown.

    def close(self):
        self.transport.close()


def save_backup(snapshot, directory):
    directory = Path(directory).expanduser().resolve()
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    payloads = {"usr1.bin": snapshot.flash, "params.bin": snapshot.memory}
    if snapshot.state_bytes is not None:
        payloads["device-state.json"] = snapshot.state_bytes
    if snapshot.helper is not None:
        payloads["ui-helper.sh"] = snapshot.helper
    if snapshot.swapon is not None:
        payloads["shared-swapon.sh"] = snapshot.swapon
    manifest = {"schema": 1, "firmware_md5": FIRMWARE_MD5, "device": snapshot.identity,
                "ui_language": snapshot.ui_language,
                "sha256": {k: hashlib.sha256(v).hexdigest() for k, v in payloads.items()}}
    payloads["manifest.json"] = (json.dumps(manifest, indent=2) + "\n").encode()
    for name, data in payloads.items():
        with (directory / name).open("xb") as stream:
            os.chmod(directory / name, 0o600)
            stream.write(data)
    return directory


def load_backup(directory, current):
    directory = Path(directory).expanduser().resolve()
    manifest = json.loads((directory / "manifest.json").read_text())
    if (manifest.get("schema"), manifest.get("firmware_md5"), manifest.get("device")) != (
            1, FIRMWARE_MD5, current.identity):
        raise SettingsError("Backup is from another device, firmware, or format")
    hashes = manifest.get("sha256", {})
    names = {"usr1.bin", "params.bin", "device-state.json", "ui-helper.sh", "shared-swapon.sh"}
    if not isinstance(hashes, dict) or not {"usr1.bin", "params.bin"} <= hashes.keys() or not hashes.keys() <= names:
        raise SettingsError("Invalid backup manifest")
    data = {}
    for name, digest in hashes.items():
        file = directory / name
        if file.stat().st_size > FLASH_SIZE:
            raise SettingsError("Invalid backup file size")
        data[name] = file.read_bytes()
        if hashlib.sha256(data[name]).hexdigest() != digest:
            raise SettingsError("Backup checksum mismatch")
    original = Snapshot(data["usr1.bin"], data["params.bin"], data.get("device-state.json"),
                        data.get("ui-helper.sh"), manifest.get("ui_language"), current.identity, data.get("shared-swapon.sh"))
    if len(original.flash) != FLASH_SIZE or original.flash[:BLOCK_SIZE] != original.memory:
        raise SettingsError("Inconsistent backup parameters")
    original.state
    if original.ui_language not in UI_POINTERS or original.helper not in (None, helper_bytes()):
        raise SettingsError("Unsupported UI backup")
    vals = original.params
    if vals[20] not in range(36) or vals[21] not in DATE_ORDERS.values() or vals[41] not in UNITS.values():
        raise SettingsError("Invalid setting values in backup")
    # Restore only the three owned native slots; retain all newer camera settings.
    return replace(current, memory=with_parameters(current.memory, {i: vals[i] for i in WRITABLE}),
                   state_bytes=original.state_bytes, helper=original.helper, ui_language=original.ui_language,
                   swapon=with_boot_hook(current.swapon, original.helper is not None))


def apply(device, before, desired):
    changes = {i: desired.params[i] for i in WRITABLE if desired.params[i] != before.params[i]}
    gps = before.params[22]
    if gps not in (0, 1):
        raise SettingsError("Unknown GPS auto-time setting")
    if device.snapshot() != before:
        raise SettingsError("Camera settings changed after backup; no changes applied")
    if changes:
        device.set_gps(gps)  # Authenticate without changing or saving anything.
    touched = []
    files_touched = False
    try:
        for index, value in changes.items():
            # Include a slot before the attempt: failed readback may follow a write.
            touched.append(index)
            device.set_param(index, value)
        if changes:
            if device.memory(BLOCK_ADDRESS, BLOCK_SIZE) != desired.memory:
                raise SettingsError("Concurrent parameter change; reverting owned settings")
            device.flush(gps)
            flash = device.read("/dev/mtd6")
            if flash[:BLOCK_SIZE] != desired.memory or flash[PAD_END:] != before.flash[PAD_END:]:
                raise SettingsError("Saved parameters did not verify")
        files_touched = True
        device.file(HELPER_PATH, desired.helper)
        if device.optional_file(BOOT_PATH) != before.swapon:
            raise SettingsError("Shared startup script changed during the operation")
        device.file(BOOT_PATH, desired.swapon)
        device.file(STATE_PATH, desired.state_bytes)
        if desired.ui_language != before.ui_language:
            device.ui_language(desired.ui_language)
        after = device.snapshot()
        if (after.memory, after.state_bytes, after.helper, after.ui_language, after.swapon) != (
                desired.memory, desired.state_bytes, desired.helper, desired.ui_language, desired.swapon):
            raise SettingsError("Final readback did not verify")
    except Exception as exc:
        errors = []
        for index in touched:
            try:
                device.set_param(index, before.params[index])
            except Exception:
                errors.append(f"parameter {index}")
        if touched:
            try:
                device.flush(gps)
            except Exception:
                errors.append("native save / GPS restore")
        if files_touched:
            try:
                # Preserve changes another owner may have made to the shared wrapper.
                existing = device.optional_file(BOOT_PATH)
                had_hook = before.swapon is not None and BOOT_HOOK in before.swapon
                device.file(BOOT_PATH, with_boot_hook(existing, had_hook))
            except Exception:
                errors.append("startup hook")
            for path, data in ((HELPER_PATH, before.helper), (STATE_PATH, before.state_bytes)):
                try:
                    device.file(path, data)
                except Exception:
                    errors.append(path)
            try:
                device.ui_language(before.ui_language)
            except Exception:
                errors.append("UI table")
        try:
            recovered = device.snapshot()
            if any(recovered.params[i] != before.params[i] for i in touched + [22]):
                errors.append("recovery parameter readback")
            if files_touched and (recovered.helper, recovered.state_bytes, recovered.ui_language) != (
                    before.helper, before.state_bytes, before.ui_language):
                errors.append("recovery file/UI readback")
            if files_touched and bool(recovered.swapon and BOOT_HOOK in recovered.swapon) != bool(
                    before.swapon and BOOT_HOOK in before.swapon):
                errors.append("recovery startup hook readback")
        except Exception:
            errors.append("recovery verification")
        if errors:
            raise SettingsError("Operation failed; recovery is incomplete for: " + ", ".join(errors)) from None
        raise SettingsError(f"Operation failed; owned settings restored: {exc}") from None


def describe(snapshot):
    values = snapshot.params
    state = snapshot.state
    tracked = state is not None and state["dst"] is not None and state["effective_enum"] == values[20]
    return {
        "firmware": "DR2830 CN 1.2.42", "device": snapshot.identity[:12],
        "ui_language": snapshot.ui_language, "ui_helper_installed": snapshot.helper is not None,
        "ui_boot_hook_installed": bool(snapshot.swapon and BOOT_HOOK in snapshot.swapon),
        "native_language_parameter": values[23],
        "units": next((k for k, v in UNITS.items() if v == values[41]), f"unknown:{values[41]}"),
        "date_order": next((k for k, v in DATE_ORDERS.items() if v == values[21]), f"unknown:{values[21]}"),
        "gps_auto_time": bool(values[22]), "effective_utc_offset": offset_text(effective_offset(values[20])),
        "base_utc_offset": offset_text(state["base_utc_offset_minutes"]) if tracked else None,
        "manual_dst": state["dst"] if tracked else None,
        "timezone_metadata_stale": bool(state and state["effective_enum"] is not None and not tracked),
    }


def default_backup_directory():
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path.home() / ".local/state/70mai-settings/backups" / (stamp + "-" + secrets.token_hex(3))


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Change hidden settings on a 70mai A810Pro (DR2830 CN 1.2.42) over Telnet.")
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("--host", default="192.168.0.1", help="camera IP address (default 192.168.0.1)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("show", help="print the current settings")
    commands.add_parser("options", help="list accepted values (does not connect)")
    backup = commands.add_parser("backup", help="save a backup of the settings partition and managed files")
    backup.add_argument("directory", type=Path, help="new directory to create for the backup")
    change = commands.add_parser("set", help="change settings (backs up first, then saves and verifies)")
    change.add_argument("--language", choices=UI_POINTERS, help="menu language")
    change.add_argument("--units", choices=UNITS, help="speed units")
    change.add_argument("--date-order", choices=DATE_ORDERS, help="date format: year/month/day, month/day/year or day/month/year")
    change.add_argument("--utc-offset", type=parse_offset, metavar="±HH:MM",
                        help="standard-time UTC offset, e.g. --utc-offset=-07:00")
    change.add_argument("--dst", choices=("on", "off"), help="daylight saving time: adds one hour to --utc-offset")
    change.add_argument("--dry-run", action="store_true", help="show what would change without writing anything")
    change.add_argument("--backup-dir", type=Path, help="where to put the automatic backup (must not exist yet)")
    change.add_argument("--reboot", action="store_true", help="reboot the camera afterwards")
    restore = commands.add_parser("restore", help="put back the settings this tool manages from a backup")
    restore.add_argument("directory", type=Path, help="backup directory to restore from")
    restore.add_argument("--dry-run", action="store_true", help="show what would change without writing anything")
    restore.add_argument("--backup-dir", type=Path, help="where to put the automatic backup (must not exist yet)")
    restore.add_argument("--reboot", action="store_true", help="reboot the camera afterwards")
    args = parser.parse_args(argv)
    if args.command == "options":
        print(json.dumps({"language": list(UI_POINTERS), "units": list(UNITS),
                          "date_order": list(DATE_ORDERS),
                          "effective_utc_offsets": [offset_text(m) for m in OFFSETS.values()],
                          "dst": "manual +60 minutes; effective UTC+06:00 is unavailable"}, indent=2))
        return 0
    device = None
    try:
        device = Device(args.host)
        if args.command == "show":
            print(json.dumps(describe(device.snapshot()), indent=2))
            return 0
        with device.lock():
            before = device.snapshot()
            if args.command == "backup":
                print(save_backup(before, args.directory))
                return 0
            if args.command == "set":
                desired = plan(before, language=args.language, units=args.units, date_order=args.date_order,
                               utc_offset=args.utc_offset, dst=None if args.dst is None else args.dst == "on")
            else:
                desired = load_backup(args.directory, before)
            print(json.dumps({"before": describe(before), "after": describe(desired)}, indent=2))
            if args.dry_run:
                print("Dry run: nothing was changed.")
                return 0
            if (before.memory, before.state_bytes, before.helper, before.ui_language, before.swapon) == (
                    desired.memory, desired.state_bytes, desired.helper, desired.ui_language, desired.swapon):
                print("Already set; nothing to do.")
                unchanged = True
            else:
                unchanged = False
                directory = save_backup(before, args.backup_dir or default_backup_directory())
                print(f"Backup: {directory}", flush=True)
                try:
                    apply(device, before, desired)
                except SettingsError:
                    print(f"Recovery backup: {directory}", file=sys.stderr)
                    raise
        if not unchanged:
            print("Saved and verified. Leave and reopen the menu or screensaver to see the change.")
        if before.params[21] != desired.params[21] or before.params[41] != desired.params[41]:
            print("Reboot the camera so the video watermark picks up the new units/date format.")
        if args.reboot:
            device.reboot()
            print("Rebooting. Reconnect to the camera's Wi-Fi when it is back up.")
        return 0
    except (SettingsError, OSError, ValueError, KeyError, struct.error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        if device is not None:
            device.close()


if __name__ == "__main__":
    raise SystemExit(main())
