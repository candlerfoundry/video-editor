@echo off
title Building Foundry Video Editor Launcher
echo Installing PyInstaller...
python -m pip install pyinstaller -q
if errorlevel 1 (
  echo.
  echo Failed to install PyInstaller.
  pause
  exit /b 1
)

echo Building launcher...
python -m PyInstaller --onefile --windowed --icon foundry.ico --name "Foundry Video Editor" launcher.py
if errorlevel 1 (
  echo.
  echo Build failed.
  pause
  exit /b 1
)

echo.
echo Build complete. Find your exe in the dist\ folder.
pause