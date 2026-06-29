@echo off
setlocal
set "BUDDY_HOME=%~dp0"
set "TARGET_DIR=%LOCALAPPDATA%\Microsoft\WindowsApps"
set "TARGET=%TARGET_DIR%\buddy.cmd"
set "BUDDY_PYTHON=python"

if not exist "%TARGET_DIR%" mkdir "%TARGET_DIR%"

call :ensure_python312
if not defined BUDDY_PYTHON_OK (
    echo Code Buddy requires Python 3.12 or newer.
    echo Make sure the python command points to Python 3.12+.
    exit /b 1
)

echo Installing Code Buddy dependencies from requirements.txt...
if exist "%BUDDY_HOME%requirements.txt" (
    "%BUDDY_PYTHON%" -m pip install -r "%BUDDY_HOME%requirements.txt"
    if errorlevel 1 (
        echo Failed to install Code Buddy requirements.
        exit /b 1
    )
) else (
    echo Missing requirements.txt:
    echo   %BUDDY_HOME%requirements.txt
    exit /b 1
)

echo Installing Code Buddy Python package...
"%BUDDY_PYTHON%" -m pip install -e "%BUDDY_HOME%."
if errorlevel 1 (
    echo Failed to install Code Buddy package.
    exit /b 1
)

> "%TARGET%" echo @echo off
>> "%TARGET%" echo set "CODEBUDDY_START_DIR=%%CD%%"
>> "%TARGET%" echo call "%BUDDY_HOME%buddy.cmd" %%*

echo Installed buddy launcher:
echo   %TARGET%
echo.
echo Open a new cmd.exe window, then run:
echo   buddy

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
