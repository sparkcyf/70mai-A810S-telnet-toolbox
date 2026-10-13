# 70mai A810Pro: Telnet, hidden settings and a better screensaver

[中文说明](README.zh-CN.md)

The China-market A810Pro hides several settings that the hardware and firmware
already support: an English menu, mph, other date formats and time zones outside
China. This repo first gets you a root shell on the camera over Telnet, then
changes its configuration to bring back the English menu, time zones and the
other hidden settings.

1. **Enable Telnet** — a one-time install over USB in factory mode.
2. **Change settings** — language, speed units, date format, time zone and
   daylight saving time, over the camera's Wi-Fi.
3. **Screensaver overlay (optional)** — adds time, heading, GPS position,
   altitude, satellite count and mph to the speed screensaver, and keeps a
   second GPS log that includes altitude.

> [!IMPORTANT]
> Only tested on the **70mai A810Pro (model DR2830), China firmware 1.2.42**.
> Every tool checks the model and firmware first and stops on anything else.

You need a Mac, Python 3.10 or newer, and a USB cable that carries data. Nothing
needs to be installed with pip.

Before you start:

- Getting into factory mode requires a **factory reset**, which wipes your
  camera settings. Write down anything you want to set again.
- Telnet gives anyone on the camera's Wi-Fi a root shell **without a password**.
  Keep the Wi-Fi password to yourself, and turn Wi-Fi off when you don't need it.
- You are modifying your own camera at your own risk.

| Folder | What it does |
|---|---|
| [`telnet/`](telnet) | `macos.py`: installs the Telnet hook over the USB serial port |
| [`settings/`](settings) | `70mai-settings`: reads and changes settings over Telnet |
| [`ssoverlay/`](ssoverlay) | `deploy.py` and the prebuilt `ssoverlay` overlay program |

Run every command below from the top of this repo.

For the reverse-engineering process behind the hidden entry, see
[Finding the factory-mode entry and USB serial command interface](docs/factory-mode-and-usb-serial-discovery.md).

---

## Part 1: Enable Telnet

### 1.1 Enter factory mode

Power the camera normally (car or USB power adapter, not your computer).

1. In the camera's settings, choose **Factory reset** and wait for it to reboot.
2. Stop on the **first welcome screen** (just a 70mai logo and a ✅ in the
   bottom-right corner). Don't go on to the time setup.
3. Facing the screen, the four buttons are `1  [2]  3  4` from left to right.
   Press **button 2** five times: short presses, about 0.3–0.5 s apart (you
   should hear a beep on each press), never more than about 1 s apart.
4. The camera reboots by itself into a factory/engineering screen.

The button trick only works on that first welcome screen, not on the time setup
page or anywhere else. If you've already moved past it, factory reset again.

### 1.2 Install from your Mac

With the factory screen showing, connect the camera to your Mac with the USB
cable and run:

```sh
python3 telnet/macos.py ports
python3 telnet/macos.py check
python3 telnet/macos.py install
```

- `ports` lists the serial ports it can see.
- `check` only reads: it shows the model, firmware and whether the hook is
  already installed.
- `install` checks again, makes backups, installs two small scripts and verifies
  them.

If exactly one `/dev/cu.usbmodem…` port exists it is picked automatically. With
more than one, name it:

```sh
python3 telnet/macos.py --port /dev/cu.usbmodem1101 install
```

When it's done you'll see the paths of the Mac and camera backups, followed by
`Installed and verified.` Running `install` again just says it is already
installed.

### 1.3 Go back to normal mode and test

1. In the factory menu, choose **恢复出厂** (factory reset, at the very bottom
   of the menu). The camera reboots.
2. Unplug it from the Mac, power it normally, and go through the normal welcome
   and time setup.
3. Turn on the camera's Wi-Fi and join it from your Mac.
4. Telnet to `192.168.0.1`, user `root`, no password:

   ```sh
   telnet 192.168.0.1
   ```

   Run `id`; it should print `uid=0(root)`. Type `exit` to leave.

macOS no longer ships a `telnet` command. Either `brew install telnet`, or skip
it and go straight to Part 2: `./settings/70mai-settings show` logs in by itself
and is a good test on its own.

From now on Telnet starts every time the camera's Wi-Fi is turned on, including
after reboots. You don't need factory mode again.

### Turning Telnet off

These commands run **on the camera**, in a Telnet session, not on your Mac.

Stop Telnet from starting automatically:

```sh
touch /usr/bin/70mai-telnet.disabled
sync
```

Turn it back on:

```sh
rm -f /usr/bin/70mai-telnet.disabled
sync
```

