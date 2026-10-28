@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not defined HIYORI_FLASH_HOME set "HIYORI_FLASH_HOME=E:\unser\q\hiyoribot_flash"
set "HIYORI_PYTHON=E:\unser\q\hiyoribot_flash\env\Scripts\python.exe"
if not exist "%HIYORI_PYTHON%" set "HIYORI_PYTHON=%~dp0hiyoribot-backend\venv\Scripts\python.exe"
"%HIYORI_PYTHON%" tools\start_qq.py --stop
"%HIYORI_PYTHON%" tools\start_hiyoribot.py --stop
if errorlevel 1 pause
