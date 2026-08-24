@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Please run INSTALL_AND_START.bat first.
  pause
  exit /b 1
)
if not exist "data\captures" mkdir "data\captures"
if exist "data\roxy-capture.stop" del /q "data\roxy-capture.stop"

set "OUTPUT=%~1"
if "%OUTPUT%"=="" set "OUTPUT=data\captures\roxy-%RANDOM%-%RANDOM%.har"
set "PYTHON=%~dp0.venv\Scripts\python.exe"
echo Existing RoxyBrowser pages:
"%PYTHON%" tools\roxy_har_capture.py --list
if errorlevel 1 (
  echo No open RoxyBrowser page was found.
  if not defined HAR_TOOLS_NO_PAUSE pause
  exit /b 2
)
echo Output: %OUTPUT%
echo Select a page, navigate it to the exact start node, then press Enter.
"%PYTHON%" tools\roxy_har_capture.py --output "%OUTPUT%" --stop-file "data\roxy-capture.stop" --require-complete
set "EXIT_CODE=%ERRORLEVEL%"
echo Roxy capture exit status: %EXIT_CODE%
if not defined HAR_TOOLS_NO_PAUSE pause
exit /b %EXIT_CODE%
