# FireTV Voice

A private, always-listening voice remote for an Amazon Fire TV that runs entirely on an Apple Silicon Mac.
It only reacts to **your** voice, only when the **whole phrase is a TV command**, and nothing leaves the Mac
(unless you turn on the optional cloud fallback).

Write-up: [FireTV Voice: A Private, On-Device Voice Remote for My Fire TV](https://97harsh.github.io/2026/10/18/firetv-voice/)

```
mic ─► FireTVVoice.app (owns mic permission, 16 kHz audio)
        └► engine (Python):  Silero VAD ─► your-voice check (TitaNet + ERes2Net + trained head)
                                         └► whisper.cpp small.en on Metal (in parallel)
                             ─► rule parser ─► Fire TV over a persistent ADB link
                                               (virtual Bluetooth remote via `hid`, `am start`)
        └► FireTVVoiceUI.app: screen-edge glow + pop-up + menu bar icon
```

Status: built and running on my M1 MacBook Air in observe mode. Voice enrollment and a live on-TV test are
still to do, so the latency numbers below are dry runs.

## What's in here

| Path | What it is |
|---|---|
| `ftvoice/` | The Python engine: VAD, speaker check, speech-to-text, intent parser, ADB link, CLI |
| `app/` | Tiny Swift mic host. macOS grants microphone access to an app, not to a script run by launchd |
| `ui/` | Swift menu bar icon, screen-edge glow and pop-ups |
| `install.sh` / `uninstall.sh` | Install as launchd agents (starts at login) |
| `scripts/hid_remote.py` | The fast key-press trick on its own: a virtual HID remote over ADB |
| `scripts/live_tv_test.py` | End-to-end test against a real TV: speaks commands, checks the TV's state |
| `tests/` | Unit tests for the intent parser and the "TV or a person?" logic (no audio, models or TV needed) |

## Setting up the Fire TV

1. On the TV: **Settings > My Fire TV > About**, click the device name 7 times to unlock Developer Options.
2. **Settings > My Fire TV > Developer Options > ADB Debugging: On.**
3. Find the TV's IP: **Settings > My Fire TV > About > Network**.
4. On the Mac: `adb connect <tv-ip>:5555`, then accept the **Allow debugging?** prompt on the TV
   (tick "Always allow from this computer").

**Security note:** with network ADB on, port 5555 is open to your local network. The key prompt in step 4 is
what stops other devices from taking control. That's reasonable on a home network you trust, not on shared Wi-Fi.
After a TV reboot, network ADB closes; toggle ADB Debugging off and on, and FireTV Voice reconnects within ~30 s.

## Install

```bash
FTV_TV_HOST=<tv-ip> ./install.sh   # installs to ~/Library/Application Support/FireTVVoice, starts at login
ftv status
ftv enroll                          # ~2 min of natural talking; learns your voice
ftv live                            # when you're confident (default is observe mode)
```

Needs Homebrew; the installer pulls `adb`, `whisper-cpp`, `ffmpeg`, Python 3.12 packages and the models
(Silero VAD, TitaNet-small, ERes2Net, whisper `small.en`). The microphone prompt appears the first time the
service starts.

## Using it

| You say | It does |
|---|---|
| "open Netflix" / "Hulu" / "put on YouTube" / "launch Apple TV" | opens the app |
| "pause", "play", "resume", "next episode" | media control |
| "go home", "go back", "up/down/left/right (3 times)", "select" | remote keys |
| "volume up/down", "turn it up a little", "volume up 10", "set volume to 20", "mute" | volume |
| "fast forward 30 seconds", "rewind", "skip back 10 seconds" | seek (10 s per press) |
| "search for The Bear on Hulu", "find Severance on Apple TV" | search (Prime Video if no app named) |
| "turn off the TV" / "turn on the TV" | sleep / wake (untested) |
| "stop listening" / "start listening" | mute/unmute the assistant itself |

"TV" works as an optional wake word ("TV, pause"). Until you enroll your voice it is required, so dialogue from
the TV can't trigger anything.

**TV or a person?** Commands that name an app, a volume or a search, or start with "TV", always act. Ambiguous
ones ("pause", "go back", "next", "select") are held if you were chatting in the last 8 s, and go straight
through if you're mid TV session (a command in the last 20 s and no chat since).

**Modes:** *observe* (default) shows what it would do without touching the TV; *live* acts. `ftv observe` /
`ftv live`, or the menu bar icon.

```
ftv status | start | stop | restart | enable | disable
ftv enroll [SECONDS] [--scripted]
ftv observe | live
ftv say open netflix      # run a command by text (no voice)
ftv log [N] | latency | wrong [note] | pause | resume | reload | tail
```

## Measured latency

End of speech → command sent, M1 MacBook Air, dry runs on recorded commands:

| stage | typical |
|---|---|
| end-of-speech detection (VAD silence window) | ~255 ms |
| your-voice check (runs in parallel with STT) | 40–125 ms |
| whisper.cpp small.en (Metal) | 180–250 ms |
| intent parsing | < 1 ms |
| **total, end of speech → command sent** | **440–510 ms** |
| ADB delivery to TV (persistent link): key | ~10–20 ms |
| ADB delivery: `am start` / media command | ~30–60 ms |

Live on-TV latency hasn't been measured yet.

## How it's tested

```bash
pip install -r requirements-dev.txt
pytest                                                    # parser + addressee logic, runs anywhere (CI too)
python scripts/hid_remote.py <tv-ip>:5555 --time HOME     # hid key press vs input keyevent, on a real TV
FTV_TV_SERIAL=<tv-ip>:5555 python scripts/live_tv_test.py --text   # end to end against the running service
```

## Findings worth knowing

* `adb shell input keyevent` costs ~1.5 s per key (it starts a JVM). `sendevent` is blocked by SELinux on
  Fire OS 8. Android's `hid` tool registers a virtual HID remote through `/dev/uhid`; each key is then one JSON
  line on its stdin and lands in ~10–20 ms. See `scripts/hid_remote.py`.
* Fire TV's system search can't be reached from ADB (its activities need a `signature|privileged` permission),
  so search uses per-app deep links. Prime Video, Apple TV, Tubi and Spotify formats are confirmed; Netflix,
  Hulu, Disney+, HBO Max and YouTube accept the link but query pre-fill is unverified; Peacock and Pluto just open.
* Speaker models: in an A/B test, CAM++ and ResNet34 confused similar voices; TitaNet-small + ERes2Net
  separated them best. On synthetic voices: owner 15/15 accepted, impostors 0/15.

## Limitations

* The Mac must be awake, open and in the room (closing the lid cuts the built-in mic).
* Loud TV dialogue hurts both the voice check and transcription; there's no echo cancellation.
* Very short commands ("pause") carry little voice information; "TV, pause" passes more reliably.
* A family member with a very similar voice could be accepted; re-enroll with them talking in the background.
* It also hears you dictating to the Mac; dictating literally "pause" would pause the TV.
* Audio is never stored. Transcripts of non-command speech are logged while tuning
  (`"log_ignored_text": false` in `config.json` turns that off).

## Configuration

`~/Library/Application Support/FireTVVoice/config.json` overrides the defaults in `ftvoice/config.py`, e.g.
`{"tv": {"host": "<tv-ip>"}, "wake_word": {"mode": "required"}, "volume_step": 3}`.
Add apps in `apps.json` (package, launch activity, spoken aliases, optional search deep link with `{q}`).

## License

MIT
