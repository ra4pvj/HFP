@echo off
rem Builds a single-file Windows executable (HFPagerApp.exe) in the "dist" folder.
cd /d "%~dp0"
py -m pip install -r requirements.txt pyinstaller
py -m PyInstaller --noconfirm --onefile --windowed --name HFPagerApp --collect-all sounddevice hfpager_app.py
echo.
echo Done: dist\HFPagerApp.exe
pause
