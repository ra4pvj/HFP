#!/bin/bash
# Builds dist/HFPagerApp.app on a Mac (needs Python 3 from python.org and internet access).
set -e
cd "$(dirname "$0")"
python3 -m pip install --user -r requirements.txt pyinstaller
python3 -m PyInstaller --noconfirm hfpager_mac.spec
# ad-hoc signature so macOS asks for microphone permission for the app
codesign --force --deep --sign - dist/HFPagerApp.app || true
echo "Готово: dist/HFPagerApp.app"
