@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
  echo Python launcher was not found.
  pause
  exit /b 1
)

set "CHANNEL=%OPLL_CAPTURE_CHANNEL%"
if "%CHANNEL%"=="" set "CHANNEL=gopay"
set "OUTPUT=%~1"
set "URL=%~2"
if "%URL%"=="" set "URL=https://chatgpt.com/"
set "PROXY_PORT=%~3"
if "%PROXY_PORT%"=="" set "PROXY_PORT=%OPLL_MITM_PROXY_PORT%"
if "%PROXY_PORT%"=="" set "PROXY_PORT=8899"
set "WEB_PORT=%~4"
if "%WEB_PORT%"=="" set "WEB_PORT=%OPLL_MITM_WEB_PORT%"
if "%WEB_PORT%"=="" set "WEB_PORT=8081"
set "DURATION_ARGS="
if defined OPLL_CAPTURE_DURATION set "DURATION_ARGS=--duration %OPLL_CAPTURE_DURATION%"

echo Starting %CHANNEL% capture through mitmproxy.
echo Proxy port: %PROXY_PORT%
echo Web UI: http://127.0.0.1:%WEB_PORT%/
echo Roxy proxy: HTTP 127.0.0.1:%PROXY_PORT% with blank credentials.
echo Enter the upstream proxy when prompted. Input is hidden.
echo Complete the flow in RoxyBrowser, then press Ctrl+C here.
if "%OUTPUT%"=="" (
  py -3 tools\mitm_capture.py --channel "%CHANNEL%" --url "%URL%" --proxy-port %PROXY_PORT% --web-port %WEB_PORT% --prompt-upstream --no-browser %DURATION_ARGS%
) else (
  py -3 tools\mitm_capture.py --channel "%CHANNEL%" --url "%URL%" --output "%OUTPUT%" --proxy-port %PROXY_PORT% --web-port %WEB_PORT% --prompt-upstream --no-browser %DURATION_ARGS%
)
set "EXIT_CODE=%ERRORLEVEL%"
echo Capture exit status: %EXIT_CODE%
if not defined HAR_TOOLS_NO_PAUSE pause
exit /b %EXIT_CODE%
