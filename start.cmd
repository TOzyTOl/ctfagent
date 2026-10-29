@echo off
REM One-click launcher for CTFAGENT on Windows.
REM Double-click this file, or run it from any terminal.
REM
REM   start.cmd             start, or open the page if it is already running
REM   start.cmd -Restart    stop the running instance first, then start fresh
REM
REM If the code on disk is newer than the running process, start.cmd says so
REM and offers to restart -- that is the case where a page added today 404s.

setlocal
cd /d "%~dp0"

where pwsh >nul 2>nul
if %ERRORLEVEL%==0 (
    pwsh -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
    goto :end
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo   CTFAGENT failed to start. See the messages above.
    echo.
    pause
)

:end
endlocal
