@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0build-windows.ps1"
set "BUILD_RESULT=%ERRORLEVEL%"
echo.
if not "%BUILD_RESULT%"=="0" echo Build failed. See the message above and output\build.log.
pause
exit /b %BUILD_RESULT%
