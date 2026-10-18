"""Live end-to-end test: speak commands through the Mac speaker, the running service hears them
through the mic, and we verify the TV state over a separate adb connection.

  FTV_TV_SERIAL=<tv-ip>:5555 python scripts/live_tv_test.py          # uses macOS `say` (wake word "TV" is prepended)
  FTV_TV_SERIAL=<tv-ip>:5555 python scripts/live_tv_test.py --text   # skip audio; send commands via the control socket
"""
import json
import os
import re
import subprocess
import sys
import time

EVENTS = os.path.expanduser("~/Library/Logs/FireTVVoice/events.jsonl")
SERIAL = os.environ.get("FTV_TV_SERIAL", "")  # e.g. 192.168.1.20:5555
TEXT = "--text" in sys.argv


def adb(cmd):
    return subprocess.run(["adb", "-s", SERIAL, "shell", cmd], capture_output=True, text=True, timeout=15).stdout


def focus():
    m = re.search(r"u0 ([\w.]+)/", adb("dumpsys window | grep -m1 mCurrentFocus"))
    return m.group(1) if m else "?"


def media_state():
    m = re.search(r"state=PlaybackState \{state=(\d+)", adb("dumpsys media_session | grep -m1 'state=PlaybackState'"))
    return {"2": "paused", "3": "playing"}.get(m.group(1) if m else "", m.group(1) if m else "?")


def volume():
    m = re.search(r"streamVolume:(\d+)", adb("dumpsys audio | grep -m1 streamVolume"))
    return int(m.group(1)) if m else None


def nevents():
    return sum(1 for _ in open(EVENTS)) if os.path.exists(EVENTS) else 0


def wait_event(start_n, timeout=6):
    t = time.time()
    while time.time() - t < timeout:
        with open(EVENTS) as f:
            lines = f.readlines()[start_n:]
        for l in lines:
            e = json.loads(l)
            if e["kind"] in ("command", "ignored", "reject", "paused"):
                return e
        time.sleep(0.05)
    return None


def run(phrase):
    n = nevents()
    t0 = time.time()
    if TEXT:
        subprocess.run([os.path.expanduser("/opt/homebrew/bin/ftv"), "say", phrase], capture_output=True)
    else:
        subprocess.run(["say", "-r", "190", "TV, " + phrase])
    t_spoken = time.time()
    e = wait_event(n)
    return e, t_spoken


def wait_for(fn, want, timeout=8):
    t = time.time()
    while time.time() - t < timeout:
        v = fn()
        if (want(v) if callable(want) else v == want):
            return v, time.time() - t
        time.sleep(0.15)
    return fn(), None


CASES = [
    ("pause", lambda: wait_for(media_state, "paused")),
    ("play", lambda: wait_for(media_state, "playing")),
    ("go home", lambda: wait_for(focus, lambda f: "launcher" in f)),
    ("open Apple TV", lambda: wait_for(focus, "com.apple.atve.amazon.appletv")),
    ("go back", None),
    ("open Hulu", lambda: wait_for(focus, "com.hulu.plus", 12)),
    ("search for The Bear on Hulu", lambda: wait_for(focus, "com.hulu.plus", 12)),
    ("open YouTube", lambda: wait_for(focus, "com.amazon.firetv.youtube", 12)),
    ("open Peacock", lambda: wait_for(focus, "com.peacock.peacockfiretv", 12)),
    ("volume up", "VOL+"),
    ("volume down", "VOL-"),
    ("go home", lambda: wait_for(focus, lambda f: "launcher" in f)),
    ("open Netflix", lambda: wait_for(focus, "com.netflix.ninja", 12)),
]

if __name__ == "__main__":
    if not SERIAL:
        sys.exit("Set FTV_TV_SERIAL=<tv-ip>:5555")
    results = []
    for phrase, check in CASES:
        v0 = volume() if check in ("VOL+", "VOL-") else None
        e, _ = run(phrase)
        if not e or e["kind"] != "command":
            print(f"FAIL  {phrase!r}: {e}")
            results.append((phrase, False, e))
            time.sleep(1.5)
            continue
        verdict, dt = "sent", None
        if callable(check):
            v, dt = check()
            verdict = f"TV {'OK' if dt is not None else 'NOT CONFIRMED'} ({v})"
        elif check in ("VOL+", "VOL-"):
            time.sleep(0.8)
            v1 = volume()
            verdict = f"TV volume {v0} -> {v1}"
        lat = e.get("total_ms")
        print(f"OK    {phrase!r:32} heard {e['text']!r:34} -> {e['intent']:28} "
              f"end-of-speech->sent {lat if lat is not None else '-':>6} ms | {verdict}"
              + (f" screen +{dt * 1000:.0f} ms" if dt else ""))
        results.append((phrase, True, e))
        time.sleep(2.5)
    ok = sum(1 for r in results if r[1])
    print(f"\n{ok}/{len(results)} commands understood and sent")
