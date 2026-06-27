@echo off
setlocal
set "BUDDY_HOME=%~dp0"
set "TARGET_DIR=%LOCALAPPDATA%\Microsoft\WindowsApps"
set "TARGET=%TARGET_DIR%\buddy.cmd"

if not exist "%TARGET_DIR%" mkdir "%TARGET_DIR%"

python -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>nul
if errorlevel 1 (
    echo Code Buddy requires Python 3.12 or newer.
    echo Make sure the python command points to Python 3.12+.
    exit /b 1
)

echo Installing Code Buddy Python package and terminal UI dependencies...
python -m pip install -e "%BUDDY_HOME%."
if errorlevel 1 (
    echo Failed to install Code Buddy dependencies.
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
