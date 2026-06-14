#!/usr/bin/env python3
"""Drive a paired Pixel over KDE Connect: library + CLI.

This is the *stable* transport. Where pixel.py (adb) is stateless and pays a
rediscover/reconnect tax on every call over Wireless debugging's dynamic port,
KDE Connect keeps a persistent, cert-pinned TLS link warm in its own daemon
(kdeconnectd) and heals drops in the background. Pair once on the phone and the
link survives reboots; each request here is a fast local call into the daemon.

It does NOT replace adb — KDE Connect cannot screencap the display or run a
shell. The two are complementary: KDE Connect for camera capture, input,
notifications, file transfer, battery/connectivity, ring/find; adb (pixel.py)
for screenshot + raw shell. pixel_mcp.py exposes both.

Backends:
    kdeconnect-cli   actions (ping, ring, share, photo, send-keys, sms, ...)
    gdbus            rich queries the CLI omits (battery, connectivity, paired)

Override targeting with the environment:
    KDECONNECT_CLI     kdeconnect-cli binary       (default on PATH)
    KDECONNECT_DEVICE  device id to target         (default: auto-resolve)
    KDECONNECT_NAME    device name to match        (default: first paired one)
    PIXEL_PULL_DIR     where pulled photos land     (default ~/Pictures/pixel)
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

CLI = os.environ.get("KDECONNECT_CLI", "kdeconnect-cli")
GDBUS = os.environ.get("GDBUS", "gdbus")
DEVICE = os.environ.get("KDECONNECT_DEVICE")  # None -> auto-resolve
NAME = os.environ.get("KDECONNECT_NAME")  # None -> first paired+reachable
PULL_DIR = Path(os.environ.get("PIXEL_PULL_DIR",
                               os.path.expanduser("~/Pictures/pixel")))

DEST = "org.kde.kdeconnect"
DEV_IFACE = "org.kde.kdeconnect.device"
MPRIS_IFACE = "org.kde.kdeconnect.device.mprisremote"
SFTP_IFACE = "org.kde.kdeconnect.device.sftp"
CAMERA_SUBPATH = "storage/emulated/0/DCIM/Camera"  # under the SFTP mountpoint
PHOTO_EXTS = (".jpg", ".jpeg", ".png", ".heic", ".mp4")
MEDIA_ACTIONS = {  # accepted (lowercased) -> KDE Connect MPRIS action
    "play": "Play", "pause": "Pause", "playpause": "PlayPause",
    "toggle": "PlayPause", "next": "Next", "previous": "Previous",
    "prev": "Previous", "stop": "Stop",
}


class KDEConnectError(RuntimeError):
    """kdeconnect-cli / gdbus failed, or the phone is not paired/reachable."""


class KDEConnect:
    """One KDE Connect device, reached through the local kdeconnectd daemon.

    The device id is resolved lazily: an explicit id (constructor or
    KDECONNECT_DEVICE) wins; otherwise the first paired+reachable device whose
    name matches ``name`` is used. Resolution is cached after the first hit.
    """

    def __init__(self, device_id=DEVICE, name=NAME, cli=CLI, gdbus=GDBUS):
        self.cli = cli
        self.gdbus = gdbus
        self.name = name
        self._id = device_id

    # --- process plumbing -------------------------------------------------

    def _run(self, argv, timeout=30, check=True):
        proc = subprocess.run(argv, capture_output=True, timeout=timeout)
        out = proc.stdout.decode(errors="replace")
        if check and proc.returncode != 0:
            err = proc.stderr.decode(errors="replace").strip()
            raise KDEConnectError(f"{argv[0]}: {err or out.strip() or 'failed'}")
        return out

    def _cli(self, args, timeout=30, check=True, target=True):
        argv = [self.cli]
        if target:
            argv += ["-d", self.dev()]
        argv += args
        return self._run(argv, timeout=timeout, check=check)

    # --- device resolution ------------------------------------------------

    def list_devices(self, available_only=False):
        """Parse `kdeconnect-cli -l/-a --id-name-only` into [{id, name}]."""
        flag = "-a" if available_only else "-l"
        out = self._run([self.cli, flag, "--id-name-only"], check=False)
        devs = []
        for line in out.splitlines():
            line = line.strip()
            if not line or " " not in line:
                continue
            dev_id, dev_name = line.split(" ", 1)
            devs.append({"id": dev_id, "name": dev_name})
        return devs

    def resolve(self):
        """Device id of the target, or raise. Prefers an explicit id; else the
        device matching ``name`` (if set); else the first paired+reachable."""
        if self._id:
            return self._id
        avail = self.list_devices(available_only=True)
        if self.name:
            for d in avail:
                if d["name"] == self.name:
                    self._id = d["id"]
                    return self._id
        if avail:  # no name pin (or no match) -> first paired+reachable
            self._id = avail[0]["id"]
            return self._id
        want = f" matching '{self.name}'" if self.name else ""
        raise KDEConnectError(
            f"no paired+reachable KDE Connect device{want}. On the phone, open "
            "KDE Connect, ensure it's on the home Wi-Fi and paired with this "
            "desktop (run `kdeconnect-cli --pair` and Accept).")

    def dev(self):
        return self._id or self.resolve()

    # --- D-Bus queries (CLI has no flag for these) ------------------------

    def _path(self, sub=""):
        return f"/modules/kdeconnect/devices/{self.dev()}{sub}"

    def _coerce(self, raw):
        """Turn a gdbus reply into a Python value. Handles both a property
        Get (variant: `(<value>,)`) and a plain method return (`(value,)`)."""
        s = raw.strip()
        if s.startswith("(") and s.endswith(")"):
            s = s[1:-1].rstrip(",").strip()
        if s.startswith("<") and s.endswith(">"):
            s = s[1:-1].strip()
        return self._gv(s)

    def _gv(self, v):
        """Parse a single GVariant text value (scalar or string array)."""
        v = re.sub(r"^@\S+\s+", "", v.strip())  # drop type annotation (@as ...)
        if v in ("true", "false"):
            return v == "true"
        if re.match(r"^-?\d+$", v):
            return int(v)
        if v.startswith("[") and v.endswith("]"):
            inner = v[1:-1].strip()
            return [self._gv(x.strip()) for x in re.split(r",\s*", inner)] \
                if inner else []
        if len(v) >= 2 and v[0] == v[-1] == "'":
            return v[1:-1]
        return v

    def _gdbus(self, method, *args, sub=""):
        return self._run([self.gdbus, "call", "--session", "--dest", DEST,
                          "--object-path", self._path(sub), "--method",
                          method, *args])

    def _prop(self, prop, iface=DEV_IFACE, sub=""):
        return self._coerce(self._gdbus(
            "org.freedesktop.DBus.Properties.Get", iface, prop, sub=sub))

    def _set_prop(self, iface, prop, variant, sub=""):
        self._gdbus("org.freedesktop.DBus.Properties.Set",
                    iface, prop, variant, sub=sub)

    def is_paired(self):
        return bool(self._prop("isPaired"))

    def is_reachable(self):
        return bool(self._prop("isReachable"))

    def battery(self):
        """{'charge': int, 'charging': bool} from the battery plugin."""
        return {
            "charge": self._prop("charge", iface=f"{DEV_IFACE}.battery",
                                 sub="/battery"),
            "charging": self._prop("isCharging", iface=f"{DEV_IFACE}.battery",
                                   sub="/battery"),
        }

    def connectivity(self):
        ci = f"{DEV_IFACE}.connectivity_report"
        return {
            "signal": self._prop("cellularNetworkStrength", iface=ci,
                                  sub="/connectivity_report"),
            "network": self._prop("cellularNetworkType", iface=ci,
                                   sub="/connectivity_report"),
        }

    # --- media (mprisremote plugin, D-Bus only) ---------------------------

    def now_playing(self):
        """Current media track + transport state from the phone's player."""
        p = lambda name: self._prop(name, iface=MPRIS_IFACE, sub="/mprisremote")
        return {
            "player": p("player"),
            "title": p("title"),
            "artist": p("artist"),
            "album": p("album"),
            "playing": bool(p("isPlaying")),
            "length_ms": p("length"),
            "position_ms": p("position"),
            "players": p("playerList"),
        }

    def media_players(self):
        """Refresh and return the list of available players on the phone."""
        self._gdbus(f"{MPRIS_IFACE}.requestPlayerList", sub="/mprisremote")
        return self._prop("playerList", iface=MPRIS_IFACE, sub="/mprisremote")

    def media_control(self, action, player=None):
        """Send a transport command (play/pause/playpause/next/previous/stop).
        Optionally target a specific ``player`` (else the current one)."""
        norm = MEDIA_ACTIONS.get(action.strip().lower())
        if not norm:
            raise KDEConnectError(
                f"unknown media action '{action}' — use one of: "
                + ", ".join(sorted(set(MEDIA_ACTIONS))))
        if player:
            self._set_prop(MPRIS_IFACE, "player", f"<'{player}'>",
                           sub="/mprisremote")
        self._gdbus(f"{MPRIS_IFACE}.sendAction", norm, sub="/mprisremote")
        return norm

    def status(self):
        """Snapshot: which device, paired/reachable, battery, connectivity.

        Resilient — degrades gracefully if the device is gone or a plugin
        object isn't present. Read it first for context."""
        snap = {"target_name": self.name or "(auto: first paired)"}
        try:
            snap["id"] = self.dev()
        except KDEConnectError as e:
            snap["reachable"] = False
            snap["error"] = str(e)
            return snap
        try:
            snap["name"] = self._prop("name")
            snap["paired"] = self.is_paired()
            snap["reachable"] = self.is_reachable()
        except KDEConnectError as e:
            snap["error"] = str(e)
            return snap
        for key, fn in (("battery", self.battery),
                        ("connectivity", self.connectivity)):
            try:
                snap[key] = fn()
            except KDEConnectError:
                pass  # plugin object may be absent if unreachable
        return snap

    # --- actions (kdeconnect-cli) -----------------------------------------

    def pair(self):
        """Request pairing — then Accept the prompt on the phone."""
        return self._cli(["--pair"]).strip() or "pair requested"

    def ping(self, message=None):
        """Buzz the phone with a notification ping."""
        args = ["--ping-msg", message] if message else ["--ping"]
        self._cli(args)
        return "ping sent"

    def ring(self):
        """Ring the phone to find it (findmyphone)."""
        self._cli(["--ring"])
        return "ringing"

    def photo(self, dest=None, timeout=180):
        """Open the phone camera, wait for a capture, transfer it to the host.

        This is the on-demand "take a snap now" move: the camera app opens on
        the phone, the user shoots, and the image lands at ``dest``. Returns
        the local Path.

        `kdeconnect-cli --photo` returns as soon as the camera *opens*; the
        daemon writes the file asynchronously once the shot is taken. So we
        kick off the request, then poll for the file up to ``timeout``."""
        dest = Path(dest) if dest else (
            PULL_DIR / f"kde_photo_{int(time.time())}.jpg")
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            dest.unlink()  # avoid a stale file reading as success
        self._cli(["--photo", str(dest)], timeout=30)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if dest.exists() and dest.stat().st_size > 0:
                return dest
            time.sleep(0.5)
        raise KDEConnectError(
            f"camera opened but no photo arrived within {timeout}s — take the "
            "shot when the camera appears on the phone")

    # --- pull existing photos (sftp plugin) -------------------------------

    def _sftp(self, method):
        return self._coerce(self._gdbus(f"{SFTP_IFACE}.{method}", sub="/sftp"))

    def mount(self):
        """Mount the phone storage over SFTP; return the local mountpoint."""
        if not self._sftp("mountAndWait"):
            raise KDEConnectError(
                "sftp mount failed: " + (self._sftp("getMountError") or
                "grant KDE Connect storage access on the phone"))
        return self._sftp("mountPoint")

    def camera_dir(self):
        return Path(self.mount()) / CAMERA_SUBPATH

    def list_photos(self, n=10):
        """Newest-first camera files on the phone (name, size, mtime)."""
        cam = self.camera_dir()
        try:
            files = [p for p in cam.iterdir()
                     if p.suffix.lower() in PHOTO_EXTS]
        except OSError as e:
            raise KDEConnectError(f"cannot read {cam}: {e}")
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        out = []
        for p in files[:n]:
            st = p.stat()
            out.append({
                "name": p.name, "size": st.st_size, "mtime": int(st.st_mtime),
                "modified": time.strftime("%Y-%m-%d %H:%M:%S",
                                          time.localtime(st.st_mtime)),
            })
        return out

    def pull(self, name=None, dest_dir=None):
        """Pull a camera file (default: the newest) to the host. The "took a
        snap -> look at it" move, over the stable SFTP link. Returns the Path."""
        cam = self.camera_dir()
        if name is None:
            photos = self.list_photos(n=1)
            if not photos:
                raise KDEConnectError("no photos in the phone's camera folder")
            name = photos[0]["name"]
        src = cam / name
        if not src.exists():
            raise KDEConnectError(f"{name} not found in the phone camera folder")
        dest_dir = Path(dest_dir) if dest_dir else PULL_DIR
        dest_dir.mkdir(parents=True, exist_ok=True)
        local = dest_dir / name
        shutil.copy2(src, local)
        return local

    def send_text(self, text):
        """Type text into the phone's focused field (remote keyboard)."""
        self._cli(["--send-keys", text])
        return "sent"

    def share(self, path_or_url):
        """Push a local file or a URL to the phone."""
        self._cli(["--share", str(path_or_url)])
        return "shared"

    def share_text(self, text):
        self._cli(["--share-text", text])
        return "shared"

    def send_clipboard(self):
        """Send the desktop clipboard to the phone."""
        self._cli(["--send-clipboard"])
        return "clipboard sent"

    def notifications(self):
        """List active notifications on the phone (raw text)."""
        return self._cli(["--list-notifications"])

    def send_sms(self, message, destination, attachments=None):
        args = ["--send-sms", message, "--destination", destination]
        for a in attachments or []:
            args += ["--attachment", str(a)]
        self._cli(args)
        return "sms sent"

    def lock(self):
        self._cli(["--lock"])
        return "locked"

    def unlock(self):
        self._cli(["--unlock"])
        return "unlocked"

    def list_commands(self):
        return self._cli(["--list-commands"])

    def run_command(self, command_id):
        self._cli(["--execute-command", command_id])
        return "executed"


