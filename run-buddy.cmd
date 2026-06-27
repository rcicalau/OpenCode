@echo off
setlocal

set "BUDDY_HOME=%~dp0"
set "CODEBUDDY_START_DIR=%CD%"
set "PYTHONPATH=%BUDDY_HOME%src;%PYTHONPATH%"

if exist "%BUDDY_HOME%.venv\Scripts\python.exe" (
    set "BUDDY_PYTHON=%BUDDY_HOME%.venv\Scripts\python.exe"
) else (
    set "BUDDY_PYTHON=python"
)

"%BUDDY_PYTHON%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>nul
if errorlevel 1 (
    echo Code Buddy requires Python 3.12 or newer.
    echo Make sure the python command points to Python 3.12+, or create C:\Users\RaduC\Documents\OpenCode\.venv with Python 3.12+.
    exit /b 1
)

if "%~1"=="" (
    "%BUDDY_PYTHON%" -m codebuddy chat
) else (
    "%BUDDY_PYTHON%" -m codebuddy %*
)

endlocal
