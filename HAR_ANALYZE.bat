@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Please run INSTALL_AND_START.bat first.
  pause
  exit /b 1
)

set "INPUT=%~1"
if "%INPUT%"=="" set /p "INPUT=Enter HAR path: "
if "%INPUT%"=="" (
  echo HAR path is required.
  pause
  exit /b 2
)
if not exist "%INPUT%" (
  echo HAR file not found: %INPUT%
  pause
  exit /b 2
)

set "OUTPUT=%~2"
if "%OUTPUT%"=="" for %%I in ("%INPUT%") do set "OUTPUT=%%~dpnI.report.md"
set "PYTHON=%~dp0.venv\Scripts\python.exe"

echo Parsing: %INPUT%
echo Report: %OUTPUT%
"%PYTHON%" tools\har_analyze.py "%INPUT%" --format markdown --output "%OUTPUT%"
set "EXIT_CODE=%ERRORLEVEL%"
echo Parser exit status: %EXIT_CODE%
if not defined HAR_TOOLS_NO_PAUSE pause
exit /b %EXIT_CODE%
