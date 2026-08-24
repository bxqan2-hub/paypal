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

set "OUTPUT=%~1"
if "%OUTPUT%"=="" set "OUTPUT=data\captures\gcash-%RANDOM%-%RANDOM%.har"
set "URL=%~2"
if "%URL%"=="" set "URL=https://chatgpt.com/?promo_campaign=plus-1-month-free"
set "PYTHON=%~dp0.venv\Scripts\python.exe"
set "MODE_ARGS="
if /I "%OPLL_CAPTURE_HEADLESS%"=="1" set "MODE_ARGS=%MODE_ARGS% --headless"
if defined OPLL_CAPTURE_DURATION set "MODE_ARGS=%MODE_ARGS% --duration %OPLL_CAPTURE_DURATION%"

echo Starting manual HAR capture.
echo URL: %URL%
echo Output: %OUTPUT%
echo Complete the flow in the Chrome window, then press Ctrl+C here to save.

if defined OPLL_CAPTURE_SOCKS5 (
  "%PYTHON%" tools\har_capture.py --url "%URL%" --socks5-proxy-env OPLL_CAPTURE_SOCKS5 %MODE_ARGS% --output "%OUTPUT%"
) else (
  "%PYTHON%" tools\har_capture.py --url "%URL%" %MODE_ARGS% --output "%OUTPUT%"
)
set "EXIT_CODE=%ERRORLEVEL%"
echo Capture exit status: %EXIT_CODE%
if not defined HAR_TOOLS_NO_PAUSE pause
exit /b %EXIT_CODE%
