"""Speech-to-text with whisper.cpp (Metal) kept warm in a local whisper-server child process."""
import io
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.request
import uuid
import wave

import numpy as np

log = logging.getLogger("stt")

WHISPER_SERVER = shutil.which("whisper-server") or "/opt/homebrew/bin/whisper-server"


def wav_bytes(samples, sr=16000):
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2").tobytes()
    b = io.BytesIO()
    with wave.open(b, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)
    return b.getvalue()


class Whisper:
    def __init__(self, model_path, port=8178, audio_ctx=512, threads=4, prompt="", log_path=None):
        self.model_path, self.port, self.audio_ctx, self.threads = model_path, port, audio_ctx, threads
        self.prompt = prompt
        self.log_path = log_path
        self.proc = None
        self.lock = threading.Lock()

    def start(self):
        if self.proc and self.proc.poll() is None:
            return
        subprocess.run(["pkill", "-f", f"whisper-server.*--port {self.port}"], capture_output=True)
        out = open(self.log_path, "ab") if self.log_path else subprocess.DEVNULL
        self.proc = subprocess.Popen([WHISPER_SERVER, "-m", self.model_path, "--host", "127.0.0.1", "--port", str(self.port),
                                      "-ac", str(self.audio_ctx), "-t", str(self.threads), "-nt", "--prompt", self.prompt],
                                     stdout=out, stderr=out)
        for _ in range(150):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.port}/", timeout=1)
                break
            except Exception:
                if self.proc.poll() is not None:
                    raise RuntimeError("whisper-server exited during startup; see whisper.log")
                time.sleep(0.2)
        # warm-up so the first real request doesn't pay Metal shader compilation
        self.transcribe(np.zeros(16000, np.float32))
        log.info("whisper-server ready (%s)", os.path.basename(self.model_path))

    def stop(self):
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except Exception:
                self.proc.kill()

    def transcribe(self, samples):
        if not self.proc or self.proc.poll() is not None:
            self.start()
        b = uuid.uuid4().hex
        parts = [(f"--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"a.wav\"\r\n"
                  "Content-Type: audio/wav\r\n\r\n").encode() + wav_bytes(samples) + b"\r\n"]
        for k, v in (("response_format", "json"), ("temperature", "0"), ("temperature_inc", "0")):
            parts.append(f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
        body = b"".join(parts) + f"--{b}--\r\n".encode()
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/inference", data=body,
                                     headers={"Content-Type": f"multipart/form-data; boundary={b}"})
        with self.lock:
            r = json.load(urllib.request.urlopen(req, timeout=10))
        return clean(r.get("text", ""))


HALLUCINATIONS = {"thank you", "thanks for watching", "you", "bye", "thank you for watching", "subtitles by"}


def clean(text):
    t = re.sub(r"\[.*?\]|\(.*?\)", " ", text).strip()
    t = re.sub(r"\s+", " ", t)
    # collapse whisper's repetition loops: "Open Netflix Open Netflix Open Netflix"
    m = re.fullmatch(r"(.+?)(?:[\s.,!?]+\1)+[\s.,!?]*", t, flags=re.I)
    if m:
        t = m.group(1)
    if t.lower().strip(" .!?,") in HALLUCINATIONS:
        return ""
    return t
