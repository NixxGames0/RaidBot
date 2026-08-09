@echo off
title RaidBot - Sync & Deploy
color 0A
cd /d "%~dp0"

echo.
echo  ==========================================
echo       RAIDBOT  -  SYNC AND DEPLOY
echo  ==========================================
echo.

:: [1/4] Git check
echo  [1/4] Checking Git...
where git >nul 2>nul
if %errorlevel% neq 0 (
    echo  [ERROR] Git is not installed.
    echo  Download: https://git-scm.com/downloads
    pause
    exit /b 1
)
echo  [OK] Git found.
echo.

:: [2/4] Commit
echo  [2/4] Committing changes...
git add .

git diff --staged --quiet
if %errorlevel% equ 0 (
    echo  [INFO] Nothing new to commit.
    goto :push
)

git commit -m "Update RaidBot"
if %errorlevel% neq 0 (
    echo  [ERROR] Commit failed. Check git status.
    pause
    exit /b 1
)
echo  [OK] Committed successfully.

:push
echo.

:: [3/4] Push
echo  [3/4] Pushing to GitHub...
git push origin main
if %errorlevel% neq 0 (
    git push -u origin main
    if %errorlevel% neq 0 (
        echo  [ERROR] Push failed!
        echo  Try: git push -u origin main --force
        pause
        goto :start_bot
    )
)
echo  [OK] Pushed to GitHub.
echo  https://github.com/NixxGames0/RaidBot
echo.

:: [4/4] Railway deploy via GraphQL API
echo  [4/4] Triggering Railway deploy...

curl -s -X POST "https://backboard.railway.com/graphql/v2" ^
  -H "Authorization: Bearer d767fced-5893-48f6-befd-f8d41d15c3ad" ^
  -H "Content-Type: application/json" ^
  -d "{\"query\":\"mutation { serviceInstanceRedeploy(serviceId: \\\"f2f4f617-73bd-49e7-8e26-2b0b64dd92e7\\\") }\"}" ^
  >nul 2>nul

if %errorlevel% equ 0 (
    echo  [OK] Railway deploy triggered!
    echo  Monitor: https://railway.com/project/7abd34d7-50f0-49d5-9415-4a11fe453c53
) else (
    echo  [WARNING] curl failed. Manual deploy at railway.com
)
echo.

:start_bot
echo  Starting RaidBot locally...
echo.

if not exist ".env" (
    echo  [WARNING] .env not found, creating template...
    (
        echo DISCORD_TOKEN=YOUR_DISCORD_TOKEN_HERE
        echo CF_ACCOUNT_ID=YOUR_CLOUDFLARE_ACCOUNT_ID
        echo CF_DB_ID=YOUR_CLOUDFLARE_DB_ID
        echo CF_API_TOKEN=YOUR_CLOUDFLARE_API_TOKEN
        echo GUILD_ID=1535552806663094332
    ) > .env
    echo  [OK] .env created. Fill in your tokens then re-run.
    notepad .env
    pause
    exit /b 0
)

echo  Installing dependencies...
pip install -r requirements.txt -q
if %errorlevel% neq 0 (
    pip install discord.py requests python-dotenv -q
)

echo.
echo  ==========================================
echo          RAIDBOT IS STARTING
echo          Press Ctrl+C to stop
echo  ==========================================
echo.

python bot.py

echo.
echo  [INFO] Bot stopped.
pause