@echo off
REM launch_d_liner.bat — D-liner 起動ランチャー（実行用venv経由）
REM このファイルは setup_runtime_env.py が自動生成しています。
REM 削除・再生成は setup_runtime_env.bat の再実行で行ってください。

set SCRIPT_DIR=%~dp0
set VENV_PYW=C:\TOOLS\GRAPHIC_TOOLS\d_liner\d_liner_runtime_env\Scripts\pythonw.exe
set VENV_PY=C:\TOOLS\GRAPHIC_TOOLS\d_liner\d_liner_runtime_env\Scripts\python.exe
set MAIN=C:\TOOLS\GRAPHIC_TOOLS\d_liner\main_window.py

if not exist "%VENV_PYW%" (
    echo [ERROR] venv が見つかりません: %VENV_PYW%
    echo setup_runtime_env.bat を先に実行してください。
    pause
    exit /b 1
)

REM デバッグしたい場合は VENV_PY（コンソール表示あり）を使う:
REM "%VENV_PY%" "%MAIN%"
start "" "%VENV_PYW%" "%MAIN%"
