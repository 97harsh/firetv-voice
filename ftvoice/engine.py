"""The always-on engine: audio -> VAD -> owner check -> whisper -> intent -> Fire TV.

Audio arrives as raw float32 16 kHz mono on stdin (from FireTVVoice.app, which owns the
mic permission), or from a WAV file for testing (`--wav`).
A unix socket (ctl.sock) lets the `ftv` CLI query status, enroll, and send text commands.
"""
import argparse
import collections
import json
import logging
import os
import queue
import socket
import socketserver
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import sherpa_onnx

from . import config as C
from .actions import Executor, llm_parse
from .intents import Parser
from .speaker import SpeakerGate
from .stt import Whisper
from .tv import OK, FireTV, TVError

SR = 16000
WIN = 512
log = logging.getLogger("engine")


UI_ADDR = ("127.0.0.1", 47811)
_ui_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)


def ui(t, **kw):
    """Fire-and-forget event for FireTVVoiceUI.app (glow + pop-up + menu bar)."""
    kw["t"] = t
    try:
        _ui_sock.sendto(json.dumps(kw).encode()[:8000], UI_ADDR)
    except OSError:
        pass


def notify(title, msg):
    subprocess.Popen(["/usr/bin/osascript", "-e", f'display notification {json.dumps(msg)} with title {json.dumps(title)}'],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def sound(name):
    subprocess.Popen(["/usr/bin/afplay", "-v", "0.6", f"/System/Library/Sounds/{name}.aiff"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def speak(text):
    subprocess.Popen(["/usr/bin/say", "-r", "210", text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Ring:
    """Last N seconds of audio, addressable by absolute sample index, plus arrival times."""

    def __init__(self, seconds=40):
        self.buf = np.zeros(SR * seconds, np.float32)
        self.n = 0                        # total samples written
        self.times = collections.deque(maxlen=4000)   # (end_sample_index, wall_time)

    def write(self, x, t):
        L = len(self.buf)
        i = self.n % L
        k = min(len(x), L - i)
        self.buf[i:i + k] = x[:k]
        if k < len(x):
            self.buf[: len(x) - k] = x[k:]
        self.n += len(x)
        self.times.append((self.n, t))

    def read(self, a, b):
        a = max(a, self.n - len(self.buf) + 1, 0)
        b = min(b, self.n)
        idx = np.arange(a, b) % len(self.buf)
        return self.buf[idx]

    def time_of(self, sample):
        for end, t in self.times:
            if end >= sample:
                return t
        return time.time()


class Engine:
    def __init__(self, args):
        self.args = args
        self.cfg = C.load()
        self.apps = C.apps()
        os.makedirs(C.LOGS, exist_ok=True)
        self.events = open(C.EVENTS, "a", buffering=1)
        self.parser = Parser(self.apps, self.cfg["wake_word"]["words"], self.cfg["volume_step"])
        self.ring = Ring()
        self.segq = queue.Queue(maxsize=20)
        self.pool = ThreadPoolExecutor(2)
        self.capture = None            # enrollment capture: {"mode": "one"|"ambient", "items": [], "event": Event}
        self.cohort_new = 0
        self.stats = collections.deque(maxlen=200)
        self.started = time.time()
        self.last_audio = 0.0
        self.enrolling_until = 0.0
        self.wake_until = 0.0
        self.last_chat = 0.0
        self.last_cmd = 0.0

        vc = sherpa_onnx.VadModelConfig()
        vc.silero_vad.model = os.path.join(C.MODELS, "silero_vad.onnx")
        vc.silero_vad.threshold = self.cfg["vad"]["threshold"]
        vc.silero_vad.min_silence_duration = self.cfg["vad"]["min_silence"]
        vc.silero_vad.min_speech_duration = self.cfg["vad"]["min_speech"]
        vc.silero_vad.max_speech_duration = self.cfg["vad"]["max_speech"]
        vc.silero_vad.window_size = WIN
        vc.sample_rate = SR
        vc.num_threads = 1
        self.vad = sherpa_onnx.VoiceActivityDetector(vc, buffer_size_in_seconds=60)

        self.gate = SpeakerGate(C.MODELS, C.PROFILE)
        prompt = ("Fire TV voice commands. Open Netflix, Hulu, Peacock, Disney Plus, HBO Max, Tubi, Pluto TV, "
                  "Apple TV, Prime Video, YouTube, Spotify, Steam Link. Play, pause, go home, go back, volume up, "
                  "volume down, mute, fast forward, rewind, select. Search for The Bear on Hulu.")
        st = self.cfg["stt"]
        self.stt = Whisper(os.path.join(C.MODELS, st["model"]), st["port"], st["audio_ctx"], st["threads"], prompt,
                           log_path=os.path.join(C.LOGS, "whisper.log"))
        self.tv = FireTV(self.cfg["tv"]["host"], self.cfg["tv"]["port"], on_state=self.on_tv_state, apps=self.apps)
        self.exe = Executor(self.tv, self.apps, self.cfg)

    # ---------- feedback ----------
    def on_tv_state(self, state, msg):
        self.event("tv_state", state=state, msg=msg)
        if self.cfg["feedback"]["notify"] and state != OK:
            notify("FireTV Voice: TV not connected", msg)
        elif self.cfg["feedback"]["notify"] and state == OK and self.tv.state != "unknown":
            notify("FireTV Voice", "Fire TV connected.")

    def event(self, kind, **kw):
        kw.update(kind=kind, ts=round(time.time(), 3))
        self.events.write(json.dumps(kw) + "\n")
        return kw

    # ---------- audio ----------
    def feed(self, x, t=None):
        t = t or time.time()
        self.last_audio = t
        self.ring.write(x, t)
        self.vad.accept_waveform(x)
        while not self.vad.empty():
            seg = self.vad.front
            start, n = seg.start, len(seg.samples)
            self.vad.pop()
            end = start + n
            t_end = self.ring.time_of(end)
            item = {"start": start, "end": end, "t_end": t_end, "t_detect": time.time(),
                    "audio": self.ring.read(start - int(0.2 * SR), end + int(0.05 * SR))}
            try:
                self.segq.put_nowait(item)
            except queue.Full:
                log.warning("segment queue full; dropping")

    def read_stdin(self):
        f = sys.stdin.buffer
        chunk = WIN * 4
        while True:
            b = f.read(chunk)
            if not b:
                log.error("audio stream closed")
                os._exit(3)
            if len(b) % 4:
                b = b[: len(b) - len(b) % 4]
            self.feed(np.frombuffer(b, dtype=np.float32).copy())

    def read_wav(self, path, realtime=False):
        import soundfile as sf
        a, sr = sf.read(path, dtype="float32")
        if a.ndim > 1:
            a = a.mean(1)
        assert sr == SR, "test WAVs must be 16 kHz"
        a = np.concatenate([a, np.zeros(SR, np.float32)])
        for i in range(0, len(a), WIN):
            self.feed(a[i:i + WIN])
            if realtime:
                time.sleep(WIN / SR)

    # ---------- processing ----------
    def worker(self):
        while True:
            item = self.segq.get()
            try:
                self.process(item)
            except Exception:
                log.exception("processing failed")

    def process(self, item):
        audio = item["audio"]
        dur = len(audio) / SR
        t0 = time.time()
        cap = self.capture
        if not cap and self.enrolling_until > time.time():
            return                     # enrollment in progress: never act on the TV
        if cap and cap["mode"] == "ambient":
            emb = self.gate.embed(audio)
            cap["items"].append({"emb": emb, "dur": dur})
            return
        if cap and cap["mode"] == "converse":
            emb = self.gate.embed(audio)
            text = self.stt.transcribe(audio) if dur >= 0.4 else ""
            it = self.parser.parse(text) if text else None
            label = f"TV command -> {it.describe()}" if it else "not for the TV"
            cap["items"].append({"emb": emb, "dur": dur, "text": text, "label": label})
            cap["q"].put({"text": text, "dur": round(dur, 2), "label": label})
            ui("heard" if not it else "command", text=text, intent=it.describe() if it else None,
               why=label, mode="enroll")
            return
        if cap and cap["mode"] == "one":
            emb = self.gate.embed(audio)
            text = self.stt.transcribe(audio)
            cap["items"].append({"emb": emb, "dur": dur, "text": text, "audio": audio})
            cap["event"].set()
            return

        # owner check (CPU) and speech-to-text (GPU) run in parallel; the transcript is
        # simply discarded if the voice isn't the owner's.
        f_stt = self.pool.submit(self.stt.transcribe, audio)
        if self.cfg["speaker"]["enabled"]:
            ok, score, info, emb = self.gate.score(audio)
        else:
            ok, score, info, emb = True, 1.0, {}, None
        t1 = time.time()
        if ok:
            ui("owner", score=round(score, 3))
        if not ok:
            ui("reject", score=round(score, 3))
            self.event("reject", score=round(score, 3), dur=round(dur, 2), **info)
            if self.cfg["speaker"]["learn"] and self.gate.enrolled:
                self.learn_impostor(emb, score)
            return
        text = f_stt.result()
        t2 = time.time()
        self.handle_text(text, item, score, {"spk_ms": (t1 - t0) * 1000, "stt_ms": (t2 - t0) * 1000}, emb=emb, dur=dur)

    def handle_text(self, text, item=None, score=None, timing=None, emb=None, dur=None, source="voice"):
        t2 = time.time()
        timing = timing or {}
        it = self.parser.parse(text) if text else None
        need_wake = self.cfg["wake_word"]["mode"] == "required" or not self.gate.enrolled   # unenrolled: TV audio could trigger it
        stripped, said_wake = self.parser.strip(self.parser_norm(text)) if text else ("", False)
        if said_wake and not stripped:
            # just "TV" on its own (people pause after it): arm the next phrase
            self.wake_until = time.time() + 5
            ui("owner", score=1.0)
            return self.event("wake", text=text)
        armed = self.wake_until > time.time()
        if it and armed:
            it.wake = True
            self.wake_until = 0
        if it and need_wake and not it.wake and source == "voice":
            it = None
        # --- addressee check: was this said to the TV or to a person? ---
        now = time.time()
        ctx = {"since_chat": round(now - self.last_chat, 1) if self.last_chat else None,
               "since_cmd": round(now - self.last_cmd, 1) if self.last_cmd else None,
               "words": len(text.split()) if text else 0, "dur": round(dur, 2) if dur else None}
        if it and source == "voice" and not it.strong:
            chatting = self.last_chat and now - self.last_chat < self.cfg["addressee"]["chat_window"]
            in_tv_session = self.last_cmd and now - self.last_cmd < self.cfg["addressee"]["session_window"] \
                and self.last_cmd > (self.last_chat or 0)
            if chatting and not in_tv_session:
                ui("heard", text=text, why=f"sounded like conversation - say 'TV, {it.describe().lower()}'")
                return self.event("held", text=text, intent=it.describe(), addressee="person?", **ctx)
        if not it and text and self.cfg["llm_fallback"]["enabled"]:
            lc = self.cfg["llm_fallback"]
            s_wake = self.parser.strip(self.parser_norm(text))[1]
            if s_wake or not lc.get("only_with_wake_word", True) or source != "voice":
                it = llm_parse(self.parser.strip(self.parser_norm(text))[0] or text, self.apps, self.cfg)
        t3 = time.time()
        if not it:
            if source == "voice" and text and len(text.split()) >= 2 and self.gate.enrolled:
                self.last_chat = time.time()      # only verified-you speech counts as your conversation      # you were talking (to someone / about something else)
            why = "say 'TV' first (voice not enrolled yet)" if (need_wake and text and self.parser.parse(text)) else "not for the TV"
            ui("heard", text=text, why=why)
            if not self.cfg.get("log_ignored_text", True):
                text = f"<{len(text.split())} words>"
            ev = self.event("ignored", text=text, why=why, source=source,
                            voice="you" if self.gate.enrolled and score is not None else "not checked (not enrolled)",
                            score=round(score, 3) if self.gate.enrolled and score is not None else None)
            return ev
        if not self.exe.listening and not (it.action == "listen"):
            return self.event("paused", text=text)
        err = None
        observe = self.cfg.get("mode", "observe") == "observe" and it.action != "listen"
        try:
            if not observe:
                self.exe.run(it)
        except TVError as e:
            err = str(e)
        t4 = time.time()
        if not err and source == "voice":
            self.last_cmd = time.time()
        rec = {"text": text, "intent": it.describe(), "action": it.action, "source": source,
               "strong": it.strong, **{f"ctx_{k}": v for k, v in ctx.items()},
               "score": None if score is None else round(score, 3),
               "parse_ms": round((t3 - t2) * 1000, 1), "tv_ms": round((t4 - t3) * 1000, 1),
               **{k: round(v, 1) for k, v in timing.items()}}
        if item:
            rec["vad_wait_ms"] = round((item["t_detect"] - item["t_end"]) * 1000, 1)
            rec["total_ms"] = round((t4 - item["t_end"]) * 1000, 1)
        rec["mode"] = "observe" if observe else "live"
        ui("command", text=text, intent=it.describe(), mode=rec["mode"], error=err, ms=rec.get("total_ms"))
        if err:
            rec["error"] = err
            log.warning("command failed: %s -> %s", it.describe(), err)
            if self.cfg["feedback"]["sound"]:
                sound("Basso")
            if self.cfg["feedback"]["speak_errors"]:
                speak("TV isn't connected. " + ("Toggle A D B debugging on the TV." if self.tv.state == "adb_off" else ""))
        else:
            if self.cfg["feedback"]["sound"] and source == "voice" and not observe:
                sound("Tink")
            if source == "voice" and self.cfg["speaker"]["learn"] and emb is not None:
                self.learn_owner(emb, score, dur or 1.0)
        self.stats.append(rec)
        log.info("%s", rec)
        return self.event("command", **rec)

    def parser_norm(self, text):
        from .intents import normalize
        return normalize(text)

    # ---------- continual learning ----------
    def learn_impostor(self, emb, score):
        # Keep a slice of rejected (TV / other people) speech as impostor cohort -> better normalisation.
        self.cohort_new += 1
        if self.cohort_new % 3 == 0:
            self.gate.add_cohort(emb)
        if self.cohort_new % 150 == 0:
            threading.Thread(target=self._retrain, daemon=True).start()

    def learn_owner(self, emb, score, dur):
        if self.gate.enrolled and dur >= 0.8 and score is not None and score >= min(0.99, self.gate.threshold + 0.15):
            with self.gate.lock:
                n = self.gate.enroll["titanet"].shape[0]
            if n < 120:
                self.gate.add_enroll([emb])

    def _retrain(self):
        st = self.gate.train()
        self.gate.save()
        self.event("retrain", **st)

    # ---------- control socket ----------
    def control(self, req, wfile):
        cmd = req.get("cmd")
        if cmd == "status":
            with self.gate.lock:
                ne, nc = self.gate.enroll["titanet"].shape[0], self.gate.cohort["titanet"].shape[0]
            return {"tv": self.tv.state, "listening": self.exe.listening, "enrolled_clips": ne, "cohort": nc,
                    "speaker_head": bool(self.gate.head), "threshold": self.gate.threshold, "cos_threshold": self.gate.cos_threshold,
                    "audio_age_s": round(time.time() - self.last_audio, 2) if self.last_audio else None,
                    "uptime_s": round(time.time() - self.started), "wake_word": self.cfg["wake_word"]["mode"],
                    "mode": self.cfg.get("mode", "observe"),
                    "recent": list(self.stats)[-10:]}
        if cmd == "text":
            return self.handle_text(req["text"], source="text")
        if cmd == "capture":
            ev = threading.Event()
            self.capture = {"mode": "one", "items": [], "event": ev}
            got = ev.wait(req.get("timeout", 15))
            cap, self.capture = self.capture, None
            if not got or not cap["items"]:
                return {"ok": False, "error": "no speech heard"}
            it = cap["items"][0]
            self._pending = getattr(self, "_pending", []) + [it]
            ok, s, info, _ = self.gate.score(it["audio"], it["emb"]) if self.gate.enrolled else (None, None, {}, None)
            return {"ok": True, "text": it["text"], "dur": round(it["dur"], 2), "pending": len(self._pending),
                    "score_vs_existing": None if s is None else round(s, 3)}
        if cmd == "converse":
            return self.converse(req.get("seconds", 120), wfile)
        if cmd == "wrong":
            last = list(self.stats)[-1] if self.stats else None
            self.event("feedback_wrong", last=last, note=req.get("note"))
            return {"ok": True, "logged": last}
        if cmd == "mode":
            cfg = json.load(open(C.CONFIG)) if os.path.exists(C.CONFIG) else {}
            cfg["mode"] = req["mode"]
            json.dump(cfg, open(C.CONFIG, "w"), indent=1)
            self.cfg["mode"] = req["mode"]
            ui("status", **self.status_brief())
            return {"mode": req["mode"]}
        if cmd == "ambient":
            self.capture = {"mode": "ambient", "items": [], "event": threading.Event()}
            time.sleep(req.get("seconds", 45))
            cap, self.capture = self.capture, None
            for it in cap["items"]:
                self.gate.add_cohort(it["emb"])
            return {"ok": True, "segments": len(cap["items"])}
        if cmd == "synthetic_cohort":
            return self.synthetic_cohort(req.get("voices", 40))
        if cmd == "enroll_commit":
            pend = getattr(self, "_pending", [])
            if req.get("replace"):
                with self.gate.lock:
                    for k in self.gate.enroll:
                        self.gate.enroll[k] = np.zeros((0, 0), np.float32)
            if pend:
                self.gate.add_enroll([p["emb"] for p in pend])
            self._pending = []
            self.enrolling_until = 0.0
            st = self.gate.train()
            st["cos_threshold"] = self.gate.cos_threshold
            self.gate.save()
            self.event("enroll", **st)
            return st
        if cmd == "enroll_discard":
            self._pending = []
            self.enrolling_until = time.time() + 900 if req.get("begin", True) else 0.0
            return {"ok": True}
        if cmd == "train":
            st = self.gate.train()
            self.gate.save()
            return st
        if cmd == "listen":
            self.exe.listening = bool(req.get("on", True))
            return {"listening": self.exe.listening}
        if cmd == "reload":
            self.cfg = C.load()
            self.apps = C.apps()
            self.parser = Parser(self.apps, self.cfg["wake_word"]["words"], self.cfg["volume_step"])
            self.exe.apps = self.tv.apps = self.apps
            self.exe.cfg = self.cfg
            self.tv.connect()
            return {"ok": True}
        return {"error": f"unknown cmd {cmd}"}

    def converse(self, seconds, wfile):
        """Natural-conversation enrollment: talk normally; the dominant voice becomes the owner."""
        q = queue.Queue()
        self.enrolling_until = time.time() + seconds + 60
        self.capture = {"mode": "converse", "items": [], "q": q, "event": threading.Event()}
        ui("enroll", phase="listening", seconds=seconds)
        end = time.time() + seconds
        while time.time() < end:
            try:
                p = q.get(timeout=0.5)
                wfile.write((json.dumps({"progress": p, "left": round(end - time.time())}) + "\n").encode())
                wfile.flush()
            except queue.Empty:
                pass
        cap, self.capture = self.capture, None
        items = [it for it in cap["items"] if it["dur"] >= 0.6]
        if len(items) < 6:
            self.enrolling_until = 0
            ui("enroll", phase="failed")
            return {"done": True, "ok": False, "error": f"only heard {len(items)} usable phrases; talk a bit more"}
        X = np.stack([it["emb"]["titanet"] for it in items])
        S = X @ X.T
        seed = int(np.argmax((S >= 0.55).sum(1)))
        member = S[seed] >= 0.55
        for _ in range(3):                       # refine around the cluster centroid
            c = X[member].mean(0)
            c /= np.linalg.norm(c)
            member = (X @ c) >= 0.55
        owner = [it for it, m in zip(items, member) if m]
        others = [it for it, m in zip(items, member) if not m]
        if len(owner) < 6:
            self.enrolling_until = 0
            ui("enroll", phase="failed")
            return {"done": True, "ok": False, "error": "couldn't find one consistent voice; try again with the TV quieter"}
        if True:   # natural enrollment replaces the previous profile
            with self.gate.lock:
                for k in self.gate.enroll:
                    self.gate.enroll[k] = np.zeros((0, 0), np.float32)
        self.gate.add_enroll([it["emb"] for it in owner])
        for it in others:
            self.gate.add_cohort(it["emb"])
        if self.gate.cohort["titanet"].shape[0] < 60:
            self.synthetic_cohort(30)
        st = self.gate.train()
        st["cos_threshold"] = self.gate.cos_threshold
        st.update(owner_phrases=len(owner), other_voices_phrases=len(others))
        self.gate.save()
        self.enrolling_until = 0
        self.event("enroll", **st)
        ui("enroll", phase="done", clips=len(owner))
        return {"done": True, **st}

    def status_brief(self):
        return {"tv": self.tv.state, "mode": self.cfg.get("mode", "observe"), "listening": self.exe.listening,
                "enrolled": bool(self.gate.enrolled), "audio": bool(self.last_audio and time.time() - self.last_audio < 3)}

    def heartbeat(self):
        while True:
            ui("status", **self.status_brief())
            time.sleep(2)

    def synthetic_cohort(self, nvoices):
        """Bootstrap the impostor cohort with macOS's built-in English voices."""
        import tempfile
        import soundfile as sf
        out = subprocess.run(["say", "-v", "?"], capture_output=True, text=True).stdout
        voices = [l.split("  ")[0].strip() for l in out.splitlines() if " en_" in l][:nvoices]
        lines = ["open netflix", "pause the show", "go back", "turn the volume up", "what are we watching tonight",
                 "search for the bear on hulu", "go home", "I think it's in the other room"]
        n = 0
        with tempfile.TemporaryDirectory() as d:
            for v in voices:
                for ln in lines:
                    aiff, wav = os.path.join(d, "a.aiff"), os.path.join(d, "a.wav")
                    if subprocess.run(["say", "-v", v, "-o", aiff, ln], capture_output=True).returncode:
                        continue
                    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", aiff, "-ar", "16000", "-ac", "1", wav])
                    a, _ = sf.read(wav, dtype="float32")
                    self.gate.add_cohort(self.gate.embed(a))
                    n += 1
        self.gate.save()
        return {"ok": True, "added": n, "voices": len(voices)}

    def serve_control(self):
        eng = self

        class H(socketserver.StreamRequestHandler):
            def handle(self):
                for line in self.rfile:
                    try:
                        resp = eng.control(json.loads(line), self.wfile)
                    except Exception as e:
                        log.exception("control")
                        resp = {"error": str(e)}
                    self.wfile.write((json.dumps(resp, default=str) + "\n").encode())

        class S(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True

        if os.path.exists(C.SOCKET):
            os.unlink(C.SOCKET)
        srv = S(C.SOCKET, H)
        os.chmod(C.SOCKET, 0o600)
        threading.Thread(target=srv.serve_forever, daemon=True).start()

    # ---------- main ----------
    def run(self):
        self.stt.start()
        if not os.environ.get("FTV_DRY_RUN"):
            self.tv.connect()
        threading.Thread(target=self.tv.keepalive_loop, daemon=True).start()
        threading.Thread(target=self.heartbeat, daemon=True).start()
        threading.Thread(target=self.worker, daemon=True).start()
        self.serve_control()
        log.info("engine ready (owner profile: %s)", "yes" if self.gate.enrolled else "NOT ENROLLED - accepting any voice")
        if self.args.wav:
            for w in self.args.wav:
                self.read_wav(w, realtime=self.args.realtime)
            time.sleep(1.5)
            while not self.segq.empty():
                time.sleep(0.1)
            time.sleep(1.0)
            return
        self.read_stdin()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wav", nargs="*", help="process WAV files instead of stdin audio (testing)")
    ap.add_argument("--realtime", action="store_true")
    args = ap.parse_args()
    os.makedirs(C.LOGS, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stderr)])
    Engine(args).run()


if __name__ == "__main__":
    main()
