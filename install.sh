#!/bin/bash
# Installs FireTV Voice into ~/Library/Application Support/FireTVVoice and registers a LaunchAgent.
# Safe to re-run: code/config are updated, your voice profile and mic permission are kept.
#   ./install.sh               install / update
#   ./install.sh --rebuild-app also rebuild the Swift mic host (macOS will ask for mic permission again)
#   ./install.sh --no-start    install without starting
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
DATA="$HOME/Library/Application Support/FireTVVoice"
LOGS="$HOME/Library/Logs/FireTVVoice"
APP="$DATA/FireTVVoice.app"
LABEL="com.harsh.firetvvoice"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PY="$DATA/venv/bin/python"
REBUILD=0; START=1
for a in "$@"; do case $a in --rebuild-app) REBUILD=1;; --no-start) START=0;; esac; done

mkdir -p "$DATA/models" "$DATA/app" "$LOGS" "$HOME/Library/LaunchAgents"
echo "==> dependencies"
command -v adb >/dev/null || brew install android-platform-tools
command -v whisper-server >/dev/null || brew install whisper-cpp
command -v ffmpeg >/dev/null || brew install ffmpeg
[ -x "$PY" ] || /opt/homebrew/bin/python3.12 -m venv "$DATA/venv"
"$PY" -m pip install -q --upgrade pip
"$PY" -m pip install -q "sherpa-onnx==1.13.8" numpy soundfile

echo "==> models"
cd "$DATA/models"
R=https://github.com/k2-fsa/sherpa-onnx/releases/download
H=https://huggingface.co/ggerganov/whisper.cpp/resolve/main
[ -f silero_vad.onnx ] || curl -fsSLO $R/asr-models/silero_vad.onnx
[ -f nemo_en_titanet_small.onnx ] || curl -fsSLO $R/speaker-recongition-models/nemo_en_titanet_small.onnx
[ -f 3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx ] || curl -fsSLO $R/speaker-recongition-models/3dspeaker_speech_eres2net_sv_en_voxceleb_16k.onnx
[ -f ggml-small.en-q5_1.bin ] || curl -fsSLO $H/ggml-small.en-q5_1.bin
cd "$SRC"

echo "==> code"
rm -rf "$DATA/app/ftvoice"
cp -R "$SRC/ftvoice" "$DATA/app/ftvoice"
find "$DATA/app/ftvoice" -name __pycache__ -prune -exec rm -rf {} +
[ -f "$DATA/apps.json" ] || cp "$SRC/ftvoice/apps.json" "$DATA/apps.json"
if [ ! -f "$DATA/config.json" ]; then
  if [ -n "${FTV_TV_HOST:-}" ]; then echo "{\"tv\": {\"host\": \"$FTV_TV_HOST\"}}" > "$DATA/config.json"
  else echo '{}' > "$DATA/config.json"; echo "   NOTE: set your Fire TV's IP: edit $DATA/config.json -> {\"tv\": {\"host\": \"<ip>\"}}"; fi
fi
cp "$SRC/README.md" "$DATA/README.md" 2>/dev/null || true
# full source copy, so the install never depends on where it was installed from
if [ "$SRC" != "$DATA/src" ]; then
  rm -rf "$DATA/src"; mkdir -p "$DATA/src"
  cp -R "$SRC/ftvoice" "$SRC/app" "$SRC/ui" "$SRC/install.sh" "$SRC/uninstall.sh" "$SRC/README.md" "$SRC/tests" "$SRC/scripts" "$DATA/src/"
  find "$DATA/src" -name __pycache__ -prune -exec rm -rf {} +
fi

cat > "$DATA/ftv" <<EOF
#!/bin/bash
PYTHONPATH="$DATA/app" exec "$PY" -m ftvoice.cli "\$@"
EOF
chmod +x "$DATA/ftv"
ln -sf "$DATA/ftv" /opt/homebrew/bin/ftv

