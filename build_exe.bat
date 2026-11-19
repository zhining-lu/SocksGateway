@echo off
setlocal

rem ============================================================
rem  SocksGateway build script
rem  Output: dist\SocksGateway.exe (single file, no console)
rem ============================================================
set APPNAME=SocksGateway
set ENTRY=app.py
set ICON=assets\icon.ico
set PY=.venv\Scripts\python.exe

cd /d "%~dp0"

rem ---- prefer the project venv, fall back to system python ----
if not exist "%PY%" (
    echo [WARN] .venv not found, falling back to system python
    set PY=python
)
if not exist "%ENTRY%" (
    echo [ERROR] entry script %ENTRY% not found
    exit /b 1
)

rem ---- make sure PyInstaller is available ----
%PY% -m PyInstaller --version >nul 2>&1
if errorlevel 1 (
    echo PyInstaller not found, installing...
    %PY% -m pip install -U pyinstaller
)

set ICONOPT=
if exist "%ICON%" set ICONOPT=--icon "%ICON%"

echo.
echo === Building %ENTRY% into %APPNAME%.exe ===
echo.

rem --windowed      no console window (required for a GUI app)
rem --add-data ui   the UI assets must be bundled or the window is blank
rem --collect-all   required webview runtime files (WebView2)
%PY% -m PyInstaller --noconfirm --clean ^
    --onefile ^
    --windowed ^
    --name %APPNAME% ^
    %ICONOPT% ^
    --collect-all webview ^
    --add-data "ui;ui" ^
    "%ENTRY%"

if errorlevel 1 (
    echo.
    echo [ERROR] build failed, see the log above
    exit /b 1
)

echo.
echo === DONE: dist\%APPNAME%.exe ===
echo   GUI mode     : double click it
echo   Headless mode: dist\%APPNAME%.exe --headless
echo.
