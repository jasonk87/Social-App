@echo off
setlocal
cd /d "%~dp0"
echo Checking this branch for updates...
git diff --quiet
if errorlevel 1 goto :dirty
git diff --cached --quiet
if errorlevel 1 goto :dirty
git symbolic-ref --quiet HEAD >nul
if errorlevel 1 goto :detached
git pull --ff-only
if errorlevel 1 goto :failed
echo Updated successfully. Your local files and saved social data are preserved.
pause
exit /b 0
:dirty
echo Update stopped: save or commit your tracked changes first.
goto :failed
:detached
echo Update stopped: select a branch with an upstream before updating.
:failed
echo No files were forcibly overwritten. Review the Git message above.
pause
exit /b 1
