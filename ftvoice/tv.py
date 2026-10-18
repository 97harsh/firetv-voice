"""Fire TV control over a persistent ADB connection.

Two long-lived `adb shell` processes are kept open:
  * a plain shell for `am start`, `cmd media_session`, dumpsys, ... (~10 ms round trip)
  * the Android `hid` tool, which registers a virtual Bluetooth remote via /dev/uhid.
    A key press is then a single JSON line on its stdin, so keys land in ~10-20 ms
    instead of the ~1.5 s that `input keyevent` costs (it spawns a JVM every time).
`sendevent` would be fast too, but SELinux on Fire OS 8 denies writes to /dev/input.
"""
import json
import logging
import queue
import re
import shutil
import subprocess
import threading
import time
import urllib.parse

log = logging.getLogger("tv")

ADB = shutil.which("adb") or "/opt/homebrew/bin/adb"

# HID consumer-control usages -> Linux key -> Android key (via Generic.kl)
CONSUMER = {
    "HOME": 0x223, "BACK": 0x224, "MENU": 0x40,
    "UP": 0x42, "DOWN": 0x43, "LEFT": 0x44, "RIGHT": 0x45, "SELECT": 0x41,
    "PLAY_PAUSE": 0xCD, "PLAY": 0xB0, "FAST_FORWARD": 0xB3, "REWIND": 0xB4,
    "NEXT": 0xB5, "PREVIOUS": 0xB6, "STOP": 0xB7,
    "VOLUME_UP": 0xE9, "VOLUME_DOWN": 0xEA, "MUTE": 0xE2,
    "SLEEP": 0x32, "POWER": 0x30,
}
# Consumer-control report: report id 1, one 16-bit usage (array).
HID_DESCRIPTOR = [0x05, 0x0C, 0x09, 0x01, 0xA1, 0x01, 0x85, 0x01, 0x15, 0x00,
                  0x26, 0xFF, 0x03, 0x19, 0x00, 0x2A, 0xFF, 0x03, 0x75, 0x10,
                  0x95, 0x01, 0x81, 0x00, 0xC0]

# Connection states
OK, ADB_OFF, UNAUTHORIZED, UNREACHABLE, UNKNOWN = "ok", "adb_off", "unauthorized", "unreachable", "unknown"

STATE_HELP = {
    ADB_OFF: ("Fire TV refused the ADB connection (usually after the TV restarted). "
              "On the TV: Settings > My Fire TV > Developer Options > turn ADB Debugging OFF, then ON."),
    UNAUTHORIZED: "Fire TV is asking to allow USB debugging from this Mac. Accept the prompt on the TV (tick 'Always allow').",
    UNREACHABLE: "Can't reach the Fire TV on the network. Is it on and on Wi-Fi?",
    UNKNOWN: "Fire TV connection failed for an unknown reason. Check ~/Library/Logs/FireTVVoice/service.log.",
}


class TVError(Exception):
    pass


class _Shell:
    """A persistent `adb shell` that runs one command at a time and waits for a sentinel."""

    def __init__(self, serial, argv=("shell",)):
        self.proc = subprocess.Popen([ADB, "-s", serial, *argv], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0)
        self.lines = queue.Queue()
        self.n = 0
        self.lock = threading.Lock()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in iter(self.proc.stdout.readline, b""):
            self.lines.put(line.decode(errors="replace").rstrip("\n"))
        self.lines.put(None)

    def alive(self):
        return self.proc.poll() is None

    def write(self, text):
        try:
            self.proc.stdin.write(text.encode())
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise TVError(f"adb pipe closed: {e}")

    def run(self, cmd, timeout=5.0):
        with self.lock:
            self.n += 1
            marker = f"__FTV_{self.n}__"
            self.write(f"{cmd}\necho {marker}$?\n")
            out, deadline = [], time.time() + timeout
            while True:
                try:
                    line = self.lines.get(timeout=max(0.01, deadline - time.time()))
                except queue.Empty:
                    raise TVError(f"timeout running {cmd!r}")
                if line is None:
                    raise TVError("adb shell exited")
                if line.startswith(marker):
                    return line[len(marker):], "\n".join(out)
                out.append(line)

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=2)
        except Exception:
            self.proc.kill()


