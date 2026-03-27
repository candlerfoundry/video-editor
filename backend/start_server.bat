@echo off
title Foundry Video Editor Backend
echo Starting Foundry Video Editor backend...
python --version >nul 2>&1
if errorlevel 1 (
  echo ERROR: Python not found. Please install Python from python.org
  pause
  exit /b 1
)
cd /d "%~dp0"
pip install -r requirements.txt -q
echo Backend starting on http://localhost:5000
cmd /k python server.py
