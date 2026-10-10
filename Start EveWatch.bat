@echo off
title EveWatch
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
    py evewatch.py
) else (
    python evewatch.py
)
if errorlevel 1 (
    echo.
    echo Something went wrong. If it says Python is not found,
    echo install it from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" during install.
    pause
)
