@echo off
title Mr Tofu Drop Mode - close this window to stop
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
rem Runs from wherever this folder is, so the folder can be moved anywhere.
cd /d "%~dp0"
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
  "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" drop_mode_web.py
) else (
  where py >nul 2>nul
  if not errorlevel 1 ( py -3 drop_mode_web.py ) else ( python drop_mode_web.py )
)
echo.
pause
