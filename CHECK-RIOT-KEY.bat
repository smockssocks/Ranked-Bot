@echo off
title Riot API Key Check
cd /d "%~dp0"
cls
echo.
echo   Checking your Riot API key...
echo.
if exist ".venv\Scripts\python.exe" goto run
echo   The bot environment does not exist yet.
echo   Run START-BOT.bat once first, then try this again.
echo.
pause
exit /b 1
:run
".venv\Scripts\python.exe" tools\check_riot_key.py
echo.
pause
