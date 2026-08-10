@echo off
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

start "" pythonw.exe curatarr_tray.pyw
exit
