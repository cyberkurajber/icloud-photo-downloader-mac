#!/bin/bash
# Double-click this file in Finder to start iCloud Photo Downloader.
# It uses the private virtual environment created by install.sh.

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VPY="$DIR/.venv/bin/python"
export TK_SILENCE_DEPRECATION=1   # hide the harmless system-Tk warning

if [ ! -x "$VPY" ]; then
  echo "Setup has not been run yet."
  echo "Please run install.sh first (open Terminal in this folder and type:  bash install.sh )"
  echo
  read -n 1 -s -r -p "Press any key to close..."
  exit 1
fi

exec "$VPY" "$DIR/icloud_photo_downloader.py"
