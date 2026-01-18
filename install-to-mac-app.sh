#!/bin/bash
# Build and install custom Anki to Anki.app

set -e
cd ~/workspace/anki

echo "Building Anki wheels..."
./tools/build

echo "Building macOS helper..."
(cd qt/mac && ./build.sh)
out/pyenv/bin/pip install --force-reinstall qt/mac/dist/anki_mac_helper-0.1.1-py3-none-any.whl

echo "Installing to launcher venv..."
LAUNCHER_PYTHON="$HOME/Library/Application Support/AnkiProgramFiles/.venv/bin/python"

# Find the latest wheels
ANKI_WHEEL=$(ls -t out/wheels/anki-*-cp39-*.whl | head -1)
AQT_WHEEL=$(ls -t out/wheels/aqt-*.whl | head -1)

echo "Clearing ALL Python caches..."
rm -rf "$HOME/Library/Application Support/AnkiProgramFiles/.venv/lib/python3.13/site-packages/anki/__pycache__"
rm -rf "$HOME/Library/Application Support/AnkiProgramFiles/.venv/lib/python3.13/site-packages/aqt/__pycache__"
rm -rf "$HOME/Library/Application Support/AnkiProgramFiles/.venv/lib/python3.13/site-packages/anki_mac_helper/__pycache__"
find "$HOME/Library/Application Support/AnkiProgramFiles/.venv/lib/python3.13/site-packages/anki" -name "*.pyc" -delete 2>/dev/null || true
find "$HOME/Library/Application Support/AnkiProgramFiles/.venv/lib/python3.13/site-packages/aqt" -name "*.pyc" -delete 2>/dev/null || true
find "$HOME/Library/Application Support/AnkiProgramFiles/.venv/lib/python3.13/site-packages/anki_mac_helper" -name "*.pyc" -delete 2>/dev/null || true

echo "Installing $ANKI_WHEEL"
echo "Installing $AQT_WHEEL"

# Uninstall first
"$LAUNCHER_PYTHON" -m pip uninstall -y anki aqt || true

# Install fresh without any caching
"$LAUNCHER_PYTHON" -m pip install --no-cache-dir "$ANKI_WHEEL" "$AQT_WHEEL"

# Install macOS helper
echo "Installing macOS helper to launcher venv..."
MAC_HELPER_WHEEL=$(ls -t qt/mac/dist/anki_mac_helper-*.whl | head -1)
"$LAUNCHER_PYTHON" -m pip install --force-reinstall "$MAC_HELPER_WHEEL"

echo ""
echo "✓ Installation complete!"