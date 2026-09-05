@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"
set "VENV_PY=%~dp0.venv\Scripts\python.exe"
set "VENV_OK=0"
if exist "%VENV_PY%" (
  "%VENV_PY%" -c "import sys" >nul 2>&1
  if not errorlevel 1 set "VENV_OK=1"
)

if "%VENV_OK%"=="0" (
  set "SYSTEM_PY="
  for /d %%D in ("%LocalAppData%\Programs\Python\Python*") do if not defined SYSTEM_PY if exist "%%~fD\python.exe" set "SYSTEM_PY=%%~fD\python.exe"
  if not defined SYSTEM_PY for /f "delims=" %%P in ('where py 2^>nul') do if not defined SYSTEM_PY set "SYSTEM_PY=%%P"
  if not defined SYSTEM_PY for /f "delims=" %%P in ('where python 2^>nul') do if not defined SYSTEM_PY set "SYSTEM_PY=%%P"
  if defined SYSTEM_PY (
    "!SYSTEM_PY!" -c "import sys" >nul 2>&1
    if errorlevel 1 set "SYSTEM_PY="
  )
  if not defined SYSTEM_PY (
    echo A usable Python installation was not found.
    echo Install Python 3.9 or newer, then run this file again.
    pause
    exit /b 1
  )
  if exist ".venv" rmdir /s /q ".venv"
  "!SYSTEM_PY!" -m venv .venv
  if errorlevel 1 (
    echo Python virtual environment creation failed.
    pause
    exit /b 1
  )
)
if not exist ".venv\Scripts\python.exe" (
  echo Python virtual environment creation failed.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m pip install -U pip
if errorlevel 1 (
  echo pip upgrade failed.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo Dependency installation failed.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m playwright install chromium
if errorlevel 1 (
  echo Playwright Chromium installation failed.
  pause
  exit /b 1
)
if not exist ".env" copy /y ".env.example" ".env" >nul
call START.bat
