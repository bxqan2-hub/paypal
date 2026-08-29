@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -m pip install -r requirements.txt
if errorlevel 1 goto :error
python -m playwright install chromium
if errorlevel 1 goto :error
echo.
echo 安装完成。
pause
exit /b 0

:error
echo.
echo 安装失败，请检查 Python 和网络环境。
pause
exit /b 1
