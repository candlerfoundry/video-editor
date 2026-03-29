@echo off
title Foundry Video Editor — Backend (Advanced)
echo =============================================
echo   Foundry Video Editor Backend (Advanced)
echo   Most users should use the launcher app.
echo   (Foundry Video Editor.exe in this folder)
echo =============================================
echo.
cd /d "%~dp0"
python --version >nul 2>&1
if errorlevel 1 (
  echo ERROR: Python not found. Install from python.org
  pause
  exit /b 1
)
pip install -r requirements.txt -q
echo Backend starting on http://localhost:5000
cmd /k python server.py
