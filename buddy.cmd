@echo off
setlocal
set "BUDDY_HOME=%~dp0"
if not defined CODEBUDDY_START_DIR set "CODEBUDDY_START_DIR=%CD%"
set "PYTHONPATH=%BUDDY_HOME%src;%PYTHONPATH%"

if "%~1"=="" (
    py -3.12 -m codebuddy --pick-root chat
) else (
    py -3.12 -m codebuddy %*
)

endlocal
