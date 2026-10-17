@echo off
powershell.exe -NoProfile -File "%~dp0CHECK_VPS.ps1"
if errorlevel 1 echo Check failed. Send the error text; do not change or stop the fleet.
pause
