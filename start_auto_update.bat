@echo off
chcp 65001 >nul
title AIFS 华南预报 · 自动更新
cd /d "%~dp0"

setlocal
set "PY="

if exist "%~dp0python\python.exe" set "PY=%~dp0python\python.exe"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  where py >nul 2>nul && set "PY=py"
)
if not defined PY (
  if exist "%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe" set "PY=%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
)

if not defined PY (
  echo.
  echo  [错误] 没有找到 Python 运行环境。
  echo  请安装 Python 3.10 及以上版本。
  echo.
  pause
  exit /b 1
)

echo.
echo  正在启动 AIFS 自动更新器……
echo  每 30 分钟检查一次 ECMWF Open Data 最新起报场。
echo  关闭此窗口即可停止自动更新。
echo.

"%PY%" auto_update.py
endlocal
pause
