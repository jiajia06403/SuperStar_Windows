@echo off
chcp 65001 >nul
setlocal
REM 构建 SuperStar（默认发行包：dist\SuperStar\ 整个文件夹 + 分发 zip，不含演示模式）
REM 测试包（含演示模式与自检）：build.bat --test-build
REM
REM 重要：必须用项目自带的 .venv-build 环境（Python 3.12，自带可用 tkinter）。
REM 系统默认 python 常为 3.13 且 tkinter 损坏，PyInstaller 会静默排除 tkinter，
REM 导致打出的 exe 一启动就因导入 tkinter 崩溃。
set "VENV_PY=%~dp0.venv-build\Scripts\python.exe"
if exist "%VENV_PY%" (
    "%VENV_PY%" build.py %*
) else (
    echo [警告] 未找到 .venv-build 虚拟环境，回退到系统 python（可能因缺 tkinter 构建失败）
    python build.py %*
)
pause
