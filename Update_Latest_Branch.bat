@echo off
setlocal enabledelayedexpansion

echo Checking for the latest updates from GitHub...
git fetch --all --quiet

:: Find the branch with the most recent commit on origin
for /f "tokens=*" %%i in ('git for-each-ref --sort=-committerdate refs/remotes/origin --format="%%(refname:short)"') do (
    set "LATEST_REMOTE=%%i"
    
    :: Prevent git from accidentally grabbing the HEAD pointer symlink
    if not "!LATEST_REMOTE!"=="origin/HEAD" (
        goto :found
    )
)

:found
:: Extract the local branch name
set "LOCAL_BRANCH=%LATEST_REMOTE:origin/=%"

echo.
echo Latest remote branch detected: %LATEST_REMOTE%
set /p "CONFIRM=Sync local '%LOCAL_BRANCH%' to this branch? (Untracked files will be kept) (y/n): "

if /i "%CONFIRM%" neq "y" (
    echo Update cancelled.
    pause
    exit /b
)

echo.
echo Configuring sparse-checkout to block invalid Windows file names...

:: Enable sparse checkout (non-cone mode for pattern matching)
git config core.sparseCheckout true
git config core.sparseCheckoutCone false

:: Create the info directory if it doesn't exist
if not exist .git\info mkdir .git\info

:: Write the exclusion rules
:: 1. Include everything by default
echo /* > .git\info\sparse-checkout
:: 2. Exclude any file containing a colon
echo !*:* >> .git\info\sparse-checkout
:: 3. Explicitly exclude the exact :memory: file
echo !:memory: >> .git\info\sparse-checkout

echo.
echo Switching to %LOCAL_BRANCH% and pulling changes...

:: Switch to the branch, forcing the move
git checkout -f %LOCAL_BRANCH%

:: Reset tracked files to match origin exactly
git reset --hard %LATEST_REMOTE%

echo.
echo Success! Tracked files are updated to %LATEST_REMOTE%.
echo Sparse-checkout is protecting the directory from invalid agent-generated files.
echo Your local API keys and untracked files were preserved.
pause