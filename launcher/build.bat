@echo off
title Building App Launcher
echo Installing PyInstaller...
python -m pip install pyinstaller -q
if errorlevel 1 (
  echo.
  echo Failed to install PyInstaller.
  pause
  exit /b 1
)

echo Building launcher...
python -m PyInstaller --onefile --windowed --icon rocket.ico --add-data "rocket.ico;." --name "App Launcher" launcher.py
if errorlevel 1 (
  echo.
  echo Build failed.
  pause
  exit /b 1
)

echo.
echo Build complete. Find your exe in the dist\ folder.
pause