echo "==> mic host app"
if [ ! -x "$APP/Contents/MacOS/FireTVVoice" ] || [ $REBUILD = 1 ]; then
  mkdir -p "$APP/Contents/MacOS"
  cp "$SRC/app/Info.plist" "$APP/Contents/Info.plist"
  TMP=$(mktemp -d)
  # Works around a duplicate SwiftBridging modulemap in some Command Line Tools releases.
  : > "$TMP/empty.modulemap"
  printf '{"version":0,"case-sensitive":"false","roots":[{"type":"file","name":"/Library/Developer/CommandLineTools/usr/include/swift/module.modulemap","external-contents":"%s"}]}' "$TMP/empty.modulemap" > "$TMP/vfs.yaml"
  swiftc -O -suppress-warnings "$SRC/app/main.swift" -o "$APP/Contents/MacOS/FireTVVoice" 2>/dev/null \
   || swiftc -O -suppress-warnings -vfsoverlay "$TMP/vfs.yaml" -Xcc -ivfsoverlay -Xcc "$TMP/vfs.yaml" "$SRC/app/main.swift" -o "$APP/Contents/MacOS/FireTVVoice"
  # Ad-hoc signature with an identifier-based requirement (no cdhash pin).
  codesign --force -s - -i com.harsh.firetvvoice -r='designated => identifier "com.harsh.firetvvoice"' "$APP"
  rm -rf "$TMP"
  echo "   built (macOS will ask for microphone permission on first start)"
else
  echo "   kept existing build (keeps your microphone permission)"
fi

echo "==> UI app (menu bar + glow; needs no permissions, always rebuilt)"
UIAPP="$DATA/FireTVVoiceUI.app"
mkdir -p "$UIAPP/Contents/MacOS"
cp "$SRC/ui/Info.plist" "$UIAPP/Contents/Info.plist"
TMP=$(mktemp -d); : > "$TMP/empty.modulemap"
printf '{"version":0,"case-sensitive":"false","roots":[{"type":"file","name":"/Library/Developer/CommandLineTools/usr/include/swift/module.modulemap","external-contents":"%s"}]}' "$TMP/empty.modulemap" > "$TMP/vfs.yaml"
swiftc -O -suppress-warnings "$SRC/ui/main.swift" -o "$UIAPP/Contents/MacOS/FireTVVoiceUI" 2>/dev/null \
 || swiftc -O -suppress-warnings -vfsoverlay "$TMP/vfs.yaml" -Xcc -ivfsoverlay -Xcc "$TMP/vfs.yaml" "$SRC/ui/main.swift" -o "$UIAPP/Contents/MacOS/FireTVVoiceUI"
codesign --force -s - "$UIAPP" 2>/dev/null; rm -rf "$TMP"
UIPLIST="$HOME/Library/LaunchAgents/$LABEL.ui.plist"
cat > "$UIPLIST" <<EOF2
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL.ui</string>
  <key>ProgramArguments</key><array><string>$UIAPP/Contents/MacOS/FireTVVoiceUI</string></array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ProcessType</key><string>Interactive</string>
  <key>LimitLoadToSessionType</key><string>Aqua</string>
  <key>StandardErrorPath</key><string>$LOGS/ui.log</string>
</dict></plist>
EOF2
launchctl bootout "gui/$(id -u)/$LABEL.ui" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$UIPLIST" || true

echo "==> LaunchAgent"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$APP/Contents/MacOS/FireTVVoice</string>
    <string>$PY</string><string>-m</string><string>ftvoice.engine</string>
  </array>
  <key>WorkingDirectory</key><string>$DATA/app</string>
  <key>EnvironmentVariables</key><dict>
    <key>PYTHONPATH</key><string>$DATA/app</string>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>ProcessType</key><string>Interactive</string>
  <key>AssociatedBundleIdentifiers</key><string>com.harsh.firetvvoice</string>
  <key>StandardOutPath</key><string>$LOGS/service.log</string>
  <key>StandardErrorPath</key><string>$LOGS/service.log</string>
</dict></plist>
EOF

if [ $START = 1 ]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  sleep 1
  launchctl bootstrap "gui/$(id -u)" "$PLIST"
  echo "==> started. If this is the first run, click Allow on the microphone prompt for 'FireTV Voice'."
  echo "    Then run:  ftv status   and   ftv enroll"
fi
