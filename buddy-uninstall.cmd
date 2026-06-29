@echo off
setlocal
set "BUDDY_HOME=%~dp0"
set "TARGET_DIR=%LOCALAPPDATA%\Microsoft\WindowsApps"
set "TARGET=%TARGET_DIR%\buddy.cmd"
set "BUDDY_PYTHON=python"
set "FORCE="

:parse_args
if "%~1"=="" goto args_done
if /I "%~1"=="--force" set "FORCE=1"
shift
goto parse_args

:args_done
echo Uninstalling Code Buddy...

if exist "%TARGET%" (
    findstr /I /C:"%BUDDY_HOME%buddy.cmd" "%TARGET%" >nul 2>nul
    if errorlevel 1 (
        if defined FORCE (
            del /f /q "%TARGET%"
            echo Removed launcher: %TARGET%
        ) else (
            echo Found launcher, but it does not point to this checkout:
            echo   %TARGET%
            echo Leaving it in place. Run buddy-uninstall.cmd --force to remove it anyway.
        )
    ) else (
        del /f /q "%TARGET%"
        echo Removed launcher: %TARGET%
    )
) else (
    echo No WindowsApps launcher found at:
    echo   %TARGET%
)

call :find_python
if defined BUDDY_PYTHON_OK (
    "%BUDDY_PYTHON%" -m pip show codebuddy >nul 2>nul
    if errorlevel 1 (
        echo Code Buddy Python package was not installed for this Python.
    ) else (
        "%BUDDY_PYTHON%" -m pip uninstall -y codebuddy
        if errorlevel 1 (
            echo pip could not remove the Code Buddy Python package.
        )
    )
) else (
    echo Python not found; skipped Python package uninstall.
)

echo.
echo Uninstall complete. Open a new terminal before checking 'where buddy'.
endlocal
exit /b 0

:find_python
set "BUDDY_PYTHON_OK="
"%BUDDY_PYTHON%" -c "import sys" >nul 2>nul
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
        "%%~P" -c "import sys" >nul 2>nul
        if not errorlevel 1 (
            set "BUDDY_PYTHON=%%~P"
            set "BUDDY_PYTHON_OK=1"
            exit /b 0
        )
    )
)
exit /b 0
