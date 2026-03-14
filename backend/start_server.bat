@echo off
echo ============================================
echo   Foundry Video Editor -- Local Backend
echo ============================================
echo.
echo Starting Flask server on http://localhost:5000
echo Keep this window open while using the app.
echo.
C:\Python314\python.exe "%~dp0server.py"
echo.
echo Server stopped.
pause
