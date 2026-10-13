#!/usr/bin/env python3
"""Install, try out or remove the ssoverlay speed-screensaver overlay over Telnet.

Everything except install/uninstall stays in /tmp and is gone after a reboot.
"""
import argparse
import base64
import gzip
import hashlib
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "settings"))
from mai_settings import BOOT_HOOK, CREATED_BOOT_BASE  # noqa: E402

PROMPT = rb"root@NVTEVM:[^\r\n]*\$\s*$"
SHEBANG = b"#!/bin/sh\n"
INSTALL = [("ssoverlay", "ssoverlay"), ("install/ssoverlay-start", "ssoverlay-start")]

# /usr/bin/swapon is shared with 70mai-settings, which adds a marked block
# after the shebang. These are the only four states we accept.
OURS = (HERE / "install/swapon").read_bytes()
OURS_HOOKED = SHEBANG + BOOT_HOOK + OURS[len(SHEBANG):]
SETTINGS_ONLY = SHEBANG + BOOT_HOOK + CREATED_BOOT_BASE[len(SHEBANG):]
SWAPON = {hashlib.md5(v).hexdigest(): v for v in (OURS, OURS_HOOKED, SETTINGS_ONLY)}


class Telnet:
    """Minimal Telnet client: refuses every option, strips IAC sequences."""

    def __init__(self, host):
        self.sock = socket.create_connection((host, 23), timeout=4)
        self.sock.settimeout(1)
        self.pending = b""
        self.receive(rb"login:\s*$")
        self.send("root")
        self.receive(PROMPT)

    def receive(self, pattern, timeout=8):
        output = bytearray()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data = self.pending + self.sock.recv(8192)
            except socket.timeout:
                continue
            self.pending = b""
            if not data:
                raise SystemExit("Telnet connection closed by the camera")
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
                return bytes(output).decode(errors="replace")
        raise SystemExit("Timed out waiting for the camera")

    def send(self, command):
        if any(c in command for c in "\r\n\0"):
            raise ValueError("One command per line")
        self.sock.sendall(command.encode() + b"\r\n")

    def close(self):
        try:
            self.send("exit")
        finally:
            self.sock.close()


def run(c, cmd, timeout=20):
    c.send(cmd)
    out = c.receive(PROMPT, timeout=timeout).replace("\r", "")
    return "\n".join(out.split("\n")[1:-1])


def upload(c, data, name):
    """Copy data to /tmp/<name> with uudecode and check its MD5."""
    b64 = base64.encodebytes(data).decode().split()
    c.send(f"uudecode -o /tmp/{name}.new")
    c.send(f"begin-base64 755 {name}")
    step = 8  # stay well under the 4 KB pty line buffer; 40 lines at once lost data
    for i in range(0, len(b64), step):
        for line in b64[i:i + step]:
            c.send(line)
        c.receive(re.escape(b64[min(i + step - 1, len(b64) - 1)].encode()), timeout=60)
    c.send("====")
    c.receive(PROMPT, timeout=30)
    md5 = run(c, f"md5sum /tmp/{name}.new").split()[0]
    want = hashlib.md5(data).hexdigest()
    if md5 != want:
        raise SystemExit(f"Upload of {name} failed its checksum ({md5} != {want})")
    print(run(c, f"chmod 755 /tmp/{name}.new && mv /tmp/{name}.new /tmp/{name} && ls -la /tmp/{name}"))
    return want


def current_swapon(c):
    out = run(c, "[ -e /usr/bin/swapon ] && md5sum /usr/bin/swapon || echo ABSENT").strip()
    return None if out == "ABSENT" else out.split()[0]


def install(c):
    cur = current_swapon(c)
    if cur is None or SWAPON.get(cur) == OURS:
        swapon = OURS
    elif SWAPON.get(cur) in (OURS_HOOKED, SETTINGS_ONLY):
        swapon = OURS_HOOKED  # keep the 70mai-settings block
    else:
        raise SystemExit(f"/usr/bin/swapon was not written by ssoverlay or 70mai-settings (md5 {cur}); not touching it")
    files = [((HERE / src).read_bytes(), name) for src, name in INSTALL] + [(swapon, "swapon")]
    for data, name in files:
        upload(c, data, name)
    for name in ("ssoverlay-start", "swapon"):
        if "SYNTAX_OK" not in run(c, f"sh -n /tmp/{name} && echo SYNTAX_OK"):
            raise SystemExit(f"{name} failed the shell syntax check")
    for _, name in files:
        print(run(c, f"cp /tmp/{name} /usr/bin/{name}.new && chmod 755 /usr/bin/{name}.new && "
                     f"mv /usr/bin/{name}.new /usr/bin/{name} && echo installed {name}"))
    print(run(c, "sync; md5sum /usr/bin/ssoverlay /usr/bin/ssoverlay-start /usr/bin/swapon"))
    print("Installed. The overlay starts about 20 seconds after the next boot.")


