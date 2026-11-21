#!/bin/bash
# =============================================================================
# install.sh -- one-time setup for iCloud Photo Downloader
#
# What it does:
#   1. Finds the BEST Python 3 (3.9+) -- preferring one whose Tk is 8.6+,
#      because the old system Tk 8.5 on macOS draws blank/empty windows.
#   2. Creates a private virtual environment in ./.venv (rebuilds it if the
#      existing one uses the broken Tk 8.5).
#   3. Installs icloudpd into that environment.
#
# It does NOT need administrator rights and it does not touch your system Python.
# Re-running it is safe (it upgrades icloudpd and fixes a bad Tk if needed).
#
# IMPORTANT -- the blank window problem:
#   Apple's built-in Python ships an ancient Tk 8.5 that renders empty windows
#   on modern macOS. The fix is to install Python from the official installer,
#   which bundles Tk 8.6:   https://www.python.org/downloads/macos/
#   (Python 3.12 runs on macOS 10.13 High Sierra and newer.)
# =============================================================================
set -u

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$DIR/.venv"

echo "==> iCloud Photo Downloader -- setup"
echo "    Folder: $DIR"
echo

# Helper: does this interpreter have a usable, modern Tk? (prints tk version)
tk_version() { "$1" -c 'import tkinter; print("%.1f" % tkinter.TkVersion)' 2>/dev/null; }
is_py39()    { "$1" -c 'import sys; raise SystemExit(0 if sys.version_info[:2]>=(3,9) else 1)' 2>/dev/null; }
has_tk86()   { "$1" -c 'import tkinter; raise SystemExit(0 if tkinter.TkVersion>=8.6 else 1)' 2>/dev/null; }

# Build the candidate list: python.org framework builds first (newest first),
# then Homebrew, then whatever 'python3' resolves to (often the system one).
candidates=()
for p in $(ls -d /Library/Frameworks/Python.framework/Versions/*/bin/python3 2>/dev/null | sort -rV); do
  candidates+=("$p")
done
candidates+=(/usr/local/bin/python3 /opt/homebrew/bin/python3)
command -v python3 >/dev/null 2>&1 && candidates+=("$(command -v python3)")

# Pass 1: a Python with Tk 8.6+ (ideal).  Pass 2: any 3.9+ with Tk (fallback).
PY=""; PY_FALLBACK=""
for p in "${candidates[@]}"; do
  [ -x "$p" ] || continue
  is_py39 "$p" || continue
  [ -n "$(tk_version "$p")" ] || continue          # must have tkinter at all
  if [ -z "$PY_FALLBACK" ]; then PY_FALLBACK="$p"; fi
  if has_tk86 "$p"; then PY="$p"; break; fi
done

if [ -z "$PY" ] && [ -z "$PY_FALLBACK" ]; then
  echo "ERROR: No Python 3.9+ with Tkinter was found."
  echo
  echo "Install Python from the official installer and run this again:"
  echo "    https://www.python.org/downloads/macos/"
  echo "On macOS High Sierra (10.13), choose Python 3.12."
  exit 1
fi

WARN_TK=0
if [ -z "$PY" ]; then
  PY="$PY_FALLBACK"
  WARN_TK=1
fi

echo "==> Using Python: $PY"
"$PY" --version
echo "    Tk version:  $(tk_version "$PY")"

if [ "$WARN_TK" = "1" ]; then
  echo
  echo "*********************************************************************"
  echo "WARNING: This Python uses the old Tk 8.5, which shows BLANK windows"
  echo "         on modern macOS. The graphical window may appear empty."
  echo
  echo "         To fix it, install Python 3.12 from python.org (it bundles"
  echo "         Tk 8.6), then run this script again:"
  echo "             https://www.python.org/downloads/macos/"
  echo "*********************************************************************"
  echo
fi

# ---- Create / repair the virtual environment ------------------------------
if [ -d "$VENV" ] && [ -x "$VENV/bin/python" ]; then
  if ! has_tk86 "$VENV/bin/python" && has_tk86 "$PY"; then
    echo "==> Existing environment uses old Tk ($(tk_version "$VENV/bin/python")); rebuilding with the better Python."
    rm -rf "$VENV"
  fi
fi

if [ ! -d "$VENV" ]; then
  echo "==> Creating virtual environment in: $VENV"
  "$PY" -m venv "$VENV" || { echo "ERROR: could not create the virtual environment."; exit 1; }
fi

VPY="$VENV/bin/python"
echo "==> Upgrading pip"
"$VPY" -m pip install --upgrade pip >/dev/null 2>&1 || "$VPY" -m pip install --upgrade pip

# ---- Install icloudpd -------------------------------------------------------
echo "==> Installing icloudpd (this can take a minute)"
if ! "$VPY" -m pip install --upgrade icloudpd; then
  echo
  echo "ERROR: installing icloudpd failed."
  echo "If you saw a compiler error, install Apple's command line tools and retry:"
  echo "    xcode-select --install"
  exit 1
fi

echo
echo "==> Verifying icloudpd"
if "$VENV/bin/icloudpd" --version; then
  echo
  echo "Environment Tk version: $(tk_version "$VPY")"
  echo
  echo "SUCCESS. Setup is complete."
  echo "Start it by double-clicking:  \"Launch iCloud Downloader.command\""
else
  echo "ERROR: icloudpd did not run after install."
  exit 1
fi
