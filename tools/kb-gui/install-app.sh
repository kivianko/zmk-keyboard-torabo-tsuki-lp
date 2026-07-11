#!/usr/bin/env bash
# ~/Applications に「torabo-tsuki Keymap.app」を作成する
# ダブルクリック → サーバ未起動なら起動 → ブラウザでGUIを開く (冪等)
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
APP="$HOME/Applications/torabo-tsuki Keymap.app"
TOOLCHAIN="${ZMK_TOOLCHAIN:-$HOME/dev/zmk-toolchain}"

mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

cat > "$APP/Contents/Info.plist" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>torabo-tsuki Keymap</string>
  <key>CFBundleDisplayName</key><string>torabo-tsuki Keymap</string>
  <key>CFBundleIdentifier</key><string>local.torabo-tsuki.keymap</string>
  <key>CFBundleExecutable</key><string>run</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleIconFile</key><string>app.icns</string>
  <key>LSUIElement</key><true/>
</dict></plist>
PLIST

cat > "$APP/Contents/MacOS/run" << RUN
#!/bin/bash
# サーバが生きていなければ起動してからGUIを開く
REPO="$REPO"
TOOLCHAIN="$TOOLCHAIN"
URL="http://localhost:8756"
if ! curl -s -o /dev/null -m 1 "\$URL/keymap"; then
  nohup "\$TOOLCHAIN/venv/bin/python3" "\$REPO/tools/kb-gui/server.py" \\
    >> "\$TOOLCHAIN/gui-server.log" 2>&1 &
  for _ in \$(seq 1 20); do
    curl -s -o /dev/null -m 1 "\$URL/" && break
    sleep 0.3
  done
fi
open "\$URL"
RUN
chmod +x "$APP/Contents/MacOS/run"

echo "作成: $APP"