def uninstall(c):
    print(run(c, "killall ssoverlay 2>/dev/null; rm -f /usr/bin/ssoverlay-start /usr/bin/ssoverlay; sync; echo removed"))
    cur = current_swapon(c)
    kind = SWAPON.get(cur)
    if cur is None or kind == SETTINGS_ONLY:
        pass
    elif kind == OURS:
        run(c, "rm -f /usr/bin/swapon; sync")
    elif kind == OURS_HOOKED:
        # Hand /usr/bin/swapon back to 70mai-settings in the form it creates itself.
        upload(c, SETTINGS_ONLY, "swapon")
        run(c, "cp /tmp/swapon /usr/bin/swapon.new && chmod 755 /usr/bin/swapon.new && "
               "mv /usr/bin/swapon.new /usr/bin/swapon && sync")
    else:
        print(f"Left /usr/bin/swapon alone: it was not written by ssoverlay (md5 {cur}).")
    print("Uninstalled.")


def snap(c, out):
    text = run(c, "gzip -c /dev/fb0 | uuencode -m fb.gz", timeout=60)
    body = text.split("begin-base64", 1)[1].split("\n", 1)[1].split("====")[0]
    raw = gzip.decompress(base64.b64decode(re.sub(r"\s", "", body)))
    tmp = out.with_suffix(".raw")
    tmp.write_bytes(raw)
    subprocess.check_call([sys.executable, "-I", str(HERE / "fb2png.py"), str(tmp), str(out)])
    tmp.unlink()
    print(out)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="192.168.0.1", help="camera IP address (default 192.168.0.1)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("install", help="install to /usr/bin so the overlay starts on every boot")
    sub.add_parser("uninstall", help="remove the files that install added")
    sub.add_parser("upload", help="copy ./ssoverlay to /tmp/ssoverlay (for trying it without installing)")
    start = sub.add_parser("start", help="run /tmp/ssoverlay in the background")
    start.add_argument("mode", nargs="?", default="real", choices=("real", "fake", "fakenofix", "calib"),
                       help="real: live GPS data (default); fake/fakenofix: demo data; calib: draw a ruler")
    sub.add_parser("stop", help="stop the overlay (it erases what it drew)")
    sub.add_parser("clean", help="stop the overlay and blank its areas once")
    sub.add_parser("status", help="show whether it is running and the end of its log")
    shot = sub.add_parser("snap", help="save a screenshot of the camera screen as PNG")
    shot.add_argument("out", type=Path, help="output .png path")
    args = parser.parse_args()

    c = Telnet(args.host)
    try:
        if args.command == "install":
            install(c)
        elif args.command == "uninstall":
            uninstall(c)
        elif args.command == "upload":
            upload(c, (HERE / "ssoverlay").read_bytes(), "ssoverlay")
        elif args.command == "start":
            print(run(c, "killall ssoverlay 2>/dev/null; sleep 1; "
                         f"nohup /tmp/ssoverlay $(pidof main_app) {args.mode} >/tmp/ssoverlay.log 2>&1 & "
                         "sleep 2; pidof ssoverlay; cat /tmp/ssoverlay.log"))
        elif args.command == "clean":
            print(run(c, "killall ssoverlay 2>/dev/null; sleep 1; b=/tmp/ssoverlay; [ -x $b ] || b=/usr/bin/ssoverlay; "
                         "$b $(pidof main_app) clean"))
        elif args.command == "stop":
            print(run(c, "killall ssoverlay; sleep 1; pidof ssoverlay; cat /tmp/ssoverlay.log"))
        elif args.command == "status":
            print(run(c, "pidof ssoverlay; tail -n 20 /tmp/ssoverlay.log; "
                         "grep VmRSS /proc/$(pidof ssoverlay)/status 2>/dev/null"))
        elif args.command == "snap":
            snap(c, args.out)
    finally:
        c.close()


if __name__ == "__main__":
    main()
