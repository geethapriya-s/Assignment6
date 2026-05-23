#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

# Create the virtual environment if it doesn't exist
if [ ! -d .venv ]; then
  python -m venv .venv
fi

# Detect if running on Windows or Linux/macOS
if [ -d ".venv/Scripts" ]; then
  VENV_BIN=".venv/Scripts"
else
  VENV_BIN=".venv/bin"
fi

# Install requirements and execute using the correct path
"$VENV_BIN/pip" install -q -r requirements.txt
exec "$VENV_BIN/python" main.py
