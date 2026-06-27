@echo off
setlocal
set "BUDDY_HOME=%~dp0"
if not defined CODEBUDDY_START_DIR set "CODEBUDDY_START_DIR=%CD%"
set "PYTHONPATH=%BUDDY_HOME%src;%PYTHONPATH%"

python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>nul
if errorlevel 1 (
    echo Code Buddy requires Python 3.12 or newer.
    echo Make sure the python command points to Python 3.12+.
    exit /b 1
)

if "%~1"=="" (
    python -m codebuddy chat
) else (
    python -m codebuddy %*
)

endlocal
