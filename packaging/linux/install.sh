#!/usr/bin/env bash
# EveWatch installer for Linux (per-user, no root needed).
#   ./install.sh              install / update
#   ./install.sh --autostart  also start EveWatch when you log in
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
APP="$HOME/.local/share/evewatch-app"
BIN="$HOME/.local/bin"
APPS="$HOME/.local/share/applications"
ICONS="$HOME/.local/share/icons/hicolor/256x256/apps"

if [ ! -d "$HERE/EveWatch" ]; then echo "Run this from the extracted EveWatch folder."; exit 1; fi

mkdir -p "$APP" "$BIN" "$APPS" "$ICONS"
rm -rf "$APP"/*
cp -r "$HERE/EveWatch/." "$APP/"
chmod +x "$APP/EveWatch"
ln -sf "$APP/EveWatch" "$BIN/evewatch"
cp "$HERE/icon.png" "$ICONS/evewatch.png"
sed "s|@EXEC@|$APP/EveWatch|" "$HERE/evewatch.desktop" > "$APPS/evewatch.desktop"
if command -v update-desktop-database >/dev/null 2>&1; then update-desktop-database "$APPS" || true; fi

if [ "$1" = "--autostart" ]; then
  mkdir -p "$HOME/.config/autostart"
  sed "s|@EXEC@|$APP/EveWatch --no-browser|" "$HERE/evewatch.desktop" > "$HOME/.config/autostart/evewatch.desktop"
  echo "EveWatch will start when you log in."
fi

echo "Installed. Start it from your applications menu, or run: evewatch"
echo "(If 'evewatch' isn't found, add $BIN to your PATH.)"
echo "Your data lives in ~/.local/share/EveWatch and is kept when updating."
