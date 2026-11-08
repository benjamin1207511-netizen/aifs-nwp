@echo off
chcp 65001 >nul
title AIFS 华南预报 · 16 类预报图
cd /d "%~dp0"

setlocal
set "PY="

rem 1) 随包附带的 python（如果有）
if exist "%~dp0python\python.exe" set "PY=%~dp0python\python.exe"

rem 2) 系统 PATH 里的 python / py
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  where py >nul 2>nul && set "PY=py"
)

rem 3) DeepSeek Harness 自带的 python
if not defined PY (
  if exist "%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe" set "PY=%USERPROFILE%\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
)

if not defined PY (
  echo.
  echo  [错误] 没有找到 Python 运行环境。
  echo  请安装 Python 3.8 及以上版本：https://www.python.org/downloads/
  echo  安装时记得勾选 "Add Python to PATH"。
  echo.
  pause
  exit /b 1
)

echo.
echo  正在启动「AIFS 华南预报」站点，浏览器会自动打开……
echo  关闭此窗口即可停止服务。
echo.

"%PY%" server.py --port 8788 --open
endlocal
pause
