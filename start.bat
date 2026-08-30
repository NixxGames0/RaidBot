@echo off
cd /d "%~dp0"
echo Starting RaidBot...
if exist "venv\Scripts\python.exe" (
    echo Using venv Python (official discord.py)...
    "venv\Scripts\python.exe" bot.py
) else (
    python bot.py
)
pause