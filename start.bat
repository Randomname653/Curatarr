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

REM Check / install missing deps silently.
REM Import every third-party module the app needs at runtime, not just one
REM sentinel package -- otherwise a single pre-installed package (e.g.
REM apscheduler) makes the check pass while others are still missing.
python -c "import uvicorn, fastapi, multipart, sqlalchemy, jose, Crypto, httpx, aiohttp, chromadb, pydantic_settings, dotenv, numpy, pythonjsonlogger, apscheduler, psutil" >nul 2>&1
if errorlevel 1 (
    echo  [SETUP] Installing missing dependencies...
    pip install -r requirements.txt -q
    echo  Done.
    echo.
)

REM Check if Ollama models are built (curator + summarizer + embedding model).
REM nomic-embed-text is easy to miss: it is NOT baked like the curatarr-* models,
REM so a fresh / reinstalled Ollama without it makes every embedding call 404
REM and leaves all items vector_ready=0. build_models.py pulls whatever
REM EMBEDDING_MODEL is set to, so re-running it covers a custom .env value too.
echo Checking Ollama models...
set "_MODELS_OK=1"
ollama show curatarr-curator >nul 2>&1
if errorlevel 1 set "_MODELS_OK=0"
ollama show nomic-embed-text >nul 2>&1
if errorlevel 1 set "_MODELS_OK=0"
if "%_MODELS_OK%"=="0" (
    echo.
    echo  [SETUP] Ollama models missing. Building / pulling now...
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
