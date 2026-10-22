@echo off
setlocal
cd /d "D:\napcat-toolkit\napcat"
if not exist "launcher.bat" (
  echo NapCat standard launcher was not found.
  pause
  exit /b 1
)
echo Starting the NapCat standard launcher for the info-hub main QQ account.
echo If Windows asks for permission, review it and click Yes yourself.
echo Then complete any QR or safety check in the QQ window.
echo Do not share QR codes or verification codes.
call "D:\napcat-toolkit\napcat\launcher.bat" -q 1000000001
echo NapCat launcher exited with code %ERRORLEVEL%.
pause
