@echo off
setlocal
set "BUDDY_HOME=%~dp0"
if /I "%~1"=="uninstall" (
    call "%BUDDY_HOME%buddy-uninstall.cmd" %*
    exit /b %ERRORLEVEL%
)
call "%BUDDY_HOME%install-buddy.cmd" %*
endlocal
