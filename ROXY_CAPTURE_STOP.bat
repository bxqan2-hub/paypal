@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"
if not exist "data" mkdir "data"
type nul > "data\roxy-capture.stop"
echo Stop signal sent. The attached Roxy capture will save its HAR shortly.
if not defined HAR_TOOLS_NO_PAUSE pause
exit /b 0
