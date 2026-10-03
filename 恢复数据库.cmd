@echo off
chcp 65001 >nul
cd /d "%~dp0"
if "%~1"=="" (
  echo 请将需要恢复的 .dump 文件拖到本脚本上，旁边应保留对应的 .json 校验文件。
  pause
  exit /b 1
)
set "HIYORI_PYTHON=hiyoribot-backend\venv\Scripts\python.exe"
if not exist "%HIYORI_PYTHON%" set "HIYORI_PYTHON=hiyoribot-backend\.venv\Scripts\python.exe"
"%HIYORI_PYTHON%" -X utf8 tools\database_backup.py restore --file "%~1" --interactive
pause
