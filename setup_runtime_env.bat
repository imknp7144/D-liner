@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul

rem ============================================================
rem  D-liner runtime venv setup launcher
rem
rem  Usage:
rem    setup_runtime_env.bat
rem    setup_runtime_env.bat --skip-probe
rem    setup_runtime_env.bat --runtime npu
rem    setup_runtime_env.bat --runtime directml
rem    setup_runtime_env.bat --runtime cpu
rem
rem  This creates d_liner_runtime_env\ isolated from system Python and
rem  generates launch_d_liner.bat / launch_d_liner_debug.bat to run
rem  D-liner from it.
rem
rem  --runtime (default: auto) selects which onnxruntime variant to
rem  install:
rem    auto      Detect Intel NPU via Windows CIM/WMI. If found, install
rem              onnxruntime-openvino + openvino (NPU/iGPU/CPU capable).
rem              If not found, install onnxruntime-directml instead
rem              (works with NVIDIA/AMD/Intel GPUs via DirectX 12).
rem              If detection itself fails, fall back to plain
rem              onnxruntime (CPU only, safest default).
rem    npu       Force onnxruntime-openvino + openvino.
rem    directml  Force onnxruntime-directml.
rem    cpu       Force plain onnxruntime (CPU only).
rem
rem  Note: these three are mutually exclusive within one venv (they are
rem  different distributions of the same "onnxruntime" package name).
rem ============================================================

echo.
echo D-liner runtime venv setup
echo ==========================================
echo.

cd /d "%~dp0"

rem --- check required files exist ---
if not exist "setup_runtime_env.py" (
    echo [ERROR] setup_runtime_env.py not found.
    echo         Place it in the same folder as this bat file.
    pause
    exit /b 1
)
if not exist "main_window.py" (
    echo [WARN] main_window.py not found in this folder.
    echo        Make sure this bat file sits next to the D-liner source files.
)

rem --- locate Python (priority order) ---
set PYTHON_EXE=

where py >nul 2>&1
if %errorlevel% equ 0 (
    set PYTHON_EXE=py
    goto :found
)

where python >nul 2>&1
if %errorlevel% equ 0 (
    set PYTHON_EXE=python
    goto :found
)

where python3 >nul 2>&1
if %errorlevel% equ 0 (
    set PYTHON_EXE=python3
    goto :found
)

echo [ERROR] Python not found.
echo         Install Python 3.10 or later and add it to PATH.
echo         https://www.python.org/downloads/
pause
exit /b 1

:found
%PYTHON_EXE% -c "import sys; exit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python 3.10 or later is required.
    %PYTHON_EXE% --version
    pause
    exit /b 1
)

echo [OK] Using: %PYTHON_EXE%
%PYTHON_EXE% --version
echo.

echo Arguments: %*
echo.

%PYTHON_EXE% setup_runtime_env.py %*

if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Setup failed. Check runtime_setup_log_*.txt for details.
    pause
    exit /b 1
)

echo.
echo Setup complete.
echo Double-click launch_d_liner.bat to start D-liner.
echo.
pause
