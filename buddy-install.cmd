@echo off
setlocal
set "BUDDY_HOME=%~dp0"
call "%BUDDY_HOME%install-buddy.cmd" %*
endlocal
