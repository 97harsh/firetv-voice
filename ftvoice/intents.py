"""Rule-based intent parser: transcript -> Intent.

The whole utterance must be a command (after stripping fillers like "please",
"can you", and an optional wake word such as "TV"). That's what keeps normal
conversation from triggering it. To add a command, add a rule to RULES below,
or an app to apps.json.
"""
import difflib
import re
from dataclasses import dataclass, field

NUM = {"zero": 0, "one": 1, "a": 1, "an": 1, "two": 2, "to": 2, "too": 2, "three": 3, "four": 4, "for": 4,
       "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
       "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
       "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
       "eighty": 80, "ninety": 90, "hundred": 100, "couple": 2, "few": 3}

LEAD = r"^(?:(?:hey|ok|okay|um|uh|so|alright|please|can you|could you|would you|will you|i want to|i wanna|" \
       r"i'd like to|let's|lets|go ahead and|just|now)\s+)+"
TRAIL = r"(?:\s+(?:please|now|for me|thanks|thank you|again|real quick))+$"


@dataclass
class Intent:
    action: str                      # key | media | launch | search | volume_set | wake | sleep | type | listen
    args: dict = field(default_factory=dict)
    text: str = ""
    wake: bool = False

    WEAK_KEYS = {"BACK", "HOME", "UP", "DOWN", "LEFT", "RIGHT", "SELECT", "MENU", "MUTE"}

    @property
    def strong(self):
        """True if the phrase is unmistakably for the TV (names an app/volume/search, or used the wake word).
        Weak ones ("pause", "go back", "next", "up") are also things people say to each other."""
        if self.wake:
            return True
        if self.action in ("launch", "search", "volume_set", "sleep", "wake", "type"):
            return True
        if self.action == "key":
            return self.args["key"] not in self.WEAK_KEYS
        return False   # media play/pause/next/previous, listen

    def describe(self):
        a = self.args
        if self.action == "key":
            return f"{a['key']}" + (f" x{a['count']}" if a.get("count", 1) > 1 else "")
        if self.action == "launch":
            return f"open {a['app']}"
        if self.action == "search":
            return f"search {a['query']!r}" + (f" on {a['app']}" if a.get("app") else "")
        return f"{self.action} {a}" if a else self.action


def words_to_numbers(s):
    toks, out, i = s.split(), [], 0
    while i < len(toks):
        t = toks[i]
        if t in NUM and t not in ("a", "an", "to", "too", "for") or (t in ("a", "an") and i + 1 < len(toks) and toks[i + 1] in ("couple", "few")):
            if t in ("a", "an"):
                i += 1
                t = toks[i]
            n = NUM[t]
            if n >= 20 and i + 1 < len(toks) and toks[i + 1] in NUM and 0 < NUM[toks[i + 1]] < 10 and toks[i + 1] not in ("a", "an", "to", "too", "for"):
                n += NUM[toks[i + 1]]
                i += 1
            out.append(str(n))
        else:
            out.append(t)
        i += 1
    return " ".join(out)


def normalize(text):
    s = text.lower().strip()
    s = s.replace("&", " and ").replace("+", " plus ").replace("-", " ")
    s = re.sub(r"\bt\.\s*v\.?", "tv", s)
    s = re.sub(r"\b([a-z])\.\s*(?=[a-z]\.)", r"\1", s)   # "h. b. o." -> "hbo"
    s = re.sub(r"[^\w\s']", " ", s)
    s = s.replace("'s", "s").replace("'", "")
    s = re.sub(r"\s+", " ", s).strip()
    return words_to_numbers(s)


class Parser:
    def __init__(self, apps, wake_words=("tv", "hey tv", "television", "fire tv", "ok tv"), volume_step=5):
        self.apps = apps
        self.volume_step = volume_step
        self.wake_words = sorted(wake_words, key=len, reverse=True)
        self.alias = {}
        for name, app in apps.items():
            if name.startswith("_"):
                continue
            for al in [name, *app.get("aliases", [])]:
                self.alias[normalize(al)] = name

    # ---- helpers ----
    def match_app(self, s, fuzzy=True):
        s = re.sub(r"\s+(app|application|channel)$", "", s.strip())
        s = re.sub(r"^(the)\s+", "", s)
        if s in self.alias:
            return self.alias[s]
        if s.replace(" ", "") in {k.replace(" ", ""): v for k, v in self.alias.items()}:
            return {k.replace(" ", ""): v for k, v in self.alias.items()}[s.replace(" ", "")]
        if fuzzy and s:
            best = difflib.get_close_matches(s, self.alias.keys(), n=1, cutoff=0.8)
            if best:
                return self.alias[best[0]]
        return None

    def strip(self, s):
        wake = False
        for w in self.wake_words:
            if s == w:
                return "", True
            if s.startswith(w + " "):
                s, wake = s[len(w) + 1:], True
                break
        s = re.sub(LEAD, "", s)
        s = re.sub(TRAIL, "", s)
        # wake word at the end: "pause tv"
        for w in self.wake_words:
            if s.endswith(" " + w):
                s, wake = s[: -len(w) - 1], True
                break
        return s.strip(), wake

    # ---- main ----
    def parse(self, text):
        s, wake = self.strip(normalize(text))
        if not s:
            return None
        it = self._rules(s)
        if it:
            it.text, it.wake = text, wake
        return it

    def _rules(self, s):
        K = lambda k, n=1: Intent("key", {"key": k, "count": max(1, min(int(n), 50))})
        n_ = lambda m, g, d: int(m.group(g)) if m.group(g) else d
        step = self.volume_step

        # navigation / keys
        if re.fullmatch(r"(go )?back|back up|previous (screen|page)|exit", s):
            return K("BACK")
        if re.fullmatch(r"(go |take me )?(to )?(the )?home( screen)?|go home|main menu", s):
            return K("HOME")
        if re.fullmatch(r"(open |show )?(the )?menu|options", s):
            return K("MENU")
        if re.fullmatch(r"(select|ok|okay|enter|click|choose|pick|press) ?(it|that|this|ok)?|play that|open (it|that|this)", s):
            return K("SELECT")
        m = re.fullmatch(r"(?:move |go |scroll |press |arrow )?(up|down|left|right)(?: (\d+)(?: times?)?)?(?: (\d+) times?)?", s)
        if m:
            return K(m.group(1).upper(), m.group(2) or m.group(3) or 1)

        # playback
        if re.fullmatch(r"(pause|stop|hold on|wait|freeze)( it| this| that| the (show|movie|video|episode|music|song)| playback| playing| for a (sec|second|minute))?", s):
            return Intent("media", {"what": "pause"})
        if re.fullmatch(r"(play|resume|unpause|un pause|continue|keep (playing|going)|start (playing|it|again))( it| this| that| the (show|movie|video|episode|music))?", s):
            return Intent("media", {"what": "play"})
        if re.fullmatch(r"play ?pause|toggle( play(back)?)?", s):
            return K("PLAY_PAUSE")
        m = re.fullmatch(r"(?:fast forward|skip ahead|skip forward|jump ahead|go forward|forward)(?: (?:by )?(\d+)(?: (second|seconds|sec|secs|minute|minutes|min|mins))?)?", s)
        if m:
            return self._seek("FAST_FORWARD", m)
        m = re.fullmatch(r"(?:rewind|skip back|jump back|go back|back)(?: (?:by )?(\d+)(?: (second|seconds|sec|secs|minute|minutes|min|mins))?)", s) \
            or re.fullmatch(r"(?:rewind|skip back(?:ward)?|jump back)(?: (?:by )?(\d+)(?: (second|seconds|sec|secs|minute|minutes|min|mins))?)?", s)
        if m:
            return self._seek("REWIND", m)
        if re.fullmatch(r"(next|skip)( (episode|track|song|one|video))?", s):
            return Intent("media", {"what": "next"})
        if re.fullmatch(r"previous( (episode|track|song|one|video))?|last (track|song)", s):
            return Intent("media", {"what": "previous"})

        # volume
        m = re.fullmatch(r"(?:set |change |put )?(?:the )?volume (?:to|at) (\d+)(?: percent)?", s)
        if m:
            return Intent("volume_set", {"level": max(0, min(100, int(m.group(1))))})
        m = re.fullmatch(r"(?:turn |turn the |the )?(?:volume up|volume higher|louder|turn it up|turn up(?: the)? volume|raise(?: the)? volume|increase(?: the)? volume|crank it(?: up)?)"
                         r"(?: (?:by )?(\d+))?(?: (?:a|a lot|a little|a bit|more|notches|steps|clicks|points))?(?: (?:a|a lot|a little|a bit))?", s)
        if m:
            return K("VOLUME_UP", self._vol_count(s, m, step))
        m = re.fullmatch(r"(?:turn |turn the |the )?(?:volume down|volume lower|quieter|softer|turn it down|turn down(?: the)? volume|lower(?: the)? volume|decrease(?: the)? volume)"
                         r"(?: (?:by )?(\d+))?(?: (?:a|a lot|a little|a bit|more|notches|steps|clicks|points))?(?: (?:a|a lot|a little|a bit))?", s)
        if m:
            return K("VOLUME_DOWN", self._vol_count(s, m, step))
        if re.fullmatch(r"(un ?mute|mute)( (the )?(tv|sound|audio|volume))?|silence", s):
            return K("MUTE")

        # power
        if re.fullmatch(r"(turn|switch|power) (the )?(tv )?off|(turn|switch|power) off (the )?tv|(turn|switch|power) off( the)?|tv off|go to sleep|sleep|goodnight|good night", s):
            return Intent("sleep")
        if re.fullmatch(r"(turn|switch|power) (the )?(tv )?on|(turn|switch|power) on (the )?tv|(turn|switch|power) on( the)?|tv on|wake up", s):
            return Intent("wake")

        # assistant itself
        if re.fullmatch(r"stop listening|go to sleep assistant|ignore me|mute (yourself|assistant)", s):
            return Intent("listen", {"on": False})
        if re.fullmatch(r"(start|resume) listening|wake up assistant|listen( to me)?", s):
            return Intent("listen", {"on": True})

        # search: "search for X on APP", "find X on APP", "play X on APP"
        m = re.fullmatch(r"(?:search for|search|find me|find|look for|look up|show me|i want to watch|watch|play|put on) (.+?) (?:on|in|from|using) (.+)", s)
        if m:
            app = self.match_app(m.group(2))
            if app:
                return Intent("search", {"query": m.group(1), "app": app})
        m = re.fullmatch(r"(?:open|launch|go to) (.+?) (?:and )?(?:search for|search|find|look for) (.+)", s)
        if m and self.match_app(m.group(1)):
            return Intent("search", {"query": m.group(2), "app": self.match_app(m.group(1))})

        # open app
        m = re.fullmatch(r"(?:open|open up|launch|start|go to|switch to|put on|show me|bring up|load|run|play|watch|take me to)(?: the)? (.+)", s)
        if m:
            app = self.match_app(m.group(1))
            if app:
                return Intent("launch", {"app": app})
        app = self.match_app(s, fuzzy=False)
        if app:
            return Intent("launch", {"app": app})

        m = re.fullmatch(r"(?:search for|search|find|look for|look up) (.+)", s)
        if m:
            return Intent("search", {"query": m.group(1), "app": None})
        m = re.fullmatch(r"type (.+)", s)
        if m:
            return Intent("type", {"text": m.group(1)})
        return None

    @staticmethod
    def _vol_count(s, m, step):
        if m.group(1):
            return int(m.group(1))
        if "a lot" in s or "crank" in s:
            return step * 3
        if "a little" in s or "a bit" in s:
            return max(1, step // 2)
        return step

    @staticmethod
    def _seek(key, m):
        n = int(m.group(1)) if m.group(1) else 10
        unit = m.group(2) or "seconds"
        secs = n * 60 if unit.startswith("min") else n
        if not m.group(2) and m.group(1) and n <= 10:
            secs = n * 10      # "fast forward 3" -> three skips
        return Intent("key", {"key": key, "count": max(1, min(60, round(secs / 10)))})
