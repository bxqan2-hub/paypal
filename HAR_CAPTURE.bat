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
if not defined OPLL_CAPTURE_PROXY_MAX_LATENCY_MS set "OPLL_CAPTURE_PROXY_MAX_LATENCY_MS=10000"
if /I "%OPLL_CAPTURE_SKIP_PROXY_PROMPT%"=="1" goto PROXY_READY

:PROXY_PROMPT
echo Enter authenticated SOCKS5 proxy as HOST:PORT:USERNAME:PASSWORD.
set "PROXY_VALUE="
set /p "PROXY_VALUE=Proxy: "
if not defined PROXY_VALUE (
  echo A proxy is required; capture stopped.
  exit /b 2
)
set "OPLL_CAPTURE_SOCKS5=%PROXY_VALUE%"
echo Checking proxy connectivity and latency before opening Chrome...
"%PYTHON%" tools\har_capture.py --check-proxy --socks5-proxy-env OPLL_CAPTURE_SOCKS5 --proxy-check-url "https://chatgpt.com/" --proxy-check-attempts 2 --proxy-max-latency-ms %OPLL_CAPTURE_PROXY_MAX_LATENCY_MS%
if not errorlevel 1 goto PROXY_READY
echo Proxy check failed or is too slow. Chrome was not started.
set "RETRY="
set /p "RETRY=Type R to enter another proxy, or Q to quit: "
if /I "%RETRY%"=="R" goto PROXY_PROMPT
if /I "%RETRY%"=="Q" exit /b 2
exit /b 2

:PROXY_READY
if /I "%OPLL_CAPTURE_SKIP_PROXY_PROMPT%"=="1" set "PROXY_ARGS="
if defined OPLL_CAPTURE_SOCKS5 set "PROXY_ARGS=--socks5-proxy-env OPLL_CAPTURE_SOCKS5"
if /I "%OPLL_CAPTURE_REUSE_PROFILE%"=="1" (
  set "PROFILE=data\har-capture-profile\default"
) else (
set "PROFILE=data\har-capture-profile\run-%RANDOM%-%RANDOM%"
)
set "MODE_ARGS="
if /I "%OPLL_CAPTURE_HEADLESS%"=="1" set "MODE_ARGS=%MODE_ARGS% --headless"
if defined OPLL_CAPTURE_DURATION set "MODE_ARGS=%MODE_ARGS% --duration %OPLL_CAPTURE_DURATION%"

echo Starting manual HAR capture.
echo URL: %URL%
echo Output: %OUTPUT%
echo Profile: %PROFILE%
echo Complete the flow in the Chrome window, then press Ctrl+C here to save.

"%PYTHON%" tools\har_capture.py --url "%URL%" %PROXY_ARGS% --user-data-dir "%PROFILE%" %MODE_ARGS% --output "%OUTPUT%"
set "EXIT_CODE=%ERRORLEVEL%"
echo Capture exit status: %EXIT_CODE%
if not defined HAR_TOOLS_NO_PAUSE pause
exit /b %EXIT_CODE%