Either change takes effect the next time Wi-Fi is turned on. A Telnet server
that is already running keeps going until the camera reboots.

### Uninstalling

From factory mode (same USB setup as installing):

```sh
python3 telnet/macos.py uninstall
```

It backs up first, and only removes files that exactly match what it installed.

Without factory mode, from a Telnet session on the camera, first check the
files are the ones this tool installed:

```sh
md5sum /usr/bin/hostapd /usr/bin/70mai-telnet-start
```

Only if they are `551795c98df05487a9e6349d78f64e6c` and
`d31f9adc33086436184fe58895e5f7c7`, remove them (and the `.disabled` file, if you
made one):

```sh
rm /usr/bin/hostapd /usr/bin/70mai-telnet-start
sync
```

Your current session keeps working until the camera reboots. After that, Telnet
is gone until you install it again.

### Trying other models or firmware

`--skip-version-check` (before the subcommand) skips the model, version and
stock-file checksum checks:

```sh
python3 telnet/macos.py --skip-version-check check
python3 telnet/macos.py --skip-version-check install
```

It still checks that the camera is laid out the way this method needs: a root
shell on the serial port, `/usr/bin` on persistent, writable UBIFS, the stock
`hostapd` and `telnetd` where expected, `/usr/bin` ahead of `/mnt/app/bin` in
`main_app`'s `PATH`, and the Wi-Fi script calling `hostapd` by name. It stops if
any of that differs.

This is untested. The factory-mode button trick has only been confirmed on the
DR2830 China 1.2.42. The flag only applies to the Telnet installer: Parts 2 and 3
depend on memory addresses specific to that firmware and refuse to run on
anything else.

### Troubleshooting

| Problem | What to try |
|---|---|
| Five presses do nothing | Make sure you're on the first welcome screen and pressing the second button from the left. Short presses, under a second apart. |
| The Mac sees a USB drive, not a serial port | The camera isn't in factory mode. In normal mode, USB means file transfer. |
| No `/dev/cu.usbmodem…` | Check the cable carries data, try another port, and confirm the factory screen is showing. No driver is needed. |
| Several serial ports | Pass `--port`, or unplug the other USB serial devices. |
| Firmware or file mismatch | Your firmware isn't the tested one. See "Trying other models or firmware". |
| Layout mismatch with `--skip-version-check` | This method won't work on that firmware. |
| A file with the same name already exists | Something else (another mod?) put it there. The installer won't overwrite it; find out where it came from first. |
| Timeout or checksum failure | Keep the backup folder it printed. Reconnect and run `check` before doing anything else. |
| "lock may still be on the camera" | Make sure no other copy of the installer is running, then reboot the camera. The lock is in `/tmp` and disappears on reboot. |
| Telnet won't connect | The camera must be booted normally with Wi-Fi on, and your Mac must be on the camera's network. Check you haven't created the `.disabled` file. |

---

## Part 2: Language, units, date and time zone

Join the camera's Wi-Fi, then:

```sh
./settings/70mai-settings show       # current settings
./settings/70mai-settings options    # every accepted value
```

The wrapper just runs `python3 settings/mai_settings.py`. Global options such
as `--host` go before the subcommand.

### Common changes

Preview first. `--dry-run` shows before/after and changes nothing:

```sh
./settings/70mai-settings set --language en --units mph --date-order mdy --dry-run
```

Then apply:

```sh
./settings/70mai-settings set --language en --units mph --date-order mdy
```

| Option | Values |
|---|---|
| `--language` | `en`, `zh-CN` |
| `--units` | `kmh`, `mph` |
| `--date-order` | `ymd` (2026/10/12), `mdy` (10/12/2026), `dmy` (12/10/2026) |
| `--utc-offset` | your standard-time offset, e.g. `--utc-offset=-07:00` or `--utc-offset=+05:45` |
| `--dst` | `on` or `off` |
| `--reboot` | reboot when done |

Leave and reopen the menu or screensaver to see the new values. The video
watermark only picks up new units and date format after a reboot, so add
`--reboot` or power-cycle the camera. You'll need to rejoin its Wi-Fi
afterwards.

### Time zone and daylight saving time

The China firmware has a time zone setting but no DST switch. The tool handles
DST by choosing the time zone one hour ahead, and remembers your standard offset
on the camera so switching DST on and off is simple:

```sh
# First time: give your standard offset (US Mountain Time is -07:00)
./settings/70mai-settings set --utc-offset=-07:00 --dst off

# Later, when the clocks change:
./settings/70mai-settings set --dst on     # camera now uses UTC-6
./settings/70mai-settings set --dst off    # back to UTC-7
```

