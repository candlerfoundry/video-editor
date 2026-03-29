@echo off
title Building Foundry Video Editor Launcher
echo =============================================
echo   Foundry Video Editor — Build Launcher EXE
echo =============================================
echo.

echo Installing PyInstaller...
pip install pyinstaller -q

echo.
echo Building launcher...

REM --onefile   : single standalone .exe, no folder of dependencies
REM --windowed  : suppresses the console window (intern-facing build)
REM --name      : sets the .exe filename
REM
REM To build a DEBUG version that shows a console with live logs,
REM remove the --windowed flag:
REM   pyinstaller --onefile --name "Foundry Video Editor" launcher.py
REM
REM If you have an icon.ico file in this folder, add:
REM   --icon=icon.ico

pyinstaller --onefile --windowed --name "Foundry Video Editor" launcher.py

echo.
echo Build complete. Your exe is in the dist\ folder.
echo Copy "dist\Foundry Video Editor.exe" and server.py into the same folder.
echo.
pause
