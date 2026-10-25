@echo off
chcp 65001 >nul
REM 构建 SuperStar（默认发行包：dist\SuperStar-<版本>.exe + 随包文件 + 分发 zip，不含演示模式）
REM 测试包（含演示模式与自检）：python build.py --test-build
python build.py %*
pause
