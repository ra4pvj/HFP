@echo off
rem Builds HFPagerApp (folder dist\HFPagerApp). For Windows 7 use Python 3.8 (32 or 64 bit, matching the PC).
cd /d "%~dp0"
py -m pip install -r requirements.txt "pyinstaller==5.13.2"
py -m PyInstaller --noconfirm --onedir --windowed --name HFPagerApp --collect-all sounddevice --hidden-import hfpager_ptt --hidden-import hfpager_rig --hidden-import serial.tools.list_ports hfpager_app.py
echo.
echo Done: dist\HFPagerApp\HFPagerApp.exe
pause
