#!/usr/bin/env bash
# Removes the EveWatch program. Your data (~/.local/share/EveWatch) is kept
# unless you pass --purge.
rm -rf "$HOME/.local/share/evewatch-app" "$HOME/.local/bin/evewatch" \
  "$HOME/.local/share/applications/evewatch.desktop" \
  "$HOME/.local/share/icons/hicolor/256x256/apps/evewatch.png" \
  "$HOME/.config/autostart/evewatch.desktop"
if [ "$1" = "--purge" ]; then rm -rf "$HOME/.local/share/EveWatch"; echo "Data removed too."; fi
echo "EveWatch removed."
