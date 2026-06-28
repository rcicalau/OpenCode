@echo off
setlocal
set "BUDDY_HOME=%~dp0"
if not defined CODEBUDDY_START_DIR set "CODEBUDDY_START_DIR=%CD%"
set "PYTHONPATH=%BUDDY_HOME%src;%PYTHONPATH%"
set "BUDDY_PYTHON=python"

call :ensure_python312
if not defined BUDDY_PYTHON_OK (
    echo Code Buddy requires Python 3.12 or newer.
    echo Make sure the python command points to Python 3.12+.
    exit /b 1
)

if "%~1"=="" (
    "%BUDDY_PYTHON%" -m codebuddy chat
) else (
    "%BUDDY_PYTHON%" -m codebuddy %*
)

endlocal
exit /b 0

:ensure_python312
set "BUDDY_PYTHON_OK="
"%BUDDY_PYTHON%" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>nul
if not errorlevel 1 (
    set "BUDDY_PYTHON_OK=1"
    exit /b 0
)
for %%P in (
    "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
    "%ProgramFiles%\Python312\python.exe"
    "%ProgramFiles(x86)%\Python312\python.exe"
) do (
    if exist "%%~P" (
        "%%~P" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>nul
        if not errorlevel 1 (
            set "BUDDY_PYTHON=%%~P"
            set "BUDDY_PYTHON_OK=1"
            exit /b 0
        )
    )
)
exit /b 0
