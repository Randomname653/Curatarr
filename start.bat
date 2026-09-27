@echo off
setlocal EnableDelayedExpansion
REM This console's PATH is missing System32 (bare "timeout" gave "not
REM recognized", and "chcp" lives there too). Prepend the standard Windows dirs
REM so the built-in tools resolve; %SystemRoot% is always defined.
set "PATH=%SystemRoot%\System32;%SystemRoot%;%PATH%"
title Curatarr
color 0E

REM Plain-ASCII banner on purpose. A Unicode / box-drawing banner only renders
REM when the console codepage matches the file encoding, and forcing it with
REM "chcp 65001" makes cmd mis-read this UTF-8 .bat on a double-click launch
REM (the window then fails to start). ASCII renders the same in every codepage.
echo.
echo   ================================================================
echo                          C U R A T A R R
echo                Personal AI Media Curator for Plex
echo   ================================================================
echo.

if exist venv\Scripts\activate.bat (
    call venv\Scripts\activate.bat
) else if exist .venv\Scripts\activate.bat (
    call .venv\Scripts\activate.bat
)

REM One interpreter for every start, however this window was opened. A bare
REM "python" follows PATH, and PATH is not the same in every console: a
REM double-click window can come up without the machine part of PATH (see
REM the System32 note above), and on the owner's box PlatformIO keeps its own
REM Python there - so one start ran on PlatformIO's Python and the next on
REM Python 3.12 (2026-09-25 / 09-27): two installs, each raised only when it
REM happened to run, and both shared with other tools whose own pins the
REM raises broke. Order: a venv in this folder, else the Python launcher's
REM 3.12 (the version the lock is compiled for), else PATH. The window and
REM the app log name the one that runs.
set "PY="
if exist "venv\Scripts\python.exe" set "PY=venv\Scripts\python.exe"
if not defined PY if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY for /f "usebackq delims=" %%i in (`py -3.12 -c "import sys; print(sys.executable)" 2^>nul`) do set "PY=%%i"
if not defined PY set "PY=python"
echo  Python: !PY!
if not exist "venv\Scripts\python.exe" if not exist ".venv\Scripts\python.exe" (
    echo  [hint] No venv in this folder: Curatarr shares that Python with other tools
    echo         and raises packages in it. Keep them apart once with: py -3.12 -m venv venv
)
echo.

REM Check the PINNED versions against this interpreter and pull what is
REM missing or outdated (src/deps_check.py, stdlib only). The old sentinel
REM import proved presence, not version -- a Dependabot bump passed it
REM unnoticed -- and imported Crypto, which is neither pinned nor used, so a
REM fresh install ran pip on every start.
"!PY!" -m src.deps_check >nul 2>&1
if errorlevel 1 (
    echo  [SETUP] Dependencies missing or outdated - installing. A fresh venv gets the
    echo          tested lock, exact and hash-checked - a few minutes the first time.
    "!PY!" -m src.deps_check --install
    echo.
)

REM The tested install (lock\requirements.txt): raise what fell below it,
REM then let the lock follow this interpreter (src\deps_lock.py). Never
REM lowers anything; a package the lock does not know is added to it.
"!PY!" -m src.deps_lock --apply

REM Check that the Ollama models THIS install runs on are present: curator,
REM summarizer, the embedding model of the stored profile (not the .env
REM default - a hardcoded nomic-embed-text probe here cried "missing" on an
REM install running v2-moe), the pitcher when enabled. build_models.py
REM --check returns 0 = all present, 1 = some missing (built below),
REM 2 = Ollama not answering (nothing can be said, so nothing is pulled).
echo Checking Ollama models...
set "_MODELS_RC=0"
"!PY!" build_models.py --check
if errorlevel 1 set "_MODELS_RC=1"
if errorlevel 2 set "_MODELS_RC=2"
if "%_MODELS_RC%"=="2" (
    echo  [WARN] Ollama is not answering - model check skipped. The app checks again at startup.
)
if "%_MODELS_RC%"=="1" (
    echo.
    echo  [SETUP] Ollama models missing. Building / pulling now...
    echo  This only happens once.
    echo.
    "!PY!" build_models.py
    if errorlevel 1 (
        echo.
        echo  [ERROR] Model build failed. Check the output above.
        echo  Make sure Ollama is running and your base models are pulled.
        pause
        exit /b 1
    )
    echo.
)

if not exist .env (
    echo  [FIRST RUN] No configuration found.
    echo  The setup wizard will open in your browser.
    echo.
)

REM Open browser after 3s. Full path to timeout.exe - the console PATH here is
REM missing System32 (bare "timeout" gave "not recognized"); %SystemRoot% is
REM always defined, so this works regardless of a truncated PATH.
start "" /B cmd /c "%SystemRoot%\System32\timeout.exe /t 3 /nobreak >nul && start http://localhost:8000"

echo  Running at http://localhost:8000  ^|  Press Ctrl+C to stop
echo.

REM --reload-dir src: only watch the source tree. By default --reload watches
REM the whole project, including data/ where the app constantly writes the
REM metadata cache + SQLite DBs - that spammed "watchfiles: N changes detected"
REM and risked reload churn. Scoping to src/ keeps hot-reload for code while the
REM data writes go unwatched. (The frontend is served statically - no app
REM reload needed for index.html edits; just refresh the browser.)
REM --timeout-graceful-shutdown: backstop so lingering connections (an SSE
REM stream from a tab that never got the shutdown signal, e.g. a sleeping
REM phone) can only delay shutdown by 8s instead of forever.
"!PY!" -m uvicorn src.main:app --host 0.0.0.0 --port 8000 --reload --reload-dir src --timeout-graceful-shutdown 8 --no-server-header

REM Clean exit (web-UI shutdown / Ctrl+C) -> close the window by itself.
REM Crash (port in use, import error) -> keep the output visible.
if errorlevel 1 (
    echo.
    echo  [ERROR] Curatarr exited with an error ^(code %errorlevel%^). Output above.
    pause
)
