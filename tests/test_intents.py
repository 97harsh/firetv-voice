"""The rule parser: whole phrases that are TV commands, and everyday speech that must not be."""
import json
import os

import pytest

from ftvoice.intents import Parser

APPS_JSON = os.path.join(os.path.dirname(__file__), "..", "ftvoice", "apps.json")
APPS = {k: v for k, v in json.load(open(APPS_JSON)).items() if not k.startswith("_")}
PARSER = Parser(APPS)

COMMANDS = {
    "Open Netflix.": "open netflix", "Open to be.": "open tubi", "Open H. B. O. Max": "open hbo max",
    "Open Apple TV.": "open apple tv", "Open Disney+.": "open disney plus", "Launch Peacock please": "open peacock",
    "Netflix": "open netflix", "Hulu.": "open hulu", "Can you open YouTube?": "open youtube",
    "TV, open Prime Video": "open prime video", "Put on Spotify": "open spotify",
    "Pause.": "media {'what': 'pause'}", "pause,": "media {'what': 'pause'}", "TV pause": "media {'what': 'pause'}",
    "Play.": "media {'what': 'play'}", "Resume": "media {'what': 'play'}",
    "Go home.": "HOME", "Go back.": "BACK", "Back": "BACK", "home": "HOME",
    "Volume up.": "VOLUME_UP x5", "Volume up 3.": "VOLUME_UP x3", "Volume up three": "VOLUME_UP x3",
    "Turn the volume down.": "VOLUME_DOWN x5", "Louder": "VOLUME_UP x5", "Turn it up a little": "VOLUME_UP x2",
    "Mute": "MUTE", "Set volume to 20": "volume_set {'level': 20}",
    "Fast forward 30 seconds": "FAST_FORWARD x3", "Rewind": "REWIND", "Skip back 10 seconds": "REWIND",
    "Go back 30 seconds": "REWIND x3", "Down": "DOWN", "Move right 3 times": "RIGHT x3", "Select": "SELECT", "OK": "SELECT",
    "Search for The Bear on Hulu.": "search 'the bear' on hulu",
    "Search for Stranger Things on Netflix.": "search 'stranger things' on netflix",
    "Find The Office": "search 'the office'", "Play The Bear on Hulu": "search 'the bear' on hulu",
    "Next episode": "media {'what': 'next'}", "Turn off the TV": "sleep", "Stop listening": "listen {'on': False}",
}

NOT_COMMANDS = [
    "I'm going to go back to the kitchen", "What do you want to watch tonight?", "Thank you",
    "I paused it earlier", "Can you pass me the remote", "That show was great",
]


def describe(text):
    it = PARSER.parse(text)
    return it.describe() if it else None


@pytest.mark.parametrize("text,want", COMMANDS.items())
def test_command(text, want):
    assert describe(text) == want


@pytest.mark.parametrize("text", NOT_COMMANDS)
def test_not_a_command(text):
    assert describe(text) is None
