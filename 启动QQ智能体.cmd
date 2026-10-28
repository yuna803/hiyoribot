@echo off
cd /d "%~dp0"
if not defined HIYORI_FLASH_HOME set "HIYORI_FLASH_HOME=E:\unser\q\hiyoribot_flash"
if exist "E:\unser\q\hiyoribot_flash\env\Scripts\python.exe" (
 "E:\unser\q\hiyoribot_flash\env\Scripts\python.exe" "%~dp0tools\start_qq.py"
) else (
 "%~dp0hiyoribot-backend\venv\Scripts\python.exe" "%~dp0tools\start_qq.py"
)
pause
