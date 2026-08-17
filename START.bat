@echo off
chcp 65001 >nul
cd /d "%~dp0"
call "%~dp0STOP.bat" >nul 2>&1
if not exist ".venv\Scripts\python.exe" (
  echo Please run INSTALL_AND_START.bat first.
  pause
  exit /b 1
)
if not exist ".env" copy /y ".env.example" ".env" >nul
if not exist "data" mkdir data
start "OAI IPRocket Bridge" /min ".venv\Scripts\python.exe" -u iprocket_chain_bridge.py
if /I not "%OPLL_NO_BROWSER%"=="1" start "" /b powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 2; Start-Process 'http://127.0.0.1:18794/'"
".venv\Scripts\python.exe" -m payment_link_extractor.web --env-file .env
