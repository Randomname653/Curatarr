@echo off
setlocal EnableDelayedExpansion
REM Curatarr tray launcher — starts the tray app on the SAME interpreter
REM start.bat uses (PATH resolution + venv activation), then closes.
REM
REM Why not double-click curatarr_tray.pyw directly? The .pyw association
REM goes through the Windows py-launcher, which picks the NEWEST installed
REM Python — on a box with several installs that can be a different
REM interpreter than the one carrying Curatarr's dependencies. This bat
REM pins the tray to the exact environment the dev entry runs on.
set "PATH=%SystemRoot%\System32;%SystemRoot%;%PATH%"
cd /d "%~dp0"

if exist venv\Scripts\activate.bat (
    call venv\Scripts\activate.bat
) else if exist .venv\Scripts\activate.bat (
    call .venv\Scripts\activate.bat
)

REM Same order as start.bat: a venv here, else Python 3.12 through the
REM launcher, else pythonw on PATH - PATH alone gave different starts
REM different interpreters (2026-09-25 / 09-27).
set "PYW="
if exist "venv\Scripts\pythonw.exe" set "PYW=venv\Scripts\pythonw.exe"
if not defined PYW if exist ".venv\Scripts\pythonw.exe" set "PYW=.venv\Scripts\pythonw.exe"
if not defined PYW for /f "usebackq delims=" %%i in (`py -3.12 -c "import os, sys; print(os.path.join(os.path.dirname(sys.executable), 'pythonw.exe'))" 2^>nul`) do set "PYW=%%i"
if not defined PYW set "PYW=pythonw.exe"
start "" "!PYW!" curatarr_tray.pyw
exit
