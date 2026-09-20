@echo off
title Ranked Inhouse Bot
cd /d "%~dp0"
color 0F
cls
echo.
echo   ==========================================
echo      RANKED INHOUSE BOT
echo   ==========================================
echo.

REM ============================================================
REM  1. Find Python. Prefer the "py" launcher because plain
REM     "python" on Windows often opens the Microsoft Store.
REM ============================================================
set "PY="
py -3 --version >nul 2>&1
if %errorlevel%==0 set "PY=py -3"
if defined PY goto have_python

python --version >nul 2>&1
if %errorlevel%==0 set "PY=python"
if defined PY goto have_python
goto no_python

:have_python
echo   Using Python: %PY%

REM ============================================================
REM  2. Create the private Python environment (first run only)
REM ============================================================
if exist ".venv\Scripts\python.exe" goto have_venv
echo.
echo   First run: building the Python environment.
echo   This takes about a minute. Please wait...
echo.
%PY% -m venv .venv
if errorlevel 1 goto venv_failed
:have_venv
set "VPY=.venv\Scripts\python.exe"

REM ============================================================
REM  3. Install the bot's dependencies (first run only)
REM ============================================================
"%VPY%" -c "import discord, sqlalchemy, aiohttp, aiosqlite, dotenv" >nul 2>&1
if %errorlevel%==0 goto have_deps
echo.
echo   Installing the bot's dependencies.
echo   This takes a minute or two on the first run. Please wait...
echo.
"%VPY%" -m pip install --upgrade pip --quiet --disable-pip-version-check
"%VPY%" -m pip install -r requirements.txt --disable-pip-version-check
if errorlevel 1 goto pip_failed
:have_deps

REM ============================================================
REM  4. Make the settings file the first time
REM ============================================================
if exist ".env" goto have_env
echo.
echo   ------------------------------------------------------
echo    FIRST TIME SETUP
echo   ------------------------------------------------------
echo    I am creating your settings file and opening it in
echo    Notepad.
echo.
echo    Fill in these three lines:
echo        DISCORD_TOKEN=
echo        DISCORD_GUILD_ID=
echo        RIOT_API_KEY=
echo.
echo    Then SAVE the file with Ctrl+S and CLOSE Notepad.
echo    This window will carry on by itself.
echo   ------------------------------------------------------
echo.
pause
copy ".env.example" ".env" >nul
notepad .env
:have_env

REM ============================================================
REM  5. Check everything before starting
REM ============================================================
"%VPY%" tools\preflight.py
if errorlevel 1 goto preflight_failed

REM ============================================================
REM  6. Run
REM ============================================================
echo   ==========================================
echo    The bot is running. KEEP THIS WINDOW OPEN.
echo    Closing it turns the bot off.
echo    Press Ctrl+C to stop it on purpose.
echo   ==========================================
echo.
"%VPY%" -m bot.main
echo.
echo   The bot stopped. Any error is printed above.
goto end


:no_python
color 0C
echo.
echo   PROBLEM: Python is not installed, or Windows cannot find it.
echo.
echo   How to fix it:
echo     1. Go to   https://www.python.org/downloads/
echo     2. Click the big yellow "Download Python" button.
echo     3. Run the installer.
echo     4. IMPORTANT: on the very first screen, TICK THE BOX at the
echo        bottom that says "Add python.exe to PATH" BEFORE you click
echo        Install. This is the step everyone misses.
echo     5. Finish the install, then RESTART your computer.
echo     6. Double-click START-BOT.bat again.
echo.
echo   Note: do NOT install Python from the Microsoft Store. It causes
echo   exactly the problems you are seeing.
echo.
goto end

:venv_failed
color 0C
echo.
echo   PROBLEM: could not build the Python environment.
echo.
echo   Usually this means the Python install is incomplete.
echo   Try this:
echo     1. Delete the folder called  .venv  in this directory, if it exists.
echo     2. Reinstall Python from https://www.python.org/downloads/
echo        and TICK "Add python.exe to PATH" during the install.
echo     3. Restart your computer and run START-BOT.bat again.
echo.
goto end

:pip_failed
color 0C
echo.
echo   PROBLEM: could not download the bot's dependencies.
echo.
echo   Usually this is the internet connection or antivirus.
echo   Try this:
echo     1. Check that you are online.
echo     2. Temporarily allow Python through your antivirus / firewall.
echo     3. Delete the folder called  .venv  in this directory.
echo     4. Run START-BOT.bat again.
echo.
goto end

:preflight_failed
color 0E
echo.
echo   The bot did not start. The numbered list above tells you
echo   exactly what to fix.
echo.
echo   To edit your settings: open the file called  .env  in this
echo   folder with Notepad, fix it, save, and run START-BOT.bat again.
echo.
goto end

:end
echo.
pause
