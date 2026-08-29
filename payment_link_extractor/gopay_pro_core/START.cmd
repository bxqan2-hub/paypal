@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "MK_HOST=127.0.0.1"
set "MK_PORT=8931"
python app.py --open-browser
pause
