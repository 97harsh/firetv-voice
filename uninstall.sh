#!/bin/bash
# Stops and removes the LaunchAgent and the `ftv` link. Pass --all to also delete models, venv and your voice profile.
LABEL="com.harsh.firetvvoice"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
launchctl bootout "gui/$(id -u)/$LABEL.ui" 2>/dev/null
rm -f "$HOME/Library/LaunchAgents/$LABEL.ui.plist"
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist" /opt/homebrew/bin/ftv
pkill -f "whisper-server.*--port 8178" 2>/dev/null
if [ "$1" = "--all" ]; then
  rm -rf "$HOME/Library/Application Support/FireTVVoice" "$HOME/Library/Logs/FireTVVoice"
  echo "Removed everything. (The microphone entry in System Settings can be removed there.)"
else
  echo "Service removed. Data kept in ~/Library/Application Support/FireTVVoice (use --all to delete)."
fi
