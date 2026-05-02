@echo off
title Curatarr
color 0E

echo.
echo  ██████╗██╗   ██╗██████╗  █████╗ ████████╗ █████╗ ██████╗ ██████╗
echo ██╔════╝██║   ██║██╔══██╗██╔══██╗╚══██╔══╝██╔══██╗██╔══██╗██╔══██╗
echo ██║     ██║   ██║██████╔╝███████║   ██║   ███████║██████╔╝██████╔╝
echo ██║     ██║   ██║██╔══██╗██╔══██║   ██║   ██╔══██║██╔══██╗██╔══██╗
echo ╚██████╗╚██████╔╝██║  ██║██║  ██║   ██║   ██║  ██║██║  ██║██║  ██║
echo  ╚═════╝ ╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═╝   ╚═╝   ╚═╝  ╚═╝╚═╝  ╚═╝╚═╝  ╚═╝
echo.
echo  Personal AI Media Curator for Plex
echo  ─────────────────────────────────────────────────────────────────────
echo.

if exist venv\Scripts\activate.bat (
    call venv\Scripts\activate.bat
) else if exist .venv\Scripts\activate.bat (
    call .venv\Scripts\activate.bat
)

REM Check / install missing deps silently
python -c "import apscheduler" >nul 2>&1
if errorlevel 1 (
    echo  [SETUP] Installing missing dependencies...
    pip install -r requirements.txt -q
    echo  Done.
    echo.
)

REM Check if Ollama models are built
echo Checking Ollama models...
ollama show curatarr-curator >nul 2>&1
if errorlevel 1 (
    echo.
    echo  [SETUP] Ollama models not found. Building now...
    echo  This only happens once.
    echo.
    python build_models.py
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

REM Open browser after 3s
start "" /B cmd /c "timeout /t 3 /nobreak > nul && start http://localhost:8000"

echo  Running at http://localhost:8000  ^|  Press Ctrl+C to stop
echo.

python -m uvicorn src.main:app --host 0.0.0.0 --port 8000 --reload

pause
