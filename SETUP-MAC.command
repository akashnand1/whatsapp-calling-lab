#!/bin/bash
# Double-click this file in Finder, or run: bash SETUP-MAC.command
# Creates the venv, installs dependencies, and prepares .env
set -e
cd "$(dirname "$0")"

echo "=== WhatsApp Calling Lab — setup ==="
echo "Working in: $(pwd)"
echo

# --- Homebrew native libs that aiortc needs ---
if command -v brew >/dev/null 2>&1; then
  echo "--> Installing native libraries (ffmpeg, opus, libvpx, srtp)…"
  brew list ffmpeg   >/dev/null 2>&1 || brew install ffmpeg
  brew list opus     >/dev/null 2>&1 || brew install opus
  brew list libvpx   >/dev/null 2>&1 || brew install libvpx
  brew list srtp     >/dev/null 2>&1 || brew install srtp
  brew list pkg-config >/dev/null 2>&1 || brew install pkg-config
else
  echo "!! Homebrew not found. Install it first:"
  echo '   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
  exit 1
fi

# --- Python venv ---
PY=$(command -v python3.11 || command -v python3.12 || command -v python3)
echo
echo "--> Using $PY ($($PY --version))"
[ -d .venv ] || "$PY" -m venv .venv
source .venv/bin/activate
python -m pip install --quiet --upgrade pip
echo "--> Installing Python packages (this takes a few minutes)…"
pip install --quiet -r requirements.txt

# --- .env ---
if [ ! -f .env ]; then
  cp .env.example .env
  echo
  echo "--> Created .env from the template. Open it and fill in your values:"
  echo "      open -e .env"
else
  echo "--> .env already exists, leaving it alone."
fi

echo
echo "=== Done ==="
echo "Next:"
echo "  source .venv/bin/activate"
echo "  python cli.py doctor"
