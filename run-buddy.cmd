@echo off
setlocal

set "BUDDY_HOME=%~dp0"
set "CODEBUDDY_START_DIR=%CD%"
set "PYTHONPATH=%BUDDY_HOME%src;%PYTHONPATH%"

if exist "%BUDDY_HOME%.venv\Scripts\python.exe" (
    set "BUDDY_PYTHON=%BUDDY_HOME%.venv\Scripts\python.exe"
) else (
    set "BUDDY_PYTHON=py -3.12"
)

if "%~1"=="" (
    %BUDDY_PYTHON% -m codebuddy --root "%CD%" chat
) else (
    %BUDDY_PYTHON% -m codebuddy --root "%CD%" %*
)

endlocal