class FireTV:
    def __init__(self, host, port=5555, on_state=None, apps=None):
        self.serial = f"{host}:{port}"
        self.on_state = on_state or (lambda state, msg: None)
        self.apps = apps or {}
        self.state = UNKNOWN
        self.shell = None
        self.hid = None
        self.components = {}
        self._lock = threading.RLock()
        self._stop = False

    # ---------- connection ----------
    def _adb(self, *args, timeout=8):
        try:
            r = subprocess.run([ADB, *args], capture_output=True, text=True, timeout=timeout)
            return (r.stdout + r.stderr).strip()
        except subprocess.TimeoutExpired:
            return "timeout"

    def _classify(self, out):
        o = out.lower()
        if "connection refused" in o:
            return ADB_OFF
        if "unauthorized" in o or "failed to authenticate" in o:
            return UNAUTHORIZED
        if any(s in o for s in ("no route", "timed out", "timeout", "host is down", "network is unreachable", "cannot connect")):
            return UNREACHABLE
        return UNKNOWN

    def _set_state(self, state, detail=""):
        if state != self.state:
            prev, self.state = self.state, state
            log.info("TV state %s -> %s %s", prev, state, detail)
            self.on_state(state, STATE_HELP.get(state, "Fire TV connected.") if state != OK else "Fire TV connected.")

    def connect(self):
        with self._lock:
            self._close()
            devs = self._adb("devices")
            if not re.search(re.escape(self.serial) + r"\s+device\b", devs):
                self._adb("disconnect", self.serial, timeout=3)
                out = self._adb("connect", self.serial)
                if "connected to" not in out:
                    self._set_state(self._classify(out), out)
                    return False
                time.sleep(0.3)
                devs = self._adb("devices")
                if not re.search(re.escape(self.serial) + r"\s+device\b", devs):
                    st = UNAUTHORIZED if "unauthorized" in devs else self._classify(devs)
                    self._set_state(st, devs)
                    return False
            try:
                self.shell = _Shell(self.serial)
                self.shell.run("true", timeout=5)
                self._start_hid()
                self._resolve_components()
            except TVError as e:
                self._close()
                self._set_state(UNKNOWN, str(e))
                return False
            self._set_state(OK)
            return True

    def _start_hid(self):
        self.hid = _Shell(self.serial, ("shell", "hid", "-"))
        self.hid.write(json.dumps({"id": 1, "command": "register", "name": "FireTV Voice Remote",
                                   "vid": 0x1234, "pid": 0x5678, "bus": "bluetooth",
                                   "descriptor": HID_DESCRIPTOR}) + "\n")
        # Wait until the virtual device shows up so the first key isn't lost.
        for _ in range(40):
            _, out = self.shell.run("getevent -pl 2>/dev/null | grep -c 'FireTV Voice Remote'")
            if out.strip() not in ("", "0"):
                return
            time.sleep(0.1)
        raise TVError("virtual HID remote did not register")

    def _resolve_components(self):
        for app in self.apps.values():
            pkg = app["package"]
            if app.get("activity"):
                self.components[pkg] = app["activity"]
                continue
            for cat in ("LEANBACK_LAUNCHER", "LAUNCHER"):
                _, out = self.shell.run(f"cmd package resolve-activity --brief -a android.intent.action.MAIN "
                                        f"-c android.intent.category.{cat} {pkg} 2>/dev/null | tail -1")
                if "/" in out:
                    self.components[pkg] = out.strip()
                    break

    def _close(self):
        for s in (self.hid, self.shell):
            if s:
                s.close()
        self.hid = self.shell = None

    def ensure(self):
        with self._lock:
            if self.shell and self.shell.alive() and self.hid and self.hid.alive():
                return True
            return self.connect()

    def keepalive_loop(self, interval=20):
        """Background: health-check the connection and reconnect with backoff."""
        backoff = 3
        while not self._stop:
            try:
                ok = self.ensure() and self.ping()
            except Exception as e:
                log.warning("keepalive: %s", e)
                ok = False
            if ok:
                backoff = 3
                time.sleep(interval)
            else:
                with self._lock:
                    self._close()
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)

    def ping(self):
        try:
            self.shell.run("true", timeout=4)
            return True
        except (TVError, AttributeError):
            with self._lock:
                self._close()
            self._set_state(UNREACHABLE, "ping failed")
            return False

    def stop(self):
        self._stop = True
        self._close()

    # ---------- actions ----------
    def _with_retry(self, fn):
        for attempt in (1, 2):
            if not self.ensure():
                raise TVError(STATE_HELP.get(self.state, "TV not connected"))
            try:
                return fn()
            except TVError as e:
                log.warning("TV action failed (attempt %d): %s", attempt, e)
                with self._lock:
                    self._close()
        raise TVError("TV command failed after reconnect")

    def key(self, name, count=1, gap=0.06):
        usage = CONSUMER[name]

        def go():
            for i in range(count):
                self.hid.write(json.dumps({"id": 1, "command": "report", "report": [1, usage & 0xFF, usage >> 8]}) + "\n"
                               + json.dumps({"id": 1, "command": "report", "report": [1, 0, 0]}) + "\n")
                if i < count - 1:
                    time.sleep(gap)
            # Round-trip on the plain shell so we know the link is alive (and get a timing point).
            self.shell.run("true", timeout=3)
        return self._with_retry(go)

    def sh(self, cmd, timeout=6):
        return self._with_retry(lambda: self.shell.run(cmd, timeout=timeout))

    def media(self, what):
        # play / pause / play-pause / stop / next / previous -> the active media session
        return self.sh(f"cmd media_session dispatch {what}")

    def launch(self, pkg):
        comp = self.components.get(pkg)
        if comp:
            return self.sh(f"am start -n {comp} -a android.intent.action.MAIN "
                           f"-c android.intent.category.LEANBACK_LAUNCHER -f 0x10200000 >/dev/null 2>&1")
        return self.sh(f"monkey -p {pkg} -c android.intent.category.LEANBACK_LAUNCHER 1 >/dev/null 2>&1")

    def view(self, uri, pkg=None, extra=""):
        q = uri.replace("'", "'\\''")
        p = f" -p {pkg}" if pkg else ""
        return self.sh(f"am start -a android.intent.action.VIEW -d '{q}'{p} {extra} -f 0x10000000 >/dev/null 2>&1")

    def start_raw(self, args):
        return self.sh(f"am start {args} >/dev/null 2>&1")

    def type_text(self, text):
        t = re.sub(r"[^A-Za-z0-9 ]", "", text).replace(" ", "%s")
        return self.sh(f"input text '{t}'", timeout=10)

    def volume(self):
        _, out = self.sh("dumpsys audio | grep -m1 'streamVolume'")
        m = re.search(r"streamVolume:(\d+)", out)
        return int(m.group(1)) if m else None

    def foreground(self):
        _, out = self.sh("dumpsys window | grep -m1 mCurrentFocus")
        m = re.search(r"u0 ([\w.]+)/", out)
        return m.group(1) if m else None

    def wake(self):
        return self.sh("input keyevent WAKEUP", timeout=10)

    @staticmethod
    def quote(s):
        return urllib.parse.quote(s)
