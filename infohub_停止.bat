@echo off
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "D:\info-hub\infohub_stop.ps1"
exit /b %errorlevel%
