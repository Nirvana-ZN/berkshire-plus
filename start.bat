@echo off
cd /d "%~dp0"
if exist "%USERPROFILE%\.local\bin\uv.exe" set "PATH=%USERPROFILE%\.local\bin;%PATH%"
curl -s -o nul --connect-timeout 1 http://127.0.0.1:8765/api/health
if %errorlevel%==0 (
  start "" "http://127.0.0.1:8765"
  exit /b 0
)
start "" cmd /c "timeout /t 2 /nobreak >nul & start http://127.0.0.1:8765"
uv run berkshire-plus
