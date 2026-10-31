#!/bin/bash
# Double-click in Finder (first time: right-click -> Open). Installs dependencies and starts the app.
cd "$(dirname "$0")"
python3 -m pip install --user -r requirements.txt || { echo "Установите Python 3 с python.org"; read -p "Enter..."; exit 1; }
python3 hfpager_app.py