# --- CLI -------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("devices", help="list paired/reachable KDE Connect devices")
    sub.add_parser("status", help="device snapshot (JSON)")
    sub.add_parser("pair", help="request pairing (Accept on the phone)")
    sub.add_parser("ring", help="ring the phone to find it")
    sub.add_parser("notifications", help="list phone notifications")
    sub.add_parser("clipboard", help="send desktop clipboard to the phone")
    sub.add_parser("playing", help="current media track + state (JSON)")
    sub.add_parser("players", help="list available media players")

    md = sub.add_parser("media", help="media transport control")
    md.add_argument("action", help="play|pause|playpause|next|previous|stop")
    md.add_argument("player", nargs="?", help="target player (default current)")

    pg = sub.add_parser("ping", help="buzz the phone")
    pg.add_argument("message", nargs="?")

    ph = sub.add_parser("photo", help="open camera, capture, transfer to host")
    ph.add_argument("dest", nargs="?", help="output path; default ~/Pictures/pixel")

    pf = sub.add_parser("photos", help="list newest camera files (over SFTP)")
    pf.add_argument("-n", type=int, default=10)

    pl = sub.add_parser("pull", help="pull newest (or named) camera photo to the host")
    pl.add_argument("name", nargs="?", help="file name; default = newest")

    tx = sub.add_parser("type", help="type text on the phone (remote keyboard)")
    tx.add_argument("text")

    sh = sub.add_parser("share", help="push a file/URL to the phone")
    sh.add_argument("path_or_url")

    sm = sub.add_parser("sms", help="send an SMS")
    sm.add_argument("message")
    sm.add_argument("destination")

    a = p.parse_args()
    kc = KDEConnect()

    try:
        if a.cmd == "devices":
            print(json.dumps(kc.list_devices(), indent=2))
        elif a.cmd == "status":
            print(json.dumps(kc.status(), indent=2))
        elif a.cmd == "pair":
            print(kc.pair())
        elif a.cmd == "ring":
            print(kc.ring())
        elif a.cmd == "notifications":
            print(kc.notifications())
        elif a.cmd == "clipboard":
            print(kc.send_clipboard())
        elif a.cmd == "playing":
            print(json.dumps(kc.now_playing(), indent=2))
        elif a.cmd == "players":
            print(json.dumps(kc.media_players(), indent=2))
        elif a.cmd == "media":
            print(kc.media_control(a.action, a.player))
        elif a.cmd == "photos":
            print(json.dumps(kc.list_photos(n=a.n), indent=2))
        elif a.cmd == "pull":
            print(kc.pull(a.name))
        elif a.cmd == "ping":
            print(kc.ping(a.message))
        elif a.cmd == "photo":
            print(kc.photo(a.dest))
        elif a.cmd == "type":
            print(kc.send_text(a.text))
        elif a.cmd == "share":
            print(kc.share(a.path_or_url))
        elif a.cmd == "sms":
            print(kc.send_sms(a.message, a.destination))
    except KDEConnectError as e:
        import sys
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
