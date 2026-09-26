@echo off
rem Double-click to start Rainbow Octopus on Windows.
chcp 65001 >nul
cd /d "%~dp0"
python --version >nul 2>&1
if errorlevel 1 (
  echo 没有找到 Python。请先安装 Python 3.10 或更新版本：https://www.python.org/downloads/
  echo 安装时记得勾选 "Add python.exe to PATH"。
  pause
  exit /b 1
)
python -m pip install -q -e .
if errorlevel 1 (
  echo 安装失败，请把上面的错误信息截图保存。
  pause
  exit /b 1
)
python -m rainbow_octopus
pause