- Running `--dst on` twice does not add two hours.
- Nothing switches automatically. You have to run it in spring and autumn.
- Give `--utc-offset` the first time, and again if the time zone was changed
  some other way. If `show` says `"manual_dst": null`, it means the tool doesn't
  know your standard offset, not that DST is off.
- **UTC+6 isn't available.** The China firmware treats that time zone entry as
  UTC+8, so the tool refuses any combination that ends up at UTC+6. Half- and
  quarter-hour offsets such as +05:30 and +05:45 work.
- This is the offset used when the camera sets its clock from GPS. It doesn't
  turn GPS time sync on if you've turned it off.

### Backups and going back

Every `set` or `restore` first saves a backup to
`~/.local/state/70mai-settings/backups/` (or `--backup-dir`, which must not
exist yet). You can also make one yourself:

```sh
./settings/70mai-settings backup ./my-camera-backup
./settings/70mai-settings restore ./my-camera-backup --dry-run
./settings/70mai-settings restore ./my-camera-backup
```

`restore` only puts back what this tool manages (time zone, units, date format,
menu language). Things you changed later, such as video quality or volume, are
left alone. The backup contains your camera's configuration, so keep it on your
own computer.

To go back to the stock Chinese setup:

```sh
./settings/70mai-settings set --language zh-CN --units kmh --date-order ymd
```

### How the English menu works

Changing the firmware's language setting alone doesn't work: the China firmware
forces Chinese at startup. It does contain the English text, though, so the tool
points the menu at the English text in memory instead.

Because that has to happen again after every boot, choosing English installs a
small script, `/usr/bin/70mai-settings-ui`, and adds a marked block to
`/usr/bin/swapon` that starts it. (`/usr/bin` survives reboots; `/etc` does not.)
At boot the script waits for `main_app`, checks its MD5, swaps the pointer and
exits. Its log is `/tmp/70mai-settings-ui.log`.

Choosing `zh-CN` removes the script and the marked block and restores the
pointer. Voice prompts and voice control are not touched.

If only `main_app` restarts (without a full reboot), run
`set --language en` again.

### What has been tested on the camera

Confirmed: English menus and automatic re-apply after boot, mph on the
screensaver, GPS information on the screensaver, km/h–mph conversion,
month/day/year on screen, settings surviving a reboot, backup and restore, and
turning DST on, on again, and off.

---

## Part 3 (optional): Screensaver overlay

![Speed screensaver with the overlay (demo data)](docs/screensaver-overlay.png)

`ssoverlay` adds to the speed screensaver:

- top left, next to REC: altitude (metres above mean sea level)
- top centre: the time
- top right: heading, a GPS dot (green with a fix, grey without) and the number
  of satellites used
- bottom left: latitude and longitude (`No GPS fix` without a fix)
- bottom right: speed in mph

It only draws while the **speed screensaver** is on screen, so select that
screensaver in the camera's settings. It never writes to `main_app` or its
settings. It only draws over pixels that are black or that it drew itself, so if
a popup appears it skips that frame. On startup it checks `main_app`'s MD5 and 152
code locations it depends on, and exits if anything differs.

### GPS log (gpsx)

The stock `GPSData*.txt` has no altitude. `ssoverlay` writes its own CSV log,
`/mnt/sd/gpsx/GPSX.csv`, whenever it is running, whether or not the screensaver
is showing:

- One line per GPS update (1 Hz). Like the stock log it writes 5 lines at a
  time; each batch is synced to the card and the file is closed again, so it
  never keeps the SD card busy (formatting still works). A power cut loses at
  most about 5 seconds.
- At 100 MB the file is renamed to `GPSX.old.csv` and a new one is started, so
  there are at most two files. A line is about 110 bytes, about 0.4 MB per hour.
- Nothing is written until the GPS has a date. Without a fix `status` is `V`
  and the position columns are empty.

| Column | Meaning |
|---|---|
| `unix`, `utc` | GPS time (true UTC). On our camera the stock `GPSData` timestamps were 28800 s smaller |
| `status` | `A` fix / `V` no fix |
| `lat`, `lon` | decimal degrees, 7 decimals |
| `alt_m` | altitude above mean sea level, metres |
| `speed_kn`, `course` | speed over ground (knots), course (degrees; meaningless when stationary) |
| `ui_kmh` | the speed the screensaver shows |
| `fix_quality`, `fix_mode` | GGA fix quality; GSA 1/2/3 = none/2D/3D |
| `sats_used`, `sats_in_view` | satellites used in the fix, satellites in view across all constellations |
| `hdop`, `pdop`, `vdop` | dilution of precision |
| `temp_c` | SoC temperature |

