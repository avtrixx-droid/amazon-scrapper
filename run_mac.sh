#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

VENV_PYTHON="$SCRIPT_DIR/.venv/bin/python3"

# Bootstrap venv if it doesn't exist
if [ ! -f "$VENV_PYTHON" ]; then
    echo "Setting up Python environment (first run only)..."
    python3 -m venv .venv
fi

echo "Installing / updating dependencies..."
"$VENV_PYTHON" -m pip install -r requirements.txt --quiet

echo "Starting Amazon Scraper UI..."
"$VENV_PYTHON" gui.py
