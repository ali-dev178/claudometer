#!/usr/bin/env bash
# Build a standalone Claudometer.app menu-bar agent.
# Prereq:  python3 -m pip install -r requirements-dev.txt
# Output:  dist/Claudometer.app
set -euo pipefail
cd "$(dirname "$0")/.."

# --icon expects .icns on macOS; convert if you have iconutil:
#   mkdir icon.iconset && sips -z 512 512 assets/icon.png --out icon.iconset/icon_512x512.png
#   iconutil -c icns icon.iconset -o assets/icon.icns   (then add: --icon assets/icon.icns)

python3 -m PyInstaller --onefile --windowed --name Claudometer \
    --collect-submodules PIL \
    --osx-bundle-identifier com.claudometer.app \
    app.py

# Hide the Dock icon (menu-bar agent) by adding LSUIElement to the app plist:
PLIST="dist/Claudometer.app/Contents/Info.plist"
if [ -f "$PLIST" ]; then
    /usr/libexec/PlistBuddy -c "Add :LSUIElement bool true" "$PLIST" 2>/dev/null || \
    /usr/libexec/PlistBuddy -c "Set :LSUIElement true" "$PLIST"
fi
echo "Built: dist/Claudometer.app"