### Try it without installing

This copies the program to `/tmp`, so it's gone after a reboot:

```sh
python3 ssoverlay/deploy.py upload
python3 ssoverlay/deploy.py start          # live GPS data
python3 ssoverlay/deploy.py start fake     # demo data, handy indoors
python3 ssoverlay/deploy.py snap shot.png  # screenshot of the camera screen
python3 ssoverlay/deploy.py stop
```

### Install

```sh
python3 ssoverlay/deploy.py install
```

This adds `/usr/bin/ssoverlay`, `/usr/bin/ssoverlay-start` and a `/usr/bin/swapon`
hook. After each boot the overlay starts about 20 seconds after `main_app`.

It shares `/usr/bin/swapon` with Part 2's English menu, and doing Parts 2 and 3
in either order is fine: each keeps the other's part of the file. If
`/usr/bin/swapon` contains anything else, install stops without touching it.

Logs: `/tmp/ssoverlay.log`, and `ssoverlay.log` in the root of the SD card
(rotated at 1 MB). The GPS log adds a line there when writing starts working or
fails (`gpsx ok` / `gpsx error -N`).

### Turn it off or remove it

- Turn it off: put an empty file named `ssoverlay.off` in the root of the SD
  card, or run `touch /usr/bin/ssoverlay.disabled` on the camera. Takes effect at
  the next boot and stops the GPS log too.
- Stop it now: `python3 ssoverlay/deploy.py stop`
- Remove it: `python3 ssoverlay/deploy.py uninstall` (keeps the English-menu hook
  if you have one)

### Limitations

- It's a prototype. Speed is always shown in mph and altitude in metres.
- The GPS log's 100 MB rollover hasn't been triggered on a camera yet (renaming
  over an existing file on the SD card was checked).
- Tested on the camera: live GPS data (positions match the stock GPS log for the
  same second), altitude and satellite count, writing the GPS log, starting at
  boot, and coexisting with the English menu. Installing it **after** the
  English menu goes through new code in `deploy.py`; the resulting `swapon` file
  is byte-for-byte the one tested on the camera, but that path itself has only
  been run in unit tests.

### Building from source

The repo includes a prebuilt `ssoverlay` (MD5 `646f7c0bf1def2e43d75cc48d5c25ba4`),
so you don't need to build it. To rebuild you need Homebrew `llvm` and `lld`, plus
your own copy of the camera's `main_app` (for example from `/proc/<pid>/exe`):
the font and code fingerprints are extracted from it. `main_app` is 70mai's code,
so it isn't included here.

```sh
MAIN_APP_ELF=/path/to/main_app ./ssoverlay/build.sh
```

---

## How Telnet is installed

The installer adds two scripts and changes no stock files:

- `/usr/bin/hostapd`: a wrapper. `main_app` finds it before the real
  `/mnt/app/bin/hostapd` because `/usr/bin` comes first in its `PATH`. When Wi-Fi
  starts, it launches `70mai-telnet-start` in the background and then hands over
  to the real `hostapd` with the same arguments.
- `/usr/bin/70mai-telnet-start`: waits for the Wi-Fi address to appear, then
  starts the stock `telnetd` on `192.168.0.1:23` with the stock `/bin/login`.

`/usr/bin` is on persistent flash; files added under `/etc/init.d` disappear on
reboot, which is why it hooks Wi-Fi startup instead. Everything goes through the
factory serial shell as ordinary file writes. It doesn't write flash partitions
directly and doesn't touch `main_app`, the boot scripts or factory data.

Backups:

- On the Mac: `~/.local/state/70mai-telnet/backups/` (or `--backup-dir`), with a
  `manifest.json` of what was done.
- On the camera: `/mnt/other/70mai-telnet-backup-…/`, with the stock `hostapd`
  (`vendor-hostapd`), related boot scripts, and any earlier copy of the wrapper
  (`previous-hostapd`). Uninstalling doesn't delete these.

The manual steps and both installed scripts have been tested on a camera: factory mode, serial install, Telnet login after returning to normal
mode, and Telnet still working after a reboot. `macos.py` itself, which automates
those steps, has so far only been tested on a Mac against a simulated serial port.
Surviving a firmware update hasn't been tested; an update may remove the files.

## Running the tests

```sh
python3 -m unittest discover -s telnet/tests -v
python3 -m unittest discover -s settings/tests -v
python3 -m unittest discover -s ssoverlay/tests -v
```

Two Telnet tests compare against files captured from a real camera and are
skipped unless `MAI_CAPTURES` points to those captures.
