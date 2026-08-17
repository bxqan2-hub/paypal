@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  where py >nul 2>nul
  if not errorlevel 1 (
    py -3 -m venv .venv
  ) else (
    python -m venv .venv
  )
)
if not exist ".venv\Scripts\python.exe" (
  echo Python virtual environment creation failed.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m pip install -U pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if not exist ".env" copy /y ".env.example" ".env" >nul
call START.bat
