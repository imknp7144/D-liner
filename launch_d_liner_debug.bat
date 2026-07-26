@echo off
REM launch_d_liner_debug.bat — コンソール表示ありのデバッグ起動用
set VENV_PY=C:\TOOLS\GRAPHIC_TOOLS\d_liner\d_liner_runtime_env\Scripts\python.exe
set MAIN=C:\TOOLS\GRAPHIC_TOOLS\d_liner\main_window.py
"%VENV_PY%" "%MAIN%"
pause
