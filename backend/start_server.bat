@echo off
cd /d "%~dp0"
title Foundry Video Editor — Backend Server

echo ============================================
echo   Foundry Video Editor — Backend Server
echo ============================================
echo.

REM ── Check Python ──
C:\Python314\python.exe --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found at C:\Python314\python.exe
    echo.
    echo Please install Python 3.14 from python.org
    echo Make sure to check "Add Python to PATH" during install.
    echo.
    pause
    exit /b 1
)

REM ── Install / update dependencies ──
echo Checking dependencies...
C:\Python314\python.exe -m pip install --quiet flask flask-cors openai-whisper anthropic Pillow
if errorlevel 1 (
    echo.
    echo WARNING: Could not install dependencies. Server may not start.
    echo Try running as Administrator if this keeps happening.
    echo.
)

echo.
echo Starting server on http://localhost:5000
echo Keep this window open while using the Foundry Video Editor.
echo Close this window to stop the server.
echo.

REM ── Start server ──
C:\Python314\python.exe server.py

echo.
echo Server stopped.
pause
