"""`ftv` command-line tool: control and inspect the FireTV Voice service."""
import json
import os
import socket
import statistics
import subprocess
import sys
import time

from . import config as C

LABEL = "com.harsh.firetvvoice"
PLIST = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")
UID = os.getuid()

ENROLL_PHRASES = [
    "TV, open Netflix", "pause", "play", "go home", "go back", "volume up", "volume down",
    "open Hulu", "open Apple TV", "search for The Bear on Hulu", "fast forward thirty seconds",
    "open Disney Plus", "mute", "open YouTube", "select", "TV, pause",
    "open Prime Video", "turn the volume down a little", "rewind", "open Peacock",
    "Hey, what should we watch tonight? Maybe something funny.",
    "I'm going to make some popcorn first, then we can start the movie.",
]


def ctl(req, timeout=120):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(C.SOCKET)
    except (FileNotFoundError, ConnectionRefusedError):
        sys.exit("FireTV Voice service is not running. Start it with: ftv start")
    s.sendall((json.dumps(req) + "\n").encode())
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
    return json.loads(buf)


def ctl_stream(req, timeout=600):
    """For requests that stream progress lines before the final answer."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(C.SOCKET)
    except (FileNotFoundError, ConnectionRefusedError):
        sys.exit("FireTV Voice service is not running. Start it with: ftv start")
    s.sendall((json.dumps(req) + "\n").encode())
    f = s.makefile("rb")
    for line in f:
        msg = json.loads(line)
        yield msg
        if "progress" not in msg:
            return


def cmd_enroll_natural(args):
    secs = 120
    for a in args:
        if a.isdigit():
            secs = int(a)
    st = ctl({"cmd": "status"}, timeout=5)
    if st.get("audio_age_s") is None or st["audio_age_s"] > 2:
        sys.exit("No microphone audio reaching the service. Check System Settings > Privacy & Security > Microphone > FireTV Voice.")
    print(f"Voice enrollment - just talk naturally for {secs // 60} min {secs % 60}s.\n"
          "Sit where you normally watch. Talk about anything (your day, a show, read something aloud),\n"
          "and mix in a few TV commands the way you'd really say them (\"open Netflix\", \"pause\", \"turn it up\").\n"
          "Nothing is sent to the TV while enrolling. Each thing you say is labelled below.\n")
    for msg in ctl_stream({"cmd": "converse", "seconds": secs}, timeout=secs + 600):
        if "progress" in msg:
            p = msg["progress"]
            if p["text"]:
                mark = "\033[1;32mTV\033[0m " if p["label"].startswith("TV") else "   "
                print(f"  {mark}[{msg['left']:>3}s left] {p['text']!r:60} {p['label']}")
            continue
        if not msg.get("ok", False) and msg.get("error"):
            sys.exit("Enrollment not saved: " + msg["error"])
        print("\nTrained your voice model:")
        for k, v in msg.items():
            if k not in ("done",):
                print(f"  {k}: {v}")
        print("\nDone. The glow now appears only for your voice. `ftv live` when you want it to control the TV.")


def launchctl(*a):
    return subprocess.run(["launchctl", *a], capture_output=True, text=True)


def cmd_start():
    if not os.path.exists(PLIST):
        sys.exit("Not installed. Run ./install.sh first.")
    r = launchctl("bootstrap", f"gui/{UID}", PLIST)
    if r.returncode and "already" not in (r.stderr + r.stdout).lower() and "5:" not in r.stderr:
        print(r.stderr.strip())
    launchctl("kickstart", f"gui/{UID}/{LABEL}")
    print("started. First run: macOS asks to allow 'FireTV Voice' to use the microphone - click Allow.")
    wait_ready()


def wait_ready(timeout=40):
    t = time.time()
    while time.time() - t < timeout:
        try:
            st = ctl({"cmd": "status"}, timeout=3)
            print(f"ready: TV={st['tv']}  enrolled clips={st['enrolled_clips']}  listening={st['listening']}")
            return st
        except SystemExit:
            time.sleep(1)
    print("service did not come up; see ~/Library/Logs/FireTVVoice/service.log")


def cmd_stop():
    r = launchctl("bootout", f"gui/{UID}/{LABEL}")
    print("stopped" if r.returncode == 0 else (r.stderr.strip() or "not running"))
    print("(it will start again at next login; use `ftv disable` to prevent that)")


def cmd_restart():
    launchctl("kickstart", "-k", f"gui/{UID}/{LABEL}")
    wait_ready()


def cmd_status():
    r = launchctl("print", f"gui/{UID}/{LABEL}")
    running = "state = running" in r.stdout
    print(f"service: {'running' if running else 'NOT running'}")
    if not running:
        return
    st = ctl({"cmd": "status"}, timeout=5)
    age = st.get("audio_age_s")
    print(f"mode: {st.get('mode', '?').upper()}   TV: {st['tv']}   listening: {st['listening']}   wake word: {st['wake_word']}")
    print(f"mic audio: {'flowing' if age is not None and age < 2 else 'NOT flowing (mic permission?)'}")
    print(f"voice profile: {st['enrolled_clips']} clips, cohort {st['cohort']}, trained head: {st['speaker_head']}, threshold {st['threshold']}")
    if not st["enrolled_clips"]:
        print("  -> not enrolled yet: any voice saying \"TV, ...\" can trigger it. Run: ftv enroll")


def cmd_enroll(args):
    replace = "--add" not in args
    n = len(ENROLL_PHRASES)
    st = ctl({"cmd": "status"}, timeout=5)
    if st.get("audio_age_s") is None or st["audio_age_s"] > 2:
        sys.exit("No microphone audio reaching the service. Check System Settings > Privacy & Security > Microphone > FireTV Voice.")
    print(f"Voice enrollment: say each phrase in your normal voice from where you usually sit.\n"
          f"{n} phrases, about 2 minutes. Commands are NOT sent to the TV during enrollment.\n"
          "Tip: do this with the TV playing at normal volume - it makes the model robust to it.\n")
    ctl({"cmd": "enroll_discard"})
    got = 0
    for i, ph in enumerate(ENROLL_PHRASES, 1):
        for attempt in range(3):
            print(f"[{i}/{n}] say:  \033[1m{ph}\033[0m")
            r = ctl({"cmd": "capture", "timeout": 12}, timeout=30)
            if r.get("ok"):
                got += 1
                print(f"        heard ({r['dur']}s): {r['text']!r}")
                break
            print("        didn't catch that, again...")
    if got < 8:
        ctl({"cmd": "enroll_discard", "begin": False})
        sys.exit("Too few clips captured; enrollment not saved.")
    print("\nNow stay quiet for 40 s (let the TV play, or others talk) so it learns what is NOT you...")
    r = ctl({"cmd": "ambient", "seconds": 40}, timeout=90)
    print(f"  captured {r.get('segments', 0)} background speech segments")
    print("Adding synthetic voices to the 'not you' set (takes ~1 min)...")
    ctl({"cmd": "synthetic_cohort", "voices": 40}, timeout=600)
    st = ctl({"cmd": "enroll_commit", "replace": replace}, timeout=300)
    print("\nTrained your voice model:")
    for k, v in st.items():
        print(f"  {k}: {v}")
    print("\nDone. Try: 'open Netflix', 'pause', 'volume up'.")


def cmd_say(args):
    r = ctl({"cmd": "text", "text": " ".join(args)})
    print(json.dumps(r, indent=1))


def read_events(n=2000):
    if not os.path.exists(C.EVENTS):
        return []
    with open(C.EVENTS) as f:
        lines = f.readlines()[-n:]
    return [json.loads(l) for l in lines if l.strip()]


def cmd_log(args):
    n = int(args[0]) if args else 30
    for e in read_events()[-n:]:
        t = time.strftime("%H:%M:%S", time.localtime(e["ts"]))
        k = e["kind"]
        if k == "command":
            lat = f" total {e['total_ms']:.0f}ms" if "total_ms" in e else ""
            print(f"{t} CMD    {e['text']!r} -> {e['intent']}{lat}{'  ERROR ' + e['error'] if e.get('error') else ''}")
        elif k == "reject":
            print(f"{t} reject (not you, score {e['score']} < {e.get('thr')}, {e['dur']}s)")
        elif k == "ignored":
            who = "you" if e.get("voice") == "you" else "unverified voice"
            print(f"{t} heard  {e['text']!r} ({who}; {e.get('why', 'not a command')})")
        else:
            print(f"{t} {k:6} {json.dumps({x: y for x, y in e.items() if x not in ('ts', 'kind')})}")


def cmd_latency():
    ev = [e for e in read_events() if e["kind"] == "command" and "total_ms" in e and not e.get("error")]
    if not ev:
        sys.exit("no voice commands logged yet")
    keys = ["vad_wait_ms", "spk_ms", "stt_ms", "parse_ms", "tv_ms", "total_ms"]
    print(f"{len(ev)} voice commands (end of speech -> TV command delivered):")
    for k in keys:
        v = [e[k] for e in ev if k in e]
        if v:
            print(f"  {k:12} median {statistics.median(v):6.0f}   p90 {sorted(v)[int(len(v) * .9) - 1 if len(v) > 1 else 0]:6.0f}   max {max(v):6.0f}")


def main():
    a = sys.argv[1:]
    cmd = a[0] if a else "status"
    rest = a[1:]
    if cmd == "start":
        cmd_start()
    elif cmd == "stop":
        cmd_stop()
    elif cmd == "restart":
        cmd_restart()
    elif cmd == "disable":
        for lb in (LABEL, LABEL + ".ui"):
            launchctl("bootout", f"gui/{UID}/{lb}")
            launchctl("disable", f"gui/{UID}/{lb}")
        print("stopped and disabled at login. Re-enable: ftv enable")
    elif cmd == "enable":
        for lb in (LABEL, LABEL + ".ui"):
            launchctl("enable", f"gui/{UID}/{lb}")
        launchctl("bootstrap", f"gui/{UID}", PLIST.replace(".plist", ".ui.plist"))
        cmd_start()
    elif cmd == "status":
        cmd_status()
    elif cmd == "enroll":
        if "--scripted" in rest:
            cmd_enroll([a for a in rest if a != "--scripted"])
        else:
            cmd_enroll_natural(rest)
    elif cmd in ("live", "observe"):
        print(ctl({"cmd": "mode", "mode": cmd}))
        print("LIVE: voice commands now control the TV." if cmd == "live" else "OBSERVE: shows what it would do; TV untouched.")
    elif cmd == "wrong":
        print(ctl({"cmd": "wrong", "note": " ".join(rest) or None}))
    elif cmd in ("say", "do"):
        cmd_say(rest)
    elif cmd == "log":
        cmd_log(rest)
    elif cmd == "tail":
        os.execvp("tail", ["tail", "-f", os.path.join(C.LOGS, "service.log")])
    elif cmd == "latency":
        cmd_latency()
    elif cmd == "train":
        print(json.dumps(ctl({"cmd": "train"}, timeout=300), indent=1))
    elif cmd in ("pause", "mute"):
        print(ctl({"cmd": "listen", "on": False}))
    elif cmd in ("resume", "unmute"):
        print(ctl({"cmd": "listen", "on": True}))
    elif cmd == "reload":
        print(ctl({"cmd": "reload"}))
    else:
        print(__doc__ + """
  ftv status            service, TV, mic and voice-profile status
  ftv start|stop|restart
  ftv enable|disable    start at login on/off
  ftv enroll [SECONDS]  talk naturally ~2 min; learns your voice (--scripted: read phrases instead)
  ftv observe|live      observe = show what it would do (default); live = control the TV
  ftv wrong [note]      flag the last recognition as wrong (logged for tuning)
  ftv say open netflix  run a text command without speaking
  ftv log [N]           recent heard/rejected/executed events
  ftv latency           latency breakdown of recent voice commands
  ftv pause|resume      stop/start reacting to voice (mic stays open)
  ftv reload            re-read config.json / apps.json
  ftv train             retrain the voice model from stored samples
  ftv tail              follow the service log""")


if __name__ == "__main__":
    main()
