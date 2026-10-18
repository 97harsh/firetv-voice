"""Turn an Intent into Fire TV commands."""
import json
import logging
import os
import time
import urllib.request

from .intents import Intent
from .tv import FireTV, TVError

log = logging.getLogger("actions")

# Note: Fire TV's own system search intents need signature permissions the adb shell
# doesn't have, so search goes through each app's deep link (apps.json).


class Executor:
    def __init__(self, tv: FireTV, apps, cfg):
        self.tv, self.apps, self.cfg = tv, apps, cfg
        self.listening = True

    def run(self, it: Intent):
        a = it.args
        if os.environ.get("FTV_DRY_RUN"):
            log.info("DRY RUN: would %s", it.describe())
            return "dry"
        if it.action == "listen":
            self.listening = a["on"]
            return "listening" if a["on"] else "paused listening"
        if it.action == "key":
            self.tv.key(a["key"], a.get("count", 1))
        elif it.action == "media":
            self.tv.media(a["what"])
        elif it.action == "launch":
            self.tv.launch(self.apps[a["app"]]["package"])
        elif it.action == "search":
            self.search(a["query"], a.get("app"))
        elif it.action == "volume_set":
            cur = self.tv.volume()
            if cur is not None and cur != a["level"]:
                d = a["level"] - cur
                self.tv.key("VOLUME_UP" if d > 0 else "VOLUME_DOWN", abs(d), gap=0.03)
        elif it.action == "sleep":
            self.tv.key("SLEEP")
        elif it.action == "wake":
            self.tv.wake()
        elif it.action == "type":
            self.tv.type_text(a["text"])
        else:
            raise TVError(f"unknown action {it.action}")
        return "ok"

    def search(self, query, app=None):
        app = app or "prime video"          # no app named: Prime Video's search covers most catalogs
        spec = self.apps.get(app, {}).get("search")
        q = query.strip()
        if not spec:                        # app has no search deep link (e.g. Peacock): just open it
            self.tv.launch(self.apps[app]["package"])
            return "opened (no search link)"
        if spec.get("type") == "uri":
            self.tv.view(spec["uri"].replace("{q}", FireTV.quote(q)), self.apps[app]["package"])
        elif spec.get("type") == "am":
            self.tv.start_raw(spec["args"].replace("{q}", q.replace("'", "")))


# ---------------- optional cloud fallback (off by default) ----------------
LLM_PROMPT = """You map a spoken TV command to JSON. Reply with ONLY one JSON object, no prose.
Schema: {"action": one of ["key","media","launch","search","volume_set","sleep","wake","none"], ...}
- key: {"action":"key","key": one of [HOME,BACK,MENU,UP,DOWN,LEFT,RIGHT,SELECT,PLAY_PAUSE,FAST_FORWARD,REWIND,VOLUME_UP,VOLUME_DOWN,MUTE],"count":int}
- media: {"action":"media","what": one of [play,pause,next,previous,stop]}
- launch: {"action":"launch","app": one of APPS}
- search: {"action":"search","query":str,"app": one of APPS or null}
- volume_set: {"action":"volume_set","level":0-100}
- none: if it is not a TV command (e.g. talking to another person).
APPS = %s
Utterance: %s"""


def llm_parse(text, apps, cfg):
    lc = cfg["llm_fallback"]
    key = os.environ.get(lc["api_key_env"]) or _read_key()
    if not key:
        return None
    body = {"model": lc["model"], "temperature": 0, "max_tokens": 80,
            "messages": [{"role": "user", "content": LLM_PROMPT % (json.dumps(sorted(apps)), json.dumps(text))}]}
    req = urllib.request.Request(lc["base_url"], data=json.dumps(body).encode(),
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    t = time.time()
    try:
        r = json.load(urllib.request.urlopen(req, timeout=4))
        content = r["choices"][0]["message"]["content"]
        d = json.loads(content[content.find("{"): content.rfind("}") + 1])
    except Exception as e:
        log.warning("llm fallback failed: %s", e)
        return None
    log.info("llm fallback %.0f ms: %s", (time.time() - t) * 1000, d)
    act = d.pop("action", "none")
    if act == "none":
        return None
    if act == "launch" and d.get("app") not in apps:
        return None
    if act == "key":
        d.setdefault("count", 1)
    return Intent(act, d, text=text)


def _read_key():
    from .config import DATA
    p = os.path.join(DATA, "llm_api_key")
    if os.path.exists(p):
        return open(p).read().strip()
    return None
