@echo off
title RaidBot - Sync
color 0A
cd /d "%~dp0"

echo.
echo  ==========================================
echo          RAIDBOT  -  SYNC TO GITHUB
echo  ==========================================
echo.

:: [1/2] Git check
echo  [1/2] Checking Git...
where git >nul 2>nul
if %errorlevel% neq 0 (
    echo  [ERROR] Git is not installed.
    echo  Download: https://git-scm.com/downloads
    pause
    exit /b 1
)
echo  [OK] Git found.
echo.

:: [2/2] Commit and push
echo  [2/2] Committing changes...
git add .

git diff --staged --quiet
if %errorlevel% equ 0 (
    echo  [INFO] Nothing new to commit.
    goto :done
)

git commit -m "Update RaidBot"
if %errorlevel% neq 0 (
    echo  [ERROR] Commit failed. Check git status.
    pause
    exit /b 1
)
echo  [OK] Committed successfully.
echo.

echo  Pushing to GitHub...
git push origin main
if %errorlevel% neq 0 (
    git push -u origin main
    if %errorlevel% neq 0 (
        echo  [ERROR] Push failed!
        pause
        exit /b 1
    )
)
echo  [OK] Pushed to GitHub.
echo  https://github.com/NixxGames0/RaidBot

:done
echo.
pause