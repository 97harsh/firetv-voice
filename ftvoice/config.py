import copy
import json
import os

HOME = os.path.expanduser("~")
DATA = os.environ.get("FTV_DATA", os.path.join(HOME, "Library/Application Support/FireTVVoice"))
LOGS = os.environ.get("FTV_LOGS", os.path.join(HOME, "Library/Logs/FireTVVoice"))
MODELS = os.path.join(DATA, "models")
SOCKET = os.environ.get("FTV_SOCKET", os.path.join(DATA, "ctl.sock"))
PROFILE = os.environ.get("FTV_PROFILE", os.path.join(DATA, "voice_profile.npz"))
CONFIG = os.path.join(DATA, "config.json")
EVENTS = os.path.join(LOGS, "events.jsonl")
PKG = os.path.dirname(os.path.abspath(__file__))

DEFAULTS = {
    # "observe": recognise and show what it would do, but don't touch the TV. "live": act on the TV.
    "mode": "observe",
    # keep transcripts of non-command speech in events.jsonl (handy while tuning; set false for privacy)
    "log_ignored_text": True,
    # Your Fire TV's IP on the LAN (Settings > My Fire TV > About > Network). Set it in config.json
    # or with FTV_TV_HOST when running install.sh.
    "tv": {"host": os.environ.get("FTV_TV_HOST", ""), "port": 5555},
    "stt": {"model": "ggml-small.en-q5_1.bin", "port": 8178, "audio_ctx": 512, "threads": 4},
    "vad": {"threshold": 0.5, "min_silence": 0.25, "min_speech": 0.2, "max_speech": 8.0},
    "speaker": {"enabled": True, "learn": True},
    # "optional": "TV" prefix allowed but not needed. "required": only act on "TV, ..." commands.
    "wake_word": {"mode": "optional", "words": ["tv", "hey tv", "ok tv", "television", "fire tv"]},
    "volume_step": 5,
    # Ambiguous commands ("pause", "go back", "next", "up") are held if you were chatting within
    # chat_window seconds, unless you're mid TV session (a command within session_window s, no chat since).
    "addressee": {"chat_window": 8, "session_window": 20},
    "feedback": {"sound": True, "speak_errors": True, "notify": True},
    "llm_fallback": {
        "enabled": False,
        # Any OpenAI-compatible endpoint. xAI default; for OpenRouter use
        # "https://openrouter.ai/api/v1/chat/completions" and api_key_env "OPENROUTER_API_KEY".
        "base_url": "https://api.x.ai/v1/chat/completions",
        "model": "grok-3-mini",
        "api_key_env": "XAI_API_KEY",
        "only_with_wake_word": True,
    },
}


def _merge(a, b):
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(a.get(k), dict):
            _merge(a[k], v)
        else:
            a[k] = v
    return a


def load():
    cfg = copy.deepcopy(DEFAULTS)
    if os.path.exists(CONFIG):
        with open(CONFIG) as f:
            _merge(cfg, json.load(f))
    return cfg


def apps():
    path = os.path.join(DATA, "apps.json")
    if not os.path.exists(path):
        path = os.path.join(PKG, "apps.json")
    with open(path) as f:
        return {k: v for k, v in json.load(f).items() if not k.startswith("_")}
