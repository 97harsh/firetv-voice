"""Press Fire TV remote keys over ADB in ~10-20 ms, using Android's `hid` tool.

The trick behind FireTV Voice's key presses, on its own:
`adb shell input keyevent` takes ~1.5 s per key (it starts a JVM each time), and `sendevent`
is blocked by SELinux on Fire OS 8. Android's `hid` command instead registers a virtual HID
device through /dev/uhid; after that, each key press is one JSON line on its stdin.

  python scripts/hid_remote.py <tv-ip>:5555 HOME
  python scripts/hid_remote.py <tv-ip>:5555 DOWN DOWN SELECT
  python scripts/hid_remote.py <tv-ip>:5555 --time PLAY_PAUSE    # also time input keyevent for comparison

Needs `adb connect <tv-ip>:5555` to have been accepted on the TV (ADB debugging on).
"""
import json
import subprocess
import sys
import time

# HID consumer-control usages (Linux maps them to Android keys via Generic.kl)
USAGE = {
    "HOME": 0x223, "BACK": 0x224, "MENU": 0x40,
    "UP": 0x42, "DOWN": 0x43, "LEFT": 0x44, "RIGHT": 0x45, "SELECT": 0x41,
    "PLAY_PAUSE": 0xCD, "FAST_FORWARD": 0xB3, "REWIND": 0xB4, "NEXT": 0xB5, "PREVIOUS": 0xB6,
    "VOLUME_UP": 0xE9, "VOLUME_DOWN": 0xEA, "MUTE": 0xE2,
}
# One consumer-control report: report id 1, a single 16-bit usage.
DESCRIPTOR = [0x05, 0x0C, 0x09, 0x01, 0xA1, 0x01, 0x85, 0x01, 0x15, 0x00,
              0x26, 0xFF, 0x03, 0x19, 0x00, 0x2A, 0xFF, 0x03, 0x75, 0x10,
              0x95, 0x01, 0x81, 0x00, 0xC0]
ANDROID_KEY = {"HOME": "HOME", "BACK": "BACK", "UP": "DPAD_UP", "DOWN": "DPAD_DOWN", "LEFT": "DPAD_LEFT",
               "RIGHT": "DPAD_RIGHT", "SELECT": "DPAD_CENTER", "PLAY_PAUSE": "MEDIA_PLAY_PAUSE",
               "VOLUME_UP": "VOLUME_UP", "VOLUME_DOWN": "VOLUME_DOWN", "MUTE": "VOLUME_MUTE"}


def send(proc, obj):
    proc.stdin.write(json.dumps(obj) + "\n")
    proc.stdin.flush()


def main():
    args = sys.argv[1:]
    if len(args) < 2 or args[0].startswith("-"):
        sys.exit(__doc__)
    serial, keys = args[0], [a for a in args[1:] if not a.startswith("--")]
    timing = "--time" in args
    bad = [k for k in keys if k not in USAGE]
    if bad:
        sys.exit(f"unknown key(s) {bad}; choose from {sorted(USAGE)}")

    hid = subprocess.Popen(["adb", "-s", serial, "shell", "hid", "-"], stdin=subprocess.PIPE, text=True)
    send(hid, {"id": 1, "command": "register", "name": "hid_remote demo", "vid": 0x1234, "pid": 0x5678,
               "bus": "bluetooth", "descriptor": DESCRIPTOR})
    # Wait for the device to appear, or the first key press is lost.
    for _ in range(40):
        out = subprocess.run(["adb", "-s", serial, "shell", "getevent -pl | grep -c 'hid_remote demo'"],
                             capture_output=True, text=True).stdout.strip()
        if out not in ("", "0"):
            break
        time.sleep(0.1)
    else:
        sys.exit("virtual remote did not register")

    for k in keys:
        t0 = time.perf_counter()
        usage = USAGE[k]
        send(hid, {"id": 1, "command": "report", "report": [1, usage & 0xFF, usage >> 8]})
        send(hid, {"id": 1, "command": "report", "report": [1, 0, 0]})  # key up
        print(f"hid  {k:<12} {1000 * (time.perf_counter() - t0):6.1f} ms to write")
        time.sleep(0.15)
        if timing and k in ANDROID_KEY:
            t0 = time.perf_counter()
            subprocess.run(["adb", "-s", serial, "shell", "input", "keyevent", ANDROID_KEY[k]], check=True)
            print(f"input keyevent {ANDROID_KEY[k]:<12} {1000 * (time.perf_counter() - t0):6.1f} ms round trip")
    hid.stdin.close()
    hid.wait(timeout=5)


if __name__ == "__main__":
    main()
