@echo off
chcp 65001 >nul
cd /d D:\info-hub
if not exist data mkdir data
if exist data\PAUSED del data\PAUSED
"D:\info-hub\.venv\Scripts\python.exe" "D:\info-hub\infohub.py"
exit /b %errorlevel%
