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

set "PROXY_PORT=%~3"
if "%PROXY_PORT%"=="" set "PROXY_PORT=%OPLL_MITM_PROXY_PORT%"
if "%PROXY_PORT%"=="" set "PROXY_PORT=8899"
set "WEB_PORT=%~4"
if "%WEB_PORT%"=="" set "WEB_PORT=%OPLL_MITM_WEB_PORT%"
if "%WEB_PORT%"=="" set "WEB_PORT=8081"
set "CONTROL_PORT=%OPLL_MITM_CONTROL_PORT%"
if "%CONTROL_PORT%"=="" set "CONTROL_PORT=8080"

echo Opening mitmproxy Roxy control panel.
echo Control: http://127.0.0.1:%CONTROL_PORT%/
echo Roxy proxy: HTTP 127.0.0.1:%PROXY_PORT% with blank credentials.
py -3 tools\roxy_mitm_control.py --port %CONTROL_PORT% --proxy-port %PROXY_PORT% --web-port %WEB_PORT%
set "EXIT_CODE=%ERRORLEVEL%"
echo Capture exit status: %EXIT_CODE%
if not defined HAR_TOOLS_NO_PAUSE pause
exit /b %EXIT_CODE%
