@echo off
title RaidBot - Full Sync
color 0A

:: ── Go to the script's directory ──────────────────────────────────────────
cd /d "%~dp0"

echo ========================================
echo     RAID BOT - FULL SYNC
echo ========================================
echo.

:: ── Check Git ────────────────────────────────────────────────────────────
echo [1/9] Checking Git...
where git >nul 2>nul
if %errorlevel% neq 0 (
    echo [INSTALL] Git not found! Downloading...
    powershell -Command "Invoke-WebRequest -Uri 'https://github.com/git-for-windows/git/releases/download/v2.43.0.windows.1/Git-2.43.0-64-bit.exe' -OutFile '%TEMP%\git-installer.exe'"
    if exist "%TEMP%\git-installer.exe" (
        start /wait "%TEMP%\git-installer.exe" /VERYSILENT /NORESTART /NOCANCEL /SP-
        del "%TEMP%\git-installer.exe" 2>nul
        echo [OK] Git installed!
    )
) else (
    echo [OK] Git found!
)
echo.

:: ── Check GitHub authentication ──────────────────────────────────────────
echo [2/9] Checking GitHub remote...
git remote get-url origin >nul 2>nul
if %errorlevel% neq 0 (
    echo [INFO] No remote set. Adding origin...
    git remote add origin https://github.com/NixxGames0/RaidBot.git
) else (
    echo [OK] Remote found!
)
echo.

:: ── Init git repo ────────────────────────────────────────────────────────
echo [3/9] Setting up Git repository...
if not exist ".git" (
    echo Initializing git...
    git init
    echo [OK] Repository initialized!
) else (
    echo [OK] Repository found!
)
echo.

:: ── Check and set default branch ────────────────────────────────────────
echo [4/9] Checking default branch...
git branch >nul 2>nul
if %errorlevel% neq 0 (
    echo [INFO] No branches yet. Creating main branch...
    git checkout -b main
) else (
    echo [OK] Branch exists.
)

:: Get current branch name
for /f "tokens=*" %%a in ('git rev-parse --abbrev-ref HEAD 2^>nul') do set CURRENT_BRANCH=%%a
if "%CURRENT_BRANCH%"=="" (
    echo [INFO] No branch found. Creating main...
    git checkout -b main
    set CURRENT_BRANCH=main
)
echo [OK] Current branch: %CURRENT_BRANCH%
echo.

:: ── Check if files exist ──────────────────────────────────────────────────
echo [5/9] Checking files...
if not exist "bot.py" (
    echo [ERROR] bot.py not found in this folder!
    echo Please make sure you're in the correct folder.
    echo Current folder: %cd%
    echo.
    pause
    exit /b 1
)
echo [OK] Found bot.py
echo.

:: ── Create .gitignore ──────────────────────────────────────────────────
echo [6/9] Creating .gitignore...
if not exist ".gitignore" (
    (
        echo __pycache__/
        echo *.pyc
        echo *.pyo
        echo .env
        echo venv/
        echo env/
        echo .idea/
        echo .vscode/
        echo *.log
        echo .DS_Store
        echo .git/
    ) > .gitignore
)
echo [OK] .gitignore ready!
echo.

:: ── Stage files ──────────────────────────────────────────────────────────
echo [7/9] Staging files...
git add .
echo [OK] Files staged!
echo.

:: ── Commit ──────────────────────────────────────────────────────────────
echo [8/9] Committing changes...
for /f "tokens=1-6 delims=:,. " %%a in ("%date% %time%") do set timestamp=%%a-%%b-%%c_%%d-%%e-%%f
git commit -m "Deploy RaidBot %timestamp%" 2>nul
if %errorlevel% neq 0 (
    echo [INFO] No changes to commit.
) else (
    echo [OK] Commit successful!
)
echo.

:: ── Push to GitHub ──────────────────────────────────────────────────────
echo [9/9] Pushing to GitHub...
echo.

:: First, check if remote has a main branch
git ls-remote --heads origin main >nul 2>nul
if %errorlevel% neq 0 (
    echo [INFO] Remote main branch doesn't exist. Pushing as new branch...
    git push -u origin %CURRENT_BRANCH%:main
) else (
    echo [INFO] Remote main branch exists. Pushing...
    git push -u origin main
)

if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Push failed!
    echo.
    echo If this is your first push, run these commands manually:
    echo.
    echo   git push -u origin main
    echo.
    echo If that fails, try:
    echo   git push -u origin master
    echo.
    echo Or check your branch name:
    echo   git branch
    echo.
    echo For authentication, use:
    echo   Username: NixxGames0
    echo   Password: Your Personal Access Token (not GitHub password!)
    echo.
    echo Get a token: https://github.com/settings/tokens
    echo Enable: repo permissions
    echo.
    pause
    goto start_bot
)

echo [OK] GitHub push complete!
echo.
echo View your repository: https://github.com/NixxGames0/RaidBot
echo.

:start_bot

:: ── Start bot ────────────────────────────────────────────────────────────
if not exist ".env" (
    echo [WARNING] .env not found! Creating template...
    (
        echo DISCORD_TOKEN=YOUR_DISCORD_TOKEN_HERE
        echo CF_ACCOUNT_ID=YOUR_CLOUDFLARE_ACCOUNT_ID
        echo CF_DB_ID=YOUR_CLOUDFLARE_DB_ID
        echo CF_API_TOKEN=YOUR_CLOUDFLARE_API_TOKEN
        echo GUILD_ID=1535552806663094332
    ) > .env
    echo.
    echo [OK] .env created! Edit it with your tokens.
    notepad .env
    pause
    exit /b 0
)

echo.
echo ========================================
echo         STARTING RAID BOT
echo ========================================
echo.
echo Press Ctrl+C to stop the bot
echo.

python bot.py

echo.
echo [INFO] Bot stopped.
pause