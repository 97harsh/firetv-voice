"""Addressee logic: is an ambiguous command ("pause", "go back") meant for the TV or for a person?

Drives Engine.handle_text with a fake clock: no audio, no models, no TV.
"""
import types

import pytest

import ftvoice.engine as E
from ftvoice import config as C
from ftvoice.intents import Parser

SCRIPT = [  # (utterance, seconds since the previous one, expected decision)
    ("pause", 60, "command"),                                # alone in the room -> TV
    ("did you see where I left my keys", 30, "ignored"),     # chatting
    ("go back", 3, "held"),                                  # said to a person, mid-chat
    ("TV, go back", 2, "command"),                           # wake word overrides
    ("down", 2, "command"),                                  # TV session continues
    ("select", 2, "command"),
    ("yeah this one looks good honestly", 3, "ignored"),     # chat again
    ("next", 2, "held"),
    ("open Netflix", 1, "command"),                          # strong commands always pass
    ("pause", 4, "command"),                                 # within the TV session, no chat since
    ("it's getting late we should sleep soon", 40, "ignored"),
    ("stop", 2, "held"),
    ("stop", 15, "command"),                                 # chat was a while ago -> TV
]


@pytest.fixture
def engine(monkeypatch):
    monkeypatch.setattr(E, "ui", lambda *a, **k: None)
    clock = [1000.0]
    monkeypatch.setattr(E, "time", types.SimpleNamespace(time=lambda: clock[0]))
    eng = E.Engine.__new__(E.Engine)
    eng.cfg = C.load()
    eng.cfg["mode"] = "observe"
    eng.cfg["llm_fallback"]["enabled"] = False
    eng.apps = C.apps()
    eng.parser = Parser(eng.apps)
    eng.gate = types.SimpleNamespace(enrolled=True)
    eng.exe = types.SimpleNamespace(listening=True)
    eng.tv = types.SimpleNamespace(state="ok")
    eng.stats, eng.wake_until, eng.last_chat, eng.last_cmd = [], 0.0, 0.0, 0.0
    eng.log = []
    eng.event = lambda kind, **kw: eng.log.append(kind) or {"kind": kind, **kw}
    eng.clock = clock
    return eng


def test_conversation(engine):
    got = []
    for text, gap, _ in SCRIPT:
        engine.clock[0] += gap
        engine.log.clear()
        engine.handle_text(text, dur=1.0)
        got.append(engine.log[-1] if engine.log else None)
    assert got == [want for _, _, want in SCRIPT]
