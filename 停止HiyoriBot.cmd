@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "HIYORI_PYTHON=hiyoribot-backend\venv\Scripts\python.exe"
if not exist "%HIYORI_PYTHON%" set "HIYORI_PYTHON=hiyoribot-backend\.venv\Scripts\python.exe"
"%HIYORI_PYTHON%" -X utf8 tools\start_hiyoribot.py --stop
if errorlevel 1 pause
