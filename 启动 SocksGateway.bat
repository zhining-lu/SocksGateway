@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem 优先用 pythonw 启动（不弹黑框）；失败则退回 python
where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "%~dp0app.py"
    exit /b 0
)
python "%~dp0app.py"
