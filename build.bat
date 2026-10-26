@echo off
chcp 65001 >nul
setlocal

REM Build SuperStar distribution (default): dist\SuperStar\ folder + zip, no demo mode.
REM Flags are passed through to build.py:
REM   --exe-only   build onedir product dir only, skip zip
REM   --clean      clean intermediate artifacts then rebuild
REM Demo mode and self-test are EXCLUDED from the distribution package on purpose.
REM Run them from source instead: python run.py --demo  /  python run.py --selftest

REM IMPORTANT: use the bundled .venv-build env (Python 3.12 with working tkinter).
REM System python is often 3.13 with broken tkinter; PyInstaller would silently drop
REM tkinter, and the built exe would crash on import tkinter at startup.
set "VENV_PY=%~dp0.venv-build\Scripts\python.exe"
if exist "%VENV_PY%" (
    "%VENV_PY%" build.py %*
) else (
    echo [WARN] .venv-build not found, falling back to system python, build may fail without tkinter
    python build.py %*
)
pause
