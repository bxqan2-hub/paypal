@echo off
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ports=18794,18796; Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object {$ports -contains $_.LocalPort} | ForEach-Object {Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue}"
echo OAI PayPal services stopped.
