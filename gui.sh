#!/usr/bin/env bash
# キーマップGUIを起動してブラウザで開く
set -euo pipefail
cd "$(dirname "$0")"
VENV="${ZMK_TOOLCHAIN:-$HOME/dev/zmk-toolchain}/venv"
( sleep 1; open "http://localhost:8756" ) &
exec "$VENV/bin/python3" tools/kb-gui/server.py
