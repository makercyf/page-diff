@echo off
cd /d "%~dp0"

start "Local App" cmd /k "uv run main.py"

timeout /t 2 /nobreak >nul
start "" "http://127.0.0.1:65500